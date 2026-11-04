"""Bounded local AI jobs. Results persist; in-flight jobs do not survive restart."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from uuid import uuid4

from application.audit.services.evidence_review_service import ReviewNotFound, ReviewUnavailable

logger = logging.getLogger(__name__)


class ReviewJobManager:
    def __init__(self, capacity=2):
        self.capacity = capacity
        self.jobs = {}
        self.lock = threading.RLock()

    def start(self, novel_id: str, number: int, operation) -> dict:
        with self.lock:
            active = [j for j in self.jobs.values() if j["status"] in {"queued", "running"}]
            for job in active:
                if job["novel_id"] == novel_id and job["number"] == number:
                    return self._public(job)
            if len(active) >= self.capacity:
                raise ReviewUnavailable("已有两个 AI 任务进行中，请等待完成后重试")
            for key in list(self.jobs):
                if len(self.jobs) < 80:
                    break
                if self.jobs[key]["status"] not in {"queued", "running"}:
                    self.jobs.pop(key)
            job = {"id": uuid4().hex, "novel_id": novel_id, "number": number,
                   "status": "queued", "stage": "准备审校", "created_at": datetime.now(timezone.utc).isoformat(),
                   "run_id": None, "error": "", "cancel_requested": False, "event": threading.Event()}
            self.jobs[job["id"]] = job
        def progress(stage):
            with self.lock:
                job["stage"] = stage
        def work():
            with self.lock:
                job["status"] = "running"
            try:
                result = operation(progress, job["event"].is_set)
                with self.lock:
                    if result.get("cancelled"):
                        job.update(status="cancelled", stage="已取消")
                    else:
                        job.update(status="completed", stage="审校完成", run_id=result["id"])
            except Exception as exc:
                with self.lock:
                    job.update(status="failed", stage="审校未完成", error=str(exc) if isinstance(exc, (ValueError, ReviewUnavailable)) else "任务执行失败，请查看服务日志")
                logger.error("Review job failed: %s", type(exc).__name__)
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
            return self._public(job)

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
            return self._public(job)


review_jobs = ReviewJobManager()
