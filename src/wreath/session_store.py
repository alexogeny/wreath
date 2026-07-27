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

Expired rows are removed by :meth:`PostgresSessionStore.purge_pass` -- a chunked,
resumable, paced walk you hand to the job runner rather than a background thread,
so it retries, does not duplicate across workers, and cannot hold one long
transaction open over a table that grows with every login.
"""

from __future__ import annotations

from typing import Any, Protocol

from .store import Column, Keyed, PostgresStore

__all__ = ["PostgresSessionStore", "SessionStore"]


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
    wall clocks cannot disagree about expiry -- the same reasoning, and now the
    same :mod:`wreath.store` primitive, as the PostgreSQL rate-limit store.

    Unlike the other two stores over that primitive, the lifetime is not the
    store's to decide: the cookie's ``max_age`` is what a session should outlive,
    so it is bound per write rather than fixed at construction.
    """

    __slots__ = ("_database", "_store")

    def __init__(self, database: Any, *, table: str = "wreath_session") -> None:
        self._database = database
        self._store = PostgresStore(
            database,
            Keyed(
                table=table,
                columns=(Column("data", "jsonb", null=False),),
                key="sid",
                # An index on `expires`: purge scans by it, and a session table
                # is big enough -- one row per login, not per caller -- for the
                # difference between an index and a sequential scan to matter.
                index_stamp=True,
                prefix="wreath_session",
            ),
            # Nothing here is claimed, and a session is read on nearly every
            # request, so the lookup may go to the read pool.
            read_workload="read",
        )
        self._store.define(
            "save",
            self._store.upsert(
                values={"sid": "$1", "data": "$2::jsonb", "expires": self._store.window("$3")},
                update={"data": "excluded.data", "expires": "excluded.expires"},
            ),
        )

    def schema_sql(self) -> str:
        """DDL for the backing table. Apply it as a migration."""
        return self._store.schema_sql()

    async def load(self, sid: str) -> dict[str, Any] | None:
        row = await self._store.read(sid, live=True)
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

        await self._store.statement("save").execute(
            sid, _json_dumps(data).decode("utf-8"), float(max_age)
        )

    async def delete(self, sid: str) -> None:
        await self._store.delete(sid)

    def purge_pass(self, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that deletes expired sessions, chunk by chunk.

        The supported way to keep the table small::

            jobs.drive(store.purge_pass(), cron="*/10 * * * *")

        A session table is one row per login rather than one per request, so it
        is exactly the size where a single unbounded ``DELETE`` starts holding a
        snapshot open long enough for the application to notice afterwards. The
        pass walks it in expiry order, one transaction per chunk, resumably, and
        paced. See :mod:`wreath.passes`.
        """
        from ._passes.stores import keyed_purge_pass

        return keyed_purge_pass(
            self._store.declaration, self._database,
            name=f"purge_{self._store.table}", chunk=chunk, **options,
        )

    async def purge(self) -> str:
        """Delete expired rows in **one unbounded statement**.

        Safe to run concurrently, and fine for a small table -- but on a large
        one it is the long transaction :meth:`purge_pass` exists to prevent.
        """
        return await self._store.purge()
