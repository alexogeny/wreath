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

`middleware.throttled` counts refusals. A limiter that has silently collapsed
every caller into one bucket — see the proxy note above — otherwise looks exactly
like one with nothing to do.

A request the key function cannot name — no client address in the scope, behind
a socket or an unusual server — lands in one shared bucket rather than skipping
the limiter. A limiter that lets a request past because it could not identify it
is not a limiter; use `exempt=` to allow one deliberately.

`CSRFMiddleware` runs two checks, and which one answers depends on the client.

**`Sec-Fetch-Site` decides when the browser sent it.** The browser sets this
header itself and the page making the request cannot forge it, so it settles the
question outright: an unsafe request is refused unless the value is `same-origin`
or `none`, and a safe request needs no token at all. Every browser has sent it
since 2023, OWASP accepted Fetch Metadata as a complete alternative to tokens in
December 2025, and Go ships the same check in its standard library as
`net/http.CrossOriginProtection`.

`same-site` is refused. It means a *different subdomain*, which is a different
security origin, and treating it as trusted is what a sibling-subdomain takeover
abuses.

**The signed double-submit token is the fallback**, unchanged, for a client that
sent no `Sec-Fetch-Site`: a pre-2023 browser, a proxy that strips it, or a
non-browser caller. Nothing was removed — the header check sits in front of the
token check rather than replacing it — so nothing that worked stops working.

`csrf_token(request)` still returns a token to any handler that asks. When Fetch
Metadata answered the request no token was minted eagerly, so one is minted at
that call and the cookie is written as before; the cost moved to the request that
wanted a token instead of being paid by every request that did not.

`cross_site_refusals` counts unsafe requests the header check refused. On a
browser-facing deployment a counter that never moves means the header is not
arriving — worth knowing before the fallback quietly becomes the only check
running.

A response whose cookie behaviour turned on the header carries
`Vary: Sec-Fetch-Site`, so a shared cache cannot hand a header-carrying client's
response to one without it.

`CSRFMiddleware(trusted_hosts=[...])` bounds the `Host` header the expected
origin is derived from, which the token fallback's origin check uses. Without it
the Host is trusted, so that check depends on `TrustedHostMiddleware` being
separately mounted — a dependency between two middlewares that nothing used to
state. Naming the hosts here makes the CSRF check self-contained.

A preflight is checked against `allow_methods` rather than echoing it: asking
whether `DELETE` is allowed now gets an answer about `DELETE`. Origins compare
case-insensitively on scheme and host, as an origin should.

`CORSMiddleware` refuses `allow_origins=["*"]` together with
`allow_credentials=True` at construction: honouring it means reflecting whatever
origin asked, alongside `Access-Control-Allow-Credentials: true`, which lets any
site read authenticated responses from yours. Name the origins that may send
credentials.

`SessionMiddleware` defaults to `secure=True` (matching `CSRFMiddleware`) and
requires a secret of at least 32 bytes. Pass `secure=False` for local plaintext
development. Rotate the secret without logging everyone out by naming
the old one:

```python
SessionMiddleware(secret=NEW, previous_secrets=[OLD])
```

Cookies verify under either; new ones are signed with `secret`, and a session
carried in on a previous secret is re-signed on its next write.

A server-side session store may implement `touch(sid, max_age)`; when it does, a
live but unchanged session has its expiry extended on each request, so expiry is
sliding rather than absolute.


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
