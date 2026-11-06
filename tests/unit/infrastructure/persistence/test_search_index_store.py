import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.search_index_store import SearchIndexStore
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
