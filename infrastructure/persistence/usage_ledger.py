"""Daily accounting and append-only billing corrections for the operation journal."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_POLICY = {"max_calls": None, "max_tokens": None, "max_cost": None, "prices": [],
                  "daily_max_calls": None, "daily_max_tokens": None, "daily_max_cost": None,
                  "timezone": "Asia/Shanghai"}


class ReconciliationConflict(ValueError):
    pass


def validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("请选择有效的 IANA 时区，例如 Asia/Shanghai 或 UTC") from exc
    return value


def day_window(zone, stamp, day=None):
    tz = ZoneInfo(validate_timezone(zone))
    try:
        selected = date.fromisoformat(day) if day else datetime.fromisoformat(stamp).astimezone(tz).date()
        start = datetime.combine(selected, time.min, tzinfo=tz)
        end = datetime.combine(selected + timedelta(days=1), time.min, tzinfo=tz)
    except (ValueError, OverflowError) as exc:
        raise ValueError("日期无效，请选择有效的账目日期") from exc
    return {"date": selected.isoformat(), "timezone": zone,
            "start": start.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            "end": end.astimezone(timezone.utc).isoformat(timespec="milliseconds")}


def create_ledger_schema(conn):
    # Existing journals migrate additively; manuscripts and original calls stay intact.
    conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(budget_blocks)")}
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE budget_blocks ADD COLUMN expires_at TEXT")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS call_adjustments(
            id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, call_id TEXT NOT NULL,
            revision INTEGER NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
            before_json TEXT NOT NULL, after_json TEXT NOT NULL,
            note TEXT NOT NULL, reference TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(call_id,revision), UNIQUE(novel_id,request_key)
        );
        CREATE INDEX IF NOT EXISTS adjustments_book ON call_adjustments(novel_id,call_id,revision);
        CREATE VIEW IF NOT EXISTS usage_calls AS SELECT c.id,c.novel_id,c.kind,c.model,c.status,
            c.created_at,c.updated_at,c.reserved_tokens,c.reserved_cost,c.prices_json,c.operation,
            CASE WHEN a.id IS NULL THEN c.input_tokens ELSE json_extract(a.after_json,'$.input_tokens') END AS input_tokens,
            CASE WHEN a.id IS NULL THEN c.output_tokens ELSE json_extract(a.after_json,'$.output_tokens') END AS output_tokens,
            CASE WHEN a.id IS NULL THEN c.cost ELSE coalesce(json_extract(a.after_json,'$.cost'),
                (json_extract(a.after_json,'$.input_tokens')*json_extract(c.prices_json,'$.input_per_million')+
                 json_extract(a.after_json,'$.output_tokens')*json_extract(c.prices_json,'$.output_per_million'))/1000000.0) END AS cost,
            c.input_tokens AS original_input_tokens,c.output_tokens AS original_output_tokens,c.cost AS original_cost,
            coalesce(a.revision,0) AS revision,a.created_at AS reconciled_at
        FROM calls c LEFT JOIN call_adjustments a ON a.call_id=c.id
            AND a.revision=(SELECT max(revision) FROM call_adjustments WHERE call_id=c.id);
    """)


TOTALS_SQL = """count(*) AS calls,
    coalesce(sum(coalesce(input_tokens,0)),0) AS input_tokens,
    coalesce(sum(coalesce(output_tokens,0)),0) AS output_tokens,
    coalesce(sum(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN reserved_tokens ELSE input_tokens+output_tokens END),0) AS charged_tokens,
    coalesce(sum(coalesce(cost,reserved_cost,0)),0) AS estimated_cost,
    coalesce(sum(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END),0) AS uncertain_calls,
    coalesce(sum(CASE WHEN cost IS NULL AND reserved_cost IS NULL THEN 1 ELSE 0 END),0) AS unknown_cost_calls,
    coalesce(sum(CASE WHEN revision>0 THEN 1 ELSE 0 END),0) AS reconciled_calls"""


