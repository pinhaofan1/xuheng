"""Work-scoped background search index operations."""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.search_index_store import SearchIndexStore

router = APIRouter(prefix='/novels/{novel_id}/index', tags=['search-index'])
logger = logging.getLogger(__name__)


def get_search_index():
    return SearchIndexStore(get_database())


class IndexRequest(BaseModel):
    rebuild: bool = False


@router.get('')
def status(novel_id: str, store=Depends(get_search_index)):
    try:
        return store.status(novel_id)
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


async def poll_search_index(stop: asyncio.Event):
    store = get_search_index()
    while not stop.is_set():
        try:
            worked = await asyncio.to_thread(store.process_next)
        except Exception:
            logger.exception('Search index worker could not finish a chapter')
            worked = False
        try:
            await asyncio.wait_for(stop.wait(), timeout=.01 if worked else 1)
        except asyncio.TimeoutError:
            pass
