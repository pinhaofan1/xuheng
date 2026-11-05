"""Bounded local jobs with durable state and explicit, non-automatic recovery."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from contextvars import ContextVar
from functools import lru_cache
from uuid import uuid4

from application.audit.services.evidence_review_service import ReviewNotFound, ReviewUnavailable

logger = logging.getLogger(__name__)
current_review_job = ContextVar("current_review_job", default=None)


class ReviewJobManager:
    def __init__(self, capacity=2, journal=None, completed_lookup=None):
        self.capacity = capacity
        self.jobs = {}
        self.lock = threading.RLock()
        self.journal = journal
        self.completed_lookup = completed_lookup
        if journal:
            for job in journal.jobs():
                if job["status"] in {"queued", "running"}:
                    if completed_lookup and completed_lookup(job["novel_id"], job["id"]):
                        job.update(status="completed", stage="审校结果已恢复", run_id=job["id"], error="")
                    else:
                        job.update(status="interrupted", stage="服务重启，任务已中断", error="未自动重试；在途请求可能已计费，请核对结果后重新发起")
                    job["updated_at"] = datetime.now(timezone.utc).isoformat()
                    journal.save_job(job)
                self.jobs[job["id"]] = {**job, "event": threading.Event()}

    def _save(self, job):
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        if self.journal:
            self.journal.save_job(job)

    def start(self, novel_id: str, number: int, operation, request=None, retry_of=None) -> dict:
        with self.lock:
            if retry_of:
                child = self.find_retry(novel_id,retry_of)
                if child:
                    return child
            active = [j for j in self.jobs.values() if j["status"] in {"queued", "running"}]
            for job in active:
                if job["novel_id"] == novel_id and job["number"] == number:
                    return self._public(job)
            if len(active) >= self.capacity:
                raise ReviewUnavailable("已有两个 AI 任务进行中，请等待完成后重试")
            for key in list(self.jobs) if not self.journal else []:
                if len(self.jobs) < 80:
                    break
                if self.jobs[key]["status"] not in {"queued", "running"}:
                    self.jobs.pop(key)
            job = {"id": uuid4().hex, "novel_id": novel_id, "number": number,
                   "status": "queued", "stage": "准备审校", "created_at": datetime.now(timezone.utc).isoformat(),
                   "run_id": None, "error": "", "cancel_requested": False, "retry_of": retry_of,
                   "updated_at": datetime.now(timezone.utc).isoformat(), "event": threading.Event()}
            if self.journal:
                self.journal.save_job(job, request or {})
            self.jobs[job["id"]] = job
        def progress(stage):
            with self.lock:
                job["stage"] = stage
                self._save(job)
        def work():
            done = threading.Event()
            def heartbeat():
                while not done.wait(5):
                    with self.lock:
                        self._save(job)
            with self.lock:
                job["status"] = "running"
                self._save(job)
            token = current_review_job.set(job["id"])
            threading.Thread(target=heartbeat, daemon=True, name="xh-review-heartbeat").start()
            try:
                result = operation(progress, job["event"].is_set)
                with self.lock:
                    if result.get("cancelled"):
                        job.update(status="cancelled", stage="已取消")
                    else:
                        job.update(status="completed", stage="审校完成", run_id=result["id"])
                    self._save(job)
            except Exception as exc:
                with self.lock:
                    job.update(status="failed", stage="审校未完成", error=str(exc) if isinstance(exc, (ValueError, ReviewUnavailable)) else "任务执行失败，请查看服务日志")
                    self._save(job)
                logger.error("Review job failed: %s", type(exc).__name__)
            finally:
                done.set()
                current_review_job.reset(token)
        threading.Thread(target=work, name=f"xh-review-{job['id'][:8]}", daemon=True).start()
        return self.get(novel_id, job["id"])

    @staticmethod
    def _public(job):
        return {k: v for k, v in job.items() if k != "event"}

    def get(self, novel_id: str, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ReviewUnavailable("任务已中断或已过期；可查询已保存的运行历史，再决定是否重新发起")
            if job["novel_id"] != novel_id:
                raise ReviewNotFound("未找到该作品中的任务")
            if job["status"] == "interrupted" and self.completed_lookup and self.completed_lookup(novel_id, job_id):
                job.update(status="completed",stage="审校结果已恢复",run_id=job_id,error="")
                self._save(job)
            return self._public(job)

    def history(self, novel_id):
        with self.lock:
            return [self.get(novel_id,j["id"]) for j in sorted(self.jobs.values(), key=lambda j:j["created_at"], reverse=True)
                    if j["novel_id"] == novel_id][:100]

    def find_retry(self, novel_id, parent_id):
        with self.lock:
            return next((self._public(j) for j in self.jobs.values() if j["novel_id"]==novel_id and j.get("retry_of")==parent_id),None)

    def active(self, novel_id: str, number: int) -> dict | None:
        with self.lock:
            return next((self._public(j) for j in self.jobs.values() if j["novel_id"] == novel_id
                         and j["number"] == number and j["status"] in {"queued", "running"}), None)

    def cancel(self, novel_id: str, job_id: str) -> dict:
        self.get(novel_id, job_id)
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] in {"queued", "running"}:
                job["cancel_requested"] = True
                job["event"].set()
                job["stage"] = "已请求取消，在途模型请求结束后停止；服务商仍可能计费"
                self._save(job)
            return self._public(job)


@lru_cache(maxsize=1)
def get_review_jobs():
    from infrastructure.persistence.operation_journal import get_operation_journal
    from infrastructure.persistence.database.connection import get_database
    db = get_database()
    return ReviewJobManager(journal=get_operation_journal(), completed_lookup=lambda novel, job:
        db.fetch_one("SELECT id FROM xh_review_runs WHERE novel_id=? AND id=?", (novel, job)))
