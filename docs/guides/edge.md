---
description: Run wreath as a reverse proxy in front of your origins, with an in-memory load balancer, passive health, and retries.
keywords: reverse proxy, load balancer, nginx replacement, upstream pool, ewma, passive health, hop-by-hop
---
# Putting wreath at the edge

A wreath is a circle of separate things held in one shape, and a deployment is
the same idea one level up: several processes, on several machines, that a
reader reaches through a single address. Something has to stand at that address
and hold the shape.

Usually that something is a second stack — nginx or HAProxy, with its own
configuration language, its own log format, and its own ideas about timeouts.
For a large fleet that is a good trade. For three boxes it is a second thing to
learn, patch and reason about, in front of software that already knows how to
parse HTTP.

`wreath.edge` is the other option: the proxy is a wreath application. It uses
the same server, the same client, the same `wreath.logging` and the same flight
recorder as the origins behind it, so a request that crosses the edge and the
application shows up as one story rather than two.

## The smallest useful edge

```python
from wreath import Request, Wreath
from wreath.edge import ReverseProxy, Upstream, UpstreamPool
from wreath.http_client import ClientLimits, ClientTimeout, HTTPClient

pool = UpstreamPool([
    Upstream("http://10.0.0.4:8000"),
    Upstream("http://10.0.0.5:8000"),
])
# Size the upstream pool to the *inbound* concurrency; see below, this is the
# one setting a proxy cannot leave at its default.
clients = {
    u.url: HTTPClient(
        u.url,
        base_url=u.url,
        limits=ClientLimits(max_connections=256, max_keepalive_connections=256),
        timeout=ClientTimeout(pool=10.0),
    )
    for u in pool.upstreams
}
proxy = ReverseProxy(pool, clients)

edge = Wreath()

@edge.on_startup
async def start_clients(app):
    for client in clients.values():
        await client.start()

@edge.get("/{path:path}")
async def relay(request: Request, path: str):
    return await proxy(request)
```

One `HTTPClient` per upstream, started once and held: the client owns the
connection pool, and building one per request would give away keep-alive, which
is most of what a proxy is for.

## Size the upstream pool, or it will 502 at you

The one setting a proxy cannot leave at its default. `ClientLimits` ships with
`max_connections=20`, `max_keepalive_connections=10` and a one-second `pool`
timeout, which are sensible for an application making occasional outbound calls
and wrong for a proxy, where inbound concurrency *is* upstream concurrency.

The failure is not subtle once you know it, and thoroughly confusing before:
requests queue against a pool smaller than the offered load, some of them exceed
the one-second wait, and the client raises `PoolTimeout` — which the proxy
turns into a 502 because it cannot tell a slow pool from a sick origin. Measured
on a laptop at 32 inbound connections against defaults: 86 spurious 502s, and
throughput swinging by more than 100% between runs. With the pool sized to the
load: zero non-200s and a run-to-run spread under 1%.

Set `max_connections` at or above the concurrency you expect to serve, and give
`pool` a timeout long enough that queueing is not an error.

## Choosing an upstream

The default policy is **peak EWMA**: an upstream's score is its exponentially
weighted mean response time multiplied by its queue depth. Neither signal is
enough alone — one origin with two fast requests in flight should beat one with
a single slow one, and only the product says so. `round-robin` and
`least-connections` are there when you want the simpler behaviour.

**Every upstream is tried once before any scoring happens.** A new upstream's
latency starts as a guess, and the moment a warm one measures faster than that
guess the new one is never selected again — so it never earns a measurement to
replace the guess with. Without that rule an upstream you just added starves
permanently, which is exactly what it did the first time this was measured: 40
of 40 requests to one origin, the other still showing the cold default.

All of this is memory. Selection, health and in-flight counts are read and
written on the request path, so nothing here makes a database call to decide
where a request goes.

## When an origin stops answering

Health is **passive**: the requests already flowing are the probe, so a failing
origin is noticed at the speed of real traffic rather than of a timer. After
`Ejection.failures` consecutive failures an upstream stops being selected for a
cooldown that doubles per ejection, up to `Ejection.cap`.

Ejection is a cooldown and not a removal — the upstream has to be probed back,
and removing it would lose the only record that it should return. And when
*every* upstream is ejected the pool still answers, handing back whichever
cooldown ends soonest: a proxy that refuses while every origin is briefly
ejected turns a recoverable blip into an outage of its own making, and the
request it would decline is the one that proves recovery.

## Retries, and the one method that never gets one

A failed attempt on one upstream is retried on another, up to
`DEFAULT_ATTEMPTS`. Only up to the moment the response head comes back: after
that the response is already on its way to the client, and a second attempt
would deliver a prefix twice.

