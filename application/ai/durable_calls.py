"""Request-local model snapshots for durable chapter work; inert elsewhere."""
from contextvars import ContextVar
from functools import wraps
import json
import hashlib
import inspect

current_work = ContextVar("xuheng_aftermath_work", default=None)
current_stage = ContextVar("xuheng_aftermath_stage", default=None)
current_request = ContextVar("xuheng_aftermath_request", default=None)


class ConfirmationRequired(BaseException):
    """Cannot be swallowed by optional-analysis Exception fallbacks."""


def request_role(prompt):
    # Key by the business call path, not a global ordinal: a cached baseline or
    # optional branch may disappear after restart. Distinct operations must not
    # accidentally consume one another's saved model output.
    frames = []
    frame = inspect.currentframe().f_back
    try:
        while frame is not None and len(frames) < 3:
            path = frame.f_code.co_filename.replace('\\', '/')
            if '/application/' in path and not any(part in path for part in (
                '/application/ai_invocation/', '/application/ai/durable_calls.py',
                '/application/engine/services/durable_aftermath.py', '/application/core/async_bridge.py')):
                frames.append(path.split('/application/', 1)[1] + ':' + frame.f_code.co_qualname)
            frame = frame.f_back
    finally:
        del frame
    return hashlib.sha256(('|'.join(frames) if frames else prompt.system).encode()).hexdigest()[:24]


def durable_generate(function):
    @wraps(function)
    async def wrapped(self, prompt, config):
        work = current_work.get()
        if work is None or current_request.get() is not None:
            return await function(self, prompt, config)
        from infrastructure.persistence.database.atomic_effect import binding_for
        bound = binding_for()
        if bound is not None:
            bound.failure = RuntimeError("本地章后事务内不允许模型请求")
            raise bound.failure
        from domain.ai.services.llm_service import GenerationResult
        from domain.ai.value_objects.token_usage import TokenUsage
        key = work.begin_model_request(request_role(prompt))
        old = work.store.request(work.task["id"], *key[:2])
        if old and old["status"] == "completed":
            value = json.loads(old["result"])
            return GenerationResult(value["content"], TokenUsage(**value["usage"]) if value["usage"] else None)
        token = current_request.set(key)
        try:
            result = await function(self, prompt, config)
            usage = result.token_usage
            work.store.finish_request(work.task["id"], *key, "completed", {
                "content": result.content,
                "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens} if usage else None})
            return result
        except BaseException as exc:
            record = work.store.request(work.task["id"], *key[:2])
            charged = bool(json.loads(record["call_ids"])) if record else False
            work.store.finish_request(work.task["id"], *key, "uncertain" if charged else "failed")
            if not isinstance(exc, Exception):
                raise
            if charged:
                raise ConfirmationRequired("模型请求可能已计费，但结果未留存；需要确认后再请求")
            raise
        finally:
            current_request.reset(token)
    return wrapped


def durable_effect(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            work = current_work.get()
            if work is None:
                return function(*args, **kwargs)
            return work.effect(name, lambda: function(*args, **kwargs))
        return wrapped
    return decorate


async def durable_stage(name, operation):
    work = current_work.get()
    return await work.stage(name, operation) if work else await operation()
