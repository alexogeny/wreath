"""Server-side session storage.

The default `SessionMiddleware` keeps the whole
session in a signed cookie: nothing on the server, but nothing revocable
either, and a 4 KiB ceiling. Hand the middleware a store and the cookie carries
only a signed session id, while the contents live in PostgreSQL:

```python
store = PostgresSessionStore(app.postgres("main"))
app.add_middleware(SessionMiddleware(secret="…", store=store))
```

Then a session can be revoked (delete the row), inspected, and grown past what a
cookie holds. No Redis: the database the application already has is the store,
the same choice `PostgresRateLimitStore` makes.

**The table is not created for you.** Apply `PostgresSessionStore.schema_sql`
as a migration, so schema changes stay in the migration history where the rest
of the schema lives.

Expired rows are removed by `PostgresSessionStore.purge_pass` -- a chunked,
resumable, paced walk you hand to the job runner rather than a background thread,
so it retries, does not duplicate across workers, and cannot hold one long
transaction open over a table that grows with every login.
"""

from __future__ import annotations

from typing import Any, Protocol

from .store import Column, Keyed, PostgresStore, Sql

__all__ = ["PostgresSessionStore", "SessionStore"]


class SessionStore(Protocol):
    """Where server-side session contents live."""

    async def load(self, sid: str) -> dict[str, Any] | None:
        """The session for `sid`, or None when absent or expired."""
        ...

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        """Store `data` under `sid`, expiring `max_age` seconds from now."""
        ...

    async def delete(self, sid: str) -> None:
        """Drop `sid`. Must not fail when it is already gone."""
        ...


class PostgresSessionStore:
    """Sessions in a PostgreSQL table, shared by every worker.

    `jsonb` rather than `text`: the contents stay queryable, so "log out
    every session for this user" is one statement rather than a scan.

    PostgreSQL owns the clock (`clock_timestamp()`), so workers on disagreeing
    wall clocks cannot disagree about expiry -- the same reasoning, and now the
    same `wreath.store` primitive, as the PostgreSQL rate-limit store.

    Unlike the other two stores over that primitive, the lifetime is not the
    store's to decide: the cookie's `max_age` is what a session should outlive,
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
            "delete_for",
            f"DELETE FROM {self._store.table} "
            "WHERE data -> 'principal' ->> 'sub' = $1",
        )
        self._store.define(
            "save",
            self._store.upsert(
                values={"sid": "$1", "data": "$2::jsonb", "expires": self._store.window("$3")},
                update={"data": Sql("excluded.data"), "expires": Sql("excluded.expires")},
            ),
        )

    def component(self) -> Any:
        """This store's claim on the wreath schema.

        The session table is **unqualified** -- `wreath_session`, resolved
        through `search_path` -- and it stays that way. Moving it into the
        `wreath` schema is not additive: a worker still on the previous version
        looks for the unqualified name and would not find it, which is precisely
        what the additive rule exists to prevent. So the component is registered
        where the rows actually are, and a move is a later, staged concern.
        """
        return self._store.component(name="session")

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined. A derivation of
        `component()`."""
        return self._store.schema_sql()

    async def load(self, sid: str) -> dict[str, Any] | None:
        """The session under `sid`, or `None` when it is absent or expired.

        Expiry is decided by the database's clock in the same statement as the
        lookup, so an expired row is never returned even though nothing has
        purged it yet. Anything that is not a JSON object -- a row whose payload
        was written by something else -- also reads as `None` rather than
        reaching a handler as a value it cannot use.
        """
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
        """Store `data` under `sid`, expiring `max_age` seconds from now.

        One `INSERT ... ON CONFLICT DO UPDATE`, so creating a session and
        rewriting one are the same statement and neither can lose a race to the
        other. Every write moves the deadline, which is what a session wants and
        is the opposite of the rule the other stores over this primitive follow:
        a session should survive for `max_age` past the last time it was used,
        while an idempotency key must not be extendable by the request holding
        it.

        Args:
            sid: the session id, as the cookie carries it.
            data: the whole session, as `jsonb`. Replaces; this is not a merge.
            max_age: seconds, applied against the database's clock, not this worker's.
        """
        from ._json import dumps as _json_dumps

        await self._store.statement("save").execute(
            sid, _json_dumps(data).decode("utf-8"), float(max_age)
        )

    async def delete(self, sid: str) -> None:
        """Revoke one session. Not an error when it is already gone.

        Takes effect for every worker at once, which is the thing a
        cookie-only session cannot do.
        """
        await self._store.delete(sid)

    async def delete_for(self, subject: str) -> int:
        """Drop every session whose principal is `subject`.

        One statement over the payload, because the alternative -- read every
        row, decode each one, delete the matches -- is the whole table across
        the network to end a handful of sessions. The predicate reads the same
        `principal.sub` the session backend writes.
        """
        status = await self._store.statement("delete_for").execute(subject)
        if not isinstance(status, str) or not status.startswith("DELETE"):
            return 0
        _, _, count = status.partition(" ")
        try:
            return int(count.strip())
        except ValueError:
            return 0

    def purge_pass(self, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that deletes expired sessions, chunk by chunk.

        The supported way to keep the table small:

        ```python
        jobs.drive(store.purge_pass(), cron="*/10 * * * *")
        ```

        A session table is one row per login rather than one per request, so it
        is exactly the size where a single unbounded `DELETE` starts holding a
        snapshot open long enough for the application to notice afterwards. The
        pass walks it in expiry order, one transaction per chunk, resumably, and
        paced. See `wreath.passes`.
        """
        from ._passes.stores import keyed_purge_pass

        return keyed_purge_pass(
            self._store.declaration,
            name=f"purge_{self._store.table}", chunk=chunk, **options,
        )

    async def purge(self) -> str:
        """Delete expired rows in **one unbounded statement**.

        Safe to run concurrently, and fine for a small table -- but on a large
        one it is the long transaction `purge_pass` exists to prevent.
        """
        return await self._store.purge()
