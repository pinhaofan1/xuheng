import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.audit.services.evidence_review_service import ReviewUnavailable
from application.audit.services.history_retrieval import HistoricalRetriever
from infrastructure.ai.retrieval_client import RetrievalHTTPClient
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.search_index_store import SearchIndexStore
from infrastructure.persistence.database.search_vector_store import SearchVectorStore
from infrastructure.persistence.operation_journal import OperationJournal
from interfaces.api.v1.audit import search_index_routes


@pytest.fixture
def index(tmp_path):
    db = DatabaseConnection(str(tmp_path/'search.sqlite3'))
    store = SearchIndexStore(db)
    with store.connection(write=True) as conn:
        conn.executemany('INSERT INTO novels(id,title,slug) VALUES (?,?,?)', [('a','A','a'),('b','B','b')])
        conn.executemany('INSERT INTO chapters(id,novel_id,number,content) VALUES (?,?,?,?)', [
            ('a1','a',1,'海风。\n林澈发现了失落的紫铜灯芯。'),
            ('a2','a',2,'第二章。'), ('a3','a',3,'紫铜灯芯来自未来的船。'),
            ('b1','b',1,'紫铜灯芯在另一本书里。'),
        ])
    yield store
    db.close_all()


def drain(index):
    for _ in range(20):
        if not index.process_next():
            return
    pytest.fail('index did not reach a terminal state')


def test_indexing_does_not_run_until_requested_and_keeps_exact_past_scope(index):
    assert index.status('a')['job'] is None
    assert index.process_next() is False
    index.enqueue('a')
    index.enqueue('b')
    drain(index)
    hits = index.lexical('a', 3, '紫铜灯芯')
    assert [hit['chapter_id'] for hit in hits] == ['a1']
    hit = hits[0]
    with index.connection() as conn:
        content = conn.execute("SELECT content FROM chapters WHERE id='a1'").fetchone()[0]
    assert content[hit['start']:hit['end']] == hit['text']


def test_summaries_rank_only_body_within_completed_past_scope(index):
    index.enqueue('a')
    drain(index)
    with index.connection(write=True) as conn:
        conn.execute("UPDATE chapters SET content='紫铜灯芯。' WHERE id IN ('a1','a2')")
        conn.execute("INSERT INTO knowledge(id,novel_id) VALUES ('ka','a'),('kb','b')")
        conn.execute("""INSERT INTO chapter_summaries(id,knowledge_id,chapter_number,summary) VALUES
            ('past','ka',1,'紫铜灯芯，仅出现在摘要的说明'),('other','kb',2,'紫铜灯芯'),
            ('future','ka',3,'紫铜灯芯')""")
        conn.execute("""INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index,chapter_start,chapter_end,metadata)
            VALUES ('past-act','a','act',1,'过去',1,1,1,'{"summary":"紫铜灯芯"}'),
                   ('mixed-act','a','act',2,'跨越目标',2,2,3,'{"summary":"紫铜灯芯"}')""")
        cues = index.summary_cues(conn, 'a', 3, {'紫铜', '铜灯', '灯芯'})
        assert [(first,last) for first,last,_ in cues] == [(1,1),(1,1)]
    drain(index)
    hits = index.lexical('a', 3, '紫铜灯芯')
    assert [hit['chapter_id'] for hit in hits] == ['a1','a2']
    assert all(hit['text'] == '紫铜灯芯。' for hit in hits)


def test_edits_and_reordering_hide_stale_results_before_worker_runs(index):
    index.enqueue('a')
    drain(index)
    with index.connection(write=True) as conn:
        conn.execute("UPDATE chapters SET content='海图藏有琉璃航标' WHERE id='a1'")
    assert index.status('a')['indexed_chapters'] == 2
    assert index.status('a')['job']['status'] == 'queued'
    assert index.lexical('a', 3, '紫铜灯芯') == []
    drain(index)
    assert index.lexical('a', 3, '琉璃航标')[0]['chapter_id'] == 'a1'
    with index.connection(write=True) as conn:
        conn.execute("UPDATE chapters SET number=4 WHERE id='a1'")
    drain(index)
    assert index.lexical('a', 3, '琉璃航标') == []
    with index.connection(write=True) as conn:
        conn.execute("DELETE FROM chapters WHERE id='a1'")
        assert conn.execute("SELECT count(*) FROM xh_search_postings WHERE term='琉璃'").fetchone()[0] == 0


