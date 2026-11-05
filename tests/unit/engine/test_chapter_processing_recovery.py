"""Runtime audit recovery against real manuscript/journal databases.

Only external model operations and auxiliary analyses are fixtures. No paid
service is used; the actual audit delegate, repositories and recovery policy run.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy
from application.engine.services.chapter_processing import content_digest,journal_for_db,pending_saved_chapter
from domain.novel.entities.novel import Novel,NovelStage,AutopilotStatus
from domain.novel.value_objects.novel_id import NovelId
from engine.runtime.audit_delegate import run_chapter_audit
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.steps import StepResult
from engine.pipeline.prose_composer import ProseCompositionResult


@pytest.fixture
def database(tmp_path):
    db=DatabaseConnection(str(tmp_path/"recovery.db"))
    for book in ("book","other"):
        db.execute("INSERT INTO novels(id,title,slug,current_stage,autopilot_status,target_chapters) VALUES (?,?,?,'writing','running',3)",(book,book,book))
        db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES (?,?,1,'测试','沈澜保留了渡船的绳结。','completed')",(book+"-1",book))
    db.commit()
    yield db
    db.close_all()


def make_host(db,book="book",narrative_ok=True):
    host=MagicMock()
    host.novel_repository=SimpleNamespace(db=db)
    host.chapter_repository=SqliteChapterRepository(db)
    host._pending_story_pipeline_aftermath={}
    host._pending_chapter_micro_beats={}
    host._is_still_running.side_effect=lambda novel:novel.autopilot_status==AutopilotStatus.RUNNING
    host._latest_completed_chapter_number.side_effect=lambda nid:db.fetch_one("SELECT max(number) n FROM chapters WHERE novel_id=? AND status='completed'",(nid.value,))["n"]
    host._count_completed_chapters.side_effect=lambda nid:db.fetch_one("SELECT count(*) n FROM chapters WHERE novel_id=? AND status='completed'",(nid.value,))["n"]
    host._read_chapter_stats_ephemeral.return_value=None
    host._similarity_below_warning_threshold.return_value=False
    host._score_voice_only=AsyncMock(return_value={"drift_alert":False,"similarity_score":.9})
    async def timeout(work,**kwargs): return await work
    host._call_with_timeout=timeout
    host._apply_voice_rewrite_loop=AsyncMock(side_effect=lambda novel,chapter,content,result:(content,result))
    host.aftermath_pipeline=SimpleNamespace(run_after_chapter_saved=AsyncMock(return_value={"narrative_sync_ok":narrative_ok,"vector_stored":True,"tension_composite":65}))
    host._run_anti_ai_audit=AsyncMock(return_value=None)
    for name in ("_extract_chapter_bridge","_auto_trigger_macro_diagnosis","_maybe_generate_summaries"):
        setattr(host,name,AsyncMock())
    def persist(novel):
        db.execute("UPDATE novels SET current_stage=?,autopilot_status=?,current_auto_chapters=? WHERE id=?",
                   (novel.current_stage.value,novel.autopilot_status.value,novel.current_auto_chapters,novel.novel_id.value))
        db.commit()
    host._flush_novel.side_effect=persist
    host._save_novel_ephemeral.side_effect=persist
    novel=Novel(NovelId(book),book,"测试",3,current_stage=NovelStage.AUDITING,autopilot_status=AutopilotStatus.RUNNING,auto_approve_mode=True)
    return host,novel


@pytest.fixture
def invocation_fixture(monkeypatch):
    calls=[]
    fail={"operation":None}
    async def request(host,**kwargs):
        operation=kwargs["operation"]
        calls.append((kwargs["novel"].novel_id.value,kwargs["chapter_number"],operation))
        if fail["operation"]==operation:
            raise OSError("fixture disconnected")
        payload={"chapter.audit.risk_flags":[]} if operation.endswith(".audit") else {"chapter.summary":"测试摘要"}
        return SimpleNamespace(status="completed",payload={"commit":SimpleNamespace(result={"continuation":payload})})
    monkeypatch.setattr("engine.runtime.audit_delegate._read_shared_state",lambda _: {})
    monkeypatch.setattr("engine.runtime.audit_delegate._write_autopilot_invocation_input",lambda **_:None)
    monkeypatch.setattr("engine.runtime.audit_delegate._request_autopilot_invocation",request)
    # Tension persistence elsewhere is independently tested; never touch the global DB.
    monkeypatch.setattr("application.world.services.chapter_narrative_sync._write_tension_ephemeral",lambda *a,**k:None)
    return calls,fail


@pytest.mark.asyncio
async def test_restart_after_completed_audit_report_does_not_request_it_again(database,invocation_fixture):
    calls,fail=invocation_fixture
    host,novel=make_host(database)
    fail["operation"]="autopilot.chapter.aftermath"
    with pytest.raises(OSError): await run_chapter_audit(host,novel)
    assert pending_saved_chapter(database,"book")==1
    restarted,restored=make_host(database)
    fail["operation"]=None
    await run_chapter_audit(restarted,restored)
    assert [c[2] for c in calls].count("autopilot.chapter.audit")==1
    assert [c[2] for c in calls].count("autopilot.chapter.aftermath")==2
    restarted._score_voice_only.assert_not_awaited()
    assert pending_saved_chapter(database,"book") is None
    assert restored.current_auto_chapters==1
    assert database.fetch_one("SELECT content FROM chapters WHERE id='book-1'")["content"]=="沈澜保留了渡船的绳结。"


@pytest.mark.asyncio
async def test_failed_narrative_sync_stops_auto_mode_and_explicit_resume_reuses_reports(database,invocation_fixture):
    calls,fail=invocation_fixture
    host,novel=make_host(database,narrative_ok=False)
    await run_chapter_audit(host,novel)
    assert novel.autopilot_status==AutopilotStatus.STOPPED
    assert novel.current_stage==NovelStage.AUDITING
    host._maybe_generate_summaries.assert_not_awaited()
    assert journal_for_db(database).chapter_progress("book")[0]["status"]=="failed"
    previous=list(calls)
    restarted,restored=make_host(database,narrative_ok=True)
    await run_chapter_audit(restarted,restored)
    assert calls==previous
    assert restored.current_stage==NovelStage.WRITING
    assert journal_for_db(database).chapter_progress("book")[0]["status"]=="completed"


@pytest.mark.asyncio
async def test_saved_results_are_not_reused_for_another_book_or_edited_text(database,invocation_fixture):
    calls,fail=invocation_fixture
    host,novel=make_host(database)
    await run_chapter_audit(host,novel)
    database.execute("UPDATE chapters SET content='沈澜解开绳结，渡船驶离。' WHERE id='book-1'");database.commit()
    edited,updated=make_host(database)
    await run_chapter_audit(edited,updated)
    other,other_novel=make_host(database,"other")
    await run_chapter_audit(other,other_novel)
    assert len(calls)==6
    assert sum(c[0]=="other" for c in calls)==2
    assert len(journal_for_db(database).chapter_progress("book"))==2


def test_recovery_detects_commit_before_checkpoint_ack_and_ignores_changed_text(database):
    journal=journal_for_db(database)
    digest=content_digest("沈澜保留了渡船的绳结。")
    journal.checkpoint("book",1,digest,"saving")
    decision=AutopilotRecoveryPolicy(database).decide_on_start("book")
    assert decision.next_stage=="auditing" and decision.chapter_number==1
    assert not decision.discard_transient_generation
    database.execute("UPDATE chapters SET content='作者修改后的正文' WHERE id='book-1'");database.commit()
    assert pending_saved_chapter(database,"book") is None
    assert pending_saved_chapter(database,"other") is None


@pytest.mark.parametrize("operation",["autopilot.chapter.audit","autopilot.chapter.aftermath"])
def test_legacy_stopped_audit_review_resumes_last_saved_chapter(database,operation):
    database.execute("UPDATE novels SET current_stage='paused_for_review',autopilot_status='stopped' WHERE id='book'")
    database.execute("INSERT INTO ai_invocation_sessions(id,operation,node_key,policy,status,context_json,metadata_json) VALUES ('session',?,'fixture','AUTOPILOT_PAUSE','failed','{\"novel_id\":\"book\",\"chapter_number\":1}','{}')",(operation,))
    database.commit()
    decision=AutopilotRecoveryPolicy(database).decide_on_start("book")
    assert decision.next_stage=="auditing"
    assert decision.chapter_number==1
    assert not decision.discard_transient_generation


def test_explicit_resume_after_completed_chapter_review_advances_without_reauditing(database):
    database.execute("UPDATE novels SET current_stage='paused_for_review',autopilot_status='stopped' WHERE id='book'")
    database.execute("INSERT INTO ai_invocation_sessions(id,operation,node_key,policy,status,context_json) VALUES ('session','autopilot.chapter.aftermath','fixture','AUTOPILOT_PAUSE','completed','{\"novel_id\":\"book\",\"chapter_number\":1}')")
    database.commit()
    journal_for_db(database).checkpoint("book",1,content_digest("沈澜保留了渡船的绳结。"),"audited","completed")
    decision=AutopilotRecoveryPolicy(database).decide_on_start("book")
    assert decision.next_stage=="writing" and decision.chapter_number==2


@pytest.mark.asyncio
async def test_writing_guard_uses_persisted_chapter_count_after_counter_update_was_lost(database,monkeypatch):
    from engine.runtime.writing_delegate import run_story_pipeline_writing
    host,novel=make_host(database)
    novel.current_auto_chapters=0;novel.max_auto_chapters=1
    novel.current_stage=NovelStage.WRITING
    build=MagicMock(side_effect=AssertionError("Must stop before another generation"))
    monkeypatch.setattr("engine.runtime.writing_delegate._build_runner",build)
    await run_story_pipeline_writing(host,novel)
    assert novel.current_auto_chapters==1
    assert novel.autopilot_status==AutopilotStatus.STOPPED
    build.assert_not_called()


def test_real_process_exit_preserves_saved_manuscript_and_journal_boundary(tmp_path):
    path=tmp_path/"crash.db"
    # Use a real child termination, not a Python exception caught by the pipeline.
    source="""
