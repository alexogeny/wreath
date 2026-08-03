# Quotas and graceful degradation

A rate limit and a quota look like the same feature until the day they disagree.
One asks *how fast may this caller go* and answers in seconds; the other asks
*how much have they used this month* and answers against something a customer
was sold. Applications usually build them as two pieces of middleware, and then
discover the failure that follows: a caller who is both throttled and out of
allowance receives two refusals with contradictory advice, and neither one tells
them which problem to solve.

Wreath decides both in a single hook. Whichever refuses, exactly one refusal is
built.

```python
from wreath.middleware import TieredRateLimitMiddleware
from wreath.quota import Quotas

quotas = Quotas()
api_calls = quotas.declare("api_calls", limit=10_000, period=30 * 86400.0)

app.add_middleware(TieredRateLimitMiddleware(
    tiers={"pro": (600, 60.0), "enterprise": (10_000, 60.0)},
    default=(60, 60.0),
    quota=api_calls,
))
```

The tier decides how fast; the quota decides how much. There is one meter across
every tier, not one per tier — otherwise changing plan would hand a caller a
fresh monthly allowance, which is a bypass rather than an upgrade.

## The order matters, and it is the accounting

The rate limit is checked first. A throttled request did no work, so charging it
against a monthly allowance would put requests the server *rejected* onto an
invoice. That is the one thing a meter may never do.

The visible consequence, so it does not read as a bug: a caller who is both
throttled and out of quota is told to slow down first, and learns about the
quota on the retry a second later. One coherent answer either way.

The meter is also never sampled. Metering is the single signal that cannot be,
because it has to reconcile with a bill — a path that drops counts under load is
a revenue bug wearing an observability costume.

## Where the counters live

`MemoryQuotaStore` is the default and it is **per-worker**. Four workers admit
four times the allowance, so the number that reaches an invoice is not the
number that was sold. It is right for a single-process deployment and for tests,
and wrong for anything else.

```python
from wreath.quota import PostgresQuotaStore

quotas = Quotas(store_factory=lambda: PostgresQuotaStore(app.postgres("main")))
```

The Postgres store makes the whole check-and-count decision one
`INSERT ... ON CONFLICT DO UPDATE`. That statement takes a row lock, so
concurrent workers serialize per key and the allowance holds exactly; a
read-then-write would race, and a raced quota overspends in the direction that
reaches a customer.

A period rolling over needs no sweep and no scheduled `UPDATE ... SET used = 0`
that a worker could miss. The period index is part of the key, so the first
request of a new month simply counts against a row nobody has written yet. Old
rows are unreachable rather than stale; `purge_pass()` retires them as
housekeeping.

## Graceful degradation is a declared state

Running out of allowance is rarely meant to be an outage. What buyers expect —
and what applications implement inconsistently, one `if` at a time, in every
handler — is that a failed payment turns the product read-only rather than off.

That is not a quota counter. It is a fact about the caller, and it belongs where
every other such fact already lives: in Cedar context, decided by the policy set.

```python
class Billing:
    def states(self, identity):
        return frozenset({"read_only"}) if self.past_due(identity) else frozenset()

    def names(self):
        return frozenset({"read_only", "past_due"})

quotas = Quotas(states=Billing())
app.configure_auth(backend, CedarAuthorizer(engine=engine, quota=quotas))
```

```cedar
forbid(principal, action in [Action::"create", Action::"update"], resource)
when { context.quota.contains("read_only") };
```

`names()` is what makes a typo a boot failure instead of a silent forever-deny:
a policy testing `context.quota.contains("read_onyl")` refuses to start, naming
the state. That is the same bargain `wreath.flags` and organisation roles make.

### One rule that is specific to this fact

Quota states are **never** intersected with a delegation's limits, and every
other set fact is. The difference is the shape of what they say.

An entitlement is a grant, so narrowing it can only subtract — that is the law
composition rests on. A quota state is read to *forbid*, so subtracting it would
**grant**. If a delegated agent could narrow `read_only` out of its own context,
it would hold a permission its delegator does not, which is precisely the thing
composition must never do.

## What this does not do

No rating, no invoicing, no proration, no dunning, and no payment integration.
The stored counters are ordinary rows: rate them with `wreath.passes` and chart
them with `wreath.series`, both off the request path. An application that sells
overage raises `wreath.signatures.PaymentRequired` from its own handler, where
it knows the price.

The refusal here is a `429`, deliberately, and not a `402`. A 402 means *pay and
this succeeds*, which is only true when someone sells overage — and wreath ships
nothing that would know.

Reference: [`wreath.quota`](../reference/quota.md).
