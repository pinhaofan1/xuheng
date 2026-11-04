"""Behavioral tests of attribution, exact evidence and persisted review workflow."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from application.audit.evidence_models import CharacterRule, RuleSet, text_hash
from application.audit.services.evidence_review_service import (
    EvidenceReviewService, ReviewConflict, ReviewNotFound, ReviewUnavailable, review_text,
)
from domain.novel.services.action_attribution import attribute_actions
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore
from interfaces.api.v1.audit.evidence_review_routes import get_evidence_review_service, router


def rules():
    return RuleSet(profiles=[
        CharacterRule(id="lin", name="林烬", aliases=["阿烬"], allowed_magic=["火系"]),
        CharacterRule(id="shen", name="沈澜", allowed_magic=["水系"], forbidden_actions=["use_gun"]),
    ])


@pytest.mark.parametrize("text, expected", [
    ("林烬释放火球，沈澜施展水系法术。", []),
    ("林烬施展水系法术，沈澜释放火球。", ["lin", "shen"]),
    ("阿烬施展水系法术。", ["lin"]),
    ("沈澜开枪射击。", ["shen"]),
    ("沈澜拔剑，随后挥剑。", []),  # A usual weapon is not an exclusive restriction.
    ("沈澜没有释放火球。", []),
    ("沈澜未曾施展火系法术。", []),
    ("林烬看着火球。", []),
    ("林烬释放火球，沈澜站在远处。", []),
    ("林烬离开了。释放水系法术。", []),
    ("林烬释放火球，然后施展水系法术。", ["lin"]),
    ("林烬施展水系法术。林烬施展水系法术。", ["lin", "lin"]),
])
def test_explicit_subjects_and_non_actions(text, expected):
    issues, _ = review_text(text, rules())
    assert [i["entity_id"] for i in issues] == expected
    for issue in issues:
        assert text[issue["start"]:issue["end"]] == issue["quote"]
        assert text_hash(issue["source_quote"]) == issue["source_hash"]


@pytest.mark.parametrize("text", [
    "林烬和沈澜释放火球。", "林烬对沈澜释放火球。",
    "如果沈澜释放火球，就能脱身。", "据说沈澜释放火球。",
    "沈澜说：“我释放火球。”", "沈澜被要求释放火球。",
    "林烬站在岸边，他释放水系法术。", "他释放火球。",
    "林烬站在岸边，陌生人释放水球。",
    "林烬看到沈澜被要求释放火球。",
])
def test_uncertain_language_is_not_asserted_as_a_conflict(text):
    issues, uncertain = review_text(text, rules())
    assert issues == []
    assert uncertain
    assert all(i["description"] for i in uncertain)


def test_offsets_preserve_emoji_and_distinct_repeated_occurrences():
    text = "🌊沈澜释放火球。\n沈澜释放火球。"
    issues, _ = review_text(text, rules())
    assert issues[0]["start"] == 1
    assert len({i["id"] for i in issues}) == 2
    assert all(text[i["start"]:i["end"]] == "沈澜释放火球" for i in issues)


def test_longest_alias_does_not_match_the_shorter_character_first():
    mentions = attribute_actions("林烬释放火球。", {"林": "other", "林烬": "lin"})
    assert [m.entity_id for m in mentions] == ["lin"]


def test_ambiguous_aliases_and_unknown_rule_codes_are_rejected():
    with pytest.raises(ValidationError):
        RuleSet(profiles=[CharacterRule(id="a", name="林烬"), CharacterRule(id="b", name="沈澜", aliases=["林烬"])])
    with pytest.raises(ValidationError):
        CharacterRule(id="a", name="林烬", forbidden_actions=["unknown"])


@pytest.fixture
def workflow(tmp_path):
    db = DatabaseConnection(str(tmp_path / "review.db"))
    for novel in ("book", "other"):
        db.execute("INSERT INTO novels(id, title, slug) VALUES (?, ?, ?)", (novel, novel, novel))
        db.execute("INSERT INTO chapters(id, novel_id, number, title, content) VALUES (?, ?, 1, '测试', ?)",
                   (f"chapter-{novel}", novel, "🌊沈澜释放火球。"))
    db.commit()
    store = EvidenceReviewStore(db, write_timeout=0.04)
    service = EvidenceReviewService(store)
    service.save_rules("book", rules())
    yield db, store, service
    db.close_all()


def test_review_feedback_undo_and_idempotency_survive_reload(workflow):
    db, store, service = workflow
    run = service.review("book", 1)
    assert not run["stale"]
    assert len(run["issues"]) == 1
    issue = run["issues"][0]
    updated = service.feedback("book", run["id"], issue["id"], "ignored", 0)
    assert updated["issues"][0]["status"] == "ignored"
    other_service = EvidenceReviewService(EvidenceReviewStore(db))
    cached = other_service.review("book", 1)
    assert cached["id"] == run["id"]
    assert cached["issues"][0]["status"] == "ignored"
    undone = other_service.feedback("book", run["id"], issue["id"], "pending", 1)
    assert undone["issues"][0]["feedback_version"] == 2
    assert undone["issues"][0]["status"] == "pending"
    assert db.fetch_one("SELECT count(*) AS n FROM xh_feedback_events")["n"] == 2


def test_feedback_optimistic_lock_does_not_overwrite_a_newer_decision(workflow):
    _, _, service = workflow
    run = service.review("book", 1)
    issue = run["issues"][0]
    service.feedback("book", run["id"], issue["id"], "confirmed", 0)
    with pytest.raises(ReviewConflict):
        service.feedback("book", run["id"], issue["id"], "ignored", 0)
    assert service.get_run("book", run["id"])["issues"][0]["status"] == "confirmed"


def test_restoring_earlier_text_reuses_feedback_and_is_latest(workflow):
    db, _, service = workflow
    original = service.review("book", 1)
    service.feedback("book", original["id"], original["issues"][0]["id"], "ignored", 0)
    db.execute("UPDATE chapters SET content = '沈澜释放雷电。' WHERE id = 'chapter-book'")
    db.commit()
    assert service.review("book", 1)["id"] != original["id"]
    db.execute("UPDATE chapters SET content = ? WHERE id = 'chapter-book'", (original["content_snapshot"],))
    db.commit()
    restored = service.latest("book", 1)
    assert restored["id"] == original["id"]
    assert not restored["stale"]
    assert restored["issues"][0]["status"] == "ignored"


@pytest.mark.parametrize("mutation", ["content", "rules", "renumber"])
def test_changed_inputs_expire_the_result_and_block_feedback(workflow, mutation):
    db, _, service = workflow
    run = service.review("book", 1)
    if mutation == "content":
        db.execute("UPDATE chapters SET content = '沈澜施展水系法术。' WHERE id = 'chapter-book'")
        db.commit()
    elif mutation == "renumber":
        db.execute("UPDATE chapters SET number = 2 WHERE id = 'chapter-book'")
        db.commit()
    else:
        current = service.get_rules("book")
        current.profiles[1].allowed_magic = ["水系", "火系"]
        service.save_rules("book", current)
    result = service.get_run("book", run["id"])
    assert result["stale"]
    assert result["content_snapshot"] == run["content_snapshot"]
    with pytest.raises(ReviewConflict):
        service.feedback("book", run["id"], run["issues"][0]["id"], "confirmed", 0)


def test_rule_edit_version_conflicts_preserve_saved_profiles(workflow):
    _, _, service = workflow
    with pytest.raises(ReviewConflict):
        service.save_rules("book", rules())  # Empty revision is outdated.
    assert len(service.get_rules("book").profiles) == 2


def test_no_cross_work_access_or_feedback(workflow):
    _, _, service = workflow
    run = service.review("book", 1)
    with pytest.raises(ReviewNotFound):
        service.get_run("other", run["id"])
    with pytest.raises(ReviewNotFound):
        service.feedback("other", run["id"], run["issues"][0]["id"], "confirmed", 0)


def test_unsaved_content_and_empty_configuration_are_rejected(workflow):
    _, _, service = workflow
    with pytest.raises(ReviewConflict):
        service.review("book", 1, expected_content="未保存的文字")
    with pytest.raises(ValueError, match="限制"):
        service.review("other", 1)


def test_migration_is_idempotent_and_rules_survive_reopening(workflow):
    db, _, _ = workflow
    second = DatabaseConnection(db.db_path)
    try:
        assert len(EvidenceReviewStore(second).get_rules("book").profiles) == 2
    finally:
        second.close_all()


def test_queue_enqueue_is_not_reported_as_persisted_success(workflow, monkeypatch):
    from contextlib import contextmanager
    from infrastructure.persistence.database.write_dispatch import TxnCollectingConnection
    db, store, service = workflow
    @contextmanager
    def collect_only():
        yield TxnCollectingConnection()
    monkeypatch.setattr(db, "transaction", collect_only)
    with pytest.raises(ReviewUnavailable):
        service.review("book", 1)
    assert store.latest_run("book", "chapter-book") is None


def test_single_writer_queue_commits_review_and_feedback(workflow, monkeypatch):
    """Exercise the actual queue/collector path, rather than pytest's bypass."""
    from application.engine.services import persistence_queue as queues
    db, _, service = workflow
    queue = queues.PersistenceQueue()
    queue.initialize()
    def persist(payload):
        with db.transaction() as tx:
            for operation in payload["operations"]:
                tx.execute(operation["sql"], tuple(operation["params"]))
    queue.register_handler("execute_sql_txn_batch", persist)
    monkeypatch.setattr(queues, "get_persistence_queue", lambda: queue)
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "0")
    queue.start_consumer()
    service.store.write_timeout = 3.0
    try:
        run = service.review("book", 1)
        updated = service.feedback("book", run["id"], run["issues"][0]["id"], "confirmed", 0)
        assert updated["issues"][0]["status"] == "confirmed"
        assert db.fetch_one("SELECT count(*) AS n FROM xh_feedback_events")["n"] == 1
    finally:
        queue.stop_consumer()
        queue.get_queue().close()
        queue.get_queue().join_thread()


def test_routes_validate_scope_and_return_conflicts(workflow):
    _, _, service = workflow
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_evidence_review_service] = lambda: service
    with TestClient(app) as client:
        root = "/api/v1/evidence-reviews/book"
        assert client.get(root + "/chapters/1/latest").json() is None
        assert client.post(root + "/chapters/0", json={}).status_code == 422
        assert client.post(root + "/chapters/1", json={"expected_content": "未保存"}).status_code == 409
        response = client.post(root + "/chapters/1", json={})
        assert response.status_code == 200
        run = response.json()
        assert client.get(f"/api/v1/evidence-reviews/other/runs/{run['id']}").status_code == 404
        url = f"{root}/runs/{run['id']}/issues/{run['issues'][0]['id']}"
        assert client.patch(url, json={"status": "invalid", "expected_version": 0}).status_code == 422
        assert client.patch(url, json={"status": "confirmed", "expected_version": 0}).status_code == 200
        assert client.patch(url, json={"status": "ignored", "expected_version": 0}).status_code == 409
