"""Make unsafe requests replay-safe with an ``Idempotency-Key`` header.

A client that retries a ``POST`` (a dropped connection, an impatient user) must
not create two records. When a request carries an ``Idempotency-Key``, this
middleware remembers the first response and replays it for any repeat of the
same key.

Opt-in per request (the header) and safe by construction:

* only unsafe methods are considered (``POST``/``PUT``/``PATCH``/``DELETE``),
* the key is scoped by method, path, and the authenticated principal, so two
  callers cannot collide on the same key value,
* an **unauthenticated** request is therefore not guarded at all: there is no
  principal to scope it by, and sharing one keyspace between anonymous callers
  would replay one caller's response to another,
* a concurrent duplicate (the first is still running) gets ``409``,
* a ``5xx`` is not cached — a transient failure stays retryable.

**Two stores, and the difference matters.** The default
:class:`MemoryIdempotencyStore` is bounded and in-process: it covers a retry
that lands on the worker that served the original, and nothing else. Behind a
load balancer that is most retries but not all, so a deployment that needs the
guarantee should share :class:`PostgresIdempotencyStore` — one table, no Redis,
and the claim is a single statement so two workers cannot both believe they
were first.

Even then, remember what this middleware is: **response replay**. It saves the
work and keeps the answer consistent. What makes the *effect* happen once is a
unique index on something durable — the ``key`` you pass to
:meth:`wreath.jobs.JobRunner.enqueue`, or a unique column on the row you write.
Compose the two and the guarantee holds end to end; see the
[exactly-once recipe](../../docs/cookbook/recipes/exactly-once.md).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from .._jobcore import dedup_key
from ..request import Request
from ..response import ProblemResponse, Response
from ..store import CLAIMED, Column, Keyed, MemoryStore, PostgresStore

_UNCACHEABLE_BODY = ("StreamingResponse", "FileResponse", "SSEResponse", "PreparedResponse")

#: Statuses whose *cause* is expected to change on a retry, so storing one would
#: hand the client the same failure for the whole TTL. A rate limit lifts, a
#: lock frees, an authorization decision follows a role change; none of them
#: said "your write happened". 5xx is handled separately, and every other 4xx
#: (a 422 on a malformed body, a 404 on a missing row) is deterministic enough
#: that replaying the answer is the point of the feature.
_RETRYABLE_STATUSES = frozenset({401, 403, 408, 409, 423, 425, 429, 449})

#: Response headers that must never be replayed to a later request. A cookie is
#: the response's own per-request state -- a rotated session id, a CSRF token --
#: and handing a stored copy to every retry re-issues credentials the server has
#: already moved on from.
_UNREPLAYABLE_HEADERS = frozenset({b"set-cookie", b"set-cookie2"})


def _replayable_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> tuple[tuple[bytes, bytes], ...]:
    """The headers worth storing: dropped rather than filtered on the way out,
    so the store never holds a credential it does not need."""
    return tuple(
        (name, value)
        for name, value in headers
        if name.lower() not in _UNREPLAYABLE_HEADERS
    )

#: A stored response: status, headers, body.
type Replay = tuple[int, tuple[tuple[bytes, bytes], ...], bytes]


class IdempotencyStore(Protocol):
    """Where first responses are kept so a retry can replay them."""

    async def reserve(self, key: str) -> tuple[str, Replay | None]:
        """Claim ``key`` for this request.

        Returns ``("fresh", None)`` when this caller owns the key and should
        run the handler, ``("in_flight", None)`` when another request holds it,
        or ``("done", replay)`` when a response is already stored.

        Must be **atomic**: a read followed by a write lets two workers both
        conclude they were first, which is the one thing this middleware exists
        to prevent.
        """

    async def store(self, key: str, replay: Replay) -> None: ...

    async def release(self, key: str) -> None: ...


class MemoryIdempotencyStore:
    """Bounded, in-process, no external state. One worker's memory.

    Enough on its own for a single-worker deployment or a sticky load balancer;
    behind anything else it is a fast path in front of the durable guarantee
    rather than the guarantee itself.

    The window runs from the first attempt, not from whenever the handler
    finished -- :class:`wreath.store.MemoryStore` keeps the deadline a key was
    claimed with, so a slow request cannot extend its own key and the two stores
    honour a key for the same length of time.
    """

    __slots__ = ("_store",)

    def __init__(self, *, ttl: float = 24 * 60 * 60, max_entries: int = 4096) -> None:
        self._store = MemoryStore(ttl=ttl, max_entries=max_entries)

    async def reserve(self, key: str) -> tuple[str, Replay | None]:
        # The claim is atomic by virtue of being synchronous: no await between
        # the read and the write, so no other task can interleave.
        if self._store.claim(key):
            return ("fresh", None)
        entry = self._store.read(key)
        if entry is None:
            # Expired or evicted between the claim and the read. The safe
            # reading is "run it", the same as for a key never seen.
            return ("fresh", None)
        if entry is CLAIMED:
            return ("in_flight", None)
        return ("done", entry)

    async def store(self, key: str, replay: Replay) -> None:
        self._store.set(key, replay)

    async def release(self, key: str) -> None:
        self._store.delete(key)


class PostgresIdempotencyStore:
    """Replays in a shared table, so every worker honours every key.

    The claim is :meth:`wreath.store.PostgresStore.claim`: one statement, and a
    returned row **is** the claim — no owner column, no second round trip, and
    no window in which two workers both believe they were first. Everything else
    here is this middleware's payload, a status, headers, and a body, in the
    storage discipline :mod:`wreath.store` holds for all three of its callers.

    The table is not created for you; apply :meth:`schema_sql` as a migration,
    and drop expired rows with :meth:`purge_pass`.
    """

    __slots__ = ("_database", "_store")

    def __init__(
        self, database: Any, *, table: str = "wreath_idempotency", ttl: float = 24 * 60 * 60
    ) -> None:
        self._database = database
        self._store = PostgresStore(
            database,
            Keyed(
                table=table,
                columns=(
                    Column("status", "int"),
                    Column("headers", "jsonb"),
                    Column("body", "bytea"),
                ),
                # The store owns the lifetime: the window opens when the key is
                # claimed, so every worker computes the same deadline for it.
                ttl=float(ttl),
                claim=True,
                # An index on `expires`: the purge pass walks this table in
                # order of expiry, and a keyset walk with no index behind it
                # sorts the whole table once per chunk -- which is why the pass
                # refuses to be declared without one.
                index_stamp=True,
                prefix="wreath_idem",
            ),
        )
        # `expires` is deliberately absent from this DO UPDATE: the window runs
        # from the first attempt, not from whenever the handler happened to
        # finish, so a slow request cannot extend its own key.
        self._store.define(
            "store",
            self._store.upsert(
                values={
                    "key": "$1",
                    "status": "$2",
                    "headers": "$3::jsonb",
                    "body": "$4",
                    "expires": self._store.window(),
                },
                update={
                    "status": "excluded.status",
                    "headers": "excluded.headers",
                    "body": "excluded.body",
                },
            ),
        )

    def schema_sql(self) -> str:
        """DDL for the backing table. Apply it as a migration."""
        return self._store.schema_sql()

    async def reserve(self, key: str) -> tuple[str, Replay | None]:
        if await self._store.claim(key):
            return ("fresh", None)
        # Read from the primary that just refused the claim, not a replica: the
        # row we are asking about was written microseconds ago.
        row = await self._store.read(key)
        if row is None:
            # Deleted between the claim and the read -- a 5xx released it. Rare,
            # and the safe reading is "run it", the same as an evicted entry.
            return ("fresh", None)
        status = row[0]
        if status is None:
            return ("in_flight", None)
        headers = tuple(
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in (row[1] or ())
        )
        return ("done", (int(status), headers, bytes(row[2] or b"")))

    async def store(self, key: str, replay: Replay) -> None:
        status, headers, body = replay
        encoded = [
            [name.decode("latin-1"), value.decode("latin-1")] for name, value in headers
        ]
        await self._store.statement("store").execute(key, status, _json_list(encoded), body)

    async def release(self, key: str) -> None:
        await self._store.delete(key)

    def purge_pass(self, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that drops expired replays, chunk by chunk.

        This is the supported way to keep the table small. Hand it to the job
        runner and forget about it::

            jobs.drive(store.purge_pass(), cron="*/5 * * * *")

        Every property a purge loop is usually missing comes with it: one
        transaction per chunk rather than one for the whole delete, a durable
        cursor so a redeploy resumes instead of restarting, and pacing so the
        purge cannot be the reason a request waited for a connection. See
        :mod:`wreath.passes`.
        """
        from .._passes.stores import keyed_purge_pass

        return keyed_purge_pass(
            self._store.declaration, self._database,
            name=f"purge_{self._store.table}", chunk=chunk, **options,
        )

    async def purge(self) -> str:
        """Drop expired replays in **one unbounded statement**.

        Kept for a small table and for tests. On a table big enough to matter
        this is the long transaction a chunked pass exists to prevent -- it holds
        a snapshot open for as long as the delete runs, so nothing can be
        vacuumed in the meantime and the application stays slower for as long as
        the bloat takes to work back out. Prefer :meth:`purge_pass`.

        Safe either way: an expired key is already forgotten.
        """
        return await self._store.purge()


