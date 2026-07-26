"""Server-side session storage.

The default :class:`~wreath.middleware.SessionMiddleware` keeps the whole
session in a signed cookie: nothing on the server, but nothing revocable
either, and a 4 KiB ceiling. Hand the middleware a store and the cookie carries
only a signed session id, while the contents live in PostgreSQL::

    store = PostgresSessionStore(app.postgres("main"))
    app.add_middleware(SessionMiddleware(secret="…", store=store))

Then a session can be revoked (delete the row), inspected, and grown past what a
cookie holds. No Redis: the database the application already has is the store,
the same choice :class:`~wreath.middleware.PostgresRateLimitStore` makes.

**The table is not created for you.** Apply :meth:`PostgresSessionStore.schema_sql`
as a migration, so schema changes stay in the migration history where the rest
of the schema lives.

Expired rows are removed by :meth:`PostgresSessionStore.purge`; run it from a
durable job rather than a background thread, so it retries and does not
duplicate across workers.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

__all__ = ["PostgresSessionStore", "SessionStore"]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SessionStore(Protocol):
    """Where server-side session contents live."""

    async def load(self, sid: str) -> dict[str, Any] | None:
        """The session for ``sid``, or None when absent or expired."""
        ...

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        """Store ``data`` under ``sid``, expiring ``max_age`` seconds from now."""
        ...

    async def delete(self, sid: str) -> None:
        """Drop ``sid``. Must not fail when it is already gone."""
        ...


class PostgresSessionStore:
    """Sessions in a PostgreSQL table, shared by every worker.

    ``jsonb`` rather than ``text``: the contents stay queryable, so "log out
    every session for this user" is one statement rather than a scan.

    PostgreSQL owns the clock (``clock_timestamp()``), so workers on disagreeing
    wall clocks cannot disagree about expiry -- the same reasoning as the
    PostgreSQL rate-limit store.
    """

    __slots__ = ("_database", "_delete", "_load", "_purge", "_save", "_table")

    def __init__(self, database: Any, *, table: str = "wreath_session") -> None:
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("table must be a plain SQL identifier")
        self._database = database
        self._table = table
        self._load: Any = database.statement(
            f"wreath_session_load_{table}",
            f"SELECT data FROM {table} "
            "WHERE sid = $1 AND expires > clock_timestamp()",
            workload="read",
        )
        self._save: Any = database.statement(
            f"wreath_session_save_{table}",
            f"INSERT INTO {table} (sid, data, expires) VALUES "
            "($1, $2::jsonb, clock_timestamp() + make_interval(secs => $3::float8))\n"
            "ON CONFLICT (sid) DO UPDATE SET data = EXCLUDED.data, "
            "expires = EXCLUDED.expires",
            workload="write",
        )
        self._delete: Any = database.statement(
            f"wreath_session_delete_{table}",
            f"DELETE FROM {table} WHERE sid = $1",
            workload="write",
        )
        self._purge: Any = database.statement(
            f"wreath_session_purge_{table}",
            f"DELETE FROM {table} WHERE expires <= clock_timestamp()",
            workload="write",
        )

    def schema_sql(self) -> str:
        """DDL for the backing table. Apply it as a migration."""
        return (
            f"CREATE TABLE IF NOT EXISTS {self._table} (\n"
            "    sid text PRIMARY KEY,\n"
            "    data jsonb NOT NULL,\n"
            "    expires timestamptz NOT NULL\n"
            ");\n"
            f"CREATE INDEX IF NOT EXISTS {self._table}_expires_idx\n"
            f"    ON {self._table} (expires)"
        )

    async def load(self, sid: str) -> dict[str, Any] | None:
        row = await self._load.fetchrow(sid)
        if row is None:
            return None
        data = row[0]
        if isinstance(data, (str, bytes)):
            # A driver that hands jsonb back as text rather than decoded.
            from ._json import loads as _json_loads

            data = _json_loads(data)
        return data if isinstance(data, dict) else None

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        from ._json import dumps as _json_dumps

        await self._save.execute(sid, _json_dumps(data).decode("utf-8"), float(max_age))

    async def delete(self, sid: str) -> None:
        await self._delete.execute(sid)

    async def purge(self) -> str:
        """Delete expired rows. Safe to run concurrently; run it from a job."""
        return await self._purge.execute()
