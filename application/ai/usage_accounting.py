"""Book-scoped admission and settlement for model requests.

An interrupted/failed call retains its reservation: the remote side may have
billed even if no response arrived. Never retry a paid request here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

_scope = ContextVar("xuheng_usage_scope", default=None)
_one_request = ContextVar("xuheng_one_provider_request", default=False)


def has_usage_scope():
    return _one_request.get()


@contextmanager
def single_provider_request():
    token = _one_request.set(True)
    try:
        yield
    finally:
        _one_request.reset(token)


def account_for_book(operation):
    def decorate(function):
        @wraps(function)
        async def wrapped(self, novel_id, *args, **kwargs):
            from infrastructure.persistence.operation_journal import journal_at
            path = getattr(getattr(self.store, "db", None), "db_path", None)
            if (_scope.get() is not None and _scope.get()[0] == novel_id) or path is None:
                return await function(self, novel_id, *args, **kwargs)
            with usage_scope(novel_id, journal_at(str(Path(path).with_suffix(".operations.sqlite3"))), operation):
                return await function(self, novel_id, *args, **kwargs)
        return wrapped
    return decorate


def account_runtime(function):
    """Keep all daemon stages scoped and stop the book after a budget denial."""
    @wraps(function)
    async def wrapped(host, novel):
        from infrastructure.persistence.operation_journal import journal_at
        from domain.novel.entities.novel import AutopilotStatus
        path = getattr(getattr(getattr(host,"novel_repository",None),"db",None),"db_path",None)
        novel_id = str(getattr(novel.novel_id,"value",novel.novel_id))
        # Production repositories supply a real path; injected test hosts may not.
        if not isinstance(path,str):
            return await function(host,novel)
        journal = journal_at(str(Path(path).with_suffix(".operations.sqlite3")))
        def stop_if_blocked():
            block = journal.budget_block(novel_id)
            if not block:
                return False
            novel.autopilot_status = AutopilotStatus.STOPPED
            # Keep the exact stage for explicit resume; no chapter is discarded.
            host._update_shared_state(novel_id,autopilot_status="stopped",writing_substep="budget_blocked",
                writing_substep_label="作品预算已拦截，请调整预算后手动恢复",last_error=block["message"])
            host._save_novel_state(novel)
            return True
        if stop_if_blocked():
            return
        with usage_scope(novel_id,journal,"全托管创作"):
            try:
                return await function(host,novel)
            finally:
                stop_if_blocked()
    return wrapped


@contextmanager
def usage_scope(novel_id, journal=None, operation=""):
    if not novel_id:
        yield
        return
    parent = _scope.get()
    if parent is not None and parent[0] == novel_id:
        if journal is None:
            journal = parent[1]
        if not operation:
            operation = parent[2]
    token = _scope.set((novel_id, journal, operation))
    try:
        yield
    finally:
        _scope.reset(token)


class ModelCall:
    def __init__(self, kind, model, text, output_bound=0, novel_id="", operation=""):
        self.id = None
        scope = _scope.get()
        if scope is None and novel_id:
            from infrastructure.persistence.operation_journal import get_operation_journal
            scope = (novel_id, get_operation_journal(), operation)
        if scope:
            novel, self.journal, label = scope
            if self.journal is None:
                from infrastructure.persistence.operation_journal import get_operation_journal
                self.journal = get_operation_journal()
            # UTF-8 bytes plus framing are a conservative local estimate. It is
            # not a provider tokenizer or a guarantee against provider overbilling.
            self.id = self.journal.reserve(novel, kind, model, len(text.encode("utf-8"))+512, output_bound, label)

    def finish(self, input_tokens=None, output_tokens=None):
        if self.id:
            self.journal.finish(self.id, input_tokens, output_tokens)

    def fail(self):
        if self.id:
            self.journal.finish(self.id, status="failed", error="请求失败或中断，用量待核对")