def _json_list(value: list) -> str:
    from .._json import dumps

    encoded = dumps(value)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


class IdempotencyMiddleware:
    """Replay the stored response for a repeated ``Idempotency-Key``."""

    global_scope = True
    __slots__ = ("_header", "_max_body_bytes", "_methods", "_store")

    def __init__(
        self,
        *,
        ttl: float = 24 * 60 * 60,
        max_entries: int = 4096,
        methods: Iterable[str] = ("POST", "PUT", "PATCH", "DELETE"),
        header: str = "idempotency-key",
        store: IdempotencyStore | None = None,
        max_body_bytes: int = 256 * 1024,
    ) -> None:
        self._store: IdempotencyStore = (
            store
            if store is not None
            else MemoryIdempotencyStore(ttl=ttl, max_entries=max_entries)
        )
        self._methods = frozenset(m.upper() for m in methods)
        self._header = header.lower()
        # A replay holds a whole response body for the TTL, in memory or in a
        # table, and nothing bounded it -- so an endpoint that returns something
        # large was an amplifier any authenticated caller could aim. Past the
        # cap the key is released and the request stays retryable, which is the
        # same outcome as an un-snapshottable body.
        self._max_body_bytes = max_body_bytes

    def _key(self, request: Request) -> str | None:
        """The store key for this request, or ``None`` to leave it unguarded.

        **Idempotency requires an authenticated principal.** The key is scoped
        by method, path, *and* principal, and an anonymous request has no
        principal to scope it by -- so every anonymous caller of one endpoint
        would share a single keyspace, and a key value the client chooses would
        be enough to be handed back another caller's stored response. On
        ``POST /signup`` or a password reset that response carries a new id, a
        token, an email. So an unauthenticated request is not guarded at all:
        the handler runs exactly as it would without this middleware, and the
        cost of a retry is that it re-runs, which is the price of not
        disclosing. There is deliberately no address-scoped fallback -- a proxy
        or a NAT would put unrelated callers back in one bucket.
        """
        if request.method not in self._methods:
            return None
        value = request.header(self._header)
        if not value:
            return None
        identity = request.identity
        if identity is None:
            return None
        # Hashed with the scope rather than concatenated, so an arbitrary user
        # key cannot reach another principal's entry by containing a delimiter,
        # and the stored key is a bounded width whatever the path length.
        return dedup_key(f"{request.method} {request.path} {identity.id}", value)

    async def action(self, request: Request):
        """Run after the pipeline has identified the caller, before the handler.

        Deliberately the ``action`` stage and not ``before``: a global ``before``
        hook runs at *ingress*, which is upstream of authentication, so
        ``request.identity`` is always None there -- and :meth:`_key` returns
        None for an anonymous request, so mounting this as an ingress hook made
        it silently guard nothing at all. ``RateLimitMiddleware`` refuses
        ``principal_key`` at construction for exactly this reason; this
        middleware has no such choice to refuse, so it moves to the stage where
        the principal exists. The ``after`` half still runs for every response,
        as it must.
        """
        key = self._key(request)
        if key is None:
            return None
        state, entry = await self._store.reserve(key)
        if state == "in_flight":
            conflict = ProblemResponse(
                status=409,
                detail="a request with this Idempotency-Key is still in progress",
            )
            # A bare 409 invites an immediate retry into the same conflict.
            conflict.headers.append((b"retry-after", b"1"))
            return conflict
        if state == "done" and entry is not None:
            status, headers, body = entry
            replay = Response(body, status=status, headers=list(headers))
            replay.headers.append((b"idempotency-replayed", b"true"))
            return replay
        request.state.idempotency_key = key
        return None

    async def after(self, request: Request, response):
        key = getattr(request.state, "idempotency_key", None) if request._state else None
        if key is None:
            return response
        if (
            response.status >= 500
            or response.status in _RETRYABLE_STATUSES
            or type(response).__name__ in _UNCACHEABLE_BODY
            or len(getattr(response, "body", b"")) > self._max_body_bytes
        ):
            # A transient failure (or an un-snapshottable body) stays retryable.
            await self._store.release(key)
            return response
        await self._store.store(
            key, (response.status, _replayable_headers(response.headers), response.body)
        )
        return response


__all__ = [
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "MemoryIdempotencyStore",
    "PostgresIdempotencyStore",
]
