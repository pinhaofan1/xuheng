"""Integrated daily admission and append-only reconciliation using real SQLite."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from infrastructure.persistence.operation_journal import OperationJournal, BudgetExceeded
from infrastructure.persistence.usage_ledger import ReconciliationConflict, day_window
from tests.unit.application.audit.test_operations import api, journal, policy, price


def correction(**overrides):
    return {"input_tokens": 12, "output_tokens": 4, "billed_cost": None,
            "expected_revision": 0, "request_key": "first", "note": "核对服务商明细", "reference": "invoice-1",
            "request_finished": True, **overrides}


def freeze(monkeypatch, value="2026-11-05T15:59:59.000+00:00"):
    clock = [value]
    monkeypatch.setattr("infrastructure.persistence.operation_journal.now", lambda: clock[0])
    return clock


def test_local_midnight_resets_only_daily_limit_and_keeps_original_day_settlement(journal, monkeypatch):
    clock = freeze(monkeypatch)
    journal.save_policy("book", policy(daily_max_calls=1, prices=[price()]))
    first = journal.reserve("book", "llm", "fixture", 100, 50)
    with pytest.raises(BudgetExceeded):
        journal.reserve("book", "llm", "fixture", 1, 1)
    assert journal.budget_block("book")["expires_at"] == "2026-11-05T16:00:00.000+00:00"
    clock[0] = "2026-11-05T16:00:00.000+00:00"
    assert journal.budget_block("book") is None
    journal.finish(first, 12, 4)
    assert journal.summary("book")["daily"]["totals"]["calls"] == 0
    journal.reserve("book", "llm", "fixture", 1, 1)
    assert journal.summary("book")["daily"]["date"] == "2026-11-06"
    old_day = journal.summary("book", "2026-11-05")["daily"]
    assert old_day["totals"]["charged_tokens"] == 16
    assert old_day["models"][0]["estimated_cost"] == pytest.approx(.00004)
    assert journal.summary("book")["totals"]["calls"] == 2


@pytest.mark.parametrize(("day", "hours"), [("2026-03-08", 23), ("2026-11-01", 25)])
def test_dst_day_uses_local_midnights(day, hours):
    from datetime import datetime
    window = day_window("America/New_York", "2026-01-01T00:00:00+00:00", day)
    assert (datetime.fromisoformat(window["end"])-datetime.fromisoformat(window["start"])).total_seconds() == hours*3600


def test_timezone_change_rebuckets_same_calls_without_resetting_cumulative_usage(journal, monkeypatch):
    freeze(monkeypatch, "2026-11-05T16:00:00.000+00:00")
    journal.reserve("book", "llm", "fixture", 1, 1)
    assert journal.summary("book")["daily"]["date"] == "2026-11-06"
    journal.save_policy("book", policy(timezone="UTC"))
    assert journal.summary("book")["daily"]["date"] == "2026-11-05"
    assert journal.summary("book")["totals"]["calls"] == 1


def test_cumulative_limit_still_applies_after_day_rollover(journal, monkeypatch):
    clock = freeze(monkeypatch)
    journal.save_policy("book", policy(max_calls=1, daily_max_calls=5))
    journal.reserve("book", "llm", "fixture", 1, 1)
    clock[0] = "2026-11-05T16:00:00.000+00:00"
    with pytest.raises(BudgetExceeded, match="作品请求次数"):
        journal.reserve("book", "llm", "fixture", 1, 1)
    assert journal.budget_block("book")["expires_at"] is None


def test_concurrent_daily_admission_is_atomic(journal, monkeypatch):
    freeze(monkeypatch)
    journal.save_policy("book", policy(daily_max_calls=4))
    def reserve(_):
        try:
            return journal.reserve("book", "llm", "fixture", 10, 10)
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(reserve, range(20)))
    assert sum(bool(id) for id in ids) == 4
    assert journal.summary("book")["daily"]["totals"]["calls"] == 4
    assert journal.reserve("other", "llm", "fixture", 10, 10)


@pytest.mark.parametrize("limit", ["daily_max_tokens", "daily_max_cost"])
def test_unknown_usage_is_reserved_until_reconciliation_then_budget_rechecks(journal, monkeypatch, limit):
    freeze(monkeypatch)
    journal.save_policy("book", policy(prices=[price()], **{limit: 150 if limit.endswith("tokens") else .0005}))
    first = journal.reserve("book", "llm", "fixture", 100, 50)
    journal.finish(first, status="failed")
    with pytest.raises(BudgetExceeded):
        journal.reserve("book", "llm", "fixture", 100, 50)
    journal.reconcile("book", first, correction(), "2026-11-05T15:59:59.100+00:00")
    assert journal.budget_block("book") is None
    assert journal.reserve("book", "llm", "fixture", 100, 20)


def test_daily_amount_requires_known_prices_only_for_that_day(journal, monkeypatch):
    clock = freeze(monkeypatch)
    journal.reserve("book", "llm", "unpriced", 10, 10)
    journal.save_policy("book", policy(daily_max_cost=1, prices=[price()]))
    with pytest.raises(BudgetExceeded, match="费用未知"):
        journal.reserve("book", "llm", "fixture", 1, 1)
    clock[0] = "2026-11-05T16:00:00.000+00:00"
    assert journal.reserve("book", "llm", "fixture", 1, 1)


def test_report_groups_services_and_models_and_uses_whole_history(journal, monkeypatch):
    freeze(monkeypatch)
    for kind, model in [("llm", "chat"), ("embedding", "embed"), ("rerank", "rank")]:
        journal.save_policy("book", policy(prices=[price(kind, model)]))
        id = journal.reserve("book", kind, model, 100, 50)
        journal.finish(id, 12, 4)
    report = journal.summary("book")["daily"]
    assert {(row["kind"], row["model"]) for row in report["models"]} == {("llm", "chat"), ("embedding", "embed"), ("rerank", "rank")}
    assert sum(row["charged_tokens"] for row in report["models"]) == report["totals"]["charged_tokens"] == 48


def test_adjustment_preserves_original_and_records_difference_and_idempotency(journal):
    journal.save_policy("book", policy(prices=[price()]))
    id = journal.reserve("book", "llm", "fixture", 100, 50)
    journal.finish(id, status="failed")
    with journal.connection() as conn:
        before = dict(conn.execute("SELECT * FROM calls WHERE id=?", (id,)).fetchone())
    result = journal.reconcile("book", id, correction(), "2026-11-05T00:00:00+00:00")
    assert result["difference"]["charged_tokens"] == -134
    assert result["difference"]["cost"] == pytest.approx(-.00036)
    assert journal.reconcile("book", id, correction(), "2026-11-05T01:00:00+00:00")["id"] == result["id"]
    with journal.connection() as conn:
        assert dict(conn.execute("SELECT * FROM calls WHERE id=?", (id,)).fetchone()) == before
    row = journal.summary("book")["calls"][0]
    assert row["status"] == "failed" and row["revision"] == 1
    assert row["input_tokens"] == 12 and row["original_input_tokens"] is None
    assert journal.summary("book")["totals"]["uncertain_calls"] == 0
    assert len(journal.reconciliation_history("book", id)) == 1
    with pytest.raises(ReconciliationConflict):
        journal.reconcile("book", id, correction(output_tokens=5), "now")


def test_zero_bill_is_known_even_without_configured_price_and_survives_reopen(journal):
    id = journal.reserve("book", "llm", "unpriced", 100, 100)
    journal.finish(id, status="failed")
    journal.reconcile("book", id, correction(input_tokens=0, output_tokens=0, billed_cost=0), "2026-11-05T00:00:00+00:00")
    reopened = OperationJournal(journal.path)
    totals = reopened.summary("book")["totals"]
    assert totals["charged_tokens"] == totals["estimated_cost"] == totals["uncertain_calls"] == totals["unknown_cost_calls"] == 0
    assert reopened.reconciliation_history("book", id)[0]["after"]["cost_source"] == "provider"


def test_amount_can_be_confirmed_without_price_and_missing_amount_remains_unknown(journal):
    first = journal.reserve("book", "llm", "unpriced", 100, 100)
    second = journal.reserve("book", "llm", "unpriced", 100, 100)
    journal.reconcile("book", first, correction(billed_cost=.012), "2026-11-05T00:00:00+00:00")
    journal.reconcile("book", second, correction(request_key="second"), "2026-11-05T00:00:00+00:00")
    assert journal.summary("book")["totals"]["unknown_cost_calls"] == 1
    assert journal.summary("book")["totals"]["estimated_cost"] == pytest.approx(.012)
    journal.save_policy("book", policy(prices=[price(model="unpriced")]))
    assert journal.summary("book")["totals"]["unknown_cost_calls"] == 0
    assert journal.summary("book")["totals"]["estimated_cost"] == pytest.approx(.01204)
    assert journal.reconciliation_history("book", second)[0]["after"]["cost"] is None


def test_multiple_revisions_keep_snapshot_price_and_late_provider_response(journal):
    journal.save_policy("book", policy(prices=[price()]))
    id = journal.reserve("book", "llm", "fixture", 100, 100)
    journal.save_policy("book", policy(prices=[price(input=200, output=400)]))
    first = journal.reconcile("book", id, correction(), "2026-11-05T00:00:00+00:00")
    assert first["after"]["cost"] == pytest.approx(.00004)
    journal.reconcile("book", id, correction(input_tokens=20, expected_revision=1, request_key="second", note="更正账单记录"), "2026-11-05T01:00:00+00:00")
    journal.finish(id, 18, 4)
    effective = journal.summary("book")["calls"][0]
    assert effective["input_tokens"] == 20 and effective["original_input_tokens"] == 18
    history = journal.reconciliation_history("book", id)
    assert [row["revision"] for row in history] == [2, 1]
    assert history[0]["before"]["input_tokens"] == 12
    assert journal.reconcile("book", id, correction(), "now")["id"] == first["id"]
    assert journal.summary("book")["calls"][0]["revision"] == 2


def test_concurrent_corrections_cannot_overwrite_each_other(journal):
    id = journal.reserve("book", "llm", "fixture", 100, 100)
    def adjust(index):
        try:
            return journal.reconcile("book", id, correction(request_key=f"key{index}"), "2026-11-05T00:00:00+00:00")
        except ReconciliationConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(adjust, range(2)))
    assert sum(bool(row) for row in results) == 1
    assert len(journal.reconciliation_history("book", id)) == 1


def test_old_unknown_records_are_reachable_with_pagination_and_filter(journal):
    first = journal.reserve("book", "llm", "unknown", 1, 1)
    with journal.connection() as conn:
        conn.execute("UPDATE calls SET created_at='2020-01-01T00:00:00.000+00:00' WHERE id=?", (first,))
    for _ in range(101):
        id = journal.reserve("book", "llm", "known", 1, 1)
        journal.finish(id, 1, 1)
    assert len(journal.summary("book")["calls"]) == 100
    older = journal.summary("book", offset=100)
    assert first in {row["id"] for row in older["calls"]}
    journal.save_policy("book", policy(prices=[price(model="known")]))
    unknown = journal.summary("book", uncertain_only=True)
    assert unknown["calls_page"]["total"] == 1 and unknown["calls"][0]["id"] == first
    assert unknown["totals"]["calls"] == 102


def test_legacy_journal_schema_and_policy_migrate_without_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE budget_blocks(novel_id TEXT PRIMARY KEY,message TEXT,created_at TEXT)")
    conn.execute("INSERT INTO budget_blocks VALUES ('book','existing block','2026-11-05T00:00:00+00:00')")
    conn.execute("CREATE TABLE policies(novel_id TEXT PRIMARY KEY,body TEXT)")
    conn.execute("INSERT INTO policies VALUES ('book',?)", (json.dumps(policy(max_calls=4)),))
    conn.commit(); conn.close()
    journal = OperationJournal(path)
    assert journal.policy("book")["max_calls"] == 4
    assert journal.policy("book")["timezone"] == "Asia/Shanghai"
    assert journal.budget_block("book")["message"] == "existing block"
    assert journal.reserve("book", "llm", "fixture", 1, 1)


def test_reconciliation_api_scopes_history_rejects_stale_revision_and_keeps_daily_totals(api, journal, monkeypatch):
    client, _, _ = api
    freeze(monkeypatch)
    id = journal.reserve("book", "llm", "fixture", 100, 100)
    journal.finish(id, status="failed")
    url = f"/evidence-reviews/book/operations/calls/{id}/reconciliations"
    assert client.post(url, json=correction()).status_code == 200
    assert client.post(url, json=correction()).status_code == 200
    assert client.post(url, json=correction(request_key="next")).status_code == 409
    assert client.get(url).json()[0]["note"] == "核对服务商明细"
    assert client.get(url.replace("/book/", "/other/")).status_code == 404
    assert client.post(url.replace("/book/", "/other/"), json=correction()).status_code == 404
    report = client.get("/evidence-reviews/book/operations?day=2026-11-05").json()
    assert report["daily"]["totals"]["charged_tokens"] == report["totals"]["charged_tokens"] == 16
    assert client.get("/evidence-reviews/book/operations?day=2026-02-30").status_code == 422
    assert client.get("/evidence-reviews/book/operations?day=9999-12-31").status_code == 422


@pytest.mark.parametrize("patch", [{"input_tokens": -1}, {"input_tokens": True}, {"output_tokens": 1.5},
    {"billed_cost": -1}, {"note": "  "}, {"request_finished": False}, {"request_finished": 1}, {"request_key": "x/y"}])
def test_invalid_correction_cannot_release_budget(api, journal, patch):
    client, _, _ = api
    id = journal.reserve("book", "llm", "fixture", 100, 100)
    journal.finish(id, status="failed")
    response = client.post(f"/evidence-reviews/book/operations/calls/{id}/reconciliations", json=correction(**patch))
    assert response.status_code == 422
    assert journal.summary("book")["totals"]["charged_tokens"] == 200


def test_running_book_must_stop_before_reconciling_inflight_record(api, journal):
    client, _, store = api
    id = journal.reserve("book", "llm", "fixture", 100, 100)
    store.db.execute("UPDATE novels SET autopilot_status='running' WHERE id='book'")
    store.db.commit()
    response = client.post(f"/evidence-reviews/book/operations/calls/{id}/reconciliations", json=correction())
    assert response.status_code == 409
    store.db.execute("UPDATE novels SET autopilot_status='stopped' WHERE id='book'")
    store.db.commit()
    assert client.post(f"/evidence-reviews/book/operations/calls/{id}/reconciliations", json=correction()).status_code == 200


@pytest.mark.parametrize("patch", [{"timezone": "Unknown/Moon"}, {"daily_max_calls": -1},
    {"daily_max_tokens": True}, {"daily_max_cost": -1}])
def test_invalid_daily_policy_is_rejected_without_changing_policy(api, journal, patch):
    client, _, _ = api
    before = journal.policy("book")
    assert client.put("/evidence-reviews/book/operations/policy", json=policy(**patch)).status_code == 422
    assert journal.policy("book") == before


def test_journal_migration_is_safe_for_simultaneous_openers(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    with ThreadPoolExecutor(max_workers=4) as pool:
        journals = list(pool.map(lambda _: OperationJournal(path), range(4)))
    for instance in journals:
        assert instance.summary("book")["totals"]["calls"] == 0


def test_default_policy_prices_are_not_shared_between_books(journal):
    journal.policy("book")["prices"].append(price())
    assert journal.policy("other")["prices"] == []


def test_acknowledged_request_can_be_retried_after_another_task_starts(api, journal):
    client, _, store = api
    id = journal.reserve("book", "llm", "fixture", 100, 100)
    url = f"/evidence-reviews/book/operations/calls/{id}/reconciliations"
    first = client.post(url, json=correction())
    assert first.status_code == 200
    store.db.execute("UPDATE novels SET autopilot_status='running' WHERE id='book'")
    store.db.commit()
    repeated = client.post(url, json=correction())
    assert repeated.status_code == 200 and repeated.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_model_daily_reservation_then_invoice_reconciliation_then_next_request(journal, monkeypatch):
    from tests.unit.infrastructure.ai.test_stream_usage_accounting import setup_client, SSE, frame, tail, consume
    freeze(monkeypatch)
    journal.save_policy("book", policy(daily_max_tokens=700))
    provider, service, requests, _ = await setup_client(monkeypatch, [
        SSE([frame("first"), frame(finish="stop")]),
        SSE([frame("second"), frame(finish="stop"), tail()])])
    try:
        assert await consume(service, journal) == "first"
        first = journal.summary("book")["calls"][0]
        with pytest.raises(BudgetExceeded):
            await consume(service, journal)
        assert len(requests) == 1
        journal.reconcile("book", first["id"], correction(), "2026-11-05T15:59:59.100+00:00")
        assert await consume(service, journal) == "second"
        report = journal.summary("book")
        assert len(requests) == 2 and report["daily"]["totals"]["charged_tokens"] == 32
        assert report["totals"]["calls"] == 2 and report["totals"]["uncertain_calls"] == 0
    finally:
        await provider.async_client.close()
