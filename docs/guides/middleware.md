# HTTP policy and custom middleware

Wreath separates two things that are often bundled together. Standard HTTP
controls are first-class application policy. Custom middleware is application
code that wraps selected handlers.

Policy has a fixed order and, on Wreath's server, runs in C before a Python
`Request` exists and after the Python response returns to the protocol. It does
not enter the middleware tape.

```python
from wreath import Wreath
from wreath.policy import (
    CorsPolicy, HttpPolicy, RequestIdPolicy, SecurityHeadersPolicy,
)

app = Wreath(http_policy=HttpPolicy(
    cors=CorsPolicy(allow_origins=["https://app.example"]),
    security_headers=SecurityHeadersPolicy(
        content_security_policy="default-src 'none'",
    ),
    request_id=RequestIdPolicy(),
))
```

There are two places to add custom hooks, and the difference matters. **Route
middleware** wraps matched handlers — it runs when a request lands on a route.
**Global middleware** runs on every request, including the ones that match
nothing. Use first-class policy, not a global hook, for request IDs, security
headers, timing, and the other standard controls.

## Synchronous hooks

Use `before_sync` or `after_sync` when a hook never awaits anything. They have
the same ordering and short-circuit semantics as `before` and `after`, but the
compiled request path calls them directly instead of creating and awaiting a
coroutine for every hook. If both forms are present, the synchronous hook takes
precedence.

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

- A request that does not match a route runs the **whole** global custom-hook
  stack. Compartments need the route to be known, and a miss has none; this
  also stops the set of hooks that ran from advertising whether a path exists.
- A route behind authentication runs the whole stack too, because which route a
  ticket resolves to is not known until the identity is.

`applies_to` raising fails the compile rather than being caught. It stands on a
security decision often enough that guessing a direction is worse than stopping:
the safe guess silently discards a policy you believed was in effect.

## Native policy execution

A CORS preflight is answerable from configuration alone. Wreath's server answers
it before routing and before materializing a Python `Request`:

```python
app = Wreath(http_policy=HttpPolicy(
    cors=CorsPolicy(allow_origins=["https://app.example"]),
))
```

This is not an opt-in middleware mode. Proxy trust, host validation, local rate
limiting, request IDs, timing, CORS and CSRF execute in their fixed native order.
Security headers, CSRF cookies, CORS, timing and request IDs are applied natively
before response serialization. A conforming external ASGI server runs the same
policy through Wreath's readable reference executor.

`AIScrapingPolicy` is also native ingress policy, and Wreath enables its refusal
by default. It scans the bundled WUA database once and rejects autonomous AI
crawlers before routing or Python activation. `Wreath(ai_scraping="allow")`
opts into all such traffic; an explicit
`HttpPolicy(ai_scraping=AIScrapingPolicy(allow=(...)))` admits named crawlers
while refusing the remainder. User-triggered AI fetchers are not in the scraper
set.
The explicit declaration may be passed to the constructor or installed later
with `app.configure_http_policy(...)`; in the latter form it replaces the
injected default. Native and portable refusals share one native-owned aggregate
counter, and native Flight completions preserve the bounded `ai_scraping`
disposition for OTLP without promoting an expected 403 into an application
error.

This is an enforced known-product default, not bot attestation. A caller can
spoof a browser User-Agent; use Web Bot Auth for positive agent identity,
authentication for non-public content, rate limiting to bound anonymous work,
and an edge bot service when distributed/browser-emulating scraping is in
scope. `robots_txt(app)` publishes the same AI refusal to cooperative crawlers
and keeps `/robots.txt` readable to them.

Client-fact traffic classification is the explicit exception: its database
lookup is native, while `TrafficPolicy` materializes one `ClientFacts` value and
selects among the small tuple of application declarations in Python. Configuring
it therefore makes the policy descriptor use the readable executor on Wreath's
server too; there is no silent native/Python split whose ordering could differ.

## User story: give verified agents their own traffic lane

```python
from wreath.policy import (
    AIScrapingPolicy, HttpPolicy, TieredRateLimitPolicy,
    TrafficClass, TrafficPolicy, traffic_class,
)

app = Wreath(http_policy=HttpPolicy(
    ai_scraping=AIScrapingPolicy(allow=("oai-searchbot",)),
))
facts = app.client_facts("public")
app.configure_http_policy(HttpPolicy(
    traffic=TrafficPolicy(facts, (
        TrafficClass("verified-agent", verified_agent=True),
        TrafficClass("claimed-bot", claimed_agent=True),
    )),
    principal_rate_limit=TieredRateLimitPolicy(
        tiers={"verified-agent": (600, 60.0), "claimed-bot": (30, 60.0)},
        default=(120, 60.0),
        tier=traffic_class,
    ),
))
```

The selected name is also `context.client_class` in the default Cedar context.
That lets rate limiting and authorization consume one classification without
either re-running GeoIP, UA, or signature verification. The explicit scraping
opt-in is required here because this example assigns admitted AI traffic a
bounded allowance instead of accepting Wreath's default refusal.

