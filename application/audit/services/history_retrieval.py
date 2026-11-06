"""Historical retrieval with book-scoped indexes and exact source anchors."""
from __future__ import annotations

from collections import Counter
import asyncio
import json
import math
import re
from typing import Callable

from application.ai.usage_accounting import account_for_book
from application.audit.evidence_models import text_hash
from application.audit.services.evidence_review_service import ReviewUnavailable

INDEX_VERSION = "paragraph-700-overlap80-v1"
MAX_HISTORY_CHAPTERS = 100
MAX_HISTORY_CHARS = 500_000


def bounded_history(rows: list[dict]) -> list[dict]:
    remaining = MAX_HISTORY_CHARS
    result = []
    for row in rows[:MAX_HISTORY_CHAPTERS]:
        content = row.get("content") or ""
        take = content[: min(100_000, remaining)]
        if take.strip():
            result.append({**row, "content": take, "content_hash": text_hash(content)})
            remaining -= len(take)
        if remaining <= 0:
            break
    return result


def history_digest(rows: list[dict]) -> str:
    return text_hash(json.dumps([(r["id"], r["number"], r["content_hash"]) for r in rows], ensure_ascii=False))


def current_history_digest(store, novel_id, number, previous):
    if (previous or '').startswith('index-v1:'):
        from infrastructure.persistence.database.search_index_store import SearchIndexStore
        return SearchIndexStore(store.db).context_digest(novel_id, number)
    return history_digest(bounded_history(store.history_rows(novel_id, number)))


def split_chapter(row: dict) -> list[dict]:
    text = row["content"]
    chunks = []
    for paragraph in re.finditer(r"\S(?:[^\n]*\S)?", text):
        left, right = paragraph.span()
        while left < right:
            end = min(left + 700, right)
            chunks.append({
                "id": text_hash(f"{row['novel_id']}:{row['id']}:{row['content_hash']}:{left}:{end}"),
                "novel_id": row["novel_id"], "chapter_id": row["id"], "chapter_number": row["number"],
                "start": left, "end": end, "text": text[left:end], "content_hash": row["content_hash"],
            })
            if end == right:
                break
            left = end - 80
    return chunks


def terms(text: str) -> Counter:
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    result = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.extend(word[i:i + 2] for i in range(max(0, len(word) - 1)))
        else:
            result.append(word)
    return Counter(result)


def bm25(chunks: list[dict], query: str) -> list[dict]:
    if not chunks:
        return []
    query_terms = terms(query)
    documents = [terms(c["text"]) for c in chunks]
    average = sum(sum(d.values()) for d in documents) / max(1, len(documents))
    frequencies = {term: sum(term in doc for doc in documents) for term in query_terms}
    ranked = []
    for chunk, doc in zip(chunks, documents):
        score = 0.0
        length = sum(doc.values())
        for term in query_terms:
            tf = doc.get(term, 0)
            if tf:
                idf = math.log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
                score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / max(1, average)))
        if score > 0:
            ranked.append({**chunk, "score": round(score, 6)})
    return sorted(ranked, key=lambda c: (-c["score"], -c["chapter_number"], c["start"]))


