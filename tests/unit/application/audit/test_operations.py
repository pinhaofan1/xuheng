"""Durability, paid-call admission, scoping and recovery with no real service."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import threading
import time
from unittest.mock import AsyncMock

import httpx
from openai import AsyncOpenAI
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.ai.usage_accounting import usage_scope
from application.audit.services.review_jobs import ReviewJobManager
from application.audit.services.evidence_review_service import EvidenceReviewService
from application.audit.services.evidence_review_service import ReviewUnavailable
from infrastructure.persistence.operation_journal import OperationJournal, BudgetExceeded
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore


@pytest.fixture
def journal(tmp_path):
    return OperationJournal(tmp_path / "journal.sqlite3")


def policy(**overrides):
    return {"max_calls": None, "max_tokens": None, "max_cost": None, "prices": [], **overrides}


def price(kind="llm", model="fixture", input=2, output=4):
    return {"kind":kind,"model":model,"input_per_million":input,"output_per_million":output}


def wait_for(manager, book, job, status):
    deadline = time.monotonic()+3
    while time.monotonic()<deadline:
        current = manager.get(book,job)
        if current["status"]==status:
            return current
        time.sleep(.01)
    pytest.fail(f"Job did not reach {status}: {current}")


def test_atomic_budget_cannot_be_bypassed_by_parallel_requests(journal):
    journal.save_policy("book",policy(max_calls=3))
    def reserve(_):
        try:
            return journal.reserve("book","llm","fixture",100,20)
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(reserve,range(12)))
    assert sum(bool(i) for i in ids)==3
    assert journal.summary("book")["totals"]["calls"]==3
    assert journal.reserve("other","llm","fixture",100,20)
    assert journal.budget_block("book")
    journal.save_policy("book",policy(max_calls=20))
    assert journal.budget_block("book") is None


@pytest.mark.asyncio
async def test_runtime_budget_block_stops_the_book_and_preserves_stage(journal,tmp_path,monkeypatch):
    from application.ai.usage_accounting import account_runtime,ModelCall
    import infrastructure.persistence.operation_journal as module
    from domain.novel.entities.novel import AutopilotStatus, NovelStage
    from unittest.mock import Mock
    monkeypatch.setattr(module,"journal_at",lambda path:journal)
    journal.save_policy("book",policy(max_calls=0))
    novel=SimpleNamespace(novel_id=SimpleNamespace(value="book"),autopilot_status=AutopilotStatus.RUNNING,current_stage=NovelStage.WRITING)
    host=SimpleNamespace(novel_repository=SimpleNamespace(db=SimpleNamespace(db_path=str(tmp_path/"book.db"))))
    host._update_shared_state=Mock();host._save_novel_state=Mock()
    invoked=[]
    @account_runtime
    async def stage(host,novel):
        invoked.append(True)
        ModelCall("llm","fixture","test",100)
    with pytest.raises(BudgetExceeded): await stage(host,novel)
    assert novel.autopilot_status==AutopilotStatus.STOPPED
    assert novel.current_stage==NovelStage.WRITING
    await stage(host,novel)
    assert invoked==[True]
    assert journal.summary("book")["totals"]["calls"]==0


@pytest.mark.asyncio
async def test_managed_provider_does_not_retry_hidden_requests_or_invent_missing_usage(journal,monkeypatch):
    from infrastructure.ai.provider_factory import DynamicLLMService
    from infrastructure.ai.providers.openai_provider import OpenAIProvider
    from infrastructure.ai.config.settings import Settings
    from domain.ai.services.llm_service import GenerationConfig
    from domain.ai.value_objects.prompt import Prompt
    settings=Settings(api_key="fixture",base_url="https://example.test/v1",use_legacy_chat_completions=True,default_model="fixture")
    provider=OpenAIProvider(settings)
    responses=[SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))],usage=None),
               SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="正文"))],usage=None)]
    mocked=AsyncMock(side_effect=responses)
    provider.async_client.chat.completions.create=mocked
    service=DynamicLLMService()
    monkeypatch.setattr(service,"_resolve_provider",AsyncMock(return_value=provider))
    with usage_scope("book",journal):
        with pytest.raises(RuntimeError): await service.generate(Prompt(system="写作",user="正文"),GenerationConfig())
        await service.generate(Prompt(system="写作",user="正文"),GenerationConfig())
    assert mocked.await_count==2
    assert journal.summary("book")["totals"]["uncertain_calls"]==2
    assert journal.summary("book")["totals"]["calls"]==2
    await provider._http_client.aclose()


def test_token_limit_counts_unknown_and_failed_calls_conservatively(journal):
    journal.save_policy("book",policy(max_tokens=150))
    first = journal.reserve("book","llm","fixture",100,50)
    journal.finish(first,status="failed")
    with pytest.raises(BudgetExceeded):
        journal.reserve("book","llm","fixture",1,1)
    totals = journal.summary("book")["totals"]
    assert totals["charged_tokens"]==150 and totals["uncertain_calls"]==1


def test_price_snapshot_settlement_and_limits_survive_reopen(journal):
    journal.save_policy("book",policy(max_cost=.001,prices=[price()]))
    first = journal.reserve("book","llm","fixture",100,100)
    journal.save_policy("book",policy(max_cost=.001,prices=[price(input=200,output=400)]))
    journal.finish(first,20,10)
    reopened = OperationJournal(journal.path)
    assert reopened.summary("book")["totals"]["estimated_cost"]==pytest.approx(.00008)
    with pytest.raises(BudgetExceeded):
        reopened.reserve("book","llm","fixture",100,100)
    assert reopened.summary("other")["totals"]["calls"]==0


def test_amount_budget_rejects_unknown_prices_then_can_price_history(journal):
    first = journal.reserve("book","llm","fixture",100,100)
    journal.finish(first,20,10)
    journal.save_policy("book",policy(max_cost=10))
    with pytest.raises(BudgetExceeded):
        journal.reserve("book","llm","fixture",1,1)
    journal.save_policy("book",policy(max_cost=10,prices=[price()]))
    assert journal.summary("book")["totals"]["unknown_cost_calls"]==0
    assert journal.summary("book")["totals"]["estimated_cost"]==pytest.approx(.00008)
    assert journal.reserve("book","llm","fixture",1,1)


def test_unknown_usage_is_not_zero_and_cannot_be_settled_twice(journal):
    journal.save_policy("book",policy(prices=[price()]))
    first = journal.reserve("book","llm","fixture",100,100)
    journal.finish(first)
    journal.finish(first,0,0)
    totals = journal.summary("book")["totals"]
    assert totals["charged_tokens"]==200
    assert totals["estimated_cost"]==pytest.approx(.0006)
    assert totals["uncertain_calls"]==1


def test_durable_jobs_recover_without_reexecuting_and_keep_book_boundary(journal):
    stamp = datetime.now(timezone.utc).isoformat()
    job = {"id":"interrupted","novel_id":"book","number":2,"status":"running","stage":"等待模型", "created_at":stamp,"updated_at":stamp,"run_id":None,"error":"","cancel_requested":False}
    journal.save_job(job,{"content_hash":"snapshot","output_tokens":2048})
    manager = ReviewJobManager(journal=OperationJournal(journal.path))
    assert manager.get("book","interrupted")["status"]=="interrupted"
    assert manager.active("book",2) is None
    assert journal.job_request("book","interrupted")["output_tokens"]==2048
    with pytest.raises(ValueError): manager.get("other","interrupted")
    assert manager.history("other")==[]
    # Result committed just before a crash: recover it without a second request.
    recovered = ReviewJobManager(journal=OperationJournal(journal.path),completed_lookup=lambda n,j: j=="interrupted")
    assert recovered.get("book","interrupted")["run_id"]=="interrupted"
    assert recovered.get("book","interrupted")["status"]=="completed"


def test_job_progress_cancel_and_completion_persist(journal):
    gate = threading.Event()
    manager = ReviewJobManager(journal=journal)
    def operation(progress,cancelled):
        progress("等待测试响应")
        gate.wait(2)
        return {"cancelled":True} if cancelled() else {"id":"result"}
    first = manager.start("book",1,operation,request={"content_hash":"h"})
    assert manager.start("book",1,operation)["id"]==first["id"]
    manager.cancel("book",first["id"])
    gate.set()
    result = wait_for(manager,"book",first["id"],"cancelled")
    assert "updated_at" in result and "event" not in result
    assert OperationJournal(journal.path).jobs("book")[0]["status"]=="cancelled"
    second = manager.start("book",1,lambda p,c:{"id":"result"},retry_of=first["id"])
    wait_for(manager,"book",second["id"],"completed")
    assert manager.history("book")[0]["retry_of"]==first["id"]


@pytest.mark.asyncio
async def test_review_sdk_is_charged_before_validation_and_zero_budget_sends_nothing(journal):
    from infrastructure.ai.evidence_model_client import EvidenceModelClient
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={"id":"r","object":"chat.completion","created":1,"model":"fixture","choices":[{"index":0,"message":{"role":"assistant","content":"truncated"},"finish_reason":"length"}],"usage":{"prompt_tokens":20,"completion_tokens":10,"total_tokens":30}})
    def factory(**kwargs):
        kwargs["http_client"]=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AsyncOpenAI(**kwargs)
    profile=SimpleNamespace(api_key="never-store-this-key",model="fixture",protocol="openai",id="p",base_url="https://example.test/v1",extra_body={},extra_headers={},extra_query={},timeout_seconds=30)
    client=EvidenceModelClient(profile,factory)
    journal.save_policy("book",policy(max_calls=0))
    with usage_scope("book",journal), pytest.raises(BudgetExceeded):
        await client.complete("JSON","test",100)
    assert not requests
    journal.save_policy("book",policy(max_calls=1))
    with usage_scope("book",journal), pytest.raises(ReviewUnavailable):
        await client.complete("JSON","test",100)
    assert len(requests)==1
    totals=journal.summary("book")["totals"]
    assert totals["input_tokens"]==20 and totals["output_tokens"]==10
    assert "never-store-this-key" not in json.dumps(journal.summary("book"))


@pytest.mark.asyncio
async def test_embedding_and_reranker_each_admit_and_account_separately(journal):
    from infrastructure.ai.retrieval_client import RetrievalHTTPClient
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("embeddings"):
            return httpx.Response(200,json={"data":[{"index":0,"embedding":[1,0]}],"usage":{"prompt_tokens":5}})
        return httpx.Response(200,json={"results":[{"index":0,"relevance_score":.8}],"usage":{"prompt_tokens":7}})
    client=RetrievalHTTPClient(api_key="fixture",base_url="https://example.test/v1",embedding_model="embed",reranker_model="rank",dimensions=2,transport=httpx.MockTransport(handler))
    journal.save_policy("book",policy(max_calls=2,prices=[price("embedding","embed"),price("rerank","rank")]))
    with usage_scope("book",journal):
        await client.embed_batch(["正文"])
        await client.rerank("正文",["正文"],1)
        with pytest.raises(BudgetExceeded): await client.embed_batch(["正文"])
    assert len(requests)==2
    totals=journal.summary("book")["totals"]
    assert totals["input_tokens"]==12 and totals["calls"]==2
    assert {row["kind"] for row in journal.summary("book")["calls"]}=={"embedding","rerank"}


@pytest.fixture
def api(tmp_path,journal,monkeypatch):
    from interfaces.api.v1.audit import evidence_review_routes as routes
    from application.audit.services import review_jobs
    import infrastructure.persistence.operation_journal as module
    db=DatabaseConnection(str(tmp_path/"manuscript.db"))
    for name in ["book","other"]:
        db.execute("INSERT INTO novels(id,title,slug) VALUES (?,?,?)",(name,name,name))
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES ('c','book',1,'测试','已保存正文')")
    db.commit()
    manager=ReviewJobManager(journal=journal)
    monkeypatch.setattr(review_jobs,"get_review_jobs",lambda:manager)
    monkeypatch.setattr(module,"get_operation_journal",lambda:journal)
    store=EvidenceReviewStore(db)
    app=FastAPI();app.include_router(routes.router)
    app.dependency_overrides[routes.get_evidence_review_service]=lambda:EvidenceReviewService(store)
    client=TestClient(app)
    yield client,manager,store
    client.close();db.close_all()


def test_policy_api_validation_book_isolation_and_no_reset(api,journal):
    client,manager,store=api
    first=journal.reserve("book","llm","fixture",100,100)
    journal.finish(first,20,10)
    assert client.put("/evidence-reviews/book/operations/policy",json=policy(max_calls=-1)).status_code==422
    assert client.put("/evidence-reviews/book/operations/policy",json=policy(prices=[price(),price()])).status_code==422
    assert client.put("/evidence-reviews/missing/operations/policy",json=policy()).status_code==404
    assert client.put("/evidence-reviews/book/operations/policy",json=policy(max_calls=10)).status_code==200
    assert client.get("/evidence-reviews/book/operations").json()["totals"]["calls"]==1
    assert client.get("/evidence-reviews/other/operations").json()["totals"]["calls"]==0


def test_retry_rejects_changed_manuscript_and_wrong_book(api,journal):
    client,manager,store=api
    job=manager.start("book",1,lambda p,c:(_ for _ in ()).throw(ValueError("fixture")),request={"content_hash":"old"})
    wait_for(manager,"book",job["id"],"failed")
    assert client.post(f"/evidence-reviews/book/jobs/{job['id']}/retry",json={}).status_code==409
    assert client.post(f"/evidence-reviews/other/jobs/{job['id']}/retry",json={}).status_code==404


def test_retry_submits_once_retains_history_and_original_options(api,journal,monkeypatch):
    from interfaces.api.v1.audit import evidence_review_routes as routes
    from application.audit.evidence_models import text_hash
    client,manager,store=api
    job=manager.start("book",1,lambda p,c:(_ for _ in ()).throw(ValueError("fixture")),request={"content_hash":text_hash("已保存正文"),"output_tokens":2048,"retrieval_mode":"bm25"})
    wait_for(manager,"book",job["id"],"failed")
    gate=threading.Event();seen=[]
    async def run(novel,number,request,progress,cancelled):
        seen.append((novel,number,request.output_tokens,request.expected_content))
        await asyncio.to_thread(gate.wait,2)
        return {"id":"result"}
    monkeypatch.setattr(routes,"get_ai_review_service",lambda _:SimpleNamespace(run=run))
    first=client.post(f"/evidence-reviews/book/jobs/{job['id']}/retry",json={}).json()
    second=client.post(f"/evidence-reviews/book/jobs/{job['id']}/retry",json={}).json()
    assert first["id"]==second["id"] and first["retry_of"]==job["id"]
    gate.set();wait_for(manager,"book",first["id"],"completed")
    repeated=client.post(f"/evidence-reviews/book/jobs/{job['id']}/retry",json={}).json()
    assert repeated["id"]==first["id"]
    assert seen==[("book",1,2048,"已保存正文")]
    assert len(manager.history("book"))==2
