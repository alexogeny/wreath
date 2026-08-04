"""Metered allowances: counted at the boundary, refused there, explained by policy.

A rate limit and a quota answer different questions. *How fast* is a token
bucket, refilling continuously, and `wreath.middleware.ratelimit` already owns
it. *How much, this month* is a counter against a period that resets on a
calendar edge and not before -- and the two must be decided in **one hook**,
because two independent limiters on one request is how a 429 comes back
contradicting a 402.

So a quota is not a second middleware. It is a branch inside the rate limiter's
existing tape hook:

```python
quotas = Quotas(store=MemoryQuotaStore())
quotas.declare("api_calls", limit=10_000, period=30 * 86400.0)

app.add_middleware(TieredRateLimitMiddleware(
    tiers={"pro": (600, 60.0)}, default=(60, 60.0),
    quota=quotas.meter("api_calls"),
))
```

Two halves, deliberately separated, because they are not the same kind of
answer:

* **Refusing a cost.** The allowance is spent, so the request is refused with
  `429` and a `Retry-After` pointing at the period reset. That is accounting,
  not authorization -- exactly as a rate limit is -- and it is why this lives in
  the tape rather than in the policy set.
* **Establishing a fact.** Whatever degraded state the application declares for
  a caller -- "payment failed", "trial expired" -- reaches Cedar as
  `context.quota`, on the same `SetFact` machinery as entitlements. *Graceful
  degradation is a declared state, not an outage*: "past due means read-only" is
  one policy, not an `if` in every handler.

```cedar
forbid(principal, action in [Action::"create", Action::"update"], resource)
when { context.quota.contains("read_only") };
```

**The meter is never sampled.** It is the one signal that cannot be, because it
has to reconcile with an invoice; a metering path that drops under load is a
revenue bug wearing an observability costume. Both stores here count every
admitted request or fail the request -- neither ever answers "probably fine".

Nothing here ships invoicing, rating, or dashboards. The stored counters are
rows; rating them is `wreath.passes` and charting them is `wreath.series`, both
off the request path.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from time import time as _wall_time
from typing import Any, Protocol

from .response import ProblemResponse
from .store import ALIAS, Column, Keyed, PostgresStore, Sql
from .temporal import Duration

__all__ = [
    "MemoryQuotaStore",
    "PostgresQuotaStore",
    "Quota",
    "QuotaMeter",
    "QuotaStore",
    "Quotas",
]


@dataclass(frozen=True, slots=True)
class Quota:
    """A named allowance of `limit` units over a repeating `period`.

    The period is a fixed window on the wall clock, not a sliding one, and that
    is the difference from a token bucket rather than an oversight: a monthly
    allowance has to reset on the same edge for every caller and for the
    invoice, so "1000 requests since some moment 30 days ago" is not the product
    being sold. The boundary effect a sliding window exists to prevent -- a
    caller spending two periods' worth across the edge -- is what the rate
    limiter beside it already handles, on the axis where it matters.

    Args:
        name: The quota's name. It is also the member a policy tests for in
            `context.quota` when this quota is exhausted, so it is part of the
            declared vocabulary and is validated at startup.
        limit: Units admitted per period. Must be positive.
        period: Seconds in one period. Must be positive.
        cost: Units one request spends. Default 1.0.

    Raises:
        ValueError: Non-positive limit, period or cost, or a cost above the limit.
    """

    name: str
    limit: float
    period: float
    cost: float = 1.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a quota needs a name")
        if self.limit <= 0.0:
            raise ValueError("limit must be positive")
        if self.period <= 0.0:
            raise ValueError("period must be positive")
        if self.cost <= 0.0:
            raise ValueError("cost must be positive")
        if self.cost > self.limit:
            # Otherwise every request is refused and the quota is an outage
            # wearing a configuration. Caught here rather than at the first
            # request, where it would look like a store fault.
            raise ValueError("cost must not exceed the limit")

    def window(self, now: float) -> tuple[int, float]:
        """The period index containing `now`, and the seconds until it ends.

        The index is what makes a counter key unique to a period, so a period
        rolling over needs no sweep: the next request simply counts against a
        key nobody has written yet. Derived from the wall clock, because two
        workers must agree which month it is and `monotonic()` gives them no way
        to.
        """
        index = int(now // self.period)
        return index, (index + 1) * self.period - now


class QuotaStore(Protocol):
    """Where consumption is counted.

    Deliberately the same shape as `RateLimitStore`: `configure` once at
    construction so a misconfiguration is a construction-time error, and a
    `spend` that answers with *seconds to wait* rather than a boolean, so the
    refusal can carry a truthful `Retry-After` without a second round trip.

    A store may additionally expose a synchronous `try_spend` with the same
    contract; the meter binds to it and skips awaiting when it exists.
    """

    def configure(self, quota: Quota) -> None:
        """Fix the allowance this store counts against, before any request."""
        ...

    async def spend(self, key: str, now: float) -> float:
        """Return 0.0 when admitted, else the seconds until the period resets.

        Nothing is spent on a refusal. `now` is wall-clock seconds from the
        caller; a store owning a clock its workers agree on should derive the
        period from that one instead.
        """
        ...

    async def used(self, key: str, now: float) -> float:
        """Units consumed in the current period, without spending any."""
        ...


class MemoryQuotaStore:
    """Counters in this process. Dependency-free, and **per-worker**.

    Stated as plainly as `MemoryRateLimitStore` states it, because the
    consequence here is worse: a limit of N per month is N *per worker*, so four
    workers admit four times the allowance and the number that reaches an
    invoice is not the number that was sold. This store is for a single-process
    deployment and for tests. Use `PostgresQuotaStore` when the count has to
    mean something.

    **The key carries the period, and this store does not re-derive it.** That
    is the same contract `PostgresQuotaStore` works under -- there the period
    index is simply part of the primary key -- and having one owner of "which
    period is this" is the point: two of them agree right up until a clock skews
    or a rounding differs, and then one grants an allowance the other already
    spent. `QuotaMeter.key` is the owner. A counter from an elapsed period is
    therefore unreachable rather than stale, and is evicted as room is needed.

    `max_entries` bounds distinct keys; at the ceiling the **fullest** counter
    is evicted, matching the rate limiter's choice, because evicting the
    emptiest would hand a fresh allowance to the caller who has spent the most.

    Args:
        max_entries: Distinct keys tracked. Default 10000.
    """

    __slots__ = ("_counts", "_max_entries", "_quota")

    def __init__(self, *, max_entries: int = 10000) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._quota: Quota | None = None
        self._counts: dict[str, float] = {}

    def configure(self, quota: Quota) -> None:
        """Bind the allowance. Once, and only once.

        Raises `ValueError` on any second call, even one repeating the same
        quota, for the reason `MemoryRateLimitStore.configure` gives: a second
        caller means two meters over one keyspace, and requests metered by one
        would silently consume the other's allowance.
        """
        if self._quota is not None:
            raise ValueError(
                "this store is already configured; give each quota its own store"
            )
        self._quota = quota

    @property
    def tracked(self) -> int:
        """Keys with a live counter right now."""
        return len(self._counts)

    def _quota_or_raise(self) -> Quota:
        quota = self._quota
        if quota is None:
            raise RuntimeError("quota store used before configure()")
        return quota

    def try_spend(self, key: str, now: float) -> float:
        """Spend one request's cost without awaiting anything.

        Returns 0.0 when admitted, else the seconds until the period resets.
        `now` decides only how long the caller must wait, never which period the
        key belongs to -- the key already said.
        """
        quota = self._quota_or_raise()
        used = self._counts.get(key, 0.0)
        if used + quota.cost > quota.limit:
            return quota.window(now)[1]
        if key not in self._counts and len(self._counts) >= self._max_entries:
            self._evict()
        self._counts[key] = used + quota.cost
        return 0.0

    def _evict(self) -> None:
        """Drop the fullest counter to make room.

        The fullest rather than the emptiest, and rather than the oldest: at the
        ceiling something has to lose its history, and losing the history of the
        caller who has consumed the most is the choice that grants the least.
        It still grants *something*, which is why this store is documented as
        the single-process one.
        """
        del self._counts[max(self._counts, key=self._counts.__getitem__)]

    async def spend(self, key: str, now: float) -> float:
        """`try_spend` behind the awaiting `QuotaStore` protocol."""
        return self.try_spend(key, now)

    def peek(self, key: str) -> float:
        """Units consumed under `key`, spending nothing."""
        self._quota_or_raise()
        return self._counts.get(key, 0.0)

    async def used(self, key: str, now: float) -> float:
        """`peek` behind the awaiting `QuotaStore` protocol."""
        return self.peek(key)

    def clear(self) -> None:
        """Forget every counter, restoring a full allowance to every key.

        A reset between tests. In production it is an amnesty: every caller who
        had spent their month is handed it back.
        """
        self._counts.clear()


class PostgresQuotaStore:
    """Counters in a shared table, so one allowance covers every worker.

    The whole check-and-count decision is one `INSERT ... ON CONFLICT DO
    UPDATE`, for the reason `PostgresRateLimitStore` gives: the statement takes a
    row lock, so concurrent workers serialize per key and the allowance holds
    exactly. A read-then-write would race, and a raced quota overspends in the
    direction that reaches an invoice.

    The period index is part of the stored key, so a period rolling over needs
    no sweep and no `UPDATE ... SET used = 0` that a worker could miss: the first
    request of the new period inserts a row nobody has written. Old rows are
    retired by `purge_pass()`, which is bookkeeping rather than correctness --
    they are already unreachable.

    The database clock derives the period, not the caller's, so workers cannot
    disagree about which month it is.

    The table is not created for you; apply `schema_sql()` as a migration.

    Args:
        database: The pool to run against, such as `app.postgres("main")`.
        table: Table holding the counters. Default `wreath_quota`.
    """

    __slots__ = ("_database", "_quota", "_store")

    def __init__(self, database: Any, *, table: str = "wreath_quota") -> None:
        self._database = database
        self._quota: Quota | None = None
        self._store = PostgresStore(
            database,
            Keyed(
                table=table,
                columns=(
                    Column("used", "double precision", null=False),
                    Column("admitted", "boolean", null=False),
                ),
                stamp="updated",
                deadline=False,
                index_stamp=True,
                prefix="wreath_quota",
            ),
        )
        # $1 key, $2 cost, $3 limit. Nothing about the allowance is baked into
        # the text, so the statement stays one prepared entry whatever the
        # quota; the period lives in $1, which the meter composes.
        self._store.define(
            "spend",
            self._store.upsert(
                values={
                    "key": "$1",
                    "used": Sql("$2::float8"),
                    "admitted": Sql("true"),
                    "updated": Sql("clock_timestamp()"),
                },
                # `admitted` is written as well as returned for the reason the
                # rate limiter writes `allowed`: the stored count alone cannot
                # say whether this call consumed or was refused.
                update={
                    "used": Sql(
                        f"{ALIAS}.used + CASE WHEN {ALIAS}.used + $2::float8 "
                        "<= $3::float8 THEN $2::float8 ELSE 0::float8 END"
                    ),
                    "admitted": Sql(f"{ALIAS}.used + $2::float8 <= $3::float8"),
                    "updated": Sql("clock_timestamp()"),
                },
                returning="used, admitted",
            ),
        )
        # The default workload, not `read`. A `read` statement needs a `read`
        # pool configured, and an application that configured only `write` --
        # which is every application without a replica -- would get a `KeyError`
        # naming a workload it never asked for, the first time something read a
        # remaining-allowance header. `PostgresRateLimitStore` makes the same
        # choice for the same reason.
        self._store.define(
            "used", f"SELECT used FROM {self._store.table} WHERE key = $1"
        )

    def configure(self, quota: Quota) -> None:
        """Record the allowance bound into every `spend`. Once, and only once."""
        if self._quota is not None:
            raise ValueError(
                "this store is already configured; give each quota its own store"
            )
        self._quota = quota

    def component(self) -> Any:
        """This store's claim on the wreath schema."""
        return self._store.schema_claim("quota")

    @property
    def schema_database(self) -> Any:
        """The database `component()`'s tables belong to.

        The application never saw this store constructed, so it cannot know
        which `app.postgres()` the table goes to unless the store says.
        `Wreath._schema_database` reads exactly this name.
        """
        return self._database

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined."""
        return self._store.schema_sql()

    def _quota_or_raise(self) -> Quota:
        quota = self._quota
        if quota is None:
            raise RuntimeError("quota store used before configure()")
        return quota

    async def spend(self, key: str, now: float) -> float:
        """Count one request's cost against the shared counter for `key`.

        Returns 0.0 when admitted, else the seconds until the period resets. One
        round trip.
        """
        quota = self._quota_or_raise()
        _, reset = quota.window(now)
        row = await self._store.statement("spend").fetchrow(key, quota.cost, quota.limit)
        return 0.0 if row[1] else reset

    async def used(self, key: str, now: float) -> float:
        """Units consumed in the current period, spending nothing."""
        self._quota_or_raise()
        row = await self._store.statement("used").fetchrow(key)
        return 0.0 if row is None else float(row[0])

    def purge_pass(self, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass retiring counters from elapsed periods.

        Bookkeeping, not correctness: a counter whose period index has passed is
        already unreachable, because the key a request composes carries the
        current index. This keeps the table from growing without bound.

        ```python
        jobs.drive(store.purge_pass(), cron="23 4 * * *")
        ```

        Args:
            chunk: Rows deleted per transaction. Default 1000.
        """
        from ._passes.stores import keyed_purge_pass

        quota = self._quota_or_raise()
        return keyed_purge_pass(
            self._store.declaration,
            name=f"purge_{self._store.table}",
            # Two periods, so a counter is never retired while its period is
            # still the current one on a worker whose clock is behind.
            after=float(quota.period) * 2.0,
            chunk=chunk,
            **options,
        )


