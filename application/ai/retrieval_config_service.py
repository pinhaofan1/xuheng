"""Non-secret retrieval settings; credentials reuse the local embedding profile."""
from __future__ import annotations

from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from application.ai.embedding_config_service import EmbeddingConfigService
from application.audit.evidence_models import text_hash
from infrastructure.ai.retrieval_client import RetrievalHTTPClient, validate_base_url
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore


class RetrievalConfigUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = Field(default='https://api.siliconflow.cn/v1', max_length=500)
    embedding_model: str = Field(default='Qwen/Qwen3-Embedding-4B', min_length=1, max_length=160)
    reranker_model: str = Field(default='Qwen/Qwen3-Reranker-4B', min_length=1, max_length=160)
    dimensions: int = Field(default=1024, ge=64, le=4096)
    api_key: str | None = Field(default=None, max_length=2000)


class RetrievalConfigService:
    def __init__(self, db):
        self.db = db
        self.embedding = EmbeddingConfigService(db)
        self.store = EvidenceReviewStore(db)

    def raw(self):
        config = self.embedding.get_config()
        row = self.db.fetch_one("SELECT * FROM xh_retrieval_settings WHERE id='default'") or {}
        return config, row.get('reranker_model', 'Qwen/Qwen3-Reranker-4B'), row.get('dimensions', 1024)

    def public(self):
        config, reranker, dimensions = self.raw()
        return {'configured': bool(config.api_key.strip() and config.model.strip() and config.base_url.strip() and config.mode == 'openai'),
                'base_url': config.base_url or 'https://api.siliconflow.cn/v1',
                'host': urlsplit(config.base_url).hostname if config.base_url else '',
                'embedding_model': config.model or 'Qwen/Qwen3-Embedding-4B', 'reranker_model': reranker,
                'dimensions': dimensions, 'api_key_configured': bool(config.api_key.strip()),
                'credential_source': '本地嵌入配置，与聊天模型档案分开'}

    def save(self, request: RetrievalConfigUpdate):
        base = validate_base_url(request.base_url)
        current = self.embedding.get_config()
        key = request.api_key.strip() if request.api_key is not None else current.api_key
        if request.api_key is None and current.api_key and current.base_url and urlsplit(current.base_url).hostname != urlsplit(base).hostname:
            raise ValueError('更换服务域名时需要重新填写对应 Key，避免将旧凭据发送到新服务')
        revision = uuid4().hex
        with self.db.transaction() as tx:
            tx.execute("INSERT OR REPLACE INTO xh_retrieval_settings(id,reranker_model,dimensions,revision) VALUES ('default',?,?,?)",
                       (request.reranker_model.strip(), request.dimensions, revision))
        self.store._wait(lambda: self.db.fetch_one("SELECT id FROM xh_retrieval_settings WHERE revision=?", (revision,)))
        self.embedding.update_config(mode='openai', api_key=key, base_url=base, model=request.embedding_model.strip())
        return self.public()

    def client(self):
        config, reranker, dimensions = self.raw()
        return RetrievalHTTPClient(api_key=config.api_key, base_url=config.base_url,
            embedding_model=config.model, reranker_model=reranker, dimensions=dimensions)

    def embedding_factory(self):
        config, _, dimensions = self.raw()
        if config.mode == 'local':
            from interfaces.api.dependencies import get_embedding_service
            return get_embedding_service(), text_hash(f'local:{config.model_path}')
        return self.client(), text_hash(f'http:{config.base_url}:{config.model}:{dimensions}:v1')

    async def test(self):
        client = self.client()
        vectors = await client.embed_batch(['沈澜的左臂受伤了。', '沈澜无法抬起左手。', '山脚的桃花开了。'])
        result = await client.rerank('沈澜左臂是否受伤', ['沈澜的左臂受伤了。', '山脚的桃花开了。'], 2)
        return {'ok': True, 'dimension': len(vectors[0]), 'documents': len(vectors),
                'rerank_top_index': result[0]['index'], 'models': self.public(), 'usage': client.metrics()}
