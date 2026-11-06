"""Work-scoped background search index operations."""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from application.ai.retrieval_config_service import RetrievalConfigService
from application.ai.usage_accounting import usage_scope
from application.audit.services.evidence_review_service import ReviewUnavailable
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.search_index_store import SearchIndexStore
from infrastructure.persistence.database.search_vector_store import SearchVectorStore
from infrastructure.persistence.operation_journal import BudgetExceeded, journal_at

router = APIRouter(prefix='/novels/{novel_id}/index', tags=['search-index'])
logger = logging.getLogger(__name__)


def get_search_index():
    return SearchIndexStore(get_database())


class IndexRequest(BaseModel):
    rebuild: bool = False


@router.get('')
def status(novel_id: str, store=Depends(get_search_index)):
    try:
        state = store.status(novel_id)
        vectors = SearchVectorStore(store).status(novel_id)
        if vectors:
            try:
                _, fingerprint = RetrievalConfigService(store.db).embedding_factory(cloud_only=True)
                vectors['current_model'] = vectors['fingerprint'] == fingerprint
            except (ValueError, ReviewUnavailable):
                vectors['current_model'] = False
        return {**state, 'vector_job': vectors}
    except KeyError:
        raise HTTPException(404, '作品不存在') from None


@router.post('/jobs', status_code=202)
def enqueue(novel_id: str, body: IndexRequest, store=Depends(get_search_index)):
    try:
        return {'id': store.enqueue(novel_id, rebuild=body.rebuild)}
    except KeyError:
        raise HTTPException(404, '作品不存在') from None


@router.post('/jobs/{job_id}/cancel')
def cancel(novel_id: str, job_id: str, store=Depends(get_search_index)):
    if not store.set_state(novel_id, job_id):
        raise HTTPException(409, '当前索引任务不能取消')
    return store.status(novel_id)


@router.post('/jobs/{job_id}/resume')
def resume(novel_id: str, job_id: str, store=Depends(get_search_index)):
    if not store.set_state(novel_id, job_id, resume=True):
        raise HTTPException(409, '当前索引任务不能继续')
    return store.status(novel_id)


@router.post('/vector-jobs', status_code=202)
def enqueue_vectors(novel_id: str, store=Depends(get_search_index)):
    try:
        current = store.status(novel_id)
        _, fingerprint = RetrievalConfigService(store.db).embedding_factory(cloud_only=True)
        job_id = SearchVectorStore(store).enqueue(novel_id, fingerprint)
        if current['indexed_chapters'] != current['total_chapters']:
            store.enqueue(novel_id)
        return {'id': job_id}
    except KeyError:
        raise HTTPException(404, '作品不存在') from None
    except (ValueError, ReviewUnavailable) as exc:
        raise HTTPException(409, str(exc)) from None


@router.post('/vector-jobs/{job_id}/cancel')
def cancel_vectors(novel_id: str, job_id: str, store=Depends(get_search_index)):
    if not SearchVectorStore(store).cancel(novel_id, job_id):
        raise HTTPException(409, '当前向量索引任务不能暂停')
    return status(novel_id, store)


async def process_vectors(vectors, config, journal):
    job = await asyncio.to_thread(vectors.pending)
    if job is None:
        return False
    try:
        embedding, fingerprint = config.embedding_factory(cloud_only=True)
    except (ReviewUnavailable, ValueError):
        await asyncio.to_thread(vectors.claim, job, '')
        return False
    token, texts = await asyncio.to_thread(vectors.claim, job, fingerprint)
    if not token:
        return False
    try:
        with usage_scope(job['novel_id'], journal, '历史向量索引'):
            batch = await embedding.embed_batch(texts)
        await asyncio.to_thread(vectors.publish, job, token, texts, batch)
    except BudgetExceeded:
        await asyncio.to_thread(vectors.fail, job, token, unknown=False)
    except Exception:
        await asyncio.to_thread(vectors.fail, job, token, unknown=True)
        logger.warning('Vector index batch stopped pending reconciliation')
    return True


async def poll_search_index(stop: asyncio.Event):
    store = get_search_index()
    vectors = SearchVectorStore(store)
    config = RetrievalConfigService(store.db)
    journal = journal_at(Path(store.path).with_suffix('.operations.sqlite3'))
    while not stop.is_set():
        try:
            worked = await asyncio.to_thread(store.process_next)
            if not worked and not stop.is_set():
                worked = await process_vectors(vectors, config, journal)
        except Exception:
            logger.exception('Search index worker could not finish a chapter')
            worked = False
        try:
            await asyncio.wait_for(stop.wait(), timeout=.01 if worked else 1)
        except asyncio.TimeoutError:
            pass