## User story: put a ceiling on abusive clients

> *As an API author, a few clients hammer my API and occasionally knock it over.
> I want a ceiling — every client gets so many requests a minute, and the ones
> over the line get a clean `429`, without me hand-rolling a token bucket.*

```python
from wreath.policy import HttpPolicy, RateLimitPolicy

app = Wreath(http_policy=HttpPolicy(
    rate_limit=RateLimitPolicy(limit=100, window=60.0),
))
```

`RateLimitPolicy.throttled` counts refusals. A limiter that has silently collapsed
every caller into one bucket — see the proxy note above — otherwise looks exactly
like one with nothing to do.

A request the key function cannot name — no client address in the scope, behind
a socket or an unusual server — lands in one shared bucket rather than skipping
the limiter. A limiter that lets a request past because it could not identify it
is not a limiter; use `exempt=` to allow one deliberately.

`CsrfPolicy` runs two checks, and which one answers depends on the client.

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

`CsrfPolicy(trusted_hosts=[...])` bounds the `Host` header the expected
origin is derived from, which the token fallback's origin check uses. Without it
the Host is trusted, so that check depends on `TrustedHostPolicy` being
configured alongside it. Naming the hosts here makes the CSRF check
self-contained.

`TrustedHostPolicy` validates the whole authority before comparing its host.
A numeric port is ignored for matching, but user information, malformed ports,
and junk after a bracketed IPv6 literal are rejected rather than truncated into
an allowed hostname. On HTTP/2 the server also rejects a regular `Host` that
disagrees with `:authority`, before either value can reach host-based policy.

A preflight is checked against `allow_methods` rather than echoing it: asking
whether `DELETE` is allowed now gets an answer about `DELETE`. Origins compare
case-insensitively on scheme and host, as an origin should.

`CorsPolicy` refuses `allow_origins=["*"]` together with
`allow_credentials=True` at construction: honouring it means reflecting whatever
origin asked, alongside `Access-Control-Allow-Credentials: true`, which lets any
site read authenticated responses from yours. Name the origins that may send
credentials.

`SessionPolicy` defaults to `secure=True` (matching `CsrfPolicy`) and
requires a secret of at least 32 bytes. Pass `secure=False` for local plaintext
development. Rotate the secret without logging everyone out by naming
the old one:

```python
SessionPolicy(secret=NEW, previous_secrets=[OLD])
```

Cookies verify under either; new ones are signed with `secret`, and a session
carried in on a previous secret is re-signed on its next write.

A server-side session store may implement `touch(sid, max_age)`; when it does, a
live but unchanged session has its expiry extended on each request, so expiry is
sliding rather than absolute.


The default key is the client address, so each caller gets its own bucket; a
request over the limit gets a `429 Too Many Requests` (an RFC 9457 problem body)
with a whole-second `Retry-After`. Pass `key=` to bucket by an API key known at
ingress. For an authenticated identity, configure `principal_rate_limit=` so the
fixed post-authentication stage can use it. Ingress rate limiting covers every
request, misses included, so a flood of `404`s counts against the bucket too.

## Behind a proxy

If Wreath runs behind a TLS-terminating proxy or load balancer, requests arrive
over plain HTTP with the real scheme and client tucked into `X-Forwarded-*`
headers. Configure `ProxyPolicy` and Wreath will trust those headers from
your proxy — quietly fixing HSTS, secure-cookie, and CSRF decisions that would
otherwise be made as if the connection were insecure. The
[Deploy behind a proxy](../cookbook/recipes/behind-a-proxy.md) recipe shows the
full setup.

## What policy and custom hooks tell the document

Your application already knows it rate-limits, that it reads an
`Idempotency-Key`, and that it emits a `Cache-Control`. `HttpPolicy` components
and custom hooks can expose those facts as startup contracts, so they reach the
OpenAPI document and the client generated from it without request-time
introspection.

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

`generate_openapi` asks each component of the application's fixed `HttpPolicy`,
then each custom hook covering the route. There is no registry to keep in step:
a component or hook that offers nothing contributes nothing.

Three properties make the result trustworthy enough to generate a client from.

**A contract describes the instance, not the class.** A
`RateLimitPolicy(limit=60, window=60.0)` documents `RateLimit-Policy:
60;w=60`, because `describe()` reads the very tuple the refusal path appends. A
`ServerTimingPolicy(emit_header=False)` documents no header at all. The
document and the wire cannot drift, because there is only one copy of the value.

**Scope is honest.** First-class HTTP policy covers the whole application.
Custom route hooks are filtered by the same `applies_to` predicate their
compiled hook program evaluates, so a hook mounted on one router never appears
on a route outside it.

**A route's own declaration wins.** A route that documents its own `429` keeps
its wording; policy and hooks fill in only what the route left unsaid.

Because `describe()` runs at startup and never per request, none of this costs a
request anything — `wreath-request-trace` records no added boundary crossing.

**Reference:** [`wreath.policy`](../reference/policy.md) and
[`wreath.middleware`](../reference/middleware.md).
