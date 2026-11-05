"""Exercise the real SDK/SSE parser, accounting and cancellation without paid calls."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI, RateLimitError

from application.ai.usage_accounting import usage_scope
from application.ai_invocation.dtos import InvocationPolicy, PromptSnapshot
from application.ai_invocation.services import AttemptService, InvocationSessionService
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.provider_factory import DynamicLLMService
from infrastructure.ai.providers.openai_provider import OpenAIProvider
from infrastructure.persistence.operation_journal import OperationJournal


class SSE(httpx.AsyncByteStream):
    def __init__(self, events, *, error=False, gate=None):
        self.events, self.error, self.gate = events, error, gate
        self.closed = False

    async def __aiter__(self):
        for index, event in enumerate(self.events):
            await asyncio.sleep(0)
            if index == 1 and self.gate is not None:
                await self.gate.wait()
            yield ("data: " + json.dumps(event) + "\n\n").encode()
        if self.error:
            raise httpx.ReadError("fixture disconnect")
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


def frame(text="", *, finish=None, usage=None):
    return {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "delta": {"content": text},
            "finish_reason": finish}], "usage": usage}


def tail(input=12, output=4):
    return {**frame(), "choices": [], "usage": {
        "prompt_tokens": input, "completion_tokens": output, "total_tokens": input + output}}


@pytest.fixture
def journal(tmp_path):
    journal = OperationJournal(tmp_path / "operations.sqlite3")
    journal.save_policy("book", {"max_calls": None, "max_tokens": 700, "max_cost": None,
        "prices": [{"kind": "llm", "model": "fixture", "input_per_million": 2,
                    "output_per_million": 6}]})
    return journal


async def setup_client(monkeypatch, streams, *, responses=False, status=200):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "fixture", "type": "rate_limit"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=streams[len(requests)-1])
    provider = OpenAIProvider(Settings(api_key="fixture", base_url="https://fixture.test/v1",
        default_model="fixture", use_legacy_chat_completions=not responses))
    await provider.async_client.close()
    provider.async_client = AsyncOpenAI(api_key="fixture", base_url="https://fixture.test/v1",
        max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    service = DynamicLLMService()
    monkeypatch.setattr(service, "_resolve_provider", AsyncMock(return_value=provider))
    recorder = SimpleNamespace(record_span=lambda **kwargs: spans.append(kwargs))
    spans = []
    monkeypatch.setattr("infrastructure.ai.provider_factory.get_trace_recorder", lambda: recorder)
    return provider, service, requests, spans


PROMPT = Prompt(system="s", user="u")
CONFIG = GenerationConfig(model="fixture", max_tokens=64)


async def consume(service, journal, book="book"):
    with usage_scope(book, journal):
        return "".join([text async for text in service.stream_generate(PROMPT, CONFIG)])


@pytest.mark.asyncio
async def test_sdk_usage_tail_settles_snapshot_and_allows_next_budgeted_request(journal, monkeypatch):
    streams = [SSE([frame("正文"), frame(finish="stop"), tail(10, 2), tail()]) for _ in range(2)]
    provider, service, requests, spans = await setup_client(monkeypatch, streams)
    try:
        assert await consume(service, journal) == "正文"
        assert await consume(service, journal) == "正文"
        totals = journal.summary("book")["totals"]
        assert totals["charged_tokens"] == 32
        assert totals["input_tokens"] == 24 and totals["output_tokens"] == 8
        assert totals["uncertain_calls"] == 0
        assert totals["estimated_cost"] == pytest.approx(.000096)
        assert all(request["stream_options"]["include_usage"] for request in requests)
        assert all(stream.closed for stream in streams)
        assert [(span["token_input"], span["token_output"]) for span in spans
                if span["phase"] == "llm_response"] == [(12, 4), (12, 4)]
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    [frame("text"), frame(finish="stop")],
    [tail(), frame("text"), frame(finish="stop")],
    [frame("text"), frame(finish="stop"), {**tail(), "usage": {"prompt_tokens": 3}}],
    [frame("text"), frame(finish="stop"), {**tail(), "usage": {"prompt_tokens": -1, "completion_tokens": 4}}],
    [frame("text"), frame(finish="stop"), {**tail(), "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 100}}],
])
async def test_missing_early_or_invalid_usage_retains_reservation(journal, monkeypatch, events):
    stream = SSE(events)
    provider, service, _, _ = await setup_client(monkeypatch, [stream])
    try:
        assert await consume(service, journal) == "text"
        row = journal.summary("book")["calls"][0]
        assert row["status"] == "completed" and row["input_tokens"] is None
        assert journal.summary("book")["totals"]["charged_tokens"] == row["reserved_tokens"]
        assert journal.summary("book")["totals"]["uncertain_calls"] == 1
        assert stream.closed
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [False, True])
async def test_broken_stream_is_failed_reserved_and_never_retried(journal, monkeypatch, error):
    stream = SSE([frame("partial")], error=error)
    provider, service, requests, _ = await setup_client(monkeypatch, [stream])
    try:
        with pytest.raises((RuntimeError, httpx.ReadError)):
            await consume(service, journal)
        row = journal.summary("book")["calls"][0]
        assert row["status"] == "failed" and row["input_tokens"] is None
        assert len(requests) == 1 and stream.closed
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
async def test_rate_limit_is_one_physical_request(journal, monkeypatch):
    provider, service, requests, _ = await setup_client(monkeypatch, [], status=429)
    try:
        with pytest.raises(RateLimitError):
            await consume(service, journal)
        assert len(requests) == 1
        assert journal.summary("book")["totals"]["uncertain_calls"] == 1
    finally:
        await provider.async_client.close()


def invocation():
    session = InvocationSessionService().create(operation="fixture", node_key="fixture",
        policy=InvocationPolicy.DIRECT, context={"novel_id": "book"})
    snapshot = PromptSnapshot(prompt=PROMPT, **{key: "fixture" for key in (
        "node_key", "node_version_id", "asset_link_set_id", "input_binding_set_id",
        "output_binding_set_id", "variable_snapshot_hash", "template_hash",
        "composition_hash", "rendered_prompt_hash")})
    return session, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_invocation_stop_or_task_cancel_closes_immediately(journal, monkeypatch, cancel):
    gate = asyncio.Event()
    stream = SSE([frame("partial"), frame(finish="stop"), tail()], gate=gate)
    provider, service, _, _ = await setup_client(monkeypatch, [stream])
    attempts = AttemptService(service)
    session, snapshot = invocation()
    started = asyncio.Event()
    def on_chunk(*_):
        started.set()
        return None if cancel else False
    try:
        with usage_scope("book", journal):
            task = asyncio.create_task(attempts.generate_streaming(session=session,
                prompt_snapshot=snapshot, config=CONFIG, on_chunk=on_chunk))
            await asyncio.wait_for(started.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
        attempt = attempts.get(session.attempts[0])
        assert attempt.content == "partial" and attempt.token_usage is None
        assert session.status.value == "cancelled" and attempt.status.value == "failed"
        assert stream.closed
        assert journal.summary("book")["calls"][0]["status"] == "failed"
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
async def test_concurrent_books_share_provider_without_usage_leak(journal, monkeypatch):
    streams = [SSE([frame(str(index)), frame(finish="stop"), tail(index+1, index+2)]) for index in range(2)]
    provider, service, _, _ = await setup_client(monkeypatch, streams)
    try:
        assert await asyncio.gather(consume(service, journal), consume(service, journal, "other")) == ["0", "1"]
        assert journal.summary("book")["totals"]["charged_tokens"] == 3
        assert journal.summary("other")["totals"]["charged_tokens"] == 5
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
async def test_nested_streams_do_not_mix_usage(journal, monkeypatch):
    provider, service, _, _ = await setup_client(monkeypatch, [
        SSE([frame("outer"), frame(finish="stop"), tail(3, 2)]),
        SSE([frame("inner"), frame(finish="stop"), tail(7, 4)])])
    try:
        with usage_scope("book", journal):
            async for text in service.stream_generate(PROMPT, CONFIG):
                assert text == "outer"
                assert await consume(service, journal, "other") == "inner"
        assert journal.summary("book")["totals"]["charged_tokens"] == 5
        assert journal.summary("other")["totals"]["charged_tokens"] == 11
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
async def test_legacy_string_provider_remains_unknown(journal, monkeypatch):
    class Legacy:
        settings = SimpleNamespace(default_model="fixture", default_temperature=.7)
        async def stream_generate(self, prompt, config):
            yield "legacy"
    service = DynamicLLMService()
    monkeypatch.setattr(service, "_resolve_provider", AsyncMock(return_value=Legacy()))
    monkeypatch.setattr("infrastructure.ai.provider_factory.get_trace_recorder",
        lambda: SimpleNamespace(record_span=lambda **kwargs: None))
    assert await consume(service, journal) == "legacy"
    assert journal.summary("book")["totals"]["uncertain_calls"] == 1


@pytest.mark.asyncio
async def test_usage_options_cannot_be_disabled_by_profile_extra_body(journal, monkeypatch):
    provider, service, requests, _ = await setup_client(monkeypatch,
        [SSE([frame("text"), frame(finish="stop"), tail()])])
    provider.settings.extra_body = {"stream_options": {"include_usage": False}, "stream": False}
    try:
        await consume(service, journal)
        assert requests[0]["stream"] is True
        assert requests[0]["stream_options"]["include_usage"] is True
        assert provider.settings.extra_body["stream_options"]["include_usage"] is False
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete", "response.failed", None])
async def test_responses_sdk_final_event(journal, monkeypatch, terminal):
    events = [{"type": "response.output_text.delta", "delta": "text", "sequence_number": 1}]
    if terminal:
        events.append({"type": terminal, "sequence_number": 2, "response": {
            "id": "fixture", "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}}})
    stream = SSE(events)
    provider, service, _, _ = await setup_client(monkeypatch, [stream], responses=True)
    try:
        if terminal == "response.completed":
            assert await consume(service, journal) == "text"
            assert journal.summary("book")["totals"]["charged_tokens"] == 16
        else:
            with pytest.raises(RuntimeError):
                await consume(service, journal)
            assert journal.summary("book")["calls"][0]["status"] == "failed"
            assert journal.summary("book")["totals"]["uncertain_calls"] == 1
        assert stream.closed
    finally:
        await provider.async_client.close()


@pytest.mark.parametrize("value", [True, -1, 1.5, "12", None])
def test_usage_parser_rejects_coercible_or_invalid_counts(value):
    assert OpenAIProvider._final_stream_usage({"prompt_tokens": value, "completion_tokens": 4}) is None


@pytest.mark.asyncio
async def test_zero_usage_is_known_and_attempt_persists(journal, monkeypatch, tmp_path):
    from infrastructure.persistence.database.connection import DatabaseConnection
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
        SqliteInvocationSessionRepository, SqliteInvocationAttemptRepository)
    provider, service, _, _ = await setup_client(monkeypatch, [SSE([frame("text"), frame(finish="stop"), tail(0, 0)])])
    session, snapshot = invocation()
    try:
        with usage_scope("book", journal):
            attempt = await AttemptService(service).generate_streaming(session=session,
                prompt_snapshot=snapshot, config=CONFIG)
        assert attempt.token_usage.input_tokens == 0
        db = DatabaseConnection(str(tmp_path / "main.sqlite3"))
        SqliteInvocationSessionRepository(db).save(session)
        repo = SqliteInvocationAttemptRepository(db)
        repo.save(attempt)
        restored = repo.get(attempt.id)
        assert restored.token_usage == attempt.token_usage
        assert restored.content == "text"
        assert journal.summary("book")["totals"]["uncertain_calls"] == 0
    finally:
        await provider.async_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_http_background_stream_persists_usage_or_cancellation(journal, monkeypatch, tmp_path, cancel):
    from application.ai_invocation.dtos import InvocationAttempt, InvocationAttemptStatus
    from infrastructure.persistence.database.connection import DatabaseConnection
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
        SqliteInvocationSessionRepository, SqliteInvocationAttemptRepository)
    from interfaces.api.v1.engine import ai_invocation_routes as routes
    gate = asyncio.Event() if cancel else None
    stream = SSE([frame("partial"), frame(finish="stop"), tail()], gate=gate)
    provider, service, _, _ = await setup_client(monkeypatch, [stream])
    db = DatabaseConnection(str(tmp_path / "http.sqlite3"))
    session, snapshot = invocation()
    session.prompt_snapshot = snapshot
    attempt = InvocationAttempt(id="attempt", session_id=session.id,
        status=InvocationAttemptStatus.RUNNING, prompt_snapshot=snapshot)
    session.attempts.append(attempt.id)
    sessions, attempts = SqliteInvocationSessionRepository(db), SqliteInvocationAttemptRepository(db)
    sessions.save(session)
    attempts.save(attempt)
    monkeypatch.setattr(routes, "_repositories", lambda: {"session": sessions, "attempt": attempts})
    monkeypatch.setattr(routes, "get_llm_service", lambda: service)
    monkeypatch.setattr(routes, "_publish_autopilot_session_state", lambda session: None)
    try:
        with usage_scope("book", journal):
            task = asyncio.create_task(routes._run_streaming_invocation_attempt(
                session_id=session.id, attempt_id=attempt.id, config=CONFIG))
            if cancel:
                for _ in range(100):
                    if attempts.get(attempt.id).content:
                        break
                    await asyncio.sleep(.01)
                assert attempts.get(attempt.id).content == "partial"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
        restored = attempts.get(attempt.id)
        assert restored.content == "partial"
        if cancel:
            assert restored.token_usage is None
            assert sessions.get(session.id).status.value == "cancelled"
            assert journal.summary("book")["calls"][0]["status"] == "failed"
        else:
            assert restored.token_usage.input_tokens == 12 and restored.token_usage.output_tokens == 4
            assert sessions.get(session.id).status.value == "awaiting_acceptance"
            assert journal.summary("book")["totals"]["charged_tokens"] == 16
        assert stream.closed
    finally:
        await provider.async_client.close()
