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

## Behind a proxy

If Wreath runs behind a TLS-terminating proxy or load balancer, requests arrive
over plain HTTP with the real scheme and client tucked into `X-Forwarded-*`
headers. Add `ProxyHeadersMiddleware` and Wreath will trust those headers from
your proxy — quietly fixing HSTS, secure-cookie, and CSRF decisions that would
otherwise be made as if the connection were insecure. The
[Deploy behind a proxy](../cookbook/recipes/behind-a-proxy.md) recipe shows the
full setup.

**Reference:** [`wreath.middleware`](../reference/middleware.md).
