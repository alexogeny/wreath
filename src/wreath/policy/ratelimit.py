"""First-class token-bucket request policy with a pluggable store.

A token bucket rather than a fixed window: a window counter lets a client spend
its whole allowance in the last instant of one window and again in the first
instant of the next, delivering double the intended rate across the boundary.
A bucket refills continuously, so the average rate holds over any interval while
still admitting a configured burst.

The default store keeps buckets in this process:

```python
app.configure_http_policy(HttpPolicy(
    rate_limit=RateLimitPolicy(limit=60, window=60.0),
))
```

That is per-worker: with four workers the effective limit is four times the
configured one. When the limit must hold across workers, hand it a store backed
by a table the workers share:

```python
store = PostgresRateLimitStore(app.postgres("main"))
app.configure_http_policy(HttpPolicy(
    rate_limit=RateLimitPolicy(limit=60, window=60.0, store=store),
))
```

By default each client IP gets its own bucket, read from `scope["client"]`.
Behind a proxy that address is the proxy's, and every client would share one
bucket -- add `ProxyPolicy` ahead of this one so the scope carries
the real client.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any, Protocol

from .._auth.models import qualified_identity_value
from .._native import _core
from ..request import Request
from ..response import ProblemResponse
from ..store import ALIAS, Column, Keyed, PostgresStore, Sql

TokenBucket: Any = _core.TokenBucket


class RateLimitStore(Protocol):
    """Where buckets live.

    `configure` is called once, when the policy is constructed, so the
    policy has a single owner and the store can prepare statements before
    startup. A store may additionally expose a synchronous `try_acquire` with
    the same contract as `acquire`; the policy binds to it and skips
    awaiting when it exists.
    """

    def configure(self, capacity: float, rate: float) -> None:
        """Fix the policy this store enforces, before any request arrives.

        Called by `RateLimitPolicy.__init__`, not at startup, so a store can
        prepare statements and a misconfiguration is a construction-time error.
        `capacity` is the bucket size in tokens (the burst) and `rate` is tokens
        per second. Both shipped stores refuse a second call, because sharing
        one store between two policies means sharing one keyspace.
        """
        ...

    async def acquire(self, key: str, cost: float, now: float) -> float:
        """Return 0.0 when allowed, else the seconds until enough tokens.

        `now` is a monotonic reading from the caller. A store that owns a
        clock its workers agree on (Postgres does) should ignore it.
        """
        ...


class MemoryRateLimitStore:
    """In-process buckets. Fast and dependency-free, but per-worker.

    **A limitation worth stating plainly**: a limit of N per window is N *per
    worker*, not per cluster, and nothing here observes the other workers. Four
    workers admit four times the configured rate. Use `PostgresRateLimitStore`
    when the number has to mean something across a deployment.

    Not a `wreath.store.MemoryStore`: a bucket is not a stored payload but an
    arithmetic state that every read advances, and the whole refill-and-consume
    step lives in `TokenBucket` (native, with its own bounded table) so the hot
    path stays one call.

    Args:
        max_entries: Distinct keys tracked. The fullest bucket is evicted at the ceiling.
    """

    __slots__ = ("_bucket", "_max_entries", "_policy")

    def __init__(self, *, max_entries: int = 10000) -> None:
        self._max_entries = max_entries
        self._bucket: Any = None
        self._policy: tuple[float, float] | None = None

    def configure(self, capacity: float, rate: float) -> None:
        """Build the bucket table for this policy. Once, and only once.

        Raises `ValueError` on any second call, even one repeating the same
        numbers.
        """
        # Any second call, not merely a conflicting one -- matching
        # `PostgresRateLimitStore`, so the two halves of one policy cannot
        # disagree about what sharing a store means. A second `configure` is
        # always two policies over one keyspace, and identical numbers do not
        # make that harmless: requests to one route consume the other's budget.
        # Accepting the same-policy case was worse than laxer, because it
        # rebuilt the `TokenBucket` and so silently reset every caller's
        # consumption -- a throttled client let straight back through.
        if self._policy is not None:
            raise ValueError(
                "this store is already configured; give each RateLimitPolicy its own store"
            )
        self._policy = (capacity, rate)
        self._bucket = TokenBucket(capacity=capacity, rate=rate, max_entries=self._max_entries)

    @property
    def tracked(self) -> int:
        """Keys with a live bucket right now, and 0 before `configure`."""
        return 0 if self._bucket is None else self._bucket.tracked

    def try_acquire(self, key: str, cost: float, now: float) -> float:
        """Spend `cost` tokens without awaiting anything.

        Returns 0.0 when the request is allowed, else the seconds until the
        bucket holds enough. Nothing is spent on a refusal. `now` is a monotonic
        reading. This is the stage `RateLimitPolicy` binds to for a local
        store, so an allowed request costs no coroutine at all.
        """
        return float(self._bucket.acquire(key, now, cost))

    async def acquire(self, key: str, cost: float, now: float) -> float:
        """`try_acquire` behind the awaiting `RateLimitStore` protocol."""
        return self.try_acquire(key, cost, now)

    def clear(self) -> None:
        """Forget every bucket, restoring a full allowance to every key.

        Safe as a reset between tests. In production it is an amnesty, not a
        purge -- a throttled caller is let straight back through.
        """
        if self._bucket is not None:
            self._bucket.clear()


class PostgresRateLimitStore:
    """Buckets in a shared table, so one limit covers every worker.

    The whole refill-and-consume decision is one `INSERT ... ON CONFLICT DO
    UPDATE`. That statement takes a row lock, so concurrent workers serialize
    per key and the limit holds exactly; two statements or a read-then-write
    would race and overspend. The database clock drives the refill, so the
    workers cannot disagree about how much time has passed.

    `updated` is a last-touched mark rather than a deadline, which is what makes
    this store's ageing different from the other two in `wreath.store`: a bucket
    never expires, it refills, and the elapsed time since `updated` is the
    arithmetic the statement above does. Only an idle purge can retire one.

    The table is not created for you; apply `schema_sql()` as a migration. Rows
    are small and bounded by distinct keys -- run `purge_pass()` periodically to
    drop idle ones.

    Args:
        database: The pool to run against, such as `app.postgres("main")`.
        table: Table holding the buckets. Default `wreath_rate_limit`.
    """

    __slots__ = ("_capacity", "_database", "_policy", "_rate", "_store")

    def __init__(self, database: Any, *, table: str = "wreath_rate_limit") -> None:
        self._database = database
        self._store = PostgresStore(
            database,
            Keyed(
                table=table,
                columns=(
                    Column("tokens", "double precision", null=False),
                    Column("allowed", "boolean", null=False),
                ),
                stamp="updated",
                deadline=False,
                # An index on `updated`: the purge pass walks this table in
                # last-touched order, and a keyset walk needs an index under it
                # or it sorts the whole table once per chunk.
                index_stamp=True,
                prefix="wreath_rate_limit",
            ),
        )
        # $1 key, $2 capacity, $3 cost, $4 rate. Nothing about the policy is
        # baked into the text -- capacity and rate are bound per call -- so the
        # statement is built here and `configure` only records the numbers.
        # The refill expression is repeated because ON CONFLICT ... DO UPDATE
        # has nowhere to bind an alias for it; Postgres evaluates it once per
        # row regardless.
        refill = (
            f"LEAST($2::float8, {ALIAS}.tokens + "
            f"EXTRACT(EPOCH FROM (clock_timestamp() - {ALIAS}.updated)) * $4::float8)"
        )
        self._store.define(
            "acquire",
            self._store.upsert(
                values={
                    "key": "$1",
                    "tokens": Sql("$2::float8 - $3::float8"),
                    "allowed": Sql("true"),
                    "updated": Sql("clock_timestamp()"),
                },
                # `allowed` is written as well as returned: when a bucket is
                # short, the stored token count alone cannot say whether this
                # call consumed or was refused -- both land in [0, cost).
                update={
                    "tokens": Sql(
                        f"{refill}\n"
                        f"           - CASE WHEN {refill} >= $3::float8 "
                        "THEN $3::float8 ELSE 0::float8 END"
                    ),
                    "allowed": Sql(f"{refill} >= $3::float8"),
                    "updated": Sql("clock_timestamp()"),
                },
                returning="tokens, allowed",
            ),
        )
        self._capacity = 0.0
        self._rate = 0.0
        self._policy: tuple[float, float] | None = None

    def component(self) -> Any:
        """This store's claim on the wreath schema.

        The table is **unqualified** and stays that way: moving it into the
        `wreath` schema is not additive, so a worker on the previous version
        would look for a name that had gone. Registered where the rows are.
        """
        return self._store.schema_claim("ratelimit")

    @property
    def schema_database(self) -> Any:
        """The database `component()`'s tables belong to.

        The application never saw this store constructed -- a caller builds it
        and hands it to `RateLimitPolicy` -- so it cannot know which
        `app.postgres()` the tables go to unless the store says. This is that
        contract, and it is one name rather than a list of plausible ones:
        `Wreath._schema_database` reads exactly `schema_database`.
        """
        return self._database

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined. A derivation of
        `component()`."""
        return self._store.schema_sql()

    def configure(self, capacity: float, rate: float) -> None:
        """Record the policy bound into every `acquire`. Once, and only once.

        The statement text carries no policy -- capacity and rate are bound per
        call -- so this only stores the numbers. Raises `ValueError` on any
        second call, even one repeating the same numbers, because a second
        caller means two policies sharing one keyspace.
        """
        if self._policy is not None:
            raise ValueError(
                "this store is already configured; give each RateLimitPolicy its own store"
            )
        self._policy = (capacity, rate)
        self._capacity = capacity
        self._rate = rate

    async def acquire(self, key: str, cost: float, now: float) -> float:
        """Spend `cost` tokens from the shared bucket for `key`.

        Returns 0.0 when the request is allowed, else the seconds until the
        bucket holds enough. One round trip, and `now` is ignored -- the
        database clock is the only one every worker agrees on. Requires
        `configure` to have run.
        """
        # `now` is ignored: Postgres is the clock, so workers cannot disagree.
        row = await self._store.statement("acquire").fetchrow(key, self._capacity, cost, self._rate)
        tokens = float(row[0])
        if row[1]:
            return 0.0
        return (cost - tokens) / self._rate

    def purge_pass(self, idle_seconds: float, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that retires buckets untouched for *idle_seconds*.

        The supported way to keep the table small:

        ```python
        jobs.drive(store.purge_pass(3600), cron="17 * * * *")
        ```

        It walks in last-touched order behind a frontier the database clock
        re-derives each cycle, one transaction per chunk, paced. See
        `wreath.passes`. Remaining keyword options are the pass's own --
        `within`, `shift`, `pace`, `schema`, `tenant`.

        Args:
            idle_seconds: Seconds a bucket must sit untouched before it is dropped.
            chunk: Rows deleted per transaction. Default 1000.
        """
        from .._passes.stores import keyed_purge_pass

        return keyed_purge_pass(
            self._store.declaration,
            name=f"purge_{self._store.table}",
            after=float(idle_seconds),
            chunk=chunk,
            **options,
        )

    async def purge(self, idle_seconds: float) -> str:
        """Drop buckets untouched for *idle_seconds*, in **one unbounded statement**.

        Safe -- an idle bucket has refilled to capacity, so forgetting it grants
        nothing it would not have granted anyway -- but on a table big enough to
        matter this is the long transaction a chunked pass exists to prevent.
        Prefer `purge_pass()`.
        """
        return await self._store.purge(idle_seconds)


def principal_key(request: Request) -> str | None:
    """Key a limit on *who is asking* rather than where they connected from.

    Behind a proxy or a carrier NAT, the client address lumps unrelated callers
    into one bucket and lets one caller earn a fresh allowance per device. For
    an authenticated API the principal is the honest key.

    An authenticated caller is keyed on `type:id` from `request.identity` — the
    same identity the Cedar policies authorize with, so there is one answer to
    "who is this" rather than two that can disagree.

    Anonymous callers fall back to their address, because a single shared
    "anonymous" bucket is a denial of service you inflict on yourself. The
    `ip:` prefix keeps an address from ever colliding with a principal id. That
    fallback is only as trustworthy as `scope["client"]`, which behind a proxy
    is the proxy's address and puts every anonymous caller in one bucket — put
    `ProxyPolicy` ahead of the limiter so the scope carries the real
    client. Returns None when there is no identity and no client address, which
    lands the request in the limiter's shared unkeyed bucket.

    **Must run after authentication.** `request.identity` is set during route
    authorization, so a limiter using this key has to be route policy --
    a global stage runs at ingress, before anyone has been identified.
    `RateLimitPolicy` refuses this key at construction for that reason; use
    `TieredRateLimitPolicy`.
    """
    identity = request.identity
    if identity is not None:
        identity_id = qualified_identity_value(
            str(getattr(identity, "namespace", "")), str(identity.id)
        )
        return f"{identity.type}:{identity_id}"
    client = request.client
    return f"ip:{client[0]}" if client else None


def _client_key(request: Request) -> str | None:
    client = request.client
    if not client:
        return None
    return str(client[0])


class RateLimitPolicy:
    """Reject requests that exceed a per-key token-bucket allowance.

    Each key owns a bucket holding `burst` tokens (`limit` when no burst is
    given) that refills continuously at `limit / window` tokens per second. A
    request spends `cost`; when the bucket is short, the request is refused and
    nothing is spent. Continuous refill rather than a window counter is the
    point — a window lets a caller spend its whole allowance in the last instant
    of one window and again in the first instant of the next.

    A refused request is `429` as an RFC 9457 `application/problem+json` body,
    carrying `Retry-After` in whole seconds (rounded up, and never 0),
    `X-RateLimit-Limit`, `RateLimit-Policy`, and `X-RateLimit-Remaining: 0`.
    Those headers ride on the refusal only. Advertising the policy on every
    response needs a global `_egress` stage, which `wreath-request-trace` priced at
    +18 boundary crossings per request, to carry a header that matters when a
    request is refused. The remaining allowance stays absent from an allowed
    response for a second reason: neither store reports it — `acquire` answers
    "wait this long", not "you have this many left" — and a number invented here
    would be worse than no number.

    This is a **global stage**, so it runs at ingress, before authentication.
    Keying it on `principal_key` is refused at construction for that reason;
    `TieredRateLimitPolicy` is the per-principal limiter. The default key is
    the client address from `scope["client"]`, which behind a proxy is the
    proxy's — put `ProxyPolicy` ahead of this one. A request whose
    key function returns None lands in one shared `UNKEYED` bucket rather than
    skipping the limit, because a limiter that skips what it cannot name is not
    a limiter; use `exempt` to let a request past deliberately.

    `throttled` counts refusals. Without it, a limiter that keys everyone the
    same — the default key behind a proxy — looks exactly like one with nothing
    to do.

    Args:
        limit: Requests admitted per `window`. Must be positive.
        window: Seconds the limit is measured over. Default 60.
        burst: Bucket capacity in requests. Defaults to `limit`; never less than `cost`.
        cost: Tokens one request spends. Default 1.0.
        store: Where buckets live. Default a per-process `MemoryRateLimitStore`.
        key: The bucket key for a request. Default the client address.
        exempt: A request it answers True for is not limited at all.

    Raises:
        ValueError: Non-positive or non-finite limit, window, cost, or burst;
            burst below cost; `key=principal_key`.
    """

    _default_key = staticmethod(_client_key)

    __slots__ = (
        "_cost",
        "_exempt",
        "_key",
        "_policy_headers",
        "_quota",
        "_store",
        "_try_acquire",
        "_ingress",
        "_ingress_sync",
        "throttled",
    )

    def __init__(
        self,
        *,
        limit: int,
        window: float = 60.0,
        burst: int | None = None,
        cost: float = 1.0,
        store: RateLimitStore | None = None,
        key: Callable[[Request], str | None] | None = None,
        exempt: Callable[[Request], bool] | None = None,
        quota: Any = None,
        _route_scoped: bool = False,
    ) -> None:
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("limit must be positive and finite")
        if not math.isfinite(window) or window <= 0.0:
            raise ValueError("window must be positive and finite")
        if not math.isfinite(cost) or cost <= 0.0:
            raise ValueError("cost must be positive and finite")
        capacity = float(limit if burst is None else burst)
        if not math.isfinite(capacity):
            raise ValueError("burst must be positive and finite")
        if capacity < cost:
            raise ValueError("burst must be at least the per-request cost")
        if key is principal_key and not _route_scoped:
            raise ValueError(
                "RateLimitPolicy is a global stage and runs before "
                "authentication, so it cannot key on the principal; use "
                "TieredRateLimitPolicy (route policy) for per-principal "
                "limits, and keep this one keyed on the address for ingress"
            )
        if quota is not None and not _route_scoped:
            # Same reasoning as `principal_key` above, and the same failure: a
            # quota is metered per principal, so at ingress there is nobody to
            # meter. `QuotaMeter.key` would answer None for every request and
            # the quota would count nothing at all while appearing configured --
            # which is worse than refusing, because the allowance looks enforced
            # right up until the invoice.
            raise ValueError(
                "RateLimitPolicy is a global stage and runs before "
                "authentication, so it has no principal to meter a quota "
                "against; use TieredRateLimitPolicy (route policy) for "
                "quotas"
            )
        selected = store if store is not None else MemoryRateLimitStore()
        selected.configure(capacity, limit / window)
        # Attached to a *refusal* only. Advertising the policy on every response
        # meant a global `_egress` stage, and `wreath-request-trace` priced that at
        # +18 boundary crossings per request -- real work on every successful
        # request to carry a header that matters when one is refused. The 429
        # already exists, so these are free there.
        # The *remaining* allowance stays absent even then for an allowed
        # request: neither store reports it (`acquire` answers "wait this long",
        # not "you have this many left"), and a number invented here would be
        # worse than no number. Exposing a real one needs a `TokenBucket` API
        # change.
        self._policy_headers = (
            (b"x-ratelimit-limit", str(limit).encode("ascii")),
            (b"ratelimit-policy", f"{limit};w={window:g}".encode("ascii")),
        )
        self._store = selected
        self._cost = cost
        self._key = key if key is not None else _client_key
        #: Requests this limiter refused. Without it, a limiter keying everyone
        #: the same -- see `_client_key` behind a proxy -- looks exactly like one
        #: with nothing to do.
        self.throttled = 0
        self._exempt = exempt
        #: The quota metered in this same stage, or None. One stage rather than a
        #: second policy, because two independent limiters on one request is
        #: how a 429 comes back contradicting a 402: whichever refuses, exactly
        #: one refusal is built and it is built here.
        self._quota = quota
        # Resolved once rather than branching and re-looking-up per request. A
        # synchronous store also skips a coroutine on the hot path; the memory
        # store is exactly that, so it exposes _ingress_sync (fused, no await)
        # while a remote store keeps the awaiting ingress stage. A quota whose
        # store awaits forces the awaiting stage even behind a local bucket --
        # the pair is one decision and cannot be half-synchronous.
        self._try_acquire: Any = getattr(selected, "try_acquire", None)
        awaiting = self._try_acquire is None or (quota is not None and quota.awaits)
        if awaiting:
            self._ingress = self._before_remote
            self._ingress_sync = None
        else:
            self._ingress = None
            self._ingress_sync = self._before_local_sync

    #: The bucket a request lands in when the key function cannot name one.
    #: Shared by every such request on purpose: a limiter that *skips* the
    #: request instead is not a limiter, and "no client address in the scope"
    #: (a unix socket, an unusual server, a proxy chain that would not parse) is
    #: exactly the condition an ingress limiter exists to survive. Use `exempt`
    #: to let a request past deliberately.
    UNKEYED = "\x00unkeyed"

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """The store this limiter delegates its tables to.

        A limiter owns no tables itself, so it answers with the store it was
        given rather than forwarding a `component()`. Answering at all is the
        point: `Wreath.schema_components` walks policy and asks each holder
        this question, so a `PostgresRateLimitStore` contributes its
        `wreath_rate_limit` table.

        The default in-process store is returned too and contributes nothing --
        it has no `component()`, and the walk asks rather than assumes, so
        filtering here would be a second copy of that test.

        A metered quota's store is included for the same reason the limiter's
        is: a `PostgresQuotaStore` puts a `wreath_quota` table in a database, and
        a table emitted by `wreath schema sql` and created by nothing is exactly
        the defect this property was added to fix.
        """
        if self._quota is None:
            return (self._store,)
        return (self._store, self._quota.store)

    def describe(self) -> Any:
        """The 429 this limiter can answer, and the headers it puts on it.

        Derived from `self._policy_headers` -- the same tuple `_limited`
        appends to the refusal -- so the document cannot drift from the wire.
        A second format string here would be a second source of truth, and the
        two would agree only until someone changed one of them.
        """
        from ..openapi import ResponseSpec
        from .base import HeaderSpec, PolicyContract

        policy = {
            name.decode("ascii"): value.decode("ascii") for name, value in self._policy_headers
        }
        return PolicyContract(
            responses=(
                (
                    429,
                    ResponseSpec(
                        description="Rate limit exceeded",
                        media_type="application/problem+json",
                    ),
                ),
            ),
            response_headers=(
                (
                    429,
                    HeaderSpec(
                        "Retry-After",
                        description="Whole seconds to wait before retrying; never 0.",
                    ),
                ),
                (
                    429,
                    HeaderSpec(
                        "X-RateLimit-Limit",
                        description="Requests permitted per window.",
                        const=policy.get("x-ratelimit-limit"),
                    ),
                ),
                (
                    429,
                    HeaderSpec(
                        "RateLimit-Policy",
                        description="The configured policy, as limit;w=window.",
                        const=policy.get("ratelimit-policy"),
                    ),
                ),
                (
                    429,
                    HeaderSpec(
                        "X-RateLimit-Remaining",
                        description="Always 0 on a refusal.",
                        const="0",
                    ),
                ),
            ),
            behaviours=frozenset({"retry-after"}),
        )

    def _identify(self, request: Request) -> str | None:
        if self._exempt is not None and self._exempt(request):
            return None
        return self._key(request) or self.UNKEYED

    def _limited(self, retry_after: float) -> ProblemResponse:
        response = ProblemResponse(
            status=429, title="Too Many Requests", detail="Rate limit exceeded"
        )
        # Retry-After is whole seconds, and 0 would invite an instant retry.
        seconds = max(1, math.ceil(retry_after))
        response.headers.append((b"retry-after", str(seconds).encode("ascii")))
        response.headers.extend(self._policy_headers)
        # Remaining *is* known here: the request was refused, so there is none.
        response.headers.append((b"x-ratelimit-remaining", b"0"))
        self.throttled += 1
        return response

    async def admit_key(self, key: str) -> ProblemResponse | None:
        """Charge non-HTTP work against this policy's existing keyed bucket."""
        if not isinstance(key, str) or not key:
            raise ValueError("rate-limit key must be a non-empty string")
        retry_after = (
            self._try_acquire(key, self._cost, monotonic())
            if self._try_acquire is not None
            else await self._store.acquire(key, self._cost, monotonic())
        )
        return self._limited(retry_after) if retry_after > 0.0 else None

    # The rate limit is decided *before* the quota, deliberately. A throttled
    # request did no work, so charging its cost against a monthly allowance
    # would bill a caller for requests the server refused -- and the meter is the
    # one signal that has to reconcile with an invoice. The consequence, stated
    # so nobody reads it as a bug: a caller who is both throttled and out of
    # quota is told to slow down first, and learns about the quota on the retry.
    # One answer either way, which is the coherence the pairing exists for.

    def _before_local_sync(self, request: Request) -> Any | None:
        key = self._identify(request)
        if key is None:
            return None
        retry_after = self._try_acquire(key, self._cost, monotonic())
        if retry_after > 0.0:
            return self._limited(retry_after)
        quota = self._quota
        return None if quota is None else quota.spend_sync(request)

    async def _before_remote(self, request: Request) -> Any | None:
        key = self._identify(request)
        if key is None:
            return None
        # `acquire` unconditionally, even for a local store that also offers the
        # synchronous `try_acquire`. This stage is only chosen when something on
        # it awaits, and `MemoryRateLimitStore.acquire` is `try_acquire` behind
        # the protocol -- so branching here would save one coroutine on a path
        # that is already making a database round trip for the quota. A branch
        # whose saving is noise beside the work beside it is a survivor waiting
        # to happen, not an optimisation.
        retry_after = await self._store.acquire(key, self._cost, monotonic())
        if retry_after > 0.0:
            return self._limited(retry_after)
        quota = self._quota
        if quota is None:
            return None
        # `spend` unconditionally, for the reason `acquire` is unconditional
        # above: it is correct for a local store too -- `MemoryQuotaStore.spend`
        # is `try_spend` behind the protocol -- and branching would save one
        # coroutine on a stage that is only reached because something else on it
        # awaits. `QuotaMeter.awaits` decides which *stage* runs, once, at
        # construction; it has no second job here.
        return await quota.spend(request)


class TieredRateLimitPolicy:
    """A different allowance per plan, keyed on the principal.

    An authenticated API rarely wants one limit for everybody: the free tier and
    the enterprise tier are different products. The tier comes from the caller's
    roles -- the same roles the Cedar policies authorize with -- so there is one
    answer to "who is this" rather than two that can disagree:

    ```python
    app.configure_http_policy(HttpPolicy(principal_rate_limit=TieredRateLimitPolicy(
        tiers={"pro": (600, 60.0), "enterprise": (10_000, 60.0)},
        default=(60, 60.0),
    )))
    ```

    Each entry is `(limit, window_seconds)`. A caller holding more than one
    named role gets the **most generous** of them, ranked by `limit / window`,
    because holding two plans must not be worse than holding the better one. A
    caller whose roles name no tier -- and an anonymous caller, who has no roles
    at all -- is charged against `default`, so every request is limited by
    something. Each tier is a whole `RateLimitPolicy` with its own store, so
    the 429, the `Retry-After`, and the policy headers are the ones documented
    there.

    This is the first-class **post-authentication** policy stage. Configure it as
    `HttpPolicy(principal_rate_limit=...)`; it never enters a route middleware
    tape. For unauthenticated ingress protection -- a flood of 404s or a login
    flood -- also configure `HttpPolicy(rate_limit=RateLimitPolicy(...))`.
    A request that has to pass both spends a token in each.

    Each tier keeps its own buckets, so a promotion arrives with a full
    allowance rather than whatever was left of the old plan's. That is the
    intended behaviour -- an upgrade should take effect at once -- and it has a
    consequence worth naming: **moving between tiers hands the caller a fresh
    allowance every time**, so where roles are self-service, toggling one twice
    is a way to bypass the limit on demand. Keep tier membership
    server-controlled, or key the limit on something the caller cannot change.

    Args:
        tiers: Role name to `(limit, window_seconds)`. At least one is required.
        default: The `(limit, window_seconds)` for a caller matching no tier.
        tier: Names the tier for a request. Default the caller's most generous role.
        key: The bucket key within a tier. Default `principal_key`.
        cost: Tokens one request spends, in every tier. Default 1.0.
        exempt: A request it answers True for is not limited at all.
        store_factory: Builds one store per tier. Default `MemoryRateLimitStore`.
        quota: A `wreath.quota.QuotaMeter` charged in the same stage, after the
            rate limit is satisfied. One meter across every tier, not one per
            tier: the tier decides how fast a caller may go, the quota decides
            how much they get, and giving each tier its own counter would hand a
            caller a fresh monthly allowance for changing plan.

    Raises:
        ValueError: `tiers` is empty.
    """

    __slots__ = ("_children", "_default", "_dispatch", "_tier", "_tier_rates", "_tiers")

    def __init__(
        self,
        *,
        tiers: Mapping[str, tuple[int, float]],
        default: tuple[int, float],
        tier: Callable[[Request], str | None] | None = None,
        key: Callable[[Request], str | None] = principal_key,
        cost: float = 1.0,
        exempt: Callable[[Request], bool] | None = None,
        store_factory: Callable[[], RateLimitStore] | None = None,
        quota: Any = None,
    ) -> None:
        if not tiers:
            raise ValueError("at least one tier is required")
        build = store_factory if store_factory is not None else MemoryRateLimitStore
        self._tiers = dict(tiers)
        self._default = default
        self._tier = tier if tier is not None else self._tier_from_roles
        # One limiter -- and so one store, one keyspace -- per tier. Reusing
        # RateLimitPolicy keeps exactly one implementation of the bucket
        # decision, the 429, and the Retry-After.
        # Resolved to a concrete stage per tier at construction rather than
        # branched per request: `RateLimitPolicy` exposes a synchronous stage
        # for a local store and an awaiting one for a remote store, and which it
        # is cannot change afterwards.
        self._dispatch: dict[str | None, tuple[Any, bool]] = {}
        # The children themselves, not only their stages. Dispatch never needs
        # them, but `schema_owners` does: a `store_factory` returning a
        # `PostgresRateLimitStore` puts this policy's tables in a database,
        # and reaching them back through a bound stage's `__self__` would be a
        # second way to say what a tuple already says.
        children: list[Any] = []
        for name, (limit, window) in {**tiers, None: default}.items():
            # `_route_scoped` is the declaration that this limiter runs after
            # authentication, which is what makes `principal_key` valid -- the
            # global form refuses it because a global stage runs at ingress.
            child = RateLimitPolicy(
                limit=limit,
                window=window,
                cost=cost,
                exempt=exempt,
                store=build(),
                key=key,
                quota=quota,
                _route_scoped=True,
            )
            stage = child._ingress
            self._dispatch[name] = (
                (stage, True) if stage is not None else (child._ingress_sync, False)
            )
            children.append(child)
        self._children = tuple(children)
        # Child construction above remains the validation owner for non-positive
        # windows. Compile the generosity comparison only after those declarations
        # have been accepted, so the request path neither divides nor allocates.
        self._tier_rates = {name: limit / window for name, (limit, window) in self._tiers.items()}

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """Every tier's store, so their tables are collected too.

        A tier is a whole `RateLimitPolicy`, and the walk that asks this
        question goes one level down, not all the way -- so the tiers' answers
        are flattened here rather than returning the tiers themselves and
        trusting a recursion that does not happen. Every tier built by one
        `store_factory` claims the same table name and `schema_components`
        deduplicates by name, so N tiers still contribute one claim.
        """
        return tuple(owner for child in self._children for owner in child.schema_owners)

    def _tier_from_roles(self, request: Request) -> str | None:
        """The most generous tier among the caller's roles, or None."""
        identity = request.identity
        if identity is None:
            return None
        selected = None
        selected_rate = -1.0
        rates = self._tier_rates
        for role in identity.authority_roles:
            rate = rates.get(role)
            if rate is not None and rate > selected_rate:
                selected = role
                selected_rate = rate
        return selected

    async def _ingress(self, request: Request) -> Any | None:
        """Charge this request to its tier's bucket.

        Returns None when the request is admitted, or the 429 the tier's
        `RateLimitPolicy` built when it is not. A tier name that is not in
        `tiers` -- including None, which is what an unmatched or anonymous
        caller gets -- is charged against `default`.
        """
        stage, awaiting = self._dispatch.get(self._tier(request), self._dispatch[None])
        return await stage(request) if awaiting else stage(request)


__all__ = [
    "MemoryRateLimitStore",
    "PostgresRateLimitStore",
    "RateLimitPolicy",
    "RateLimitStore",
    "TieredRateLimitPolicy",
    "principal_key",
]