def test_cancel_and_reopen_preserves_published_chapters(index):
    job = index.enqueue('a')
    assert index.process_next()
    assert index.set_state('a', job)
    assert index.status('a')['indexed_chapters'] == 1
    assert not index.set_state('b', job, resume=True)
    assert index.process_next() is False
    db = DatabaseConnection(index.path)
    try:
        reopened = SearchIndexStore(db)
        assert reopened.set_state('a', job, resume=True)
        drain(reopened)
        assert reopened.status('a')['indexed_chapters'] == 3
        assert reopened.status('a')['job']['status'] == 'completed'
    finally:
        db.close_all()


def test_late_result_after_rebuild_cannot_publish_under_old_job(index, monkeypatch):
    index.enqueue('a')
    publish = index.publish

    def replace_job(*args):
        index.enqueue('a', rebuild=True)
        publish(*args)

    monkeypatch.setattr(index, 'publish', replace_job)
    index.process_next()
    assert index.status('a')['indexed_chapters'] == 0


def test_restore_quarantines_index_until_explicit_rebuild(index):
    from infrastructure.persistence.backup_service import quarantine

    job = index.enqueue('a')
    drain(index)
    quarantine(index.path, 'main')
    assert index.status('a')['job']['status'] == 'cancelled'
    assert index.lexical('a', 3, '紫铜灯芯') == []
    assert not index.process_next()
    assert index.set_state('a', job, resume=True)
    drain(index)
    assert index.lexical('a', 3, '紫铜灯芯')[0]['chapter_id'] == 'a1'


def test_http_enqueues_work_and_lifespan_worker_publishes(index, monkeypatch):
    app = FastAPI()
    app.include_router(search_index_routes.router)
    app.dependency_overrides[search_index_routes.get_search_index] = lambda: index
    monkeypatch.setattr(search_index_routes, 'get_search_index', lambda: index)
    with TestClient(app) as client:
        assert client.get('/novels/missing/index').status_code == 404
        result = client.post('/novels/a/index/jobs', json={})
        assert result.status_code == 202
        job = result.json()['id']
        assert client.post(f'/novels/b/index/jobs/{job}/cancel').status_code == 409

        async def run_worker():
            stop = asyncio.Event()
            worker = asyncio.create_task(search_index_routes.poll_search_index(stop))
            try:
                for _ in range(100):
                    if index.status('a')['job']['status'] == 'completed':
                        return
                    await asyncio.sleep(.02)
                pytest.fail('worker did not process the HTTP job')
            finally:
                stop.set()
                await worker

        asyncio.run(run_worker())
        assert client.get('/novels/a/index').json()['indexed_chapters'] == 3


def test_vector_batches_are_exclusive_and_only_reuse_matching_book_and_model(index):
    index.enqueue('a')
    drain(index)
    store = SearchVectorStore(index)
    job_id = store.enqueue('a', 'model1')
    job = store.pending()
    token, texts = store.claim(job, 'model1')
    assert len(texts) == 4
    assert store.claim(job, 'model1') == (None, [])
    assert not store.cancel('b', job_id)
    store.publish(job, token, texts, [[1., 2.]] * len(texts))
    assert store.claim(store.pending(), 'model1') == (None, [])
    assert store.status('a')['status'] == 'completed'
    assert store.status('a')['indexed_chunks'] == 4
    with index.connection(write=True) as conn:
        conn.execute("UPDATE chapters SET content=? WHERE id='a1'", ('海风。\n新灯芯。',))
        conn.execute("UPDATE chapters SET content='海风。' WHERE id='b1'")
    index.enqueue('b')
    drain(index)
    job = store.pending()
    _, texts = store.claim(job, 'model1')
    assert texts == ['新灯芯。']
    with index.connection() as conn:
        assert not conn.execute("SELECT 1 FROM xh_search_vectors WHERE text LIKE '%失落%'").fetchone()
    assert not any(row['vector_json'] for row in store.history('b', 3, 'model1')[3])
    assert not any(row['vector_json'] for row in store.history('a', 3, 'model2')[3])


