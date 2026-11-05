"""Small durable operational journal, separate from the manuscript write queue.

Reservations need a synchronous, atomic acknowledgement before a paid request.
Only short transactions touch this database; no network work runs in a transaction.
Backups must include both the manuscript database and this sibling journal.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class BudgetExceeded(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class OperationJournal:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS policies(novel_id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget_blocks(novel_id TEXT PRIMARY KEY, message TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(
                    id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, reserved_tokens INTEGER NOT NULL,
                    cost REAL, reserved_cost REAL, prices_json TEXT, error TEXT NOT NULL DEFAULT '', operation TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS calls_book ON calls(novel_id,created_at);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, number INTEGER NOT NULL,
                    status TEXT NOT NULL, body TEXT NOT NULL, request TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(novel_id,number)
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS chapter_progress(
                    novel_id TEXT NOT NULL, number INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    stage TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(novel_id,number,content_hash)
                );
                CREATE TABLE IF NOT EXISTS chapter_steps(
                    novel_id TEXT NOT NULL, number INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    step TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(novel_id,number,content_hash,step)
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def policy(self, novel_id):
        with self.connection() as conn:
            return self._policy(conn, novel_id)

    @staticmethod
    def _policy(conn, novel_id):
        row = conn.execute("SELECT body FROM policies WHERE novel_id=?", (novel_id,)).fetchone()
        return json.loads(row[0]) if row else {"max_calls": None, "max_tokens": None, "max_cost": None, "prices": []}

    def save_policy(self, novel_id, body):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO policies VALUES (?,?)", (novel_id, json.dumps(body, allow_nan=False)))
            conn.execute("DELETE FROM budget_blocks WHERE novel_id=?",(novel_id,))
            # Newly supplied prices can conservatively cover older unpriced calls.
            # Never reprice requests that already have their admission snapshot.
            for price in body["prices"]:
                rows = conn.execute("SELECT * FROM calls WHERE novel_id=? AND kind=? AND model=? AND reserved_cost IS NULL",
                                    (novel_id,price["kind"],price["model"])).fetchall()
                for row in rows:
                    reserve = row["reserved_tokens"] * max(price["input_per_million"],price["output_per_million"]) / 1_000_000
                    cost = None if row["input_tokens"] is None or row["output_tokens"] is None else (
                        row["input_tokens"]*price["input_per_million"] + row["output_tokens"]*price["output_per_million"])/1_000_000
                    conn.execute("UPDATE calls SET reserved_cost=?,cost=?,prices_json=? WHERE id=?",
                                 (reserve,cost,json.dumps(price),row["id"]))
        return body

    @staticmethod
    def _totals(conn, novel_id):
        row = conn.execute("""SELECT count(*) AS calls,
            coalesce(sum(coalesce(input_tokens,0)),0) AS input_tokens,
            coalesce(sum(coalesce(output_tokens,0)),0) AS output_tokens,
            coalesce(sum(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN reserved_tokens ELSE input_tokens+output_tokens END),0) AS charged_tokens,
            coalesce(sum(coalesce(cost,reserved_cost,0)),0) AS estimated_cost,
            coalesce(sum(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END),0) AS uncertain_calls,
            coalesce(sum(CASE WHEN reserved_cost IS NULL THEN 1 ELSE 0 END),0) AS unknown_cost_calls
            FROM calls WHERE novel_id=?""", (novel_id,)).fetchone()
        return dict(row)

    def reserve(self, novel_id, kind, model, input_bound, output_bound, operation=""):
        try:
            return self._reserve(novel_id,kind,model,input_bound,output_bound,operation)
        except BudgetExceeded as exc:
            with self.connection() as conn:
                conn.execute("INSERT OR REPLACE INTO budget_blocks VALUES (?,?,?)",(novel_id,str(exc),now()))
            raise

    def budget_block(self, novel_id):
        with self.connection() as conn:
            row = conn.execute("SELECT message,created_at FROM budget_blocks WHERE novel_id=?",(novel_id,)).fetchone()
            return dict(row) if row else None

    def _reserve(self, novel_id, kind, model, input_bound, output_bound, operation=""):
        request_id = uuid4().hex
        input_bound, output_bound = max(0, int(input_bound)), max(0, int(output_bound))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            policy = self._policy(conn, novel_id)
            totals = self._totals(conn, novel_id)
            price = next((p for p in policy["prices"] if p["kind"] == kind and p["model"] == model), None)
            cost = (input_bound * price["input_per_million"] + output_bound * price["output_per_million"]) / 1_000_000 if price else None
            if policy["max_cost"] is not None and (cost is None or totals["unknown_cost_calls"]):
                raise BudgetExceeded("已设置金额预算，但有模型未配置单价或历史请求费用未知；请补充单价，或改用请求数/Token 上限")
            checks = [(policy["max_calls"], totals["calls"] + 1, "请求次数"),
                      (policy["max_tokens"], totals["charged_tokens"] + input_bound + output_bound, "Token"),
                      (policy["max_cost"], totals["estimated_cost"] + (cost or 0), "金额")]
            for limit, projected, label in checks:
                if limit is not None and projected > limit:
                    raise BudgetExceeded(f"本次请求将超过作品{label}预算，已在发送前停止；请在任务与用量中调整预算")
            stamp = now()
            conn.execute("INSERT INTO calls(id,novel_id,kind,model,status,created_at,updated_at,reserved_tokens,reserved_cost,operation,prices_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (request_id, novel_id, kind, model, "running", stamp, stamp, input_bound+output_bound, cost, operation, json.dumps(price) if price else None))
        return request_id

    def finish(self, request_id, input_tokens=None, output_tokens=None, status="completed", error=""):
        def token(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        input_tokens, output_tokens = token(input_tokens), token(output_tokens)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM calls WHERE id=?", (request_id,)).fetchone()
            if not row or row["status"] != "running":
                return
            snapshot = json.loads(row["prices_json"]) if row["prices_json"] else None
            cost = None
            if snapshot and input_tokens is not None and output_tokens is not None:
                cost = (input_tokens*snapshot["input_per_million"] + output_tokens*snapshot["output_per_million"])/1_000_000
            conn.execute("UPDATE calls SET status=?,updated_at=?,input_tokens=?,output_tokens=?,cost=?,error=? WHERE id=?",
                         (status, now(), input_tokens, output_tokens, cost, error, request_id))

    def summary(self, novel_id):
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM calls WHERE novel_id=? ORDER BY created_at DESC LIMIT 100", (novel_id,))]
            for row in rows:
                row.pop("error", None)  # Internal snapshots are never product copy.
                row.pop("prices_json", None)
            block=conn.execute("SELECT message,created_at FROM budget_blocks WHERE novel_id=?",(novel_id,)).fetchone()
            return {"policy": self._policy(conn, novel_id), "totals": self._totals(conn, novel_id), "calls": rows,
                    "budget_block":dict(block) if block else None}

    def save_job(self, job, request=None):
        body = {k:v for k,v in job.items() if k != "event"}
        with self.connection() as conn:
            if request is not None:
                conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (job["id"], job["novel_id"], job["number"], job["status"], json.dumps(body, ensure_ascii=False), json.dumps(request, ensure_ascii=False)))
            else:
                conn.execute("UPDATE jobs SET status=?,body=? WHERE id=?", (job["status"], json.dumps(body, ensure_ascii=False), job["id"]))

    def jobs(self, novel_id=None):
        with self.connection() as conn:
            query = "SELECT body FROM jobs" + (" WHERE novel_id=?" if novel_id else "") + " ORDER BY rowid DESC"
            return [json.loads(row[0]) for row in conn.execute(query, (novel_id,) if novel_id else ())]

    def job_request(self, novel_id, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT request FROM jobs WHERE novel_id=? AND id=?", (novel_id,job_id)).fetchone()
            if row is None:
                raise ValueError("未找到该作品中的任务")
            return json.loads(row[0])

    def checkpoint(self, novel_id, number, content_hash, stage, status="running", error=""):
        with self.connection() as conn:
            conn.execute("INSERT INTO chapter_progress VALUES (?,?,?,?,?,?,?) ON CONFLICT(novel_id,number,content_hash) DO UPDATE SET stage=excluded.stage,status=excluded.status,error=excluded.error,updated_at=excluded.updated_at",
                         (novel_id,number,content_hash,stage,status,error,now()))

    def chapter_checkpoint(self, novel_id, number, content_hash):
        with self.connection() as conn:
            row=conn.execute("SELECT * FROM chapter_progress WHERE novel_id=? AND number=? AND content_hash=?",(novel_id,number,content_hash)).fetchone()
            return dict(row) if row else None

    def chapter_progress(self, novel_id, pending_only=False):
        with self.connection() as conn:
            suffix = " AND p.status<>'completed' ORDER BY p.number,p.updated_at DESC" if pending_only else " ORDER BY p.updated_at DESC LIMIT 100"
            query = """SELECT p.*, EXISTS(SELECT 1 FROM chapter_steps s WHERE s.novel_id=p.novel_id
                AND s.number=p.number AND s.content_hash=p.content_hash AND s.step='generated_draft') AS draft_available
                FROM chapter_progress p WHERE p.novel_id=?"""
            return [dict(row) for row in conn.execute(query+suffix,(novel_id,))]

    def chapter_step(self, novel_id, number, content_hash, step):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM chapter_steps WHERE novel_id=? AND number=? AND content_hash=? AND step=?",
                               (novel_id,number,content_hash,step)).fetchone()
            return json.loads(row[0]) if row else None

    def save_chapter_step(self, novel_id, number, content_hash, step, payload):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO chapter_steps VALUES (?,?,?,?,?,?)",
                         (novel_id,number,content_hash,step,json.dumps(payload,ensure_ascii=False,allow_nan=False),now()))


@lru_cache(maxsize=16)
def journal_at(path):
    return OperationJournal(path)


def get_operation_journal():
    from infrastructure.persistence.database.connection import get_database
    return journal_at(str(Path(get_database().db_path).with_suffix(".operations.sqlite3")))
