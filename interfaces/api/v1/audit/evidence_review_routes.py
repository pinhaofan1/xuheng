"""Xuheng evidence review API; additive to the upstream AI review endpoints."""
from __future__ import annotations

from collections.abc import Callable
import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from application.audit.evidence_models import AIReviewRequest, FeedbackRequest, HistorySearchRequest, RuleSet, RunReviewRequest, text_hash
from application.audit.services.evidence_review_service import (
    EvidenceReviewService, ReviewConflict, ReviewNotFound, ReviewUnavailable,
)
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore

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
    config = RetrievalConfigService(store.db)
    return HistoricalRetriever(store, config.embedding_factory, config.client)


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

    @model_validator(mode="after")
    def unique_prices(self):
        keys = [(p.kind,p.model) for p in self.prices]
        if len(keys) != len(set(keys)):
            raise ValueError("同一模型的单价不能重复")
        return self


@router.get("/{novel_id}/operations")
def operations(novel_id: str, service: EvidenceReviewService = Depends(get_evidence_review_service)):
    from application.audit.services.review_jobs import get_review_jobs
    from infrastructure.persistence.operation_journal import get_operation_journal
    def operation():
        service.store.require_novel(novel_id)
        result = get_operation_journal().summary(novel_id)
        result["jobs"] = get_review_jobs().history(novel_id)
        return result
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