class HistoricalRetriever:
    def __init__(self, store, embedding_factory: Callable | None = None, reranker_factory: Callable | None = None, *, index_store=None):
        self.store = store
        self.embedding_factory = embedding_factory
        self.reranker_factory = reranker_factory
        self.index_store = index_store

    @account_for_book("历史检索")
    async def retrieve(self, novel_id: str, number: int, query: str, limit: int = 6, semantic: bool = False,
                       retrieval_mode: str = 'bm25', progress=lambda stage: None, cancelled=lambda: False) -> dict:
        if retrieval_mode not in {'bm25', 'hybrid', 'hybrid_rerank'}:
            raise ValueError('不支持的检索模式')
        if not query.strip():
            raise ValueError('检索查询不能为空')
        self.store.get_chapter(novel_id, number=number)
        if self.index_store is not None and (retrieval_mode != 'bm25' or semantic):
            return await self._indexed_hybrid(novel_id, number, query, limit, retrieval_mode, progress, cancelled)
        if self.index_store is not None:
            context = self.index_store.lexical_context(novel_id, number, query)
            if context is not None:
                ranked, digest, indexed, total, chunks = context
                warnings = []
                if indexed < total:
                    warnings.append(f'历史索引已覆盖 {indexed}/{total} 章，可在索引管理中继续构建。')
                if not ranked:
                    warnings.append('未召回匹配的正文片段，不能据此认定历史中不存在相关事实。')
                return {'sources': self.select(ranked, min(limit, 6)), 'context_hash': digest, 'mode': 'bm25',
                        'retrieval_usage': {}, 'indexed_chapters': indexed, 'indexed_chunks': chunks,
                        'warnings': warnings, 'scope': '检索当前作品已索引的前序正文'}
        rows = bounded_history(self.store.history_rows(novel_id, number))
        chunks = [chunk for row in rows for chunk in split_chapter(row)]
        if self.index_store is None:
            self.store.update_history_index(rows, chunks)
        ranked = bm25(chunks, query[:8000])[:24]
        warnings = ['尚未构建全书索引，当前使用最近章节的关键词检索。'] if self.index_store is not None else []
        mode = "bm25"
        metrics = {}
        if retrieval_mode != 'bm25' and chunks:
            ranked, metrics = await self._hybrid(chunks, query, retrieval_mode, progress, cancelled)
            mode = 'bm25+vector+reranker' if retrieval_mode == 'hybrid_rerank' else 'bm25+vector'
        elif semantic and ranked:
            if self.embedding_factory is None:
                raise ReviewUnavailable("尚未配置嵌入模型；可先使用本地历史检索")
            embedding, fingerprint = self.embedding_factory()
            if embedding is None:
                raise ReviewUnavailable("嵌入模型不可用；请在 AI 控制台配置，或使用本地检索")
            texts = [query[:4000], *(c["text"] for c in ranked)]
            keys = [text_hash(f"{fingerprint}:{text_hash(t)}") for t in texts]
            vectors = [self.store.get_embedding(k) for k in keys]
            missing = [i for i, v in enumerate(vectors) if v is None]
            try:
                if missing:
                    generated = await embedding.embed_batch([texts[i] for i in missing])
                    if len(generated) != len(missing):
                        raise ValueError("embedding result count mismatch")
                    for index, vector in zip(missing, generated):
                        if not vector or not all(math.isfinite(x) for x in vector):
                            raise ValueError("invalid embedding vector")
                        vectors[index] = vector
                    self.store.save_embeddings({keys[i]: vectors[i] for i in missing})
                query_vector = vectors[0]
                for chunk, vector in zip(ranked, vectors[1:]):
                    if len(vector) != len(query_vector):
                        raise ValueError("embedding dimension mismatch")
                    norm = math.sqrt(sum(x*x for x in query_vector) * sum(x*x for x in vector))
                    chunk["score"] = sum(x*y for x, y in zip(query_vector, vector)) / norm if norm else 0.0
                ranked.sort(key=lambda c: -c["score"])
                mode = "bm25+semantic-rerank"
            except Exception as exc:
                # User requested semantics explicitly; do not silently pretend it worked.
                raise ReviewUnavailable("语义重排失败，请检查嵌入配置；可切换为本地检索") from exc
            finally:
                client = getattr(embedding, "_http_client", None)
                if client is not None:
                    await client.aclose()
        selected = self.select(ranked, limit)
        if not rows:
            warnings.append("当前章节之前没有可检索的正文")
        elif not selected:
            warnings.append("没有召回与查询匹配的历史片段，不能据此认定历史中不存在相关事实")
        return {"sources": selected, "context_hash": history_digest(rows), "mode": mode, "retrieval_usage": metrics,
                "indexed_chapters": len(rows), "indexed_chunks": len(chunks), "warnings": warnings,
                "scope": "仅检索当前作品前序章节，最多最近100章、50万字符；混合检索在此范围内同时进行关键词与向量召回"}

    async def _indexed_hybrid(self, novel_id, number, query, limit, mode, progress, cancelled):
        from infrastructure.persistence.database.search_vector_store import SearchVectorStore, unit_vector

        if self.embedding_factory is None:
            raise ReviewUnavailable('混合检索需要先配置 Embedding 模型')
        embedding, fingerprint = self.embedding_factory()
        if embedding is None:
            raise ReviewUnavailable('Embedding 模型不可用，请检查检索配置')
        store = SearchVectorStore(self.index_store)
        digest, indexed, total, rows = await asyncio.to_thread(store.history, novel_id, number, fingerprint)
        if indexed != total or any(row['dimensions'] is None for row in rows):
            raise ReviewUnavailable('当前模型的前序章节向量索引尚未覆盖，请在历史索引区构建或切换关键词模式')
        metrics = {'chunks': len(rows), 'cached_vectors': len(rows), 'embedded_texts': 0}
        ranked = []
        if rows and not cancelled():
            progress('向量化检索查询')
            try:
                vectors = await embedding.embed_batch([query[:8000]])
                vector = unit_vector(vectors[0])
                if len(vectors) != 1 or any(row['dimensions'] != len(vector) for row in rows):
                    raise ReviewUnavailable('查询向量维度与历史索引不一致，请检查模型配置')
                ranked = await asyncio.to_thread(self._rank_indexed, novel_id, number, query, rows, vector)
                metrics['embedded_texts'] = 1
                if hasattr(embedding, 'metrics'):
                    metrics.update(embedding.metrics())
            finally:
                transport = getattr(embedding, '_http_client', None)
                if transport is not None:
                    await transport.aclose()
        if mode == 'hybrid_rerank' and ranked and not cancelled():
            ranked = await self._rerank(ranked, query, progress, metrics)
        if await asyncio.to_thread(self.index_store.context_digest, novel_id, number) != digest:
            raise ReviewUnavailable('检索期间历史正文发生变化，请重新检索')
        return {'sources': self.select(ranked, min(limit, 6)) if not cancelled() else [],
                'context_hash': digest, 'mode': 'bm25+vector+reranker' if mode == 'hybrid_rerank' else 'bm25+vector',
                'retrieval_usage': metrics, 'indexed_chapters': indexed, 'indexed_chunks': len(rows),
                'warnings': [] if ranked else ['未召回历史正文片段。'],
                'scope': '检索当前作品使用当前模型已索引的前序正文'}

    def _rank_indexed(self, novel_id, number, query, rows, vector):
        lexical = self.index_store.lexical(novel_id, number, query)
        semantic = []
        for row in rows:
            chunk = dict(row)
            embedding = json.loads(chunk.pop('vector_json'))
            chunk.pop('dimensions')
            chunk['vector_score'] = sum(a*b for a, b in zip(vector, embedding))
            semantic.append(chunk)
        semantic.sort(key=lambda chunk: (-chunk['vector_score'], -chunk['chapter_number'], chunk['start']))
        return self._fuse(lexical, semantic)

    @staticmethod
    def _fuse(lexical, semantic):
        fusion = {}
        for channel, items in [('bm25', lexical[:24]), ('vector', semantic[:24])]:
            for rank, chunk in enumerate(items, 1):
                merged = fusion.setdefault(chunk['id'], {**chunk, 'score': 0.0, 'channels': []})
                merged['score'] += 1/(60+rank)
                merged['channels'].append(channel)
        return sorted(fusion.values(), key=lambda item: (-item['score'], -item['chapter_number'], item['start']))[:24]

    async def _rerank(self, ranked, query, progress, metrics):
        if self.reranker_factory is None:
            raise ReviewUnavailable('未配置 Reranker，请打开检索配置或选择不带重排的混合模式')
        reranker = self.reranker_factory()
        progress(f'重排 {len(ranked)} 个历史候选片段')
        results = await reranker.rerank(query[:8000], [chunk['text'] for chunk in ranked], len(ranked))
        if hasattr(reranker, 'metrics'):
            metrics['rerank'] = reranker.metrics()['rerank']
        return [{**ranked[item['index']], 'rerank_score': item['relevance_score']} for item in results]

    @staticmethod
    def select(ranked, limit):
        selected = []
        per_chapter = Counter()
        for chunk in ranked:
            if per_chapter[chunk["chapter_id"]] >= 2:
                continue
            selected.append(chunk)
            per_chapter[chunk["chapter_id"]] += 1
            if len(selected) >= limit:
                break
        return selected

    async def _hybrid(self, chunks, query, mode, progress, cancelled):
        if self.embedding_factory is None:
            raise ReviewUnavailable('混合检索需要先配置 Embedding 模型')
        embedding, fingerprint = self.embedding_factory()
        if embedding is None:
            raise ReviewUnavailable('Embedding 模型不可用，请检查检索配置')
        # All bounded source chunks participate in vector recall, including those
        # with zero lexical overlap. Persist each batch so interruption can resume.
        texts = [query[:8000], *(chunk['text'] for chunk in chunks)]
        keys = [text_hash(f'{fingerprint}:{text_hash(text)}') for text in texts]
        vectors = [self.store.get_embedding(key) for key in keys]
        missing = [index for index, vector in enumerate(vectors) if vector is None]
        metrics = {'chunks': len(chunks), 'cached_vectors': len(vectors)-len(missing), 'embedded_texts': len(missing)}
        try:
            for offset in range(0, len(missing), 32):
                if cancelled():
                    return [], {**metrics, 'cancelled': True}
                batch = missing[offset:offset+32]
                progress(f'构建语义索引 {min(offset+32, len(missing))}/{len(missing)}')
                generated = await embedding.embed_batch([texts[index] for index in batch])
                if len(generated) != len(batch):
                    raise ValueError('嵌入数量不匹配')
                for index, vector in zip(batch, generated):
                    if not vector or not all(isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) for x in vector):
                        raise ValueError('无效向量')
                    vectors[index] = vector
                self.store.save_embeddings({keys[index]: vectors[index] for index in batch})
            query_vector = vectors[0]
            if not query_vector or not sum(x*x for x in query_vector):
                raise ValueError('查询向量为空')
            semantic = []
            for chunk, vector in zip(chunks, vectors[1:]):
                if len(vector) != len(query_vector) or not sum(x*x for x in vector):
                    raise ValueError('向量维度不匹配或为空')
                norm = math.sqrt(sum(x*x for x in query_vector)*sum(x*x for x in vector))
                score = sum(x*y for x,y in zip(query_vector,vector))/norm
                semantic.append({**chunk, 'vector_score': score})
            semantic.sort(key=lambda item: (-item['vector_score'], -item['chapter_number'], item['start']))
            ranked = self._fuse(bm25(chunks, query[:8000]), semantic)
            if hasattr(embedding, 'metrics'):
                metrics.update(embedding.metrics())
        except ReviewUnavailable:
            raise
        except Exception:
            raise ReviewUnavailable('混合召回失败，未自动降级；请检查嵌入服务或切换关键词模式') from None
        finally:
            transport = getattr(embedding, '_http_client', None)
            if transport is not None:
                await transport.aclose()
        if mode == 'hybrid_rerank' and ranked and not cancelled():
            ranked = await self._rerank(ranked, query, progress, metrics)
        return ranked, metrics
