"""Full review contracts: isolated history, real SDK transport, untrusted quotes.

Model responses in these tests are fixtures, never represented as a real model
integration result. The SDK adapter is exercised against an in-memory transport.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI
import pytest

from application.audit.evidence_models import AIReviewRequest, CharacterRule, RuleSet, text_hash
from application.audit.services.ai_evidence_review import AIEvidenceReviewService, validate_model_review
from application.audit.services.evidence_review_service import EvidenceReviewService, ReviewConflict, ReviewUnavailable
from application.audit.services.history_retrieval import HistoricalRetriever, bounded_history, history_digest, split_chapter
from application.audit.services.review_jobs import ReviewJobManager
from infrastructure.ai.evidence_model_client import EvidenceModelClient, ModelReply, configured_model
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore


@pytest.fixture
def review_db(tmp_path):
    db = DatabaseConnection(str(tmp_path / "ai-review.db"))
    for novel in ("book", "other"):
        db.execute("INSERT INTO novels(id,title,slug) VALUES (?,?,?)", (novel, novel, novel))
    for chapter_id, novel, number, content in [
        ("past", "book", 1, "🌊沈澜的左臂已经断了，无法抬起。\n\n沈澜把枪扔进河里。"),
        ("current", "book", 2, "沈澜抬起左臂，指向远方。"),
        ("future", "book", 3, "沈澜在第三章治好了左臂。"),
        ("foreign", "other", 1, "沈澜的左臂从未受伤。"),
    ]:
        db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES (?,?,?,'test',?)", (chapter_id, novel, number, content))
    db.commit()
    store = EvidenceReviewStore(db)
    store.save_rules("book", RuleSet(profiles=[CharacterRule(id="shen", name="沈澜", allowed_magic=["水系"])]))
    yield db, store
    db.close_all()


@pytest.mark.asyncio
async def test_retrieval_excludes_current_future_and_foreign_and_preserves_offsets(review_db):
    db, store = review_db
    result = await HistoricalRetriever(store).retrieve("book", 2, "沈澜抬起左臂")
    assert result["sources"]
    assert {s["chapter_id"] for s in result["sources"]} == {"past"}
    body = db.fetch_one("SELECT content FROM chapters WHERE id='past'")["content"]
    assert all(body[s["start"]:s["end"]] == s["text"] for s in result["sources"])
    assert db.fetch_one("SELECT count(*) AS n FROM xh_history_chunks")["n"] == 2
    assert result["mode"] == "bm25"


@pytest.mark.asyncio
async def test_changed_history_replaces_the_index_and_changes_digest(review_db):
    db, store = review_db
    retriever = HistoricalRetriever(store)
    before = await retriever.retrieve("book", 2, "左臂")
    db.execute("UPDATE chapters SET content='沈澜的右臂受伤。' WHERE id='past'")
    db.commit()
    after = await retriever.retrieve("book", 2, "右臂")
    assert before["context_hash"] != after["context_hash"]
    rows = db.fetch_all("SELECT text FROM xh_history_chunks WHERE chapter_id='past'")
    assert [r["text"] for r in rows] == ["沈澜的右臂受伤。"]


def test_long_chunks_are_exact_and_have_overlap():
    content = "🌊" + "长" * 1500
    chunks = split_chapter({"id": "c", "novel_id": "b", "number": 1, "content": content, "content_hash": text_hash(content)})
    assert len(chunks) == 3
    assert chunks[1]["start"] == chunks[0]["end"] - 80
    assert all(content[c["start"]:c["end"]] == c["text"] for c in chunks)


def test_history_limits_are_explicit_and_digest_tracks_order():
    rows = [{"id": str(i), "number": i, "novel_id": "b", "content": "字" * 100_001} for i in range(110)]
    bounded = bounded_history(rows)
    assert sum(len(r["content"]) for r in bounded) == 500_000
    assert len(bounded) == 5
    assert history_digest(bounded) != history_digest(list(reversed(bounded)))


class StubEmbedding:
    def __init__(self): self.calls = 0
    async def embed_batch(self, texts):
        self.calls += 1
        return [[float("左臂" in t), float("枪" in t), 1.0] for t in texts]


@pytest.mark.asyncio
async def test_optional_semantic_reranking_uses_cache_and_stays_scoped(review_db):
    _, store = review_db
    embedding = StubEmbedding()
    retrieval = HistoricalRetriever(store, lambda: (embedding, "test-embedding"))
    first = await retrieval.retrieve("book", 2, "沈澜的左臂", semantic=True)
    second = await retrieval.retrieve("book", 2, "沈澜的左臂", semantic=True)
    assert first["mode"] == "bm25+semantic-rerank"
    assert embedding.calls == 1
    assert first["sources"] == second["sources"]
    assert {s["chapter_id"] for s in first["sources"]} == {"past"}


@pytest.mark.asyncio
async def test_missing_embedding_is_an_error_not_a_fake_semantic_success(review_db):
    _, store = review_db
    with pytest.raises(ReviewUnavailable, match="嵌入"):
        await HistoricalRetriever(store).retrieve("book", 2, "沈澜", semantic=True)


def source_and_finding():
    source = {"id": "H:1", "chapter_id": "past", "chapter_number": 1, "start": 1, "end": 16,
              "text": "沈澜的左臂已经断了，无法抬起。", "content_hash": "history-hash"}
    finding = {"entity_id": "shen", "issue_type": "state", "description": "左臂状态疑似不一致，需检查是否存在康复情节。",
               "target_quote": "沈澜抬起左臂", "source_id": "H:1", "source_quote": "左臂已经断了", "certainty": "conflict"}
    return source, finding


def test_verified_source_offsets_include_original_paragraph_offset():
    source, finding = source_and_finding()
    rules = RuleSet(profiles=[CharacterRule(id="shen", name="沈澜")])
    issues, uncertain, warnings, _ = validate_model_review("🌊沈澜抬起左臂。", rules, [source], json.dumps({"summary": "test", "issues": [finding]}))
    assert not uncertain and not warnings
    assert issues[0]["start"] == 1
    assert issues[0]["source_start"] == 4
    assert issues[0]["source_kind"] == "history"


@pytest.mark.parametrize("field,value", [
    ("target_quote", "沈澜抬起右臂"), ("source_quote", "左臂从未受伤"),
    ("source_id", "H:999"), ("entity_id", "foreign-person"),
])
def test_model_cannot_forge_quotes_sources_or_people(field, value):
    source, finding = source_and_finding()
    finding[field] = value
    issues, uncertain, warnings, _ = validate_model_review("沈澜抬起左臂。", RuleSet(profiles=[CharacterRule(id="shen", name="沈澜")]), [source], json.dumps({"issues": [finding]}))
    assert not issues and not uncertain
    assert len(warnings) == 1


def test_repeated_quotes_require_an_explicit_occurrence():
    source, finding = source_and_finding()
    rules = RuleSet(profiles=[CharacterRule(id="shen", name="沈澜")])
    text = "沈澜抬起左臂。沈澜抬起左臂。"
    issues, _, warnings, _ = validate_model_review(text, rules, [source], json.dumps({"issues": [finding]}))
    assert not issues and warnings
    finding["target_occurrence"] = 1
    issues, _, warnings, _ = validate_model_review(text, rules, [source], json.dumps({"issues": [finding]}))
    assert not warnings and issues[0]["start"] == 7


@pytest.mark.parametrize("output", ["not json", '{"issues": "done"}', '{"issues": [], "unknown": true}'])
def test_invalid_model_json_is_not_saved_as_no_conflicts(output):
    with pytest.raises(ReviewUnavailable):
        validate_model_review("沈澜抬起左臂。", RuleSet(), [], output)


class FixtureModel:
    fingerprint = "fixture-not-a-real-model"
    async def complete(self, system, user, output_tokens):
        payload = json.loads(user)
        assert "JSON" in system and output_tokens == 4096
        source = next(s for s in payload["history_sources"] if "左臂已经断了" in s["text"])
        _, finding = source_and_finding()
        finding["source_id"] = source["id"]
        return ModelReply(json.dumps({"summary": "Fixture response, not a real model", "issues": [finding]}), "fixture", 111, 42)


@pytest.mark.asyncio
async def test_ai_pipeline_persists_usage_sources_and_expires_on_history_change(review_db):
    db, store = review_db
    ai = AIEvidenceReviewService(store, HistoricalRetriever(store), FixtureModel())
    result = await ai.run("book", 2, AIReviewRequest())
    assert result["mode"] == "ai" and result["model_calls"] == 1
    assert result["input_tokens"] == 111 and result["output_tokens"] == 42
    assert not result["stale"] and len(result["issues"]) == 1
    service = EvidenceReviewService(store)
    assert service.latest("book", 2, "ai")["id"] == result["id"]
    service.feedback("book", result["id"], result["issues"][0]["id"], "confirmed", 0)
    db.execute("UPDATE chapters SET content='沈澜的左臂已经康复。' WHERE id='past'")
    db.commit()
    stale = service.get_run("book", result["id"])
    assert stale["stale"]
    assert "历史正文或章节顺序已变化" in stale["stale_reasons"]
    assert stale["sources"][0]["content_hash"] == result["sources"][0]["content_hash"]
    with pytest.raises(ReviewConflict):
        service.feedback("book", result["id"], result["issues"][0]["id"], "ignored", 1)


@pytest.mark.asyncio
async def test_cancelled_pipeline_does_not_call_model(review_db):
    _, store = review_db
    class NeverCall:
        async def complete(self, *args): raise AssertionError("cancelled request called the model")
    result = await AIEvidenceReviewService(store, HistoricalRetriever(store), NeverCall()).run("book", 2, AIReviewRequest(), cancelled=lambda: True)
    assert result == {"cancelled": True}


def profile(**overrides):
    return SimpleNamespace(id="p", api_key="test-secret-must-not-be-disclosed", model="configured-model", protocol="openai",
                           base_url="https://test.invalid/v1", timeout_seconds=20,
                           extra_headers={}, extra_query={}, **{"extra_body": {}, **overrides})


@pytest.mark.asyncio
async def test_openai_sdk_adapter_respects_output_cap_and_usage():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "r", "object": "chat.completion", "created": 1, "model": "configured-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"issues":[]}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29}})
    def factory(**kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AsyncOpenAI(**kwargs)
    client = EvidenceModelClient(profile(extra_body={"max_tokens": 120000, "model": "wrong"}), factory)
    reply = await client.complete("Return JSON", "test", 4096)
    assert requests[0]["max_tokens"] == 4096
    assert requests[0]["model"] == "configured-model"
    assert reply.input_tokens == 20 and reply.output_tokens == 9


@pytest.mark.asyncio
async def test_model_errors_do_not_leak_keys_or_auto_retry():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "secret in gateway response", "type": "rate_limit"}})
    def factory(**kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AsyncOpenAI(**kwargs)
    with pytest.raises(ReviewUnavailable) as failure:
        await EvidenceModelClient(profile(), factory).complete("JSON", "test")
    assert len(requests) == 1
    assert "test-secret" not in str(failure.value) and "gateway response" not in str(failure.value)


@pytest.mark.asyncio
async def test_connectivity_test_has_a_small_budget_and_never_uses_old_large_factory(monkeypatch):
    from application.ai.llm_control_service import LLMControlService, LLMProfile
    calls = []
    async def bounded(self, system, user, output_tokens):
        calls.append(output_tokens)
        return ModelReply('连接成功', self.model, 5, 3)
    monkeypatch.setattr(EvidenceModelClient, "complete", bounded)
    def forbidden_factory(_): raise AssertionError("Unbounded provider factory must not be used")
    result = await LLMControlService().test_profile_model(
        LLMProfile(id="p", name="DeepSeek", protocol="openai", api_key="fixture", model="fixture", base_url="https://api.deepseek.com"),
        forbidden_factory,
    )
    assert result.ok and calls == [128]


def test_model_status_never_returns_api_key():
    status = configured_model(SimpleNamespace(resolve_active_profile=lambda: profile()))
    assert status["configured"]
    assert "secret" not in json.dumps(status)


def test_job_deduplication_scope_and_cooperative_cancel():
    import threading
    import time
    manager = ReviewJobManager(capacity=1)
    gate = threading.Event()
    def operation(progress, cancelled):
        progress("test gate")
        gate.wait(2)
        return {"cancelled": True} if cancelled() else {"id": "run"}
    first = manager.start("book", 2, operation)
    assert manager.start("book", 2, operation)["id"] == first["id"]
    with pytest.raises(ReviewUnavailable): manager.start("other", 1, operation)
    with pytest.raises(ValueError): manager.get("other", first["id"])
    manager.cancel("book", first["id"])
    gate.set()
    deadline = time.monotonic() + 2
    while manager.get("book", first["id"])["status"] != "cancelled" and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager.get("book", first["id"])["status"] == "cancelled"
