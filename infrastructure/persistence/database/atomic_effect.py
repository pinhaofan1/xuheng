"""Bind repository writes to one short effect/receipt transaction.

Only explicitly marked local effects enter this scope. No network or model work
is allowed; normal repository callers and the manuscript queue keep their paths.
"""
from contextvars import ContextVar
from pathlib import Path

_binding = ContextVar("xuheng_atomic_effect", default=None)


def binding_for(path=None):
    bound = _binding.get()
    if bound is not None and path is not None and Path(path).resolve() != Path(bound.db.db_path).resolve():
        bound.failure = RuntimeError("章后事务不能访问其他数据库")
        raise bound.failure
    return bound


class BoundCursor:
    def __init__(self, binding, cursor):
        self.binding, self.cursor = binding, cursor

    def execute(self, sql, params=()):
        command = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if command in {"COMMIT", "ROLLBACK", "BEGIN", "END", "SAVEPOINT", "RELEASE"}:
            self.binding.failure = RuntimeError("章后事务不能被子仓储提前结束")
            raise self.binding.failure
        try:
            self.cursor.execute(sql, params)
            return self
        except BaseException as exc:
            from infrastructure.persistence.database.write_dispatch import sql_is_mutating
            if sql_is_mutating(sql):
                self.binding.failure = exc
            raise

    def executescript(self, *args):
        self.binding.failure = RuntimeError("章后事务内不允许隐式提交脚本")
        raise self.binding.failure

    def executemany(self, sql, params):
        try:
            self.cursor.executemany(sql, params)
            return self
        except BaseException as exc:
            self.binding.failure = exc
            raise

    def __getattr__(self, key):
        if key == "connection":
            return self.binding
        return getattr(self.cursor, key)

    def __iter__(self):
        return iter(self.cursor)

    def __next__(self):
        return next(self.cursor)


class EffectBinding:
    def __init__(self, db, conn):
        self.db, self.conn, self.failure = db, conn, None

    def cursor(self):
        return BoundCursor(self, self.conn.cursor())

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, params):
        return self.cursor().executemany(sql, params)

    def commit(self):
        pass  # The effect and its receipt commit together, once, at the boundary.

    def rollback(self):
        self.failure = RuntimeError("章后子步骤要求回滚")

    def executescript(self, *args):
        self.failure = RuntimeError("章后事务内不允许隐式提交的脚本")
        raise self.failure

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if value is not None:
            self.failure = value
        return False

    def __getattr__(self, key):
        if key in {"close", "backup"}:
            raise RuntimeError("不能在章后事务内关闭或替换数据库")
        return getattr(self.conn, key)
