"""Make unsafe requests replay-safe with an `Idempotency-Key` header.

A client that retries a `POST` (a dropped connection, an impatient user) must
not create two records. When a request carries an `Idempotency-Key`, this
middleware remembers the first response and replays it for any repeat of the
same key.

Opt-in per request (the header) and safe by construction:

* only unsafe methods are considered (`POST`/`PUT`/`PATCH`/`DELETE`),
* the key is scoped by method, path, and the authenticated principal, so two
  callers cannot collide on the same key value,
* an **unauthenticated** request is therefore not guarded at all: there is no
  principal to scope it by, and sharing one keyspace between anonymous callers
  would replay one caller's response to another,
* a concurrent duplicate (the first is still running) gets `409`,
* a `5xx` is not cached — a transient failure stays retryable.

**Two stores, and the difference matters.** The default
`MemoryIdempotencyStore` is bounded and in-process: it covers a retry that
lands on the worker that served the original, and nothing else. Behind a load
balancer that is most retries but not all, so a deployment that needs the
guarantee should share `PostgresIdempotencyStore` — one table, no Redis, and
the claim is a single statement so two workers cannot both believe they were
first.

Even then, remember what this middleware is: **response replay**. It saves the
work and keeps the answer consistent. What makes the *effect* happen once is a
unique index on something durable — the `key` you pass to
`wreath.jobs.JobRunner.enqueue()`, or a unique column on the row you write.
Compose the two and the guarantee holds end to end; see the
[exactly-once recipe](../cookbook/recipes/exactly-once.md).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from .._jobcore import dedup_key
from ..request import Request
from ..response import ProblemResponse, Response
from ..store import CLAIMED, Column, Keyed, MemoryStore, PostgresStore, Sql

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
        # `jsonb` cannot hold a NUL, so a header carrying one made the *store*
        # fail -- after the handler had already run its side effect, which is
        # the worst possible moment for this middleware to raise.
        and b"\x00" not in name
        and b"\x00" not in value
    )

#: A stored response: status, headers, body.
type Replay = tuple[int, tuple[tuple[bytes, bytes], ...], bytes]


class IdempotencyStore(Protocol):
    """Where first responses are kept so a retry can replay them.

    Three calls, always in this order. `reserve` claims the key and says whether
    this request owns it; the owner then either stores the response it produced
    or releases the key so a later attempt can own it. A key that is claimed and
    neither stored nor released stays claimed until its TTL expires, and every
    repeat of it is refused with 409 until then.
    """

    async def reserve(self, key: str) -> tuple[str, Replay | None]:
        """Claim `key` for this request.

        Returns `("fresh", None)` when this caller owns the key and should
        run the handler, `("in_flight", None)` when another request holds it,
        or `("done", replay)` when a response is already stored.

        Must be **atomic**: a read followed by a write lets two workers both
        conclude they were first, which is the one thing this middleware exists
        to prevent.
        """

    async def store(self, key: str, replay: Replay) -> None:
        """Record the response `key` produced, for the rest of the key's window.

        The window opened when `reserve` claimed the key, and storing does not
        move it, so a slow handler cannot extend its own key past the TTL.
        """
        ...

    async def release(self, key: str) -> None:
        """Forget `key`, so the next request carrying it runs the handler again.

        Called whenever the response must not be replayed — a 5xx, a status
        whose cause is expected to change, a body too large to snapshot. Not an
        error when the key is already gone.
        """
        ...


class MemoryIdempotencyStore:
    """Bounded, in-process, no external state. One worker's memory.

    **A limitation, not a configuration choice**: this store deduplicates only
    within the process that holds it. A retry routed to a second worker sees no
    claim and no stored response, so it runs the handler again. Enough on its
    own for a single-worker deployment or a sticky load balancer; behind
    anything else it is a fast path in front of the durable guarantee rather
    than the guarantee itself, and `PostgresIdempotencyStore` is the guarantee.

    The window runs from the first attempt, not from whenever the handler
    finished -- `wreath.store.MemoryStore` keeps the deadline a key was claimed
    with, so a slow request cannot extend its own key and the two stores honour
    a key for the same length of time.

    Args:
        ttl: Seconds a key is honoured, timed from the claim. Default one day.
        max_entries: Replays kept before the least recently used is evicted.
    """

    __slots__ = ("_store",)

    def __init__(self, *, ttl: float = 24 * 60 * 60, max_entries: int = 4096) -> None:
        self._store = MemoryStore(ttl=ttl, max_entries=max_entries)

    async def reserve(self, key: str) -> tuple[str, Replay | None]:
        """Claim `key` in this worker's memory.

        Returns `("fresh", None)` when this request owns the key and must run
        the handler, `("in_flight", None)` while another request *on this
        worker* holds it, or `("done", replay)` when a response is already
        stored. A key that expired or was evicted between the claim and the
        read reads as fresh, which is the safe answer.
        """
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
        """Keep `replay` under `key` for the remainder of the claimed window."""
        self._store.set(key, replay)

    async def release(self, key: str) -> None:
        """Drop `key` from this worker's memory. Not an error when it is gone."""
        self._store.delete(key)


