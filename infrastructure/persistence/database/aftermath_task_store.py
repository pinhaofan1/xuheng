"""Durable per-text tasks, request snapshots and effect receipts in the main DB."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
from uuid import uuid4


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class TaskConflict(ValueError):
    pass


class TaskStale(TaskConflict):
    pass


class AftermathTaskStore:
    LEASE_SECONDS = 45

    def __init__(self, db):
        self.db = db

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def check_content(conn, task):
        row = conn.execute("SELECT content FROM chapters WHERE novel_id=? AND number=?", (task["novel_id"], task["number"])).fetchone()
        if not row or hashlib.sha256((row[0] or "").encode()).hexdigest() != task["content_hash"]:
            raise TaskStale("正文已修改或删除，旧任务不会继续写入；请按当前正文重新处理")

    def enqueue(self, novel_id, number, content, micro_beats=None, voice_result=None):
        digest = hashlib.sha256(content.encode()).hexdigest()
        task_id = hashlib.sha256(f"{novel_id}\0{number}\0{digest}".encode()).hexdigest()
        payload = dump({"content": content, "chapter_micro_beats": micro_beats, "voice_result": voice_result})
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = {"novel_id": novel_id, "number": number, "content_hash": digest}
            self.check_content(conn, task)
            conn.execute("INSERT OR IGNORE INTO xh_aftermath_tasks(id,novel_id,number,content_hash,status,payload,created_at,updated_at) VALUES (?,?,?,?,'queued',?,?,?)",
                         (task_id, novel_id, number, digest, payload, stamp(), stamp()))
        return self.get(novel_id, task_id)

    def get(self, novel_id, task_id, private=False):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM xh_aftermath_tasks WHERE novel_id=? AND id=?", (novel_id, task_id)).fetchone()
            if row is None:
                raise KeyError("未找到该作品的章后任务")
            result = dict(row)
            if private:
                result["payload"] = json.loads(result["payload"])
                result["result"] = json.loads(result["result"]) if result["result"] else None
            else:
                for key in ("payload", "result", "owner", "heartbeat"):
                    result.pop(key, None)
                chapter = conn.execute("SELECT content FROM chapters WHERE novel_id=? AND number=?", (novel_id, result["number"])).fetchone()
                result["stale"] = not chapter or hashlib.sha256((chapter[0] or "").encode()).hexdigest() != result["content_hash"]
                result["steps"] = [r[0] for r in conn.execute("SELECT step FROM xh_aftermath_steps WHERE task_id=? ORDER BY created_at", (task_id,))]
                result["unknown_requests"] = conn.execute("SELECT count(*) FROM xh_aftermath_requests r WHERE task_id=? AND status IN ('running','uncertain') AND NOT EXISTS(SELECT 1 FROM xh_aftermath_steps s WHERE s.task_id=r.task_id AND s.step='stage:'||r.step)", (task_id,)).fetchone()[0]
                result["requests"] = [{**dict(r), "call_ids": json.loads(r["call_ids"])} for r in conn.execute("SELECT step,slot,attempt,status,call_ids,created_at FROM xh_aftermath_requests WHERE task_id=? ORDER BY created_at", (task_id,))]
                result["confirmations"] = [dict(r) for r in conn.execute("SELECT confirmed_unknown,note,created_at FROM xh_aftermath_resumes WHERE task_id=? ORDER BY created_at", (task_id,))]
            return result

    def list(self, novel_id):
        with self.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM xh_aftermath_tasks WHERE novel_id=? ORDER BY created_at DESC LIMIT 100", (novel_id,))]
        return [self.get(novel_id, id) for id in ids]

    def claim(self, novel_id, task_id, owner):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = dict(conn.execute("SELECT * FROM xh_aftermath_tasks WHERE novel_id=? AND id=?", (novel_id, task_id)).fetchone() or {})
            if not task:
                raise KeyError("未找到该作品的章后任务")
            self.check_content(conn, task)
            if task["status"] != "queued":
                return False
            # One chapter job at a time per book, including API/daemon contention.
            active = conn.execute("SELECT id FROM xh_aftermath_tasks WHERE novel_id=? AND status='running' AND id<>?", (novel_id, task_id)).fetchone()
            if active:
                return False
            conn.execute("UPDATE xh_aftermath_tasks SET status='running',owner=?,heartbeat=?,updated_at=?,error='' WHERE id=?", (owner, time.time(), stamp(), task_id))
        return True

    def heartbeat(self, task_id, owner):
        with self.connection() as conn:
            conn.execute("UPDATE xh_aftermath_tasks SET heartbeat=?,updated_at=? WHERE id=? AND owner=? AND status='running'", (time.time(), stamp(), task_id, owner))

    def finish(self, task_id, owner, status, error="", phase=None, result=None):
        with self.connection() as conn:
            conn.execute("UPDATE xh_aftermath_tasks SET status=?,phase=coalesce(?,phase),result=coalesce(?,result),error=?,updated_at=?,owner=NULL WHERE id=? AND owner=?",
                         (status, phase, dump(result) if result is not None else None, error, stamp(), task_id, owner))

    def recover(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM xh_aftermath_tasks WHERE status='running' AND heartbeat<?", (time.time()-self.LEASE_SECONDS,)).fetchall()
            for task in rows:
                unknown = conn.execute("SELECT 1 FROM xh_aftermath_requests r WHERE task_id=? AND status IN ('running','uncertain') AND NOT EXISTS(SELECT 1 FROM xh_aftermath_steps s WHERE s.task_id=r.task_id AND s.step='stage:'||r.step) LIMIT 1", (task["id"],)).fetchone()
                conn.execute("UPDATE xh_aftermath_tasks SET status=?,owner=NULL,error=?,updated_at=? WHERE id=?",
                             ("waiting_confirmation" if unknown else "queued", "请求结果未确认，请核对服务商用量后明确选择是否重新请求" if unknown else "", stamp(), task["id"]))
            return [(r["novel_id"], r["id"]) for r in conn.execute("SELECT novel_id,id FROM xh_aftermath_tasks WHERE status='queued' ORDER BY created_at LIMIT 30")]

    def resume(self, novel_id, task_id, confirm_unknown=False, note="", retry_model_calls=False):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM xh_aftermath_tasks WHERE novel_id=? AND id=?", (novel_id, task_id)).fetchone()
            if task is None:
                raise KeyError("未找到该作品的章后任务")
            self.check_content(conn, task)
            if task["status"] in {"queued", "completed"}:
                return
            if task["status"] == "running":
                raise TaskConflict("任务仍在执行或等待执行进程退出确认，请稍后刷新")
            unknown = conn.execute("SELECT 1 FROM xh_aftermath_requests r WHERE task_id=? AND status IN ('running','uncertain') AND NOT EXISTS(SELECT 1 FROM xh_aftermath_steps s WHERE s.task_id=r.task_id AND s.step='stage:'||r.step) LIMIT 1", (task_id,)).fetchone()
            if unknown and (not confirm_unknown or not note.strip()):
                raise TaskConflict("有可能已计费但结果未留存的请求；请先核对，再明确确认重新请求并填写依据")
            if retry_model_calls and not note.strip():
                raise TaskConflict("重新请求模型需要填写处理依据")
            conn.execute("INSERT INTO xh_aftermath_resumes VALUES (?,?,?,?,?)", (uuid4().hex, task_id, int(confirm_unknown), note.strip()[:1000], stamp()))
            conn.execute("UPDATE xh_aftermath_requests SET status='approved_retry' WHERE task_id=? AND status IN ('running','uncertain')", (task_id,))
            if retry_model_calls:
                conn.execute("""UPDATE xh_aftermath_requests AS r SET status='approved_retry'
                    WHERE task_id=? AND status='completed'
                    AND NOT EXISTS(SELECT 1 FROM xh_aftermath_steps s WHERE s.task_id=r.task_id AND (s.step='stage:'||r.step OR s.step LIKE 'effect:'||r.step||':%'))""", (task_id,))
            conn.execute("UPDATE xh_aftermath_tasks SET status='queued',owner=NULL,error='',updated_at=? WHERE id=?", (stamp(), task_id))

    def step(self, task_id, key):
        with self.connection() as conn:
            row = conn.execute("SELECT result FROM xh_aftermath_steps WHERE task_id=? AND step=?", (task_id, key)).fetchone()
            return (True, json.loads(row[0])) if row else (False, None)

    def save_step(self, task_id, key, result):
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO xh_aftermath_steps VALUES (?,?,?,?)", (task_id, key, dump(result), stamp()))

    def request(self, task_id, step, slot):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM xh_aftermath_requests WHERE task_id=? AND step=? AND slot=? ORDER BY attempt DESC LIMIT 1", (task_id, step, slot)).fetchone()
            return dict(row) if row else None

    def begin_request(self, task_id, step, slot, attempt):
        with self.connection() as conn:
            conn.execute("INSERT INTO xh_aftermath_requests(task_id,step,slot,attempt,status,created_at,updated_at) VALUES (?,?,?,?,'running',?,?)", (task_id, step, slot, attempt, stamp(), stamp()))

    def finish_request(self, task_id, step, slot, attempt, status, result=None):
        with self.connection() as conn:
            conn.execute("UPDATE xh_aftermath_requests SET status=?,result=?,updated_at=? WHERE task_id=? AND step=? AND slot=? AND attempt=?", (status, dump(result) if result is not None else None, stamp(), task_id, step, slot, attempt))
