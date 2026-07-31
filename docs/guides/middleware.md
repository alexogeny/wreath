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

## Synchronous hooks

Use `before_sync` or `after_sync` when a hook never awaits anything. They have
the same ordering and short-circuit semantics as `before` and `after`, but the
compiled request path calls them directly instead of creating and awaiting a
coroutine for every hook. If both forms are present, the synchronous hook takes
precedence; compatibility wrappers may therefore keep the async spelling for
callers that invoke a middleware method directly.

When egress only mutates headers or other response state, use `after_inplace`.
It returns `None`, which also lets the compiled path omit the return-value load,
response replacement, and coercion performed for a transforming `after_sync`.
Hook selection is `after_inplace`, then `after_sync`, then `after`; only the
highest-precedence spelling runs when a middleware exposes more than one.

```python
from wreath.middleware import MiddlewareHooks

def add_header(request, response):
    response.headers.append((b"x-service", b"catalog"))

hooks = MiddlewareHooks(after_inplace=add_header)
```

An egress hook still runs only if that middleware was entered. If an earlier
hook short-circuits, Wreath unwinds exactly the entered portion of the chain in
reverse order.

## Scoping a global middleware to some routes

Global middleware covers every request by design — that is what makes it the
right place for ingress checks and response headers. When one genuinely does not
belong on a route, give it `applies_to(method, path)`:

```python
class Auditing:
    global_scope = True

    def before_sync(self, request):
        ...

    def applies_to(self, method: str, path: str) -> bool:
        return path != "/health"
```

`applies_to` is called **once per route when routes compile**, never per
request. A route that declines a middleware dispatches through a tape compiled
without it, so declining costs nothing at request time — there is no predicate
to evaluate and no hook to skip. Both arguments are the route's, not a live
request's, which is what makes the answer cacheable; scope on the *request*
(a header, a client address) belongs inside the hook, where it has always
belonged.

The method is passed because it is the most useful axis: CSRF matters on unsafe
methods, and a route declaring `GET` and `POST` may answer differently for each.

Skipping the middleware on a route skips **both** its hooks — a middleware that
declines a route neither inspects its requests nor decorates its responses.

Two limits, both deliberate:

- A request that does not match a route runs the **whole** stack. Compartments
  need the route to be known, and a miss has none; this also keeps a rate
  limiter counting the `404`s it is documented to count, and stops the set of
  middleware that ran from advertising whether a path exists.
- A route behind authentication runs the whole stack too, because which route a
  ticket resolves to is not known until the identity is.

`applies_to` raising fails the compile rather than being caught. It stands on a
security decision often enough that guessing a direction is worse than stopping:
the safe guess silently discards a policy you believed was in effect.

## Answering CORS preflights without entering Python

A preflight — `OPTIONS` carrying both `Origin` and
`Access-Control-Request-Method` — is answerable from configuration alone. It has
no route, no handler and no body, yet by default it costs a full trip through
the global tape to produce a reply that was decided when the app booted.

```python
app = Wreath(middleware="native")
app.add_middleware(CORSMiddleware(allow_origins=["https://app.example"]))
```

At startup Wreath records every answer your `CORSMiddleware` can give a
preflight, by asking it. The server then serves them directly — before the tape,
before routing, before a `Request` object exists. **Measured at 10.89 µs →
3.32 µs, a 70% saving on preflights**, with byte-identical responses.

Nothing else changes: every other request runs the Python tape exactly as
before, and a preflight the table does not cover (an origin spelled differently
from the configured one, say) falls through to `CORSMiddleware` as usual.

It is opt-in rather than automatic because it **changes what earlier middleware
sees**. A preflight answered by the server never reaches the tape, so middleware
registered before CORS no longer observes it:

- a rate limiter does not count preflights against the bucket
- `ProxyHeadersMiddleware` does not rewrite their client address

Neither changes the response a browser receives — `CORSMiddleware` short-circuits
ahead of the route either way — but both are visible in metrics. If you
deliberately rate-limit preflights, leave the default `middleware="python"`.

A middleware whose answers are not reproducible is declined and left in Python:
each probe runs twice at boot, and anything consulting a clock or a counter
rather than the origin and requested method is not compiled.

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

`TrustedHostMiddleware` validates the whole authority before comparing its host.
A numeric port is ignored for matching, but user information, malformed ports,
and junk after a bracketed IPv6 literal are rejected rather than truncated into
an allowed hostname. On HTTP/2 the server also rejects a regular `Host` that
disagrees with `:authority`, before either value can reach host-based policy.

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

## What the tape tells the document

Your application already knows it rate-limits, that it reads an
`Idempotency-Key`, that it emits a `Cache-Control`. Until a middleware says so,
none of that reaches your OpenAPI document, and so none of it reaches the client
you generate from it — every consumer learns it from prose, and the prose stops
being true the day you change the tape.

A middleware says so by answering `describe()`:

```python
from wreath.middleware.base import HeaderSpec, MiddlewareContract

class SignedRequestMiddleware:
    async def before(self, request): ...

    def describe(self) -> MiddlewareContract:
        return MiddlewareContract(
            request_headers=(HeaderSpec("X-Signature", required=True),),
            methods=frozenset({"POST", "PUT", "PATCH", "DELETE"}),
        )
```

`generate_openapi` collects these by *asking* every middleware on a route's
tape, the same way `schema_components()` asks for `component()`. There is no
registry to keep in step: a middleware that offers nothing contributes nothing,
which is what every middleware did before this existed.

Three properties make the result trustworthy enough to generate a client from.

**A contract describes the instance, not the class.** A
`RateLimitMiddleware(limit=60, window=60.0)` documents `RateLimit-Policy:
60;w=60`, because `describe()` reads the very tuple the refusal path appends. A
`ServerTimingMiddleware(emit_header=False)` documents no header at all. The
document and the wire cannot drift, because there is only one copy of the value.

**Scope is honest.** Global middleware wraps every request, so it decorates
every operation. Route middleware is filtered by the same `applies_to` predicate
the tape itself evaluates, so a limiter you mounted on one router never appears
on a route outside it. This is not fussiness: a document claiming a `429` on an
operation that cannot answer one teaches a generated client to retry a permanent
failure.

**A route's own declaration wins.** A route that documents its own `429` keeps
its wording; the middleware fills in only what the route left unsaid.

Because `describe()` runs at startup and never per request, none of this costs a
request anything — `wreath-request-trace` records no added boundary crossing.

**Reference:** [`wreath.middleware`](../reference/middleware.md).
