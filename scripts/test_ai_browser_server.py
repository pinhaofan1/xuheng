"""TEST ONLY: isolated browser fixture server. Never calls a real model.

The normal launcher does not import this module. A separate temporary database
and explicitly labeled fixture model let browser tests exercise the full async
flow without touching user keys, user works or a paid service.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["PLOTPILOT_PROD_DATA_DIR"] = str(ROOT / ".validation" / f"browser-{uuid4().hex}")
os.environ["DISABLE_AUTO_DAEMON"] = "1"
os.environ["EMBEDDING_SERVICE"] = "openai"
for credential in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ARK_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(credential, None)

from application.audit.evidence_models import CharacterRule, RuleSet
from application.audit.services.ai_evidence_review import AIEvidenceReviewService
from application.audit.services.history_retrieval import HistoricalRetriever
from infrastructure.ai import evidence_model_client
from infrastructure.ai.evidence_model_client import ModelReply
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue
from interfaces.api.v1.audit import evidence_review_routes as routes


class BrowserFixtureModel:
    fingerprint = "browser-test-fixture-not-a-real-provider"
    async def complete(self, system, user, output_tokens):
        from application.ai.usage_accounting import ModelCall
        call = ModelCall("llm", "fixture-test-only", system+user, output_tokens)
        await asyncio.sleep(.2)
        payload = json.loads(user)
        source = next(s for s in payload["history_sources"] if "左臂已经断了" in s["text"])
        valid = {"entity_id": "shen", "issue_type": "state", "description": "左臂状态疑似不一致，需要确认是否存在康复情节。",
                 "target_quote": "沈澜抬起左臂", "source_id": source["id"], "source_quote": "左臂已经断了", "certainty": "conflict"}
        forged = {**valid, "target_quote": "正文里从未出现的这句话"}
        call.finish(111,42)
        return ModelReply(json.dumps({"summary": "浏览器测试响应，非真实模型结果。", "issues": [valid, forged]}, ensure_ascii=False),
                          "fixture-test-only", 111, 42)


def browser_aftermath_pipeline(db):
    """Test-only model and durable pipeline; never use user profiles or a network."""
    from types import SimpleNamespace
    from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
    from application.ai.durable_calls import durable_stage
    from application.engine.services.durable_aftermath import effect
    from infrastructure.ai.provider_factory import DynamicLLMService
    from infrastructure.ai.config.settings import Settings
    from domain.ai.services.llm_service import GenerationResult, GenerationConfig
    from domain.ai.value_objects.token_usage import TokenUsage
    from domain.ai.value_objects.prompt import Prompt
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
    class Provider:
        settings = Settings(api_key='fixture', default_model='fixture-aftermath-only')
        async def generate(self, prompt, config):
            await asyncio.sleep(.1)
            return GenerationResult('浏览器章后测试结果，非真实模型', TokenUsage(20,10))
    llm = DynamicLLMService()
    async def resolve():
        return Provider()
    llm._resolve_provider = resolve
    class Pipeline(ChapterAftermathPipeline):
        async def _run_after_chapter_saved_legacy(self, novel_id, number, **payload):
            async def run():
                await llm.generate(Prompt(system='fixture', user='aftermath'), GenerationConfig(model='fixture-aftermath-only', max_tokens=64))
                effect('browser_projection', lambda: None)
                return {'narrative_sync_ok': True, 'fixture': True}
            return await durable_stage('narrative_sync', run)
        async def _run_auxiliary_stages(self, novel, number, content, result):
            async def run():
                effect('browser_auxiliary', lambda: None)
                return {}
            await durable_stage('aux_fixture', run)
    return Pipeline(None, None, llm, chapter_repository=SqliteChapterRepository(db))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8011)
    args = parser.parse_args()
    db = get_database()
    with sqlite_writes_bypass_queue():
        with db.transaction() as tx:
            tx.execute("INSERT INTO novels(id,title,slug,author,target_chapters) VALUES ('ai-browser-fixture','浏览器验证作品','ai-browser-fixture','测试数据',3)")
            tx.execute("INSERT INTO unified_characters(id,novel_id,name,description) VALUES ('fixture-shen','ai-browser-fixture','沈澜','左臂受伤，尚未康复。')")
            for number, content in [(1, "🌊沈澜的左臂已经断了，无法抬起。"), (2, "沈澜抬起左臂，指向远方。"), (3, "未来章节：沈澜的左臂已经康复。")]:
                tx.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES (?,'ai-browser-fixture',?,'断臂与潮声',?)", (f"fixture-{number}", number, content))
        store = EvidenceReviewStore(db)
        store.save_rules("ai-browser-fixture", RuleSet(profiles=[CharacterRule(id="shen", name="沈澜", allowed_magic=["水系"])]))
    from application.world.services.chapter_narrative_sync import persist_fact_candidates
    persist_fact_candidates(db, 'ai-browser-fixture', 1, '🌊沈澜的左臂已经断了，无法抬起。', [
        {'character_name': '沈澜', 'assertion': '左臂骨折', 'quote': '沈澜的左臂已经断了'},
    ])
    # Seed an interrupted request to verify restart recovery in the real UI.
    from infrastructure.persistence.operation_journal import get_operation_journal, now
    from application.audit.evidence_models import text_hash
    journal = get_operation_journal()
    pending_call = journal.reserve("ai-browser-fixture", "llm", "fixture-ledger-only", 120, 80,
                                   operation="浏览器测试：待核对账单")
    journal.finish(pending_call, status="failed")
    journal.save_job({"id":"fixture-interrupted", "novel_id":"ai-browser-fixture", "number":2,
        "status":"running", "stage":"等待模型", "created_at":now(), "updated_at":now(),
        "run_id":None, "error":"", "cancel_requested":False},
        {"content_hash":text_hash("沈澜抬起左臂，指向远方。"),"output_tokens":2048,"retrieval_mode":"bm25"})
    draft="浏览器测试生成稿：沈澜握住船舷，保留了那封未寄出的信。"
    digest=text_hash(draft)
    journal.save_chapter_step("ai-browser-fixture",1,digest,"generated_draft",{"content":draft})
    journal.checkpoint("ai-browser-fixture",1,digest,"saving","failed","浏览器测试：正文写入未确认")
    evidence_model_client.configured_model = lambda control: {"configured": True, "model": "fixture-test-only", "protocol": "openai", "host": "fixture", "message": "仅用于浏览器测试，非真实模型"}
    routes.get_ai_review_service = lambda store: AIEvidenceReviewService(store, HistoricalRetriever(store), BrowserFixtureModel())
    from interfaces.main import app
    from interfaces.api import dependencies
    from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore
    pipeline = browser_aftermath_pipeline(db)
    app.dependency_overrides[dependencies.get_chapter_aftermath_pipeline] = lambda: pipeline
    dependencies.get_chapter_aftermath_pipeline = lambda: pipeline
    tasks = AftermathTaskStore(db)
    chapter_task = tasks.enqueue('ai-browser-fixture', 2, '沈澜抬起左臂，指向远方。')
    tasks.claim('ai-browser-fixture', chapter_task['id'], 'fixture-owner')
    tasks.begin_request(chapter_task['id'], 'narrative_sync', 0, 1)
    tasks.finish(chapter_task['id'], 'fixture-owner', 'waiting_confirmation', '浏览器测试：模型请求结果未留存，需确认后恢复')
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