class UsageLedger:
    @staticmethod
    def _totals(conn, novel_id, window=None):
        query = "SELECT " + TOTALS_SQL + " FROM usage_calls WHERE novel_id=?"
        params = [novel_id]
        if window:
            query += " AND created_at>=? AND created_at<?"
            params.extend((window["start"], window["end"]))
        return dict(conn.execute(query, params).fetchone())

    def _daily_report(self, conn, novel_id, policy, stamp, day=None):
        window = day_window(policy["timezone"], stamp, day)
        groups = [dict(row) for row in conn.execute("SELECT kind,model," + TOTALS_SQL + """
            FROM usage_calls WHERE novel_id=? AND created_at>=? AND created_at<?
            GROUP BY kind,model ORDER BY kind,model""", (novel_id, window["start"], window["end"]))]
        return {**window, "totals": self._totals(conn, novel_id, window), "models": groups}

    def reconcile(self, novel_id, call_id, body, stamp):
        # Validate below the API too: this ledger is also used by local scripts.
        for key in ("input_tokens", "output_tokens", "expected_revision"):
            if type(body.get(key)) is not int or not 0 <= body[key] <= 1_000_000_000_000:
                raise ValueError("用量和版本必须是非负整数")
        cost = body.get("billed_cost")
        if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost <= 1_000_000):
            raise ValueError("账单金额必须是有效的非负数")
        note = str(body.get("note") or "").strip()
        reference = str(body.get("reference") or "").strip()
        request_key = str(body.get("request_key") or "")
        if not 1 <= len(note) <= 1000 or len(reference) > 240 or not 1 <= len(request_key) <= 80:
            raise ValueError("请填写核对依据，内容或请求标识超出长度限制")
        if body.get("request_finished") is not True:
            raise ValueError("请先确认已在服务商核实此请求结束")
        fingerprint = hashlib.sha256(json.dumps({"call_id": call_id, **body}, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM usage_calls WHERE novel_id=? AND id=?", (novel_id, call_id)).fetchone()
            if current is None:
                raise KeyError("未找到该作品的请求记录")
            repeated = conn.execute("SELECT * FROM call_adjustments WHERE novel_id=? AND request_key=?", (novel_id, request_key)).fetchone()
            if repeated:
                if repeated["fingerprint"] != fingerprint:
                    raise ReconciliationConflict("此提交标识已用于其他核对，请重新打开核对窗口")
                return self._adjustment(repeated)
            if current["revision"] != body["expected_revision"]:
                raise ReconciliationConflict("此请求已被其他操作核对，请刷新后重新确认")
            price = json.loads(current["prices_json"]) if current["prices_json"] else None
            if cost is None and price:
                cost = (body["input_tokens"] * price["input_per_million"] + body["output_tokens"] * price["output_per_million"]) / 1_000_000
            before = {key: current[key] for key in ("input_tokens", "output_tokens", "cost", "reserved_tokens", "reserved_cost")}
            after = {"input_tokens": body["input_tokens"], "output_tokens": body["output_tokens"],
                     "cost": cost, "cost_source": "provider" if body.get("billed_cost") is not None else "price" if price else "unknown"}
            values = (uuid4().hex, novel_id, call_id, current["revision"]+1, request_key, fingerprint,
                      json.dumps(before, allow_nan=False), json.dumps(after, allow_nan=False), note, reference, stamp)
            conn.execute("INSERT INTO call_adjustments VALUES (?,?,?,?,?,?,?,?,?,?,?)", values)
            conn.execute("DELETE FROM budget_blocks WHERE novel_id=?", (novel_id,))
            return self._adjustment(conn.execute("SELECT * FROM call_adjustments WHERE id=?", (values[0],)).fetchone())

    @staticmethod
    def _adjustment(row):
        result = dict(row)
        result.pop("fingerprint", None)
        result["before"] = json.loads(result.pop("before_json"))
        result["after"] = json.loads(result.pop("after_json"))
        old, new = result["before"], result["after"]
        old_tokens = old["reserved_tokens"] if old["input_tokens"] is None or old["output_tokens"] is None else old["input_tokens"] + old["output_tokens"]
        old_cost = old["cost"] if old["cost"] is not None else old["reserved_cost"]
        result["difference"] = {"charged_tokens": new["input_tokens"]+new["output_tokens"]-old_tokens,
                                "cost": new["cost"]-old_cost if new["cost"] is not None and old_cost is not None else None}
        return result

    def reconciliation_history(self, novel_id, call_id):
        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM calls WHERE novel_id=? AND id=?", (novel_id, call_id)).fetchone():
                raise KeyError("未找到该作品的请求记录")
            return [self._adjustment(row) for row in conn.execute(
                "SELECT * FROM call_adjustments WHERE novel_id=? AND call_id=? ORDER BY revision DESC", (novel_id, call_id))]
