"""Real SQLite, real accounting and killed processes at chapter-work boundaries."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.ai.durable_calls import durable_stage, ConfirmationRequired
from application.engine.services.durable_aftermath import DurableAftermath, effect
from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.ai.provider_factory import DynamicLLMService
from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore, TaskConflict, TaskStale
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def db(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'main.sqlite3'))
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','fixture','book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES ('c','book',1,'fixture','saved text','completed')")
    db.execute("CREATE TABLE effects(id INTEGER PRIMARY KEY, label TEXT)")
    db.commit()
    yield db
    db.close_all()


@pytest.fixture
def store(db):
    return AftermathTaskStore(db)


def create_work(store, content='saved text', book='book'):
    task = store.enqueue(book, 1, content)
    return DurableAftermath(store, store.get(book, task['id'], private=True))


@pytest.fixture
def model(monkeypatch):
    provider = SimpleNamespace(settings=SimpleNamespace(default_model='fixture', default_temperature=.7),
        generate=AsyncMock(return_value=GenerationResult('model output', TokenUsage(12, 4))))
    service = DynamicLLMService()
    monkeypatch.setattr(service, '_resolve_provider', AsyncMock(return_value=provider))
    monkeypatch.setattr('infrastructure.ai.provider_factory.get_trace_recorder', lambda: SimpleNamespace(record_span=lambda **kwargs: None))
    return service, provider


class Pipeline:
    def __init__(self, db, model=None):
        self.db, self.model, self.fail_after_effect = db, model, False
        self.core_calls = 0

    async def _run_after_chapter_saved_legacy(self, *args, **kwargs):
        self.core_calls += 1
        async def core():
            if self.model:
                await self.model.generate(Prompt(system='s', user='u'), GenerationConfig(model='fixture', max_tokens=64))
            effect('core_data', lambda: self._insert('core'))
            if self.fail_after_effect:
                raise OSError('fixture crash after committed effect')
            return {'narrative_sync_ok': True}
        return await durable_stage('narrative_sync', core)

    def _insert(self, label):
        self.db.execute('INSERT INTO effects(label) VALUES (?)', (label,))
        self.db.get_connection().commit()

    async def _run_auxiliary_stages(self, book, number, content, result):
        async def tail():
            effect('tail_data', lambda: self._insert('tail'))
            return {'tail_saved': True}
        result.update(await durable_stage('tail', tail))


@pytest.mark.asyncio
async def test_core_and_tail_resume_without_reapplying_or_rebilling(db, store, model):
    service, provider = model
    pipeline = Pipeline(db, service)
    pipeline.fail_after_effect = True
    work = create_work(store)
    assert (await work.execute(pipeline))['aftermath_task_status'] == 'failed'
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 1
    store.resume('book', work.task['id'])
    pipeline.fail_after_effect = False
    recovered = create_work(store)
    assert (await recovered.execute(pipeline))['narrative_sync_ok']
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 1
    assert provider.generate.await_count == 1
    assert (await create_work(store).execute(pipeline))['aftermath_task_status'] == 'completed'
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 2
    await create_work(store).execute(pipeline)
    assert provider.generate.await_count == 1
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 2


@pytest.mark.asyncio
async def test_lost_paid_response_requires_explicit_confirmation(store, db, model):
    service, provider = model
    provider.generate.side_effect = OSError('fixture disconnected')
    pipeline = Pipeline(db, service)
    task = create_work(store)
    result = await task.execute(pipeline)
    assert result['aftermath_task_status'] == 'waiting_confirmation'
    assert store.get('book', task.task['id'])['unknown_requests'] == 1
    with pytest.raises(TaskConflict):
        store.resume('book', task.task['id'])
    assert (await create_work(store).execute(pipeline))['aftermath_task_status'] == 'waiting_confirmation'
    assert provider.generate.await_count == 1
    store.resume('book', task.task['id'], True, 'provider invoice checked; retry authorized')
    provider.generate.side_effect = None
    assert (await create_work(store).execute(pipeline))['narrative_sync_ok']
    history = store.get('book', task.task['id'])
    assert history['confirmations'][0]['confirmed_unknown'] == 1
    assert len(history['requests']) == 2
    assert history['requests'][0]['call_ids']
    assert provider.generate.await_count == 2


@pytest.mark.asyncio
async def test_budget_denial_is_not_a_paid_request(store, db, model):
    from application.engine.services.chapter_processing import journal_for_db
    journal_for_db(db).save_policy('book', {'max_calls': 0})
    pipeline = Pipeline(db, model[0])
    task = create_work(store)
    result = await task.execute(pipeline)
    assert result['aftermath_task_status'] == 'failed'
    assert store.get('book', task.task['id'])['unknown_requests'] == 0
    model[1].generate.assert_not_awaited()
    assert journal_for_db(db).summary('book')['totals']['calls'] == 0
    store.resume('book', task.task['id'])


@pytest.mark.asyncio
async def test_cancellation_stops_worker_and_retains_uncertain_admitted_request(store, db, model):
    service, provider = model
    started = asyncio.Event()
    async def pending(*args):
        started.set()
        await asyncio.Event().wait()
    provider.generate.side_effect = pending
    work = create_work(store)
    task = asyncio.create_task(work.execute(Pipeline(db, service)))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = store.get('book', work.task['id'])
    assert saved['status'] == 'waiting_confirmation' and saved['unknown_requests'] == 1
    assert provider.generate.await_count == 1


@pytest.mark.asyncio
async def test_sql_error_swallowed_inside_legacy_function_rolls_back_everything(store, db):
    work = create_work(store)
    assert store.claim('book', work.task['id'], work.owner)
    def legacy():
        db.execute("INSERT INTO effects(label) VALUES ('rollback')")
        db.get_connection().commit()
        try:
            db.execute('INSERT INTO missing_fixture_table VALUES (1)')
        except Exception:
            pass
    with work.scope(), pytest.raises(RuntimeError):
        effect('broken', legacy)
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 0
    assert store.step(work.task['id'], 'effect:core:broken') == (False, None)


def test_effect_cannot_enqueue_writes_or_commit_before_its_receipt(store, db, monkeypatch):
    import infrastructure.persistence.database.write_dispatch as dispatch
    work = create_work(store)
    assert store.claim('book', work.task['id'], work.owner)
    monkeypatch.setattr(dispatch, 'allow_direct_sqlite_writes', lambda: False)
    monkeypatch.setattr(dispatch, 'is_sqlite_writer_thread', lambda: False)
    monkeypatch.setattr(dispatch, 'enqueue_execute_sql', lambda *args: pytest.fail('Must join effect transaction'))
    def save():
        with db.transaction() as conn:
            conn.execute("INSERT INTO effects(label) VALUES ('joined')")
        db.commit()
        db.get_connection().commit()
    with work.scope():
        effect('joined', save)
        effect('joined', save)
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 1


@pytest.mark.parametrize('where', ['before_commit', 'after_commit'])
def test_process_exit_keeps_effect_and_receipt_in_the_same_transaction(store, db, tmp_path, where):
    task = create_work(store)
    script = tmp_path / 'crash.py'
    script.write_text('''import os,sys
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore
from application.engine.services.durable_aftermath import DurableAftermath,effect
db=DatabaseConnection(sys.argv[1]); store=AftermathTaskStore(db)
task=store.get('book',sys.argv[2],private=True); work=DurableAftermath(store,task)
assert store.claim('book',task['id'],work.owner)
def save():
    db.execute("INSERT INTO effects(label) VALUES ('killed process')")
    db.get_connection().commit()
    if sys.argv[3]=='before_commit': os._exit(23)
with work.scope(): effect('child',save)
os._exit(23)
''', encoding='utf-8')
    env = {**os.environ, 'PYTHONPATH': str(Path.cwd())}
    child = subprocess.run([sys.executable, str(script), db.db_path, task.task['id'], where], env=env, capture_output=True, timeout=20)
    assert child.returncode == 23, child.stderr.decode(errors='replace')
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == (where == 'after_commit')
    with store.connection() as conn:
        conn.execute('UPDATE xh_aftermath_tasks SET heartbeat=0 WHERE id=?', (task.task['id'],))
    assert store.recover() == [('book', task.task['id'])]
    work = create_work(store)
    assert store.claim('book', work.task['id'], work.owner)
    with work.scope():
        effect('child', lambda: db.execute("INSERT INTO effects(label) VALUES ('recovered')") and None)
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 1


def test_stale_or_deleted_chapter_cannot_apply_or_resume_old_work(store, db):
    work = create_work(store)
    assert store.claim('book', work.task['id'], work.owner)
    db.execute("UPDATE chapters SET content='changed' WHERE id='c'")
    db.commit()
    with work.scope(), pytest.raises(TaskStale):
        effect('stale', lambda: db.execute("INSERT INTO effects(label) VALUES ('bad')"))
    store.finish(work.task['id'], work.owner, 'stale')
    with pytest.raises(TaskStale):
        store.resume('book', work.task['id'])
    with pytest.raises(KeyError):
        store.get('other', work.task['id'])
    db.execute("DELETE FROM chapters WHERE id='c'"); db.commit()
    with pytest.raises(TaskStale):
        store.resume('book', work.task['id'])


@pytest.mark.asyncio
async def test_repeated_resume_does_not_reset_resolved_debts_or_duplicate_graph(store, db):
    from application.world.services.chapter_narrative_sync import persist_bundle_triples_and_foreshadows, update_narrative_debts
    from infrastructure.persistence.database.triple_repository import TripleRepository
    from infrastructure.persistence.database.sqlite_foreshadowing_repository import SqliteForeshadowingRepository
    from infrastructure.persistence.database.sqlite_narrative_debt_repository import SqliteNarrativeDebtRepository
    triples, foreshadows, debts = TripleRepository(db), SqliteForeshadowingRepository(db), SqliteNarrativeDebtRepository(db)
    bundle = {'relation_triples': [{'data': {'subject': '林澈', 'predicate': '持有', 'object': '铜铃'}, 'status': 'pending'}],
              'foreshadow_hints': [{'data': {'description': '铜铃将在雨夜响起', 'suggested_resolve_offset': 3, 'importance': 'high'}}],
              'causal_edges': [{'data': {'source_event': '铜铃丢失', 'target_event': '林澈追查', 'strength': .8}}]}
    work = create_work(store)
    assert store.claim('book', work.task['id'], work.owner)
    with work.scope():
        persist_bundle_triples_and_foreshadows('book', 1, bundle, triples, foreshadows)
        update_narrative_debts('book', 1, bundle, debts)
    debt = debts.get_unresolved('book')[0]
    debts.resolve(debt.id, 9)
    store.finish(work.task['id'], work.owner, 'failed')
    store.resume('book', work.task['id'])
    restored = create_work(store)
    assert store.claim('book', restored.task['id'], restored.owner)
    with restored.scope():
        persist_bundle_triples_and_foreshadows('book', 1, bundle, triples, foreshadows)
        update_narrative_debts('book', 1, bundle, debts)
    assert db.fetch_one("SELECT count(*) AS n FROM triples WHERE novel_id='book'")['n'] == 1
    assert db.fetch_one("SELECT count(*) AS n FROM foreshadows WHERE novel_id='book'")['n'] == 1
    assert debts.get_by_novel('book')[0].resolved_chapter == 9


def test_recovery_does_not_steal_live_owner_and_waits_for_unknown_request(store, db):
    work = create_work(store)
    assert store.claim('book', work.task['id'], work.owner)
    store.begin_request(work.task['id'], 'narrative_sync', 0, 1)
    assert store.recover() == []
    assert not store.claim('book', work.task['id'], 'another-owner')
    with store.connection() as conn:
        conn.execute('UPDATE xh_aftermath_tasks SET heartbeat=0 WHERE id=?', (work.task['id'],))
    assert store.recover() == []
    assert store.get('book', work.task['id'])['status'] == 'waiting_confirmation'


@pytest.mark.asyncio
async def test_explicit_retry_preserves_old_model_output_and_does_not_reset_written_stages(store, db, model):
    service, provider = model
    pipeline = Pipeline(db, service)
    pipeline.fail_after_effect = True
    work = create_work(store)
    await work.execute(pipeline)
    store.resume('book', work.task['id'], note='repair local write only', retry_model_calls=True)
    pipeline.fail_after_effect = False
    await create_work(store).execute(pipeline)
    assert provider.generate.await_count == 1


@pytest.mark.asyncio
async def test_invalid_saved_model_output_requires_explicit_new_request(store, db, model):
    service, provider = model
    fail = [True]
    class InvalidPipeline(Pipeline):
        async def _run_after_chapter_saved_legacy(self, *args, **kwargs):
            async def parse():
                result = await service.generate(Prompt(system='s', user='u'), GenerationConfig(model='fixture', max_tokens=64))
                if fail[0]:
                    raise ValueError('fixture output invalid')
                return {'narrative_sync_ok': True}
            return await durable_stage('parse', parse)
    pipeline = InvalidPipeline(db)
    work = create_work(store)
    await work.execute(pipeline)
    store.resume('book', work.task['id'])
    await create_work(store).execute(pipeline)
    assert provider.generate.await_count == 1
    store.resume('book', work.task['id'], note='request corrected structured output', retry_model_calls=True)
    fail[0] = False
    await create_work(store).execute(pipeline)
    assert provider.generate.await_count == 2
    with store.connection() as conn:
        old = conn.execute("SELECT result FROM xh_aftermath_requests WHERE task_id=? AND attempt=1", (work.task['id'],)).fetchone()
        assert json.loads(old[0])['content'] == 'model output'


@pytest.mark.asyncio
async def test_false_success_and_swallowed_optional_failure_never_receive_completion_receipts(store, db):
    work = create_work(store)
    class Incomplete(Pipeline):
        async def _run_after_chapter_saved_legacy(self, *args, **kwargs):
            async def invalid():
                return {'narrative_sync_ok': False}
            try:
                await durable_stage('narrative_sync', invalid)
            except Exception:
                pass
            return {'narrative_sync_ok': True}
    result = await work.execute(Incomplete(db))
    assert result['aftermath_task_status'] == 'failed'
    assert store.step(work.task['id'], 'stage:narrative_sync') == (False, None)
    assert store.step(work.task['id'], 'stage:core') == (False, None)


@pytest.mark.asyncio
async def test_changed_optional_call_path_cannot_replay_another_operations_output(store, db, model):
    service, provider = model
    provider.generate.side_effect = [GenerationResult('first role', TokenUsage(1,1)), GenerationResult('second role', TokenUsage(2,2))]
    initial = [True]
    output = []
    class Conditional(Pipeline):
        async def _run_after_chapter_saved_legacy(self, *args, **kwargs):
            async def run():
                if initial[0]:
                    await service.generate(Prompt(system='role one', user='u'), GenerationConfig(model='fixture', max_tokens=64))
                result = await service.generate(Prompt(system='role two', user='u'), GenerationConfig(model='fixture', max_tokens=64))
                output.append(result.content)
                if initial[0]:
                    raise OSError('fixture after results saved')
                return {'narrative_sync_ok': True}
            return await durable_stage('conditional', run)
    pipeline = Conditional(db)
    task = create_work(store)
    await task.execute(pipeline)
    store.resume('book', task.task['id'])
    initial[0] = False
    await create_work(store).execute(pipeline)
    assert output == ['second role','second role']
    assert provider.generate.await_count == 2


@pytest.mark.asyncio
async def test_confirming_lost_second_request_keeps_the_first_result(store, db, model):
    service, provider = model
    provider.generate.side_effect = [GenerationResult('saved first', TokenUsage(1,1)), OSError('second lost'), GenerationResult('second recovered', TokenUsage(2,2))]
    class TwoRequests(Pipeline):
        async def _run_after_chapter_saved_legacy(self, *args, **kwargs):
            async def run():
                first = await service.generate(Prompt(system='first role', user='u'), GenerationConfig(model='fixture', max_tokens=64))
                second = await service.generate(Prompt(system='second role', user='u'), GenerationConfig(model='fixture', max_tokens=64))
                assert first.content == 'saved first' and second.content == 'second recovered'
                return {'narrative_sync_ok': True}
            return await durable_stage('two_requests', run)
    task = create_work(store)
    assert (await task.execute(TwoRequests(db)))['aftermath_task_status'] == 'waiting_confirmation'
    store.resume('book', task.task['id'], True, 'provider checked second request')
    assert (await create_work(store).execute(TwoRequests(db)))['narrative_sync_ok']
    assert provider.generate.await_count == 3


@pytest.mark.asyncio
async def test_indexing_failure_keeps_bundle_and_requires_approval_before_new_embedding(store, db, model):
    from application.ai.usage_accounting import ModelCall
    service, provider = model
    disconnected = [True]
    class Indexed(Pipeline):
        async def _run_after_chapter_saved_legacy(self, *args, **kwargs):
            async def run():
                await service.generate(Prompt(system='bundle', user='u'), GenerationConfig(model='fixture', max_tokens=64))
                effect('graph', lambda: self._insert('graph saved'))
                call = ModelCall('embedding','fixture-embedding','summary')
                if disconnected[0]:
                    call.fail()
                    raise OSError('embedding disconnected')
                call.finish(8,0)
                return {'narrative_sync_ok': True, 'vector_stored': True}
            return await durable_stage('indexed', run)
    work = create_work(store)
    assert (await work.execute(Indexed(db)))['aftermath_task_status'] == 'waiting_confirmation'
    store.resume('book', work.task['id'], True, 'embedding usage checked')
    disconnected[0] = False
    assert (await create_work(store).execute(Indexed(db)))['narrative_sync_ok']
    assert provider.generate.await_count == 1
    assert db.fetch_one('SELECT count(*) AS n FROM effects')['n'] == 1


@pytest.mark.asyncio
async def test_saved_model_response_survives_process_exit_before_projection(store, db, model, tmp_path):
    work = create_work(store)
    script = tmp_path / 'response-crash.py'
    script.write_text('''import os,sys,asyncio
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore
from application.engine.services.durable_aftermath import DurableAftermath
from application.ai.durable_calls import durable_generate,durable_stage
from application.ai.usage_accounting import ModelCall
from domain.ai.services.llm_service import GenerationResult,GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
db=DatabaseConnection(sys.argv[1]); store=AftermathTaskStore(db)
work=DurableAftermath(store,store.get('book',sys.argv[2],private=True))
assert store.claim('book',work.task['id'],work.owner)
class LLM:
    @durable_generate
    async def generate(self,prompt,config):
        call=ModelCall('llm','fixture','test',64);call.finish(12,4)
        return GenerationResult('cached child response',TokenUsage(12,4))
async def go():
    with work.scope():
        async def run():
            await LLM().generate(Prompt(system='s',user='u'),GenerationConfig(model='fixture',max_tokens=64))
            os._exit(23)
        await durable_stage('narrative_sync',run)
asyncio.run(go())
''', encoding='utf-8')
    result = subprocess.run([sys.executable, str(script), db.db_path, work.task['id']],
        env={**os.environ, 'PYTHONPATH':str(Path.cwd())}, capture_output=True, timeout=20)
    assert result.returncode == 23, result.stderr.decode(errors='replace')
    with store.connection() as conn:
        conn.execute('UPDATE xh_aftermath_tasks SET heartbeat=0 WHERE id=?', (work.task['id'],))
    assert store.recover() == [('book',work.task['id'])]
    assert (await create_work(store).execute(Pipeline(db, model[0])))['narrative_sync_ok']
    model[1].generate.assert_not_awaited()


def test_api_requires_unknown_confirmation_and_never_exposes_saved_text(store, db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from interfaces.api.v1.audit import evidence_review_routes as routes
    from application.audit.services.evidence_review_service import EvidenceReviewService
    from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore
    work = create_work(store)
    assert store.claim('book', work.task['id'], work.owner)
    store.begin_request(work.task['id'], 'narrative_sync', 0, 1)
    store.finish(work.task['id'], work.owner, 'waiting_confirmation')
    app = FastAPI(); app.include_router(routes.router)
    app.dependency_overrides[routes.get_evidence_review_service] = lambda: EvidenceReviewService(EvidenceReviewStore(db))
    from application.audit.services import review_jobs
    from infrastructure.persistence import operation_journal
    from application.engine.services.chapter_processing import journal_for_db
    journal = journal_for_db(db)
    monkeypatch.setattr(operation_journal, 'get_operation_journal', lambda: journal)
    monkeypatch.setattr(review_jobs, 'get_review_jobs', lambda: SimpleNamespace(history=lambda book: []))
    with TestClient(app) as client:
        path = f"/evidence-reviews/book/operations/aftermath/{work.task['id']}/resume"
        assert client.post(path, json={}).status_code == 409
        assert client.post(path, json={'confirm_unknown': True, 'note': ''}).status_code == 409
        assert client.post(path.replace('/book/', '/other/'), json={}).status_code == 404
        response = client.post(path, json={'confirm_unknown': True, 'note': 'provider checked'})
        assert response.status_code == 200 and response.json()['status'] == 'queued'
        assert client.post(path, json={'confirm_unknown': True, 'note': 'same submit'}).status_code == 200
        tasks = client.get('/evidence-reviews/book/operations').json()['aftermath_tasks']
        assert len(tasks[0]['confirmations']) == 1
        assert 'saved text' not in json.dumps(tasks) and 'payload' not in tasks[0]
        db.execute("UPDATE chapters SET content='changed' WHERE id='c'"); db.commit()
        assert client.post(path, json={}).status_code == 409
        assert client.get('/evidence-reviews/book/operations').json()['aftermath_tasks'][0]['stale']


@pytest.mark.asyncio
async def test_real_pipeline_reuses_model_bundle_and_completes_local_auxiliary_receipts(store, db, model, monkeypatch):
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
    from application.world.services.knowledge_service import KnowledgeService
    from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
    from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
    from infrastructure.persistence.database.triple_repository import TripleRepository
    from infrastructure.persistence.database.sqlite_foreshadowing_repository import SqliteForeshadowingRepository
    from infrastructure.persistence.database.sqlite_narrative_debt_repository import SqliteNarrativeDebtRepository
    from application.checkpoint.services.unified_checkpoint_service import UnifiedCheckpointService
    from domain.novel.value_objects.tension_dimensions import TensionDimensions
    service, provider = model
    provider.generate.return_value = GenerationResult(json.dumps({'summary':'chapter summary', 'relation_triples': [
        {'subject':'林澈','predicate':'持有','object':'铜铃'}], 'foreshadow_hints':[{'description':'铜铃将在雨夜响起'}],
        'character_facts': [{'character_name':'林澈','assertion':'保存的正文事实','quote':'saved text'}]}), TokenUsage(20, 10))
    monkeypatch.setattr('infrastructure.ai.prompt_utils.render_required_prompt', lambda *args: Prompt(system='s',user='bundle'))
    monkeypatch.setattr('application.analyst.services.tension_scoring_service.TensionScoringService.score_chapter', AsyncMock(return_value=TensionDimensions(50,50,50,50)))
    monkeypatch.setattr('application.engine.services.guardrail_execution.run_guardrail_advise_sync', lambda *args: {'passed':True,'overall_score':80,'violations':[]})
    monkeypatch.setattr('interfaces.api.dependencies.get_novel_repository', lambda: SqliteNovelRepository(db))
    monkeypatch.setattr('interfaces.api.dependencies.get_confluence_point_repository', lambda: SimpleNamespace(get_by_novel_id=lambda id: []))
    monkeypatch.setattr('application.paths.get_db_path', lambda: db.db_path)
    chapters = SqliteChapterRepository(db)
    foreshadows = SqliteForeshadowingRepository(db)
    pipeline = ChapterAftermathPipeline(KnowledgeService(SqliteKnowledgeRepository(db)), None, service,
        triple_repository=TripleRepository(db), foreshadowing_repository=foreshadows,
        chapter_repository=chapters, debt_repository=SqliteNarrativeDebtRepository(db),
        unified_checkpoint_service=UnifiedCheckpointService(db, chapters, foreshadows))
    monkeypatch.setattr(pipeline, '_extract_chapter_bridge', AsyncMock(return_value=None))
    first = await pipeline.run_after_chapter_saved('book', 1, 'saved text')
    assert first['narrative_sync_ok']
    task = store.get('book', first['aftermath_task_id'], private=True)
    tail = await DurableAftermath(store, task).execute(pipeline)
    assert tail['aftermath_task_status'] == 'completed'
    assert provider.generate.await_count == 1
    before = db.fetch_one("SELECT count(*) AS n FROM novel_checkpoints WHERE novel_id='book'")['n']
    assert before == 1
    await pipeline.run_after_chapter_saved('book', 1, 'saved text')
    assert provider.generate.await_count == 1
    assert db.fetch_one("SELECT count(*) AS n FROM novel_checkpoints WHERE novel_id='book'")['n'] == before
    from infrastructure.persistence.database.fact_store import FactStore
    facts = FactStore(db).list_facts('book')['facts']
    assert len(facts) == 1 and facts[0]['status'] == 'pending' and facts[0]['source_valid']
