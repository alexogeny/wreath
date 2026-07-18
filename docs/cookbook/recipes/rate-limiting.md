# Rate-limit an endpoint

When you need to protect an endpoint from being hammered — a login route, an
expensive search — reach for `RateLimitMiddleware`. It keeps a token bucket per
caller and turns excess requests away before they reach your handler:

```python
from wreath.middleware import RateLimitMiddleware, MemoryRateLimitStore

app.add_middleware(RateLimitMiddleware(MemoryRateLimitStore(), limit=100, window=60))
```

The bucket is bounded, so a flood of new callers can't grow memory without limit
— the rate limiter can't itself become the outage. The in-memory store is perfect
for a single process; when you run several, swap in `PostgresRateLimitStore` so
every worker counts against the same shared limit.