class QuotaMeter:
    """One declared quota bound to one store, ready for the tape.

    Built by `Quotas.meter`, never directly: the registry is what makes the
    exhausted-quota *fact* and the spent-quota *refusal* two views of one
    declaration rather than two configurations that agree until someone edits
    one of them.
    """

    __slots__ = ("_key", "_registry", "_store", "_try_spend", "quota", "refused")

    def __init__(self, quota: Quota, store: Any, registry: Quotas) -> None:
        self.quota = quota
        self._store = store
        self._registry = registry
        #: Requests this meter refused. Without it, a quota nobody reaches and a
        #: quota keyed wrongly look identical.
        self.refused = 0
        # Resolved once rather than branched per request, matching the rate
        # limiter: a synchronous store skips a coroutine on the hot path.
        self._try_spend: Any = getattr(store, "try_spend", None)

    @property
    def store(self) -> Any:
        """The store counting for this meter, for schema collection."""
        return self._store

    def key(self, request: Any) -> str | None:
        """The counter key for a request, or None when nobody is identified.

        A quota is metered per principal: an unauthenticated request has no
        allowance to spend, and metering it by address would charge a shared
        proxy's callers to one counter. Returning None admits the request
        without counting -- the route's own `AuthRequirement` is what refuses an
        anonymous caller, and inventing a second refusal here would be a second
        authorization path.
        """
        identity = request.identity
        if identity is None:
            return None
        index, _ = self.quota.window(_wall_time())
        return f"{self.quota.name}:{identity.type}:{identity.id}:{index}"

    def refusal(self, reset: float) -> ProblemResponse:
        """The 429 for an exhausted allowance.

        A 429 rather than a 402, and that is a decision: 402 means *pay and this
        succeeds*, which is only true when the application sells overage, and
        wreath ships no payment integration to know. An application that does
        sell it raises `wreath.signatures.PaymentRequired` from its own handler.
        `Retry-After` is the seconds to the period reset, which for a monthly
        quota is honestly large rather than a placeholder.
        """
        response = ProblemResponse(
            status=429,
            title="Quota Exceeded",
            detail=f"The {self.quota.name} quota for this period is exhausted.",
        )
        seconds = max(1, math.ceil(reset))
        response.headers.append((b"retry-after", str(seconds).encode("ascii")))
        response.headers.append(
            (b"x-quota-limit", f"{self.quota.limit:g}".encode("ascii"))
        )
        response.headers.append((b"x-quota-remaining", b"0"))
        response.headers.append(
            (b"quota-policy", f"{self.quota.name};u={self.quota.limit:g}"
             f";w={self.quota.period:g}".encode("ascii"))
        )
        self.refused += 1
        return response

    def spend_sync(self, request: Any) -> ProblemResponse | None:
        """Count this request, synchronously. None when admitted.

        Only bound when the store offers `try_spend`; `QuotaMeter.awaits` says
        which form applies.
        """
        key = self.key(request)
        if key is None:
            return None
        reset = self._try_spend(key, _wall_time())
        return None if reset <= 0.0 else self.refusal(reset)

    async def spend(self, request: Any) -> ProblemResponse | None:
        """Count this request. None when admitted, else the refusal."""
        key = self.key(request)
        if key is None:
            return None
        reset = await self._store.spend(key, _wall_time())
        return None if reset <= 0.0 else self.refusal(reset)

    @property
    def awaits(self) -> bool:
        """Whether `spend` must be awaited rather than `spend_sync` called."""
        return self._try_spend is None


