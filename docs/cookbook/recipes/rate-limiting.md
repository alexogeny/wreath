# Rate-limit an endpoint

When you need to protect an endpoint from being hammered — a login route, an
expensive search — reach for `RateLimitMiddleware`. It keeps a token bucket per
caller and turns excess requests away before they reach your handler:

```python
from wreath.middleware import RateLimitMiddleware

app.add_middleware(RateLimitMiddleware(limit=100, window=60))
```

The bucket is bounded, so a flood of new callers can't grow memory without limit
— the rate limiter can't itself become the outage. The in-memory store is the
default and is perfect for a single process; when you run several, pass
`store=PostgresRateLimitStore(app.postgres("main"))` so every worker counts
against the same shared limit.

## Per-principal, with an allowance per plan

One limit for everybody is the wrong shape for an authenticated API: the free
tier and the enterprise tier are different products, and the client address is
the wrong key — behind a proxy or a carrier NAT it lumps unrelated callers into
one bucket, and it hands one caller a fresh allowance per device.

```python
from wreath.middleware import TieredRateLimitMiddleware

app.add_middleware(TieredRateLimitMiddleware(
    tiers={"pro": (600, 60.0), "enterprise": (10_000, 60.0)},
    default=(60, 60.0),
))
```

The tier comes from the caller's roles — the same roles the Cedar policies
authorize with — so there is one answer to "who is this" rather than two that
can disagree. A caller holding two named roles gets the **most generous** of
them, because holding two plans must not be worse than holding the better one,
and each tier keeps its own buckets, so a promotion arrives with a full
allowance rather than the remainder of the old plan's.

!!! warning "This one is route middleware, and that is deliberate"

    `request.identity` is set during route authorization, so a limiter keyed on
    the principal *cannot* be a global hook — global hooks run at ingress,
    before anyone has been identified. Passing `principal_key` to the global
    `RateLimitMiddleware` raises at startup rather than silently bucketing every
    caller together, which is the failure this design exists to avoid.

    Run both: the global limiter keyed on the address protects ingress (a 404
    flood, a login flood), and the tiered one protects the authenticated API.

Pass `tier=` to derive the tier from anything else — a plan column, a header
your gateway sets — and `store_factory=PostgresRateLimitStore` to share the
allowance across workers.