import os,sys
from infrastructure.persistence.database.connection import DatabaseConnection
from application.engine.services.chapter_processing import journal_for_db,content_digest
db=DatabaseConnection(sys.argv[1])
db.execute("INSERT INTO novels(id,title,slug,current_stage,autopilot_status) VALUES ('book','fixture','book','writing','running')")
journal_for_db(db).checkpoint('book',1,content_digest('saved body'),'saving')
db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES ('c','book',1,'fixture','saved body','completed')")
db.commit()
os._exit(23)
"""
    environment={**os.environ,"PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES":"1"}
    result=subprocess.run([sys.executable,"-c",source,str(path)],cwd=Path(__file__).resolve().parents[3],env=environment,capture_output=True,timeout=30)
    assert result.returncode==23
    db=DatabaseConnection(str(path))
    decision=AutopilotRecoveryPolicy(db).decide_on_start("book")
    assert decision.next_stage=="auditing"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='c'")["content"]=="saved body"
    db.close_all()


def pipeline_fixture(db,book,generated,crash_at=None):
    """Supply planning/model fixtures; keep actual save and checkpoint code."""
    class NodeRepo:
        async def get_by_novel(self,novel):
            return [SimpleNamespace(node_type=SimpleNamespace(value="chapter"),number=n,title=f"第{n}章",outline="循着渡船的线索推进故事。",description="") for n in range(1,5)]
    class Composer:
        async def compose(self,request):
            generated.append(request.chapter_number)
            return ProseCompositionResult(content=f"离线测试第{request.chapter_number}章：沈澜循着绳结上的暗号，终于在桥下找到了渡船留下的痕迹。")
    class Pipeline(BaseStoryPipeline):
        async def _step_build_context(self,ctx): return StepResult.ok()
        async def _step_prepare_chapter_plan(self,ctx): return StepResult.ok()
        async def _step_save_chapter(self,ctx):
            if crash_at=="before_save": raise SystemExit("fixture process lost before save")
            return await super()._step_save_chapter(ctx)
        async def _step_validate_voice(self,ctx):
            if crash_at=="after_save": raise SystemExit("fixture process lost after save")
            return await super()._step_validate_voice(ctx)
        async def _step_run_post_commit(self,ctx):
            # The real post-commit pipeline's failure handling has separate tests.
            result=await ctx.aftermath_pipeline.run_after_chapter_saved(book,ctx.chapter_number,ctx.chapter_content)
            ctx.narrative_sync_ok=result["narrative_sync_ok"];ctx.tension_composite=65
            return StepResult.ok()
        async def _step_score_tension(self,ctx):
            if crash_at=="after_aftermath": raise SystemExit("fixture process lost after aftermath")
            return await super()._step_score_tension(ctx)
        def _push_persistence_command(self,ctx): return False
    def context(**kwargs):
        ctx=PipelineContext(novel_id=book,target_word_count=100,auto_approve_mode=True)
        ctx.llm_service=object();ctx.chapter_repository=SqliteChapterRepository(db)
        ctx.story_node_repo=NodeRepo();ctx.prose_composer=Composer()
        ctx.aftermath_pipeline=SimpleNamespace(run_after_chapter_saved=AsyncMock(return_value={"narrative_sync_ok":True}))
        return ctx
    return Pipeline,context


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,expect_saved",[("before_save",False),("after_save",True),("after_aftermath",True)])
async def test_pipeline_interruption_respects_real_save_boundaries(database,boundary,expect_saved):
    database.execute("INSERT INTO novels(id,title,slug,current_stage) VALUES ('fresh','fresh','fresh','writing')");database.commit()
    generated=[]
    pipeline,context=pipeline_fixture(database,"fresh",generated,crash_at=boundary)
    with pytest.raises(SystemExit): await pipeline().run_chapter(context())
    chapter=database.fetch_one("SELECT content,status FROM chapters WHERE novel_id='fresh' AND number=1")
    assert bool(chapter)==expect_saved
    if expect_saved:
        assert chapter["status"]=="completed"
        assert pending_saved_chapter(database,"fresh")==1
        assert AutopilotRecoveryPolicy(database).decide_on_start("fresh").next_stage=="auditing"
    else:
        assert pending_saved_chapter(database,"fresh") is None
    assert generated==[1]


@pytest.mark.asyncio
async def test_three_chapters_flow_through_writer_and_audit_without_repeating_completed_work(database,invocation_fixture,monkeypatch):
    from engine.runtime.writing_delegate import run_story_pipeline_writing
    database.execute("INSERT INTO novels(id,title,slug,current_stage,autopilot_status,target_chapters) VALUES ('fresh','fresh','fresh','writing','running',3)");database.commit()
    generated=[]
    pipeline,context=pipeline_fixture(database,"fresh",generated)
    host,novel=make_host(database,"fresh")
    host._is_still_running.side_effect=lambda n:n.autopilot_status==AutopilotStatus.RUNNING
    runner=SimpleNamespace(DEFAULT_TARGET_WORDS=100,_make_context=context,_get_novel_phase=lambda n:"opening")
    monkeypatch.setattr("engine.runtime.writing_delegate._build_runner",lambda daemon:runner)
    monkeypatch.setattr("engine.pipelines.registry.get_pipeline_registry",lambda:SimpleNamespace(create_pipeline=lambda genre:pipeline()))
    for number in range(1,4):
        novel.current_stage=NovelStage.WRITING
        await run_story_pipeline_writing(host,novel)
        assert novel.current_stage==NovelStage.AUDITING
        if number==2:
            # Lose every in-memory map before audit. The durable result must survive.
            host,novel=make_host(database,"fresh")
        await run_chapter_audit(host,novel)
        assert novel.current_auto_chapters==number
    assert generated==[1,2,3]
    assert novel.current_stage==NovelStage.COMPLETED
    assert novel.autopilot_status==AutopilotStatus.STOPPED
    assert len(database.fetch_all("SELECT id FROM chapters WHERE novel_id='fresh'"))==3
    assert all(row["status"]=="completed" for row in journal_for_db(database).chapter_progress("fresh"))
    # The journal, rather than the lost in-memory map, supplied post-commit results.
    host.aftermath_pipeline.run_after_chapter_saved.assert_not_awaited()
    await run_story_pipeline_writing(host,novel)
    assert generated==[1,2,3]


@pytest.mark.asyncio
async def test_pipeline_failed_aftermath_keeps_saved_chapter_and_stops_writer(database,monkeypatch):
    from engine.runtime.writing_delegate import run_story_pipeline_writing
    database.execute("INSERT INTO novels(id,title,slug,current_stage,autopilot_status) VALUES ('fresh','fresh','fresh','writing','running')");database.commit()
    pipeline,context=pipeline_fixture(database,"fresh",[])
    def broken_context(**kwargs):
        ctx=context()
        ctx.aftermath_pipeline.run_after_chapter_saved=AsyncMock(return_value={"narrative_sync_ok":False})
        return ctx
    host,novel=make_host(database,"fresh")
    runner=SimpleNamespace(DEFAULT_TARGET_WORDS=100,_make_context=broken_context,_get_novel_phase=lambda n:"opening")
    monkeypatch.setattr("engine.runtime.writing_delegate._build_runner",lambda daemon:runner)
    monkeypatch.setattr("engine.pipelines.registry.get_pipeline_registry",lambda:SimpleNamespace(create_pipeline=lambda genre:pipeline()))
    await run_story_pipeline_writing(host,novel)
    assert novel.autopilot_status==AutopilotStatus.STOPPED
    assert novel.current_stage==NovelStage.AUDITING
    assert novel.current_auto_chapters==1
    assert database.fetch_one("SELECT status FROM chapters WHERE novel_id='fresh'")["status"]=="completed"
    assert journal_for_db(database).chapter_progress("fresh")[0]["status"]=="failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["blocked","failed","exception"])
async def test_prose_request_failure_stops_without_automatic_second_call(database,monkeypatch,failure):
    from engine.runtime.writing_delegate import run_story_pipeline_writing
    database.execute("INSERT INTO novels(id,title,slug,current_stage,autopilot_status) VALUES ('fresh','fresh','fresh','writing','running')");database.commit()
    pipeline,context=pipeline_fixture(database,"fresh",[])
    composer=AsyncMock(side_effect=OSError("fixture timeout")) if failure=="exception" else AsyncMock(return_value=ProseCompositionResult(status=failure,awaiting_review=True))
    def broken_context(**kwargs):
        ctx=context();ctx.prose_composer=SimpleNamespace(compose=composer)
        return ctx
    host,novel=make_host(database,"fresh")
    runner=SimpleNamespace(DEFAULT_TARGET_WORDS=100,_make_context=broken_context,_get_novel_phase=lambda n:"opening")
    monkeypatch.setattr("engine.runtime.writing_delegate._build_runner",lambda daemon:runner)
    monkeypatch.setattr("engine.pipelines.registry.get_pipeline_registry",lambda:SimpleNamespace(create_pipeline=lambda genre:pipeline()))
    await run_story_pipeline_writing(host,novel)
    assert novel.autopilot_status==AutopilotStatus.STOPPED
    assert novel.current_stage==NovelStage.WRITING
    composer.assert_awaited_once()
    assert database.fetch_one("SELECT count(*) n FROM chapters WHERE novel_id='fresh'")["n"]==0


@pytest.mark.parametrize("phase",["macro","act","audit"])
@pytest.mark.parametrize("status",["failed","blocked","cancelled","awaiting_pre_call_review"])
def test_invocation_pause_distinguishes_failed_request_from_human_review(phase,status):
    import importlib
    module=importlib.import_module(f"engine.runtime.{'macro_planning' if phase=='macro' else 'act_planning' if phase=='act' else 'audit'}_delegate")
    host=MagicMock()
    novel=Novel(NovelId("book"),"book","fixture",3,autopilot_status=AutopilotStatus.RUNNING)
    outcome=SimpleNamespace(status=status,session_id="fixture",operation="autopilot.chapter.audit",node_key="fixture",autopilot_pause_reason="fixture",payload={})
    module._pause_for_invocation(host,novel,outcome)
    assert novel.autopilot_status==(AutopilotStatus.RUNNING if status=="awaiting_pre_call_review" else AutopilotStatus.STOPPED)


@pytest.mark.asyncio
async def test_unacknowledged_write_keeps_generated_draft_and_late_commit_resumes_audit(database,monkeypatch):
    from engine.runtime.writing_delegate import run_story_pipeline_writing
    database.execute("INSERT INTO novels(id,title,slug,current_stage,autopilot_status) VALUES ('fresh','fresh','fresh','writing','running')");database.commit()
    generated=[];queued=[]
    pipeline,context=pipeline_fixture(database,"fresh",generated)
    pipeline.SAVE_CONFIRM_TIMEOUT=.02
    class DelayedRepository(SqliteChapterRepository):
        def save(self,chapter): queued.append(chapter)
    def delayed_context(**kwargs):
        ctx=context();ctx.chapter_repository=DelayedRepository(database)
        return ctx
    host,novel=make_host(database,"fresh")
    runner=SimpleNamespace(DEFAULT_TARGET_WORDS=100,_make_context=delayed_context,_get_novel_phase=lambda n:"opening")
    monkeypatch.setattr("engine.runtime.writing_delegate._build_runner",lambda daemon:runner)
    monkeypatch.setattr("engine.pipelines.registry.get_pipeline_registry",lambda:SimpleNamespace(create_pipeline=lambda genre:pipeline()))
    await run_story_pipeline_writing(host,novel)
    assert novel.autopilot_status==AutopilotStatus.STOPPED
    journal=journal_for_db(database);row=journal.chapter_progress("fresh")[0]
    assert row["status"]=="failed" and row["stage"]=="saving"
    draft=journal.chapter_step("fresh",1,row["content_hash"],"generated_draft")
    assert draft["content"]==queued[0].content
    assert generated==[1]
    assert pending_saved_chapter(database,"fresh") is None
    SqliteChapterRepository(database).save(queued[0]);database.commit()
    decision=AutopilotRecoveryPolicy(database).decide_on_start("fresh")
    assert decision.next_stage=="auditing" and decision.chapter_number==1
    assert generated==[1]