class Quotas:
    """Declared allowances, and the Cedar fact they produce.

    One registry per application. It is handed to two places and they read
    different halves of it, which is the point:

    ```python
    quotas = Quotas(store_factory=MemoryQuotaStore, states=billing_states)
    quotas.declare("api_calls", limit=10_000, period=30 * 86400.0)

    app.add_middleware(TieredRateLimitMiddleware(..., quota=quotas.meter("api_calls")))
    CedarAuthorizer(engine=engine, quota=quotas)
    ```

    `states` is what makes graceful degradation declarative. It answers, for an
    identity, whatever names the application wants a policy to be able to test --
    `"past_due"`, `"read_only"`, `"trial"`. Those are **restrictions**: a policy
    reads them to `forbid`, so they are never intersected with a delegation's
    `Limits` the way entitlements are. Intersecting a restriction subtracts it,
    and subtracting `read_only` from a delegated agent would let a narrowing
    *grant* — the one thing composition must never do.

    Args:
        store_factory: Builds one store per declared quota. Default
            `MemoryQuotaStore`, which is per-worker; see its documentation.
        states: `(identity) -> Iterable[str]`, the degraded states for a caller.
            Optional; without it `context.quota` is always empty, and every
            policy testing it denies -- which is the fail-closed answer, and the
            same one every other `SetFact` gives for an absent provider.
    """

    __slots__ = ("_meters", "_states", "_store_factory")

    def __init__(
        self,
        *,
        store_factory: Any = None,
        states: Any = None,
    ) -> None:
        self._store_factory = store_factory if store_factory is not None else MemoryQuotaStore
        self._states = states
        self._meters: dict[str, QuotaMeter] = {}

    def declare(
        self, name: str, *, limit: float, period: Any, cost: float = 1.0
    ) -> QuotaMeter:
        """Declare a quota and build its meter.

        Raises `ValueError` on a duplicate name, because two quotas sharing a
        name would share the `context.quota` member a policy tests and no policy
        author could tell which one had been exhausted.
        """
        if name in self._meters:
            raise ValueError(f"quota {name!r} is already declared")
        # Seconds, or any spelling `Duration` reads -- `days(30)` says what
        # `30 * 86400.0` meant, in the vocabulary every other window uses.
        quota = Quota(
            name=name, limit=limit, period=Duration.of(period).total_seconds(), cost=cost
        )
        store = self._store_factory()
        store.configure(quota)
        meter = QuotaMeter(quota, store, self)
        self._meters[name] = meter
        return meter

    def meter(self, name: str) -> QuotaMeter:
        """The meter for a declared quota.

        Raises `KeyError` naming the declared quotas, rather than returning
        None, because a meter silently absent from a middleware is a quota that
        counts nothing while appearing configured.
        """
        try:
            return self._meters[name]
        except KeyError:
            declared = ", ".join(sorted(self._meters)) or "none"
            raise KeyError(
                f"no quota named {name!r} is declared; declared: {declared}"
            ) from None

    def __getattr__(self, name: str) -> Any:
        """Offer `names` only when the states provider can actually enumerate.

        `validate_names` reads those two situations differently, and the
        difference decides whether an application boots:

        * **`names` absent** -- "this cannot be checked", so a misspelled state
          gets a `RuntimeWarning` where the authorizer is built.
        * **`names` returning nothing** -- "an empty vocabulary", so every state
          a policy references is unknown and startup *fails*.

        A registry whose provider is a plain function has no vocabulary to
        offer, and answering the empty set on its behalf would refuse every
        correct application that writes a state policy. So the method exists
        conditionally rather than always answering, which is the only way to say
        "I do not know" to a probe that is `getattr(provider, "names", None)`.

        A registry configured with **no** provider at all does answer, with
        nothing -- that application declared the capability off, so a policy
        naming a state is a misconfiguration worth refusing at boot.
        """
        if name != "names":
            raise AttributeError(name)
        states = self._states
        if states is not None and not callable(getattr(states, "names", None)):
            raise AttributeError(name)
        return self._declared_names

    def _declared_names(self) -> frozenset[str]:
        """Every name a policy may test in `context.quota`.

        **Declared quota names are deliberately not members here.** A quota that
        is exhausted refuses in the tape, so the request never reaches the policy
        set -- a `context.quota.contains("api_calls")` member would be a name in
        the vocabulary that essentially no decision can ever observe, which is a
        lie a policy author would write a rule against. Exhaustion an
        application wants a *policy* to see is a state it declares, and it
        declares it because it knows what the exhaustion means.
        """
        enumerate_states = getattr(self._states, "names", None)
        if not callable(enumerate_states):
            return frozenset()
        return frozenset(str(name) for name in enumerate_states())

    @property
    def schema_owners(self) -> tuple[Any, ...]:
        """Every meter's store, so their tables are collected.

        Answered so an application can hand this registry straight to whatever
        collects schema components, rather than reaching into the meters.
        """
        return tuple(meter.store for meter in self._meters.values())

    def for_identity(self, identity: Any) -> frozenset[str]:
        """The declared states true of `identity`.

        Synchronous and does no I/O, which is not a limitation of this class but
        the reason the split above exists: a Cedar fact is resolved inside the
        authorization decision, and a fact that went to the database there would
        put a round trip on the authorization path of every request whose policy
        set happens to name the key.
        """
        if self._states is None:
            return frozenset()
        return frozenset(str(state) for state in self._declared_states(identity))

    def _declared_states(self, identity: Any) -> Iterable[str]:
        states = self._states
        call = getattr(states, "states", None)
        return call(identity) if callable(call) else states(identity)


def quota_headers(meter: QuotaMeter, used: float) -> Mapping[bytes, bytes]:
    """The advisory headers for an *admitted* request, built on request.

    Deliberately not attached by the middleware. Advertising remaining allowance
    on every response needs a global `after` hook, which `wreath-request-trace`
    priced at +18 boundary crossings per request for the rate limiter's
    equivalent -- real work on every successful request to carry a header that
    matters when one is refused. An application that wants them on a particular
    route can call this and extend its own response.
    """
    remaining = max(0.0, meter.quota.limit - used)
    return {
        b"x-quota-limit": f"{meter.quota.limit:g}".encode("ascii"),
        b"x-quota-remaining": f"{remaining:g}".encode("ascii"),
    }