class PostgresIdempotencyStore:
    """Replays in a shared table, so every worker honours every key.

    The claim is `wreath.store.PostgresStore.claim()` — one
    `INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expired RETURNING`, where
    a returned row **is** the claim. No owner column, no second round trip, no
    advisory lock, and no window in which two workers both believe they were
    first; the row lock the statement takes is the whole of the mutual
    exclusion. Everything else here is this middleware's payload, a status,
    headers, and a body, in the storage discipline `wreath.store` holds for all
    three of its callers.

    The table is not created for you; apply `schema_sql()` as a migration, and
    drop expired rows with `purge_pass()`.

    Args:
        database: The pool to run against, such as `app.postgres("main")`.
        table: Table holding the replays. Default `wreath_idempotency`.
        ttl: Seconds a key is honoured, timed from the claim. Default one day.
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
                    "status": Sql("excluded.status"),
                    "headers": Sql("excluded.headers"),
                    "body": Sql("excluded.body"),
                },
            ),
        )

    def component(self) -> Any:
        """This store's claim on the wreath schema.

        The table is **unqualified** and stays that way: moving it into the
        `wreath` schema is not additive, so a worker on the previous version
        would look for a name that had gone. Registered where the rows are.
        """
        return self._store.schema_claim("idempotency")

    @property
    def schema_database(self) -> Any:
        """The database `component()`'s tables belong to.

        The application never saw this store constructed -- a caller builds it
        and hands it to `IdempotencyMiddleware` -- so it cannot know which
        `app.postgres()` the tables go to unless the store says. This is that
        contract, and it is one name rather than a list of plausible ones:
        `Wreath._schema_database` reads exactly `schema_database`.
        """
        return self._database

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined. A derivation of
        `component()`."""
        return self._store.schema_sql()

    async def reserve(self, key: str) -> tuple[str, Replay | None]:
        """Claim `key` in the shared table, so every worker honours it.

        The same three answers as `MemoryIdempotencyStore.reserve()`, decided by
        the single claiming statement — a claimed row with no status yet is a
        request still in flight on some worker, which is what makes a concurrent
        duplicate a 409 across the whole deployment rather than one process.
        """
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
        """Write the response `key` produced into the shared table.

        Headers go in as latin-1 text pairs in `jsonb` and the body as `bytea`.
        The row's expiry is deliberately left alone, so the window still runs
        from the claim rather than from whenever the handler finished.
        """
        status, headers, body = replay
        encoded = [
            [name.decode("latin-1"), value.decode("latin-1")] for name, value in headers
        ]
        await self._store.statement("store").execute(key, status, _json_list(encoded), body)

    async def release(self, key: str) -> None:
        """Delete `key`'s row. Not an error when it is already gone."""
        await self._store.delete(key)

    def purge_pass(self, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that drops expired replays, chunk by chunk.

        This is the supported way to keep the table small. Hand it to the job
        runner and forget about it:

        ```python
        jobs.drive(store.purge_pass(), cron="*/5 * * * *")
        ```

        Every property a purge loop is usually missing comes with it: one
        transaction per chunk rather than one for the whole delete, a durable
        cursor so a redeploy resumes instead of restarting, and pacing so the
        purge cannot be the reason a request waited for a connection. See
        `wreath.passes`. Remaining keyword options are the pass's own — `within`,
        `shift`, `pace`, `schema`, `tenant`.

        Args:
            chunk: Rows deleted per transaction. Default 1000.
        """
        from .._passes.stores import keyed_purge_pass

        return keyed_purge_pass(
            self._store.declaration,
            name=f"purge_{self._store.table}", chunk=chunk, **options,
        )

    async def purge(self) -> str:
        """Drop expired replays in **one unbounded statement**.

        Kept for a small table and for tests. On a table big enough to matter
        this is the long transaction a chunked pass exists to prevent -- it holds
        a snapshot open for as long as the delete runs, so nothing can be
        vacuumed in the meantime and the application stays slower for as long as
        the bloat takes to work back out. Prefer `purge_pass()`.

        Safe either way: an expired key is already forgotten.
        """
        return await self._store.purge()


def _json_list(value: list) -> str:
    from .._json import dumps

    encoded = dumps(value)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


def _idempotency_scope(*parts: str) -> str:
    """Length-frame scope fields so attacker-controlled boundaries cannot move."""
    return "".join(f"{len(part.encode('utf-8'))}:{part}" for part in parts)


class IdempotencyMiddleware:
    """Replay the stored response for a repeated `Idempotency-Key`.

    **An unauthenticated request is not guarded at all.** The handler runs
    exactly as it would without this middleware, and a retry re-runs it. That
    refusal is deliberate, not an oversight: the key is scoped by the
    authenticated principal, an anonymous caller has none, and one shared
    anonymous keyspace over a key value the *client* chooses would let one
    caller be handed another caller's stored response — on `POST /signup` or a
    password reset, a response carrying a new id, a token, an email. There is no
    address-scoped fallback either, because a proxy or a NAT puts unrelated
    callers back into one bucket. A key sent by an anonymous caller is counted
    in `ignored` and named on the way out with an
    `Idempotency-Ignored: unauthenticated` response header, so the client is
    told rather than left to assume.

    Only `POST`, `PUT`, `PATCH` and `DELETE` are considered, and only when the
    request carries the header. The store key is a blake2s digest over
    length-framed method, path, principal type, principal id, and the header
    value. Length framing keeps spaces or other delimiters inside one component,
    and hashing keeps the stored key bounded whatever the path.

    The first request holding a key claims it, runs the handler, and stores the
    response. A repeat arriving while the first is still running is refused with
    `409` and `Retry-After: 1`, as an RFC 9457 `application/problem+json` body —
    not a replay, and not a wait. A repeat arriving after the first finished is
    answered from the store with the original status, the stored headers, and
    `Idempotency-Replayed: true`. `Set-Cookie` is never stored or replayed,
    because a stored copy re-issues credentials the server has already rotated
    away from.

    Nothing is stored for a `5xx`, for a status whose cause is expected to
    change on a retry (401, 403, 408, 409, 423, 425, 429, 449), for a streaming
    or file response, or for a body over `max_body_bytes`. The key is released
    instead and the request stays retryable. A handler that raises has become a
    500 by the time this middleware sees it, so it releases the key too — a
    failed attempt never locks its key for the day.

    It runs at the `action` stage, after the pipeline has identified the caller
    and before the handler, rather than at ingress where a global `before` hook
    would run upstream of authentication and so guard nothing.

    Two counters are worth alerting on. `ignored` is requests that sent a key
    this middleware did not act on; silence there is how "idempotency works in
    staging" happens. `conflicts` is requests refused with 409, and a climbing
    count with no matching traffic means keys are stuck — a process that died
    between reserving a key and storing its response leaves it claimed for the
    whole TTL, and `release()` is the lever.

    Args:
        ttl: Seconds a key is honoured, for the default store only. Default one day.
        max_entries: Replays the default in-process store keeps. Default 4096.
        methods: Methods eligible for guarding, upper-cased for you.
        header: Request header carrying the key. Default `idempotency-key`.
        store: Where replays live. Default a per-process `MemoryIdempotencyStore`.
        max_body_bytes: Largest response body stored; above it the key is released.
    """

    global_scope = True
    __slots__ = (
        "_header", "_max_body_bytes", "_methods", "_store", "conflicts", "ignored",
    )

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
        #: Requests that sent a key this middleware did not act on -- an
        #: unauthenticated caller, whose key cannot be scoped to a principal.
        #: Silence there is how "idempotency works in staging" happens.
        self.ignored = 0
        #: Requests refused with 409 because the key was already in flight.
        #: A climbing count with no matching traffic means keys are *stuck*: a
        #: process that died between reserving one and storing its response
        #: leaves it claimed for the whole TTL. `release()` is the lever.
        self.conflicts = 0

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """The store this middleware delegates its tables to.

        It owns no tables itself, so it answers with the store it was given
        rather than forwarding a `component()`. Answering at all is the point:
        `Wreath.schema_components` walks middleware and asks each holder this
        question, and this class used to expose neither it nor `component()`,
        so a `PostgresIdempotencyStore`'s `wreath_idempotency` table was
        emitted by `wreath schema sql` and created by nothing.

        The default in-process store is returned too and contributes nothing --
        it has no `component()`, and the walk asks rather than assumes.
        """
        return (self._store,)

    def describe(self) -> Any:
        """The key this middleware reads, and the 409 a concurrent repeat gets.

        Scoped to the four methods `action` actually considers, so a generated
        client does not send a key on a `GET` that would ignore it.
        """
        from ..openapi import ResponseSpec
        from .base import HeaderSpec, MiddlewareContract

        return MiddlewareContract(
            request_headers=(
                HeaderSpec(
                    "Idempotency-Key",
                    description=(
                        "Client-chosen key making this request replay-safe. The "
                        "first request holding it claims it; a repeat is answered "
                        "from the store."
                    ),
                ),
            ),
            response_headers=(
                (
                    None,
                    HeaderSpec(
                        "Idempotency-Replayed",
                        description="`true` when this response came from the store.",
                    ),
                ),
                (
                    None,
                    HeaderSpec(
                        "Idempotency-Ignored",
                        description=(
                            "Present when a key was sent but not acted on -- "
                            "`unauthenticated` for an anonymous caller."
                        ),
                    ),
                ),
                (409, HeaderSpec("Retry-After", description="Whole seconds; always 1.")),
            ),
            responses=(
                (
                    409,
                    ResponseSpec(
                        description=(
                            "A request with this Idempotency-Key is still running."
                        ),
                        media_type="application/problem+json",
                    ),
                ),
            ),
            methods=frozenset({"POST", "PUT", "PATCH", "DELETE"}),
            behaviours=frozenset({"idempotency-key"}),
        )

    def _key(self, request: Request) -> str | None:
        """The store key for this request, or None to leave it unguarded.

        **Idempotency requires an authenticated principal.** The key is scoped
        by method, path, *and* principal, and an anonymous request has no
        principal to scope it by -- so every anonymous caller of one endpoint
        would share a single keyspace, and a key value the client chooses would
        be enough to be handed back another caller's stored response. On
        `POST /signup` or a password reset that response carries a new id, a
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
        # Each scope component is length-framed before hashing. A plain
        # `method path id` string lets a space in the decoded path and a space
        # in the principal id shift the boundary and replay across identities.
        # Principal type participates too: `User:42` and `Service:42` are not
        # the same authenticated principal.
        scope = _idempotency_scope(
            request.method,
            request.path,
            str(getattr(identity, "type", "User")),
            str(identity.id),
        )
        return dedup_key(scope, value)

    async def action(self, request: Request):
        """Claim the key, or answer the request from the store.

        Returns None to let the handler run, a 409 problem response when the key
        is already in flight, or the replayed response when one is stored.

        Deliberately the `action` stage and not `before`: a global `before` hook
        runs at *ingress*, which is upstream of authentication, so
        `request.identity` is always None there -- and `_key` returns None for
        an anonymous request, so mounting this as an ingress hook made it
        silently guard nothing at all. `RateLimitPolicy` refuses
        `principal_key` at construction for exactly this reason; this middleware
        has no such choice to refuse, so it moves to the stage where the
        principal exists. The `after` half still runs for every response, as it
        must.
        """
        key = self._key(request)
        if key is None:
            if request.method in self._methods and request.header(self._header):
                # A key was sent and is not being honoured. Counted here and
                # named on the response below, so the client is told rather than
                # left to assume.
                self.ignored += 1
                request.state.idempotency_ignored = True
            return None
        state, entry = await self._store.reserve(key)
        if state == "in_flight":
            conflict = ProblemResponse(
                status=409,
                detail="a request with this Idempotency-Key is still in progress",
            )
            # A bare 409 invites an immediate retry into the same conflict.
            conflict.headers.append((b"retry-after", b"1"))
            self.conflicts += 1
            return conflict
        if state == "done" and entry is not None:
            status, headers, body = entry
            replay = Response(body, status=status, headers=list(headers))
            replay.headers.append((b"idempotency-replayed", b"true"))
            return replay
        request.state.idempotency_key = key
        return None

    async def release(self, key: str) -> None:
        """Forget `key`, so a stuck claim can be cleared without waiting a day.

        A process that dies between reserving a key and storing its response
        leaves the key claimed until it expires, and every retry gets 409 until
        then. There was no way to say "that one is not coming back". The
        argument is the *store* key, the hashed value `request.state` carries as
        `idempotency_key`, not the header the client sent.
        """
        await self._store.release(key)

    async def after(self, request: Request, response):
        """Store the response under the claimed key, or release the key.

        Runs for every response, including one produced by a handler that
        raised, which is how a failure releases its key instead of caching
        itself for the day. A request that claimed no key passes straight
        through, except that an ignored key is named on the response here.
        """
        # Read through the public state API rather than the request's private
        # attribute: the state object is materialized lazily, and `get` is the
        # supported way to ask without forcing it.
        if request.state.get("idempotency_ignored"):
            headers = getattr(response, "headers", None)
            if headers is not None:
                headers.append((b"idempotency-ignored", b"unauthenticated"))
            return response
        key = request.state.get("idempotency_key")
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
