"""Full-scope vector recall and real rerank protocol, with isolated fixtures."""
import json
from types import SimpleNamespace

import httpx
import pytest

from application.ai.retrieval_config_service import RetrievalConfigService, RetrievalConfigUpdate
from application.audit.services.evidence_review_service import ReviewUnavailable
from application.audit.services.history_retrieval import HistoricalRetriever, bm25
from infrastructure.ai.retrieval_client import RetrievalHTTPClient
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore


@pytest.fixture
def store(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'hybrid.db'))
    for name in ('book', 'other'):
        db.execute('INSERT INTO novels(id,title,slug) VALUES (?,?,?)', (name,name,name))
    for id,novel,number,body in [('past','book',1,'左肢骨折，不能举起。\n\n桃花开了。'),
        ('now','book',2,'他抬起左手。'),('future','book',3,'左肢痊愈。'),('foreign','other',1,'左肢骨折。')]:
        db.execute('INSERT INTO chapters(id,novel_id,number,title,content) VALUES (?,?,?,?,?)',(id,novel,number,'test',body))
    db.commit()
    yield EvidenceReviewStore(db)
    db.close_all()


class FixtureEmbedding:
    def __init__(self): self.calls=0
    async def embed_batch(self,texts):
        self.calls+=1
        return [[0.,1.] if '桃花' in text else [1.,0.] for text in texts]


class FixtureReranker:
    def __init__(self): self.calls=0; self.documents=[]
    async def rerank(self,query,documents,top_n):
        self.calls+=1; self.documents=documents
        return sorted([{'index':i,'relevance_score': .9 if '骨折' in text else .1} for i,text in enumerate(documents)],key=lambda x:-x['relevance_score'])[:top_n]


@pytest.mark.asyncio
async def test_hybrid_recalls_zero_keyword_overlap_and_caches_vectors(store):
    embedding, reranker=FixtureEmbedding(),FixtureReranker()
    retriever=HistoricalRetriever(store,lambda:(embedding,'fixture-embedding-v1'),lambda:reranker)
    query='手臂是否健康'
    keyword=await retriever.retrieve('book',2,query)
    assert keyword['sources']==[]
    result=await retriever.retrieve('book',2,query,retrieval_mode='hybrid_rerank')
    assert result['sources'][0]['text']=='左肢骨折，不能举起。'
    assert result['sources'][0]['channels']==['vector']
    assert {source['chapter_id'] for source in result['sources']}=={'past'}
    assert result['mode']=='bm25+vector+reranker'
    assert result['retrieval_usage']['embedded_texts']==3
    assert embedding.calls==reranker.calls==1
    again=await retriever.retrieve('book',2,query,retrieval_mode='hybrid_rerank')
    assert again['retrieval_usage']['embedded_texts']==0
    assert embedding.calls==1 and reranker.calls==2


@pytest.mark.asyncio
async def test_changing_embedding_identity_invalidates_vector_cache(store):
    embedding=FixtureEmbedding()
    await HistoricalRetriever(store,lambda:(embedding,'a')).retrieve('book',2,'手臂',retrieval_mode='hybrid')
    await HistoricalRetriever(store,lambda:(embedding,'b')).retrieve('book',2,'手臂',retrieval_mode='hybrid')
    assert embedding.calls==2


@pytest.mark.asyncio
async def test_missing_semantic_provider_is_an_error_even_without_lexical_hits(store):
    with pytest.raises(ReviewUnavailable,match='Embedding'):
        await HistoricalRetriever(store).retrieve('book',2,'手臂',retrieval_mode='hybrid')


@pytest.mark.asyncio
async def test_cancel_before_embedding_does_not_send_requests(store):
    embedding=FixtureEmbedding()
    result=await HistoricalRetriever(store,lambda:(embedding,'a')).retrieve('book',2,'手臂',retrieval_mode='hybrid',cancelled=lambda:True)
    assert not result['sources'] and embedding.calls==0


def client(handler,model='fixture-embedding'):
    return RetrievalHTTPClient(api_key='fixture-secret-never-echo',base_url='https://fixture.invalid/v1',
        embedding_model=model,reranker_model='Qwen/Qwen3-Reranker-4B',transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_embedding_uses_response_indices_and_tracks_actual_usage():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'data':[{'index':1,'embedding':[0.,1.]},{'index':0,'embedding':[1.,0.]}],'usage':{'prompt_tokens':7}})
    adapter=client(handler)
    assert await adapter.embed_batch(['甲','乙'])==[[1.,0.],[0.,1.]]
    assert adapter.metrics()['embedding']=={'calls':1,'input_tokens':7}
    assert json.loads(requests[0].content)['input']==['甲','乙']
    assert requests[0].url.path=='/v1/embeddings'


@pytest.mark.asyncio
@pytest.mark.parametrize('items',[[{'index':0,'embedding':[1.]},{'index':0,'embedding':[2.]}],
    [{'index':0,'embedding':[0.,0.]},{'index':1,'embedding':[1.,0.]}],
    [{'index':0,'embedding':[1.,0.]},{'index':1,'embedding':[1.]}]])
async def test_bad_embedding_indices_dimensions_and_zero_vectors_rejected(items):
    adapter=client(lambda request:httpx.Response(200,json={'data':items}))
    with pytest.raises(ReviewUnavailable,match='不合法'):
        await adapter.embed_batch(['甲','乙'])


@pytest.mark.asyncio
async def test_reranker_receives_original_candidates_and_validates_indices():
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'results':[{'index':1,'relevance_score':.8},{'index':0,'relevance_score':.1}]})
    adapter=client(handler)
    assert (await adapter.rerank('手臂',['花开','骨折'],2))[0]['index']==1
    assert requests[0]['documents']==['花开','骨折'] and requests[0]['return_documents'] is False
    assert requests[0]['instruction']
    assert adapter.metrics()['rerank']['input_tokens'] is None
    broken=client(lambda request:httpx.Response(200,json={'results':[{'index':8,'relevance_score':.8}]}))
    with pytest.raises(ReviewUnavailable,match='无效'):
        await broken.rerank('手臂',['花开'],1)


@pytest.mark.asyncio
async def test_provider_error_is_sanitized_and_not_retried():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(401,json={'error':{'message':'fixture-secret-never-echo'}})
    with pytest.raises(ReviewUnavailable) as error:
        await client(handler).embed_batch(['甲'])
    assert 'fixture-secret' not in str(error.value) and len(calls)==1


def test_public_config_masks_and_preserves_local_credentials(store):
    service=RetrievalConfigService(store.db)
    saved=service.save(RetrievalConfigUpdate(api_key='fixture-secret-never-echo'))
    assert saved['configured'] and 'fixture-secret' not in json.dumps(saved)
    service.save(RetrievalConfigUpdate(reranker_model='BAAI/bge-reranker-v2-m3'))
    assert service.embedding.get_config().api_key=='fixture-secret-never-echo'
    assert service.public()['reranker_model']=='BAAI/bge-reranker-v2-m3'
    with pytest.raises(ValueError,match='更换服务域名'):
        service.save(RetrievalConfigUpdate(base_url='https://another.invalid/v1'))
