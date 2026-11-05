"""Controlled two-chapter pipeline check, isolated from user manuscripts.

Default: fixture HTTP transport (includes a 429 and an explicit retry).
--real: at most TWO bounded requests using the locally activated chat profile.
Planning/context are supplied fixtures; optional downstream AI audits are disabled.
This is NOT a long unattended-autopilot or literary-quality evaluation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)
os.environ["PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES"]="1"
os.environ["DISABLE_AUTO_DAEMON"]="1"


async def verify(real=False):
    import httpx
    from openai import AsyncOpenAI
    from application.ai.usage_accounting import usage_scope
    from application.audit.services.evidence_review_service import ReviewUnavailable
    from infrastructure.ai.evidence_model_client import EvidenceModelClient
    from infrastructure.persistence.database.connection import DatabaseConnection
    from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
    from infrastructure.persistence.operation_journal import OperationJournal
    from engine.pipeline.base import BaseStoryPipeline
    from engine.pipeline.context import PipelineContext
    from engine.pipeline.steps import StepResult
    from engine.pipeline.prose_composer import ProseCompositionResult

    requests=[]
    profile=None
    if real:
        from load_env import load_env
        load_env()
        from application.ai.llm_control_service import LLMControlService
        profile=LLMControlService().resolve_active_profile()
        if profile is None:
            raise RuntimeError("No activated model profile; real requests were not sent")
    path=ROOT/".validation"/f"bounded-writing-{uuid4().hex}"
    path.mkdir(parents=True)
    db=DatabaseConnection(str(path/"manuscript.db"))
    journal=OperationJournal(path/"manuscript.operations.sqlite3")
    book="bounded-fixture"
    db.execute("INSERT INTO novels(id,title,slug) VALUES (?,?,?)",(book,"隔离的两章验证",book))
    db.commit()
    journal.save_policy(book,{"max_calls":2 if real else 3,"max_tokens":None,"max_cost":None,"prices":[]})

    def handler(request):
        payload=json.loads(request.content)
        requests.append(payload["max_tokens"])
        if len(requests)==2:
            return httpx.Response(429,json={"error":{"message":"fixture rate limit"}})
        text="【离线测试正文，非真实模型】沈澜沿河寻找失踪的渡船，发现岸边的绳结与旧信中的暗号相同。他没有立即解开绳结，而是在桥下等候，直到灯影映出另一个人的踪迹。"
        return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":1,"model":"fixture-test-only","choices":[{"index":0,"message":{"role":"assistant","content":text},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":60,"total_tokens":160}})
    def factory(**kwargs):
        kwargs["http_client"]=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AsyncOpenAI(**kwargs)
    if not real:
        profile=SimpleNamespace(id="fixture",model="fixture-test-only",protocol="openai",api_key="fixture",base_url="https://fixture.test/v1",extra_body={},extra_headers={},extra_query={},timeout_seconds=30)
    client=EvidenceModelClient(profile) if real else EvidenceModelClient(profile,factory)
    repo=SqliteChapterRepository(db)
    nodes=[SimpleNamespace(node_type=SimpleNamespace(value="chapter"),number=n,title=f"第{n}章",outline="沈澜追查渡船失踪的线索，推进一个具体行动。",description="") for n in range(1,4)]
    class NodeRepo:
        async def get_by_novel(self,novel): return nodes
    class Composer:
        async def compose(self,request):
            with usage_scope(book,journal,"受控两章管线验证"):
                reply=await client.complete("请创作中文悬疑小说正文，只输出正文，不要标题或说明。",f"写第{request.chapter_number}章，300—450字。{request.outline}\n之前已保存正文：{request.context_text[-2000:]}",1024)
            return ProseCompositionResult(content=reply.content)
    class ControlledPipeline(BaseStoryPipeline):
        async def _step_prepare_chapter_plan(self,ctx): return StepResult.ok()
        async def _step_build_context(self,ctx):
            ctx.context_text="\n".join(row["content"] for row in db.fetch_all("SELECT content FROM chapters WHERE novel_id=? ORDER BY number",(book,)))
            return StepResult.ok()
        # Isolate this check from additional, unbounded optional model requests.
        async def _step_run_post_commit(self,ctx): return StepResult.ok()
        def _push_persistence_command(self,ctx): return False
    def context():
        ctx=PipelineContext(novel_id=book,target_word_count=350,auto_approve_mode=True)
        ctx.llm_service=object();ctx.chapter_repository=repo;ctx.story_node_repo=NodeRepo();ctx.prose_composer=Composer()
        return ctx
    first=await ControlledPipeline().run_chapter(context())
    assert first.success,first.error
    original=db.fetch_one("SELECT content FROM chapters WHERE novel_id=? AND number=1",(book,))["content"]
    second=await ControlledPipeline().run_chapter(context())
    if not real:
        assert not second.success and "限流" in second.error
        assert db.fetch_one("SELECT count(*) n FROM chapters WHERE novel_id=?",(book,))["n"]==1
        # Explicit recovery: recreate repositories, journal and pipeline, then retry.
        journal=OperationJournal(journal.path)
        repo=SqliteChapterRepository(db)
        second=await ControlledPipeline().run_chapter(context())
    assert second.success,second.error
    third=await ControlledPipeline().run_chapter(context())
    assert not third.success and "预算" in third.error
    assert db.fetch_one("SELECT content FROM chapters WHERE novel_id=? AND number=1",(book,))["content"]==original
    chapters=db.fetch_all("SELECT number,length(content) chars,status FROM chapters WHERE novel_id=? ORDER BY number",(book,))
    assert len(chapters)==2 and all(row["status"]=="completed" for row in chapters)
    summary=journal.summary(book)
    assert summary["totals"]["calls"]==(2 if real else 3)
    assert journal.summary("other-book")["totals"]["calls"]==0
    result={"mode":"real-model" if real else "fixture-http-not-real-model","model":profile.model,
        "scope":"preplanned two-chapter pipeline; optional downstream AI audits disabled",
        "chapters":[dict(c) for c in chapters],"usage":summary["totals"],"third_request_blocked":True,
        "explicit_rate_limit_recovery":not real,"first_chapter_preserved":True,"cross_book_usage_isolated":True}
    (path/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({**result,"artifact":str(path/"result.json")},ensure_ascii=True))
    db.close_all()
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--real",action="store_true")
    args=parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(verify(args.real))
