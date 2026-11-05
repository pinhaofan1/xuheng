"""Resume chapter work without reissuing saved requests or reapplying effects."""
import asyncio
from contextlib import contextmanager
from itertools import count
import json
import logging
import time
from uuid import uuid4

from application.ai.durable_calls import ConfirmationRequired, current_work, current_stage, current_request
from application.ai.usage_accounting import usage_scope
from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore, TaskConflict, TaskStale, dump, stamp
from infrastructure.persistence.database.atomic_effect import EffectBinding, _binding

logger = logging.getLogger(__name__)


class DurableAftermath:
    def __init__(self, store, task):
        self.store, self.task = store, task
        self.owner = uuid4().hex
        self.effect_errors = []
        self.stage_errors = []
        self.request_roles = {}

    @contextmanager
    def scope(self):
        from application.engine.services.chapter_processing import journal_for_db
        token = current_work.set(self)
        stage = current_stage.set(("core", count()))
        try:
            with usage_scope(self.task["novel_id"], journal_for_db(self.store.db), operation="章后处理"):
                yield self
        finally:
            current_stage.reset(stage)
            current_work.reset(token)

    def begin_model_request(self, role=None):
        self.assert_owned()
        if self.effect_errors or self.stage_errors:
            raise RuntimeError("已有本地写入失败，停止后续付费请求")
        stage, slots = current_stage.get()
        if role:
            ordinal = self.request_roles.get((stage, role), 0)
            self.request_roles[(stage, role)] = ordinal+1
            slot = f"{role}:{ordinal}"
        else:
            slot = next(slots)
        old = self.store.request(self.task["id"], stage, slot)
        if old and old["status"] in {"running", "uncertain"}:
            raise ConfirmationRequired("发现未确认的模型请求，已暂停恢复")
        if old and old["status"] == "completed":
            return stage, slot, old["attempt"]
        attempt = old["attempt"]+1 if old else 1
        self.store.begin_request(self.task["id"], stage, slot, attempt)
        return stage, slot, attempt

    def charge_key(self):
        self.assert_owned()
        key = current_request.get()
        if key is None:
            stage, slots = current_stage.get()
            slot = -next(slots)-1
            old = self.store.request(self.task["id"], stage, slot)
            if old and old["status"] in {"running", "uncertain"}:
                raise ConfirmationRequired("发现可能已计费的检索或分析请求，请先确认")
            key = stage, slot, old["attempt"]+1 if old else 1
            self.store.begin_request(self.task["id"], *key)
        return key

    def charge(self, call_id, key):
        with self.store.connection() as conn:
            row = conn.execute("SELECT call_ids FROM xh_aftermath_requests WHERE task_id=? AND step=? AND slot=? AND attempt=?", (self.task["id"], *key)).fetchone()
            ids = json.loads(row[0])
            ids.append(call_id)
            conn.execute("UPDATE xh_aftermath_requests SET call_ids=? WHERE task_id=? AND step=? AND slot=? AND attempt=?", (dump(ids), self.task["id"], *key))

    async def stage(self, name, operation):
        self.assert_owned()
        found, value = self.store.step(self.task["id"], "stage:"+name)
        if found:
            return value
        token = current_stage.set((name, count()))
        try:
            result = await operation()
            if self.effect_errors:
                raise RuntimeError("章后本地步骤写入未完成")
            if isinstance(result, dict) and result.get("narrative_sync_ok") is False:
                raise RuntimeError("叙事同步未成功，不保存完成标记")
            self.store.save_step(self.task["id"], "stage:"+name, result)
            return result
        except Exception:
            self.stage_errors.append(name)
            raise
        finally:
            current_stage.reset(token)

    def effect(self, name, operation):
        # A side effect and its receipt are one transaction in the same database.
        if _binding.get() is not None:
            return operation()
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.store.check_content(conn, self.task)
            owner = conn.execute("SELECT owner,status FROM xh_aftermath_tasks WHERE id=?", (self.task["id"],)).fetchone()
            if not owner or owner["owner"] != self.owner or owner["status"] != "running":
                raise TaskConflict("章后任务所有权已变化，停止旧执行器")
            key = "effect:"+current_stage.get()[0]+":"+name
            row = conn.execute("SELECT result FROM xh_aftermath_steps WHERE task_id=? AND step=?", (self.task["id"], key)).fetchone()
            if row:
                return json.loads(row[0])
            binding = EffectBinding(self.store.db, conn)
            token = _binding.set(binding)
            try:
                result = operation()
                if binding.failure:
                    raise RuntimeError("章后写入未能提交，保留已有模型结果") from binding.failure
                conn.execute("INSERT INTO xh_aftermath_steps VALUES (?,?,?,?)", (self.task["id"], key, dump(result), stamp()))
                return result
            except BaseException as exc:
                self.effect_errors.append(name)
                raise
            finally:
                _binding.reset(token)

    async def execute(self, pipeline, wait=False):
        novel, task_id = self.task["novel_id"], self.task["id"]
        waiting_since = time.monotonic()
        while not self.store.claim(novel, task_id, self.owner):
            task = self.store.get(novel, task_id, private=True)
            if not wait or task["result"] or task["status"] not in {"queued", "running"} or time.monotonic()-waiting_since > 180:
                return {**(task["result"] or {}), "narrative_sync_ok": bool(task["result"] and task["result"].get("narrative_sync_ok")), "aftermath_task_id": task_id, "aftermath_task_status": task["status"]}
            await asyncio.sleep(.2)
            self.store.recover()
        stop = asyncio.Event()
        self.request_roles.clear()
        self.effect_errors.clear()
        self.stage_errors.clear()
        async def heartbeat():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        self.store.heartbeat(task_id, self.owner)
                    except Exception:
                        logger.warning("章后任务心跳写入暂不可用 task=%s", task_id, exc_info=True)
        beating = asyncio.create_task(heartbeat())
        try:
            with self.scope():
                payload = self.task["payload"]
                task = self.store.get(novel, task_id, private=True)
                if task["phase"] == "core":
                    result = await pipeline._run_after_chapter_saved_legacy(novel, self.task["number"], **payload)
                    if not result.get("narrative_sync_ok") or self.stage_errors or self.effect_errors:
                        raise RuntimeError("正文已保存，叙事同步未完成，请检查模型配置后恢复")
                    self.store.save_step(task_id, "stage:core", result)
                    # Saving this result also durably enqueues the auxiliary tail.
                    self.store.finish(task_id, self.owner, "queued", phase="auxiliary", result=result)
                    return {**result, "aftermath_task_id": task_id, "aftermath_task_status": "queued"}
                result = dict(task["result"] or {})
                await pipeline._run_auxiliary_stages(novel, self.task["number"], payload["content"], result)
                if self.effect_errors:
                    raise RuntimeError("辅助步骤写入未完成")
                self.store.finish(task_id, self.owner, "completed", result=result)
                return {**result, "aftermath_task_id": task_id, "aftermath_task_status": "completed"}
        except ConfirmationRequired as exc:
            self.store.finish(task_id, self.owner, "waiting_confirmation", str(exc))
        except TaskStale as exc:
            self.store.finish(task_id, self.owner, "stale", str(exc))
        except asyncio.CancelledError:
            # A local effect rolls back; any admitted remote request remains unknown.
            self.store.finish(task_id, self.owner, "waiting_confirmation", "处理已中断，请核对后恢复")
            raise
        except Exception:
            logger.exception("章后任务失败 task=%s", task_id)
            unknown = self.store.get(novel, task_id)["unknown_requests"]
            self.store.finish(task_id, self.owner, "waiting_confirmation" if unknown else "failed", "章后步骤未完成，已有结果保留；请检查配置或日志后恢复")
        finally:
            stop.set()
            await beating
        task = self.store.get(novel, task_id)
        return {"narrative_sync_ok": False, "aftermath_task_id": task_id, "aftermath_task_status": task["status"]}

    def assert_owned(self):
        task = self.store.get(self.task["novel_id"], self.task["id"], private=True)
        if task["owner"] != self.owner or task["status"] != "running":
            raise TaskConflict("章后任务已停止或转交，旧执行器不再请求模型")


def effect(name, operation):
    work = current_work.get()
    return work.effect(name, operation) if work else operation()


async def poll_aftermath_tasks():
    """Only queued/safely recoverable jobs run; uncertain calls need a user decision."""
    from infrastructure.persistence.database.connection import get_database
    store = AftermathTaskStore(get_database())
    while True:
        try:
            pending = store.recover()
            if pending:
                from interfaces.api.dependencies import get_chapter_aftermath_pipeline
                pipeline = get_chapter_aftermath_pipeline()
                for novel, id in pending:
                    try:
                        await DurableAftermath(store, store.get(novel, id, private=True)).execute(pipeline)
                    except TaskStale:
                        with store.connection() as conn:
                            conn.execute("UPDATE xh_aftermath_tasks SET status='stale',error=?,updated_at=? WHERE id=? AND status='queued'", ("正文已修改或删除，旧任务停止", stamp(), id))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("章后任务恢复扫描失败")
        await asyncio.sleep(5)
