"""Xuheng evidence review API; additive to the upstream AI review endpoints."""
from __future__ import annotations

from collections.abc import Callable
import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from application.audit.evidence_models import AIReviewRequest, FeedbackRequest, HistorySearchRequest, RuleSet, RunReviewRequest, text_hash
from application.audit.fact_models import FactDecision
from application.audit.services.evidence_review_service import (
    EvidenceReviewService, ReviewConflict, ReviewNotFound, ReviewUnavailable,
)
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore
from infrastructure.persistence.database.fact_store import FactStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/evidence-reviews", tags=["evidence-reviews"])


def get_evidence_review_service() -> EvidenceReviewService:
    return EvidenceReviewService(EvidenceReviewStore(get_database()))


def _call(operation: Callable):
    try:
        return operation()
    except ReviewNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ReviewUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("Evidence review failed")
        raise HTTPException(500, "审校暂时不可用，请查看服务日志后重试") from exc


@router.get("/{novel_id}/rules", response_model=RuleSet)
def get_rules(novel_id: str, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: service.get_rules(novel_id))


@router.put("/{novel_id}/rules", response_model=RuleSet)
def save_rules(novel_id: str, request: RuleSet, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: service.save_rules(novel_id, request))


@router.get('/{novel_id}/rules/import-preview')
def import_character_preview(novel_id: str, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from application.audit.services.character_import import preview_character_rules
    return _call(lambda: preview_character_rules(service.store, novel_id))


@router.get('/{novel_id}/facts')
def list_facts(novel_id: str, status: Literal['pending', 'accept', 'coexist', 'reject'] | None = None,
               offset: int = Query(0, ge=0), service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: FactStore(service.store.db).list_facts(novel_id, status=status, offset=offset))


@router.get('/{novel_id}/facts/{fact_id}/history')
def fact_history(novel_id: str, fact_id: str, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: FactStore(service.store.db).history(novel_id, fact_id))


@router.get('/{novel_id}/facts/{fact_id}/comparison')
def compare_fact(novel_id: str, fact_id: str, profile_id: str = Query(min_length=1, max_length=80),
                 service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: FactStore(service.store.db).compare(novel_id, fact_id, profile_id))


@router.post('/{novel_id}/facts/{fact_id}/decisions')
def decide_fact(novel_id: str, fact_id: str, request: FactDecision,
                service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: FactStore(service.store.db).decide(novel_id, fact_id, request))


@router.post("/{novel_id}/chapters/{number}")
def review_chapter(novel_id: str, request: RunReviewRequest, number: int = Path(ge=1),
                   service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: service.review(novel_id, number, request.expected_content))


@router.get("/{novel_id}/chapters/{number}/latest")
def latest(novel_id: str, number: int = Path(ge=1), mode: Literal["rules", "ai"] = "rules", service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: service.latest(novel_id, number, mode))


@router.get("/{novel_id}/runs/{run_id}")
def get_run(novel_id: str, run_id: str, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: service.get_run(novel_id, run_id))


@router.patch("/{novel_id}/runs/{run_id}/issues/{issue_id}")
def feedback(novel_id: str, run_id: str, issue_id: str, request: FeedbackRequest,
             service: EvidenceReviewService = Depends(get_evidence_review_service)):
    return _call(lambda: service.feedback(novel_id, run_id, issue_id, request.status, request.expected_version))


def _embedding_factory():
    from application.ai.embedding_config_service import get_embedding_config_service
    from interfaces.api.dependencies import get_embedding_service
    cfg = get_embedding_config_service().get_config()
    # No secrets enter the embedding cache identity or response.
    identity = f"{cfg.mode}:{cfg.model}:{cfg.model_path}:{cfg.base_url}:{cfg.updated_at}"
    return get_embedding_service(), text_hash(identity)


def get_historical_retriever(store):
    from application.audit.services.history_retrieval import HistoricalRetriever
    from application.ai.retrieval_config_service import RetrievalConfigService
    from infrastructure.persistence.database.search_index_store import SearchIndexStore
    config = RetrievalConfigService(store.db)
    return HistoricalRetriever(store, config.embedding_factory, config.client, index_store=SearchIndexStore(store.db))


def get_ai_review_service(store):
    from application.ai.llm_control_service import LLMControlService
    from application.audit.services.ai_evidence_review import AIEvidenceReviewService
    from infrastructure.ai.evidence_model_client import EvidenceModelClient
    model = EvidenceModelClient(LLMControlService().resolve_active_profile())
    return AIEvidenceReviewService(store, get_historical_retriever(store), model)


@router.get("/model/status")
def model_status():
    from application.ai.llm_control_service import LLMControlService
    from infrastructure.ai.evidence_model_client import configured_model
    return _call(lambda: configured_model(LLMControlService()))


@router.post("/{novel_id}/chapters/{number}/context")
def history_context(novel_id: str, request: HistorySearchRequest, number: int = Path(ge=1),
                    service: EvidenceReviewService = Depends(get_evidence_review_service)):
    def operation():
        chapter = service.store.get_chapter(novel_id, number=number)
        retriever = get_historical_retriever(service.store)
        return asyncio.run(retriever.retrieve(novel_id, number, request.query or chapter["content"] or "", request.limit,
                                              request.semantic, retrieval_mode=request.retrieval_mode))
    return _call(operation)


@router.post("/{novel_id}/chapters/{number}/ai", status_code=202)
def start_ai_review(novel_id: str, request: AIReviewRequest, number: int = Path(ge=1),
                     service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from application.audit.services.review_jobs import get_review_jobs
    review_jobs = get_review_jobs()
    def operation():
        chapter = service.store.get_chapter(novel_id, number=number)
        if request.expected_content is not None and request.expected_content != (chapter["content"] or ""):
            raise ReviewConflict("正文已变化，请重新加载后再审校")
        active = review_jobs.active(novel_id, number)
        if active:
            return active
        ai_service = get_ai_review_service(service.store)
        saved = {**request.model_dump(exclude={"expected_content"}), "content_hash": text_hash(chapter["content"] or "")}
        return review_jobs.start(novel_id, number, lambda progress, cancelled: asyncio.run(ai_service.run(novel_id, number, request, progress, cancelled)), request=saved)
    return _call(operation)


@router.get("/{novel_id}/chapters/{number}/active-job")
def active_job(novel_id: str, number: int = Path(ge=1), service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from application.audit.services.review_jobs import get_review_jobs
    review_jobs = get_review_jobs()
    def operation():
        service.store.get_chapter(novel_id, number=number)
        return review_jobs.active(novel_id, number)
    return _call(operation)


@router.get("/{novel_id}/jobs/{job_id}")
def get_job(novel_id: str, job_id: str):
    from application.audit.services.review_jobs import get_review_jobs
    review_jobs = get_review_jobs()
    return _call(lambda: review_jobs.get(novel_id, job_id))


@router.post("/{novel_id}/jobs/{job_id}/cancel")
def cancel_job(novel_id: str, job_id: str):
    from application.audit.services.review_jobs import get_review_jobs
    review_jobs = get_review_jobs()
    return _call(lambda: review_jobs.cancel(novel_id, job_id))


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    kind: Literal["llm", "embedding", "rerank"]
    model: str = Field(min_length=1, max_length=200)
    input_per_million: float = Field(ge=0, le=1_000_000)
    output_per_million: float = Field(ge=0, le=1_000_000)


class UsagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    max_calls: int | None = Field(default=None, ge=0, le=10_000_000)
    max_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
    max_cost: float | None = Field(default=None, ge=0, le=1_000_000)
    prices: list[ModelPrice] = Field(default_factory=list, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", max_length=80)
    daily_max_calls: int | None = Field(default=None, ge=0, le=10_000_000, strict=True)
    daily_max_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000_000, strict=True)
    daily_max_cost: float | None = Field(default=None, ge=0, le=1_000_000, strict=True)

    @model_validator(mode="after")
    def unique_prices(self):
        from infrastructure.persistence.usage_ledger import validate_timezone
        validate_timezone(self.timezone)
        keys = [(p.kind,p.model) for p in self.prices]
        if len(keys) != len(set(keys)):
            raise ValueError("同一模型的单价不能重复")
        return self


@router.get("/{novel_id}/operations")
def operations(novel_id: str, day: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
               offset: int = Query(default=0, ge=0, le=1_000_000_000), uncertain_only: bool = False,
               service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from application.audit.services.review_jobs import get_review_jobs
    from infrastructure.persistence.operation_journal import get_operation_journal
    def operation():
        service.store.require_novel(novel_id)
        result = get_operation_journal().summary(novel_id, day, offset, uncertain_only)
        result["jobs"] = get_review_jobs().history(novel_id)
        rows = get_operation_journal().chapter_progress(novel_id)
        current = {}
        for row in rows:
            number = row["number"]
            if number not in current:
                chapter = service.store.db.fetch_one("SELECT content FROM chapters WHERE novel_id=? AND number=?",(novel_id,number))
                current[number] = text_hash(chapter["content"] or "") if chapter else None
            row["stale"] = row["content_hash"] != current[number]
            row["draft_available"] = bool(row["draft_available"])
            row["uncommitted"] = row["stage"] == "saving" and row["stale"] and row["draft_available"]
        result["chapter_progress"] = rows
        from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore
        tasks = AftermathTaskStore(service.store.db)
        result["aftermath_tasks"] = tasks.list(novel_id)
        return result
    return _call(operation)


class ReconcileUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    input_tokens: int = Field(ge=0, le=1_000_000_000_000, strict=True)
    output_tokens: int = Field(ge=0, le=1_000_000_000_000, strict=True)
    billed_cost: float | None = Field(default=None, ge=0, le=1_000_000, strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    request_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    note: str = Field(min_length=1, max_length=1000)
    reference: str = Field(default="", max_length=240)
    request_finished: bool = Field(strict=True)

    @model_validator(mode="after")
    def confirmed_finished(self):
        if not self.request_finished:
            raise ValueError("请先确认已在服务商核实此请求结束")
        return self


class ResumeAftermath(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    confirm_unknown: bool = Field(default=False, strict=True)
    retry_model_calls: bool = Field(default=False, strict=True)
    note: str = Field(default="", max_length=1000)


@router.post("/{novel_id}/operations/aftermath/{task_id}/resume")
def resume_aftermath(novel_id: str, task_id: str, request: ResumeAftermath,
                     service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore, TaskConflict
    def operation():
        service.store.require_novel(novel_id)
        store = AftermathTaskStore(service.store.db)
        try:
            store.resume(novel_id, task_id, request.confirm_unknown, request.note, request.retry_model_calls)
            return store.get(novel_id, task_id)
        except KeyError as exc:
            raise ReviewNotFound("未找到该作品的章后任务") from exc
        except TaskConflict as exc:
            raise ReviewConflict(str(exc)) from exc
    return _call(operation)


@router.get("/{novel_id}/operations/calls/{call_id}/reconciliations")
def usage_reconciliation_history(novel_id: str, call_id: str,
                                 service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from infrastructure.persistence.operation_journal import get_operation_journal
    def operation():
        service.store.require_novel(novel_id)
        try:
            return get_operation_journal().reconciliation_history(novel_id, call_id)
        except KeyError as exc:
            raise ReviewNotFound("未找到该作品的请求记录") from exc
    return _call(operation)


@router.post("/{novel_id}/operations/calls/{call_id}/reconciliations")
def reconcile_usage(novel_id: str, call_id: str, request: ReconcileUsage,
                    service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from infrastructure.persistence.operation_journal import get_operation_journal, now
    from infrastructure.persistence.usage_ledger import ReconciliationConflict
    def operation():
        service.store.require_novel(novel_id)
        journal = get_operation_journal()
        with journal.connection() as conn:
            original = conn.execute("SELECT status FROM calls WHERE novel_id=? AND id=?", (novel_id, call_id)).fetchone()
            repeated = conn.execute("SELECT id FROM call_adjustments WHERE novel_id=? AND request_key=?", (novel_id, request.request_key)).fetchone()
        if original is None:
            raise ReviewNotFound("未找到该作品的请求记录")
        if original["status"] == "running" and not repeated:
            novel = service.store.db.fetch_one("SELECT autopilot_status FROM novels WHERE id=?", (novel_id,))
            generating = service.store.db.fetch_one("SELECT id FROM ai_invocation_sessions WHERE status='generating' AND json_extract(context_json,'$.novel_id')=? LIMIT 1", (novel_id,))
            active_jobs = any(job["status"] in {"queued", "running"} for job in journal.jobs(novel_id))
            if novel["autopilot_status"] == "running" or generating or active_jobs:
                raise ReviewConflict("此作品仍有任务执行，请先停止并确认服务商请求结束后再核对")
        try:
            return journal.reconcile(novel_id, call_id, request.model_dump(), now())
        except ReconciliationConflict as exc:
            raise ReviewConflict(str(exc)) from exc
        except KeyError as exc:
            raise ReviewNotFound("未找到该作品的请求记录") from exc
    return _call(operation)


@router.get("/{novel_id}/chapters/{number}/drafts/{content_hash}/export")
def export_generated_draft(novel_id: str, number: int = Path(ge=1), content_hash: str = Path(pattern="^[0-9a-f]{64}$"),
                           service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from fastapi.responses import Response
    from infrastructure.persistence.operation_journal import get_operation_journal
    def operation():
        service.store.require_novel(novel_id)
        draft = get_operation_journal().chapter_step(novel_id,number,content_hash,"generated_draft")
        if draft is None or text_hash(draft.get("content", "")) != content_hash:
            raise ReviewNotFound("未找到该作品对应版本的生成稿")
        return Response(draft["content"],media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":f'attachment; filename="chapter-{number}-recovered.md"'})
    return _call(operation)


@router.put("/{novel_id}/operations/policy")
def save_usage_policy(novel_id: str, request: UsagePolicy, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from infrastructure.persistence.operation_journal import get_operation_journal
    def operation():
        service.store.require_novel(novel_id)
        return get_operation_journal().save_policy(novel_id, request.model_dump())
    return _call(operation)


@router.post("/{novel_id}/jobs/{job_id}/retry", status_code=202)
def retry_job(novel_id: str, job_id: str, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from application.audit.services.review_jobs import get_review_jobs
    from infrastructure.persistence.operation_journal import get_operation_journal
    def operation():
        manager = get_review_jobs()
        old = manager.get(novel_id, job_id)
        if old["status"] not in {"interrupted", "failed", "cancelled"}:
            raise ReviewConflict("只有中断、失败或取消的任务可以重新发起")
        child = manager.find_retry(novel_id,job_id)
        if child:
            return child
        chapter = service.store.get_chapter(novel_id, number=old["number"])
        saved = get_operation_journal().job_request(novel_id, job_id)
        if saved.get("content_hash") != text_hash(chapter["content"] or ""):
            raise ReviewConflict("正文已变化，请打开章节检查后重新审校")
        request = AIReviewRequest(**{k:v for k,v in saved.items() if k != "content_hash"}, expected_content=chapter["content"] or "")
        active = manager.active(novel_id, old["number"])
        if active:
            return active
        ai_service = get_ai_review_service(service.store)
        return manager.start(novel_id, old["number"], lambda progress,cancelled: asyncio.run(ai_service.run(novel_id, old["number"], request, progress, cancelled)),
                             request=saved, retry_of=job_id)
    return _call(operation)


@router.get("/{novel_id}/chapters/{number}/runs")
def run_history(novel_id: str, number: int = Path(ge=1), service: EvidenceReviewService = Depends(get_evidence_review_service)):
    def operation():
        chapter = service.store.get_chapter(novel_id, number=number)
        return service.store.list_runs(novel_id, chapter["id"])
    return _call(operation)


@router.get("/{novel_id}/runs/{run_id}/export")
def export_run(novel_id: str, run_id: str, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from fastapi.responses import Response
    def operation():
        run = service.get_run(novel_id, run_id)
        return Response(json.dumps(run, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="xuheng-review-{run_id[:32]}.json"'})
    return _call(operation)


@router.get('/retrieval/config')
def retrieval_config():
    from application.ai.retrieval_config_service import RetrievalConfigService
    return _call(lambda: RetrievalConfigService(get_database()).public())


from application.ai.retrieval_config_service import RetrievalConfigUpdate


@router.put('/retrieval/config')
def save_retrieval_config(request: RetrievalConfigUpdate):
    from application.ai.retrieval_config_service import RetrievalConfigService
    return _call(lambda: RetrievalConfigService(get_database()).save(request))


@router.post('/retrieval/test')
def test_retrieval_config():
    from application.ai.retrieval_config_service import RetrievalConfigService
    return _call(lambda: asyncio.run(RetrievalConfigService(get_database()).test()))
