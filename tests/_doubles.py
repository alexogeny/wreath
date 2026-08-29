from __future__ import annotations

from typing import Any

from _pgfidelity import check_for


class RecordingConnection:
    """Records every statement, answers with fixed defaults.

    The defaults are the shape a caller that ignores results wants: one row
    affected, no rows returned. A test that needs a *particular* answer wants
    its own double, not a keyword argument here.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return 1


class SilentConnection:
    """`RecordingConnection`'s answers without the recording.

    For tests that assert on what the code *did* rather than on what SQL it
    emitted. Kept separate rather than made a flag: a `calls` list nobody reads
    is an invitation to start asserting on statement text, which is the coupling
    these tests were written to avoid.
    """

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        check_for(self, sql, args)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        return 1


class PooledConnection:
    """One pooled connection, with `rows` and `calls` shared across the pool.

    **Distinct objects, shared state, and that is load-bearing.** The pool
    tracks which connection it lent out, so handing the *same* object back for a
    second slot makes a release raise `connection was not borrowed from this
    pool`. That began mattering when lifespan started bootstrapping these
    stores' tables: schema bootstrap pins one connection for the per-component
    advisory lock and acquires another to run the DDL, so the pool is asked for
    two at once.

    `rows` is consumed by `fetchrow`, so a test scripts the answers it expects
    to be asked for, in order.
    """

    def __init__(self, rows: list[Any], calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls
        self.rows = rows

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "DELETE 0"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return self.rows.pop(0)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return None


__all__ = ["PooledConnection", "RecordingConnection", "SilentConnection"]