Only the idempotent methods qualify — `GET`, `HEAD`, `PUT`, `DELETE`,
`OPTIONS`, `TRACE`. **`POST` is not retried**, and a failure after the request
reached the origin is indistinguishable from one before it, so retrying risks a
second order rather than a second attempt. A client that knows its POST is safe
to repeat says so with `Idempotency-Key` — a claim only the client can make, and
one [`wreath.middleware.idempotency`](../reference/middleware.md) already
speaks.

## What the proxy does to headers

A proxy is a recipient on one connection and a sender on another, and some
fields describe the connection rather than the message. Those are dropped in
both directions: the ones RFC 9110 §7.6.1 names, *plus* whatever the inbound
`Connection` header lists for this particular message.

Two rules about forwarding headers are worth stating outright, because the
difference between them is a security property:

- **`X-Forwarded-For` is replaced, never appended to.** Every parser that reads
  "the client" reads the leftmost element, so appending lets the caller choose
  what the origin believes about them.
- **`Via` is appended**, because a chain is the whole point of it, and it is not
  an authorization input anywhere.

Both the RFC 7239 `Forwarded` form and the `X-Forwarded-*` family are emitted,
because the latter is what almost everything actually reads — including wreath's
own [`ProxyHeadersMiddleware`](middleware.md), so wreath can sit behind itself.

`Host` and `Content-Length` are recomputed rather than relayed. The first
because the outbound `Host` is the upstream's authority, and relaying the
client's would ask one origin to answer for another's name. The second because
**the outbound framing must describe what this proxy actually sends** — a proxy
that forwards a claimed length is how a proxy and an origin come to disagree
about where one message ends and the next begins, which is the whole of request
smuggling. `Transfer-Encoding` is already gone as hop-by-hop, so between them
nothing about the inbound framing survives into the outbound message.

## How fast, honestly

A micro benchmark on one laptop, one small JSON response, 32 connections, median
of three five-second runs after a warm-up, with an A/A noise floor of about
0.5%. The same wreath origin sits behind every proxy and is measured on its own,
so the comparison says what the *proxy* costs rather than how fast the origin is:

| | req/s | p50 | p99 | share of the origin |
| --- | --- | --- | --- | --- |
| origin, nothing in front | 38,800 | 0.79 ms | 1.59 ms | — |
| haproxy 3.4.3 | 37,300 | 0.74 ms | 1.63 ms | 96% |
| nginx 1.30.4 | 32,500 | 0.97 ms | 1.20 ms | 84% |
| **`wreath.edge`** | **5,800** | 5.4 ms | 6.1 ms | **15%** |

**`wreath.edge` is roughly five to six times slower than nginx, and that is the
number to plan against.** haproxy and nginx are C event loops that have been
tuned at this for two decades, and a proxy that runs a second full HTTP request
cycle per request was never going to match them.

Where the cost is, established by ablation rather than by profiler:

- **Not the parser.** The HTTP client's native C reader is already in use, and
  turning it off (`WREATH_CLIENT_NATIVE_STREAM=0`) costs only about 20% —
  12,600 requests a second becomes 10,500. Rewriting more of this in C is not
  the lever it looks like.
- **Not the server.** Wreath answers at 38,800 in the same run, with a Python
  handler.
- **The client is the ceiling.** `HTTPClient` on its own, no server in front,
  reaches about 12,600 requests a second. Nothing built on it can go faster.
- **The proxy sits under that ceiling** at about 5,800, and roughly one loop
  turn of the difference is structural: a handler is stepped before it owns an
  asyncio Task, so the first client call inside one yields once to acquire a
  task before it can arm a timeout.

Read the headline as a sizing statement rather than a verdict. Several thousand
requests a second in front of a handful of origins covers a great many real
deployments, and it buys one stack, one configuration language and one set of
logs. Past that, put haproxy in front — which is exactly the arrangement
[`ProxyHeadersMiddleware`](middleware.md) exists for.

## What this does not do yet

Said plainly, because a proxy that is quiet about its limits is the dangerous
kind:

- **Request bodies are buffered** under `DEFAULT_MAX_BODY` and refused with a
  413 above it. Responses stream; the request half needs
  `wreath.http_client`'s write path to accept an async iterator, which it does
  not yet.
- **No WebSocket or other upgrade.** `Upgrade` is hop-by-hop and dropped, so an
  upgrade request reaches the origin as an ordinary one.
- **One certificate.** `wreath.server`'s `TLSConfig` takes a single
  certificate and key, so terminating TLS for several hostnames — SNI selection
  — is not available here yet.
- **One process.** Round-robin and peak-EWMA stay correct behind
  `SO_REUSEPORT`, because each worker's view is independently valid. *Global*
  least-connections does not: no worker sees the fleet's counts.
- **No desired-state distribution.** The pool is what you construct. Reading it
  from PostgreSQL and reconciling on `LISTEN`/`NOTIFY` is the obvious next step
  and is not built.

Reference: [`wreath.edge`](../reference/edge.md).
