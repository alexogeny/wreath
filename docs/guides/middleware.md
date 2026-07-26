# Middleware

Middleware is code that runs around your handlers — before a request reaches
them, after a response comes back, or both. It's where cross-cutting concerns
live: CORS, security headers, compression, rate limiting, request IDs, timing.
Wreath ships the ones almost every service needs, and lets you write your own
against a small, honest protocol. (It is called middleware here because that is
what the rest of the world calls it.)

```python
from wreath.middleware import (
    CORSMiddleware, SecurityHeadersMiddleware, CompressionMiddleware,
    RateLimitMiddleware, RequestIDMiddleware, ServerTimingMiddleware,
    ProxyHeadersMiddleware, TrustedHostMiddleware, CSRFMiddleware, SessionMiddleware,
)

app.add_middleware(CORSMiddleware(allow_origins=["https://app.example"]))
app.add_middleware(SecurityHeadersMiddleware(content_security_policy="default-src 'none'"))
app.add_global_middleware(RequestIDMiddleware())
```

There are two places to add middleware, and the difference matters. **Route
middleware** wraps matched handlers — it runs when a request lands on a route.
**Global middleware** runs on every request, including the ones that match
nothing. Request IDs, security headers, and timing belong at the global level,
because a `404` needs an ID and its headers just as much as a `200` does.

## User story: put a ceiling on abusive clients

> *As an API author, a few clients hammer my API and occasionally knock it over.
> I want a ceiling — every client gets so many requests a minute, and the ones
> over the line get a clean `429`, without me hand-rolling a token bucket.*

```python
from wreath.middleware import RateLimitMiddleware

app.add_middleware(RateLimitMiddleware(limit=100, window=60.0))
```

The default key is the client address, so each caller gets its own bucket; a
request over the limit gets a `429 Too Many Requests` (an RFC 9457 problem body)
with a whole-second `Retry-After`. Pass `key=` to bucket by API key or
authenticated user instead. Rate limiting is inherently global — it registers on
every request, misses included — so a flood of `404`s counts against the bucket
too.

## Behind a proxy

If Wreath runs behind a TLS-terminating proxy or load balancer, requests arrive
over plain HTTP with the real scheme and client tucked into `X-Forwarded-*`
headers. Add `ProxyHeadersMiddleware` and Wreath will trust those headers from
your proxy — quietly fixing HSTS, secure-cookie, and CSRF decisions that would
otherwise be made as if the connection were insecure. The
[Deploy behind a proxy](../cookbook/recipes/behind-a-proxy.md) recipe shows the
full setup.

**Reference:** [`wreath.middleware`](../reference/middleware.md).
