"""Bounded embedding and rerank HTTP adapters; no automatic retries or key output."""
from __future__ import annotations

import math
from urllib.parse import urlsplit

import httpx

from application.audit.services.evidence_review_service import ReviewUnavailable


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip('/')
    parts = urlsplit(value)
    if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('接口地址应是 http(s) Base URL，不能包含凭据、查询或片段')
    return value


class RetrievalHTTPClient:
    def __init__(self, *, api_key: str, base_url: str, embedding_model: str, reranker_model: str,
                 dimensions: int | None = None, transport=None):
        if not api_key.strip():
            raise ReviewUnavailable('请先在检索配置中保存硅基流动或兼容服务的 API Key')
        self.api_key = api_key
        self.base_url = validate_base_url(base_url)
        self.embedding_model, self.reranker_model = embedding_model, reranker_model
        self.dimensions, self.transport = dimensions, transport
        self.calls = {'embedding': 0, 'rerank': 0}
        self.input_tokens = {'embedding': 0, 'rerank': 0}
        self.usage_available = {'embedding': True, 'rerank': True}

    async def _post(self, endpoint: str, body: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), trust_env=False,
                                         transport=self.transport) as client:
                response = await client.post(f'{self.base_url}/{endpoint}',
                    headers={'Authorization': f'Bearer {self.api_key}'}, json=body)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('expected object')
            usage = data.get('usage') or (data.get('meta') or {}).get('billed_units') or {}
            if not isinstance(usage, dict):
                usage = {}
            tokens = usage.get('prompt_tokens', usage.get('input_tokens', usage.get('total_tokens')))
            kind = 'embedding' if endpoint == 'embeddings' else 'rerank'
            self.calls[kind] += 1
            if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
                self.input_tokens[kind] += tokens
            else:
                self.usage_available[kind] = False
            return data
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            message = {401: '鉴权失败，请检查 API Key', 403: '账户无权使用所选模型',
                       429: '限流或额度不足，未自动重试'}.get(status, f'服务返回 HTTP {status}，请检查模型名称与参数')
            raise ReviewUnavailable(f'检索模型{message}') from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise ReviewUnavailable('检索模型连接或响应无效，未自动重试；请检查接口配置') from None

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > 32 or any(not t.strip() or len(t) > 8000 for t in texts):
            raise ValueError('嵌入每批支持 1—32 条非空文本，每条最多 8000 字符')
        body = {'model': self.embedding_model, 'input': texts, 'encoding_format': 'float'}
        if self.dimensions and self.embedding_model.startswith('Qwen/Qwen3-Embedding-'):
            body['dimensions'] = self.dimensions
        data = await self._post('embeddings', body)
        try:
            items = data['data']
            if len(items) != len(texts):
                raise ValueError()
            by_index = {}
            for item in items:
                index, vector = item['index'], item['embedding']
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(texts) or index in by_index:
                    raise ValueError()
                if not vector or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vector):
                    raise ValueError()
                if not sum(x*x for x in vector):
                    raise ValueError()
                by_index[index] = vector
            vectors = [by_index[i] for i in range(len(texts))]
            if len({len(v) for v in vectors}) != 1 or (body.get('dimensions') and len(vectors[0]) != body['dimensions']):
                raise ValueError()
            return vectors
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ReviewUnavailable('嵌入响应数量、序号或向量维度不合法，未保存索引') from None

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[dict]:
        if not query.strip() or not documents or len(documents) > 48:
            raise ValueError('重排需要非空查询和 1—48 个候选片段')
        top_n = min(top_n, len(documents))
        body = {'model': self.reranker_model, 'query': query[:8000], 'documents': documents,
                'top_n': top_n, 'return_documents': False}
        if self.reranker_model.startswith('Qwen/Qwen3-Reranker-'):
            body['instruction'] = '根据查询中的人物、时间、地点与状态，找出有助于核对小说前后事实一致性的历史正文。'
        data = await self._post('rerank', body)
        try:
            results, seen = data['results'], set()
            if len(results) != top_n:
                raise ValueError()
            cleaned = []
            for item in results:
                index, score = item['index'], item['relevance_score']
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(documents) or index in seen:
                    raise ValueError()
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                    raise ValueError()
                seen.add(index)
                cleaned.append({'index': index, 'relevance_score': float(score)})
            return sorted(cleaned, key=lambda x: -x['relevance_score'])
        except (KeyError, TypeError, ValueError):
            raise ReviewUnavailable('重排响应含无效序号、数量或分数，未使用该排序') from None

    def metrics(self):
        return {kind: {'calls': self.calls[kind],
                       'input_tokens': self.input_tokens[kind] if self.usage_available[kind] else None}
                for kind in self.calls}