def test_late_vector_publish_and_unknown_request_are_fenced(index):
    index.enqueue('a')
    drain(index)
    store = SearchVectorStore(index)
    store.enqueue('a', 'model')
    job = store.pending()
    token, texts = store.claim(job, 'model')
    with index.connection(write=True) as conn:
        conn.execute("DELETE FROM chapters WHERE id='a1'")
    store.publish(job, token, texts, [[1., 2.]] * len(texts))
    with index.connection() as conn:
        assert not conn.execute("SELECT 1 FROM xh_search_vectors WHERE text LIKE '%失落%'").fetchone()
    with index.connection(write=True) as conn:
        conn.execute("UPDATE chapters SET content='新的灯塔' WHERE id='a2'")
    drain(index)
    job = store.pending()
    token, texts = store.claim(job, 'model')
    store.fail(job, token, unknown=True)
    reopened = SearchVectorStore(index)
    assert reopened.pending() is None
    assert reopened.status('a')['pending_confirmation']
    with pytest.raises(ValueError, match='尚未确认'):
        reopened.enqueue('a', 'new-model')


@pytest.mark.asyncio
async def test_vector_worker_accounts_real_adapter_and_resumes_without_sending_saved_vectors(index):
    index.enqueue('a')
    drain(index)
    requests = []

    def mock_provider(request):
        texts = json.loads(request.content)['input']
        requests.append(texts)
        return httpx.Response(200, json={'data': [
            {'index': i, 'embedding': [1., 2.]} for i in range(len(texts))
        ], 'usage': {'prompt_tokens': 20}})

    embedding = RetrievalHTTPClient(api_key='fixture', base_url='https://example.test/v1',
        embedding_model='fixture-embedding', reranker_model='fixture-rerank', transport=httpx.MockTransport(mock_provider))

    class Configuration:
        def embedding_factory(self, *, cloud_only):
            assert cloud_only
            return embedding, 'fixture'

    store = SearchVectorStore(index)
    store.enqueue('a', 'fixture')
    journal = OperationJournal(index.path + '.operations')
    assert await search_index_routes.process_vectors(store, Configuration(), journal)
    assert not await search_index_routes.process_vectors(store, Configuration(), journal)
    assert store.status('a')['status'] == 'completed'
    store.enqueue('a', 'fixture')
    assert not await search_index_routes.process_vectors(store, Configuration(), journal)
    assert len(requests) == 1
    with journal.connection() as conn:
        call = conn.execute('SELECT novel_id,kind,status,input_tokens FROM calls').fetchone()
        assert tuple(call) == ('a', 'embedding', 'completed', 20)


@pytest.mark.asyncio
async def test_production_hybrid_embeds_only_query_and_excludes_future_and_other_book(index):
    class Chapters:
        def get_chapter(self, novel_id, number):
            assert (novel_id, number) == ('a', 3)

    class Embedding:
        def __init__(self):
            self.calls = []

        async def embed_batch(self, texts):
            self.calls.append(texts)
            return [[1., 2.]] * len(texts)

    model = Embedding()
    retriever = HistoricalRetriever(Chapters(), lambda: (model, 'model'), index_store=index)
    with pytest.raises(ReviewUnavailable, match='尚未覆盖'):
        await retriever.retrieve('a', 3, '紫铜灯芯', retrieval_mode='hybrid')
    assert not model.calls
    store = SearchVectorStore(index)
    for book in ('a', 'b'):
        index.enqueue(book)
        drain(index)
        store.enqueue(book, 'model')
        job = store.pending()
        token, texts = store.claim(job, 'model')
        store.publish(job, token, texts, [[1., 2.]] * len(texts))
        assert store.claim(store.pending(), 'model') == (None, [])
    result = await retriever.retrieve('a', 3, '紫铜灯芯', retrieval_mode='hybrid')
    assert result['sources'][0]['chapter_id'] == 'a1'
    assert {source['chapter_id'] for source in result['sources']} == {'a1', 'a2'}
    assert model.calls == [['紫铜灯芯']]
