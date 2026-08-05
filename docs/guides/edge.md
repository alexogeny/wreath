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
from wreath.edge import Upstream, UpstreamPool, serve

pool = UpstreamPool([
    Upstream("http://10.0.0.4:8000"),
    Upstream("http://10.0.0.5:8000"),
])
handle = await serve(pool, host="0.0.0.0", port=8080)
```

That is the whole configuration, and everything in it happens once. `serve()`
parses the upstream URLs, compiles them into a C table, opens eight connections
to each origin and binds a listener. From then on a forwarded request never
enters Python: a native protocol parses the head in place, picks an upstream,
writes the outbound head to an already-open transport and relays the response
back. No scope, no `Request`, no coroutine, no Task.

**There is no `app` parameter, and its absence is the design.** Give the proxy
something to call and a scope has to be built to call it with, and the Python
this exists to remove comes straight back. Having nothing to call is what makes
"no Python on the request path" structural rather than aspirational.

The connections are opened up front because `loop.create_connection` is a
coroutine. Reaching for one mid-request drags asyncio's Task and Future
machinery onto the path — 6.3 CPU-microseconds for the Task alone, before any
of the orchestration around it.

`connections=` is the per-upstream concurrency: a request that arrives while
every connection to the chosen origin is busy waits in C until one frees, rather
than being refused. It is the one setting worth thinking about, and unlike the
ASGI proxy below, getting it wrong costs latency rather than spurious 502s.

### Terminating TLS

```python
from wreath.reactor import metal_tls_context

handle = await serve(
    pool, host="0.0.0.0", port=443,
    ssl=metal_tls_context(certfile="cert.pem", keyfile="key.pem"),
)
```

The crypto runs in C on the same transport as everything else. An ordinary
`ssl.SSLContext` works too and still terminates correctly, but takes asyncio's
TLS — measured at 2.14× the per-request cost, because a TLS connection on that
path leaves the native transport entirely.

An `https://` upstream is handled the same way, and its handshake is paid during
the pre-warm rather than on a request. `upstream_cafile` names the trust store;
verification is on unless `upstream_verify=False` is typed out.

### The ASGI proxy, for what `serve()` refuses

`serve()` does not retry onto a second upstream and does not carry an upgrade.
Those are refused when the proxy is configured, by name, rather than at the
request that first needs one. When you need them — or when the proxy has to sit
*inside* an application, sharing its routes and middleware — `ReverseProxy` is
the ASGI one, and slower by construction.

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

One laptop, a small JSON response, three five-second runs after a warm-up. Every
proxy is single-threaded and **pinned to its own physical core**, with the load
generator on separate cores; the origin has a core of its own. That pinning is
not fussiness — see the note at the end, because without it none of these
numbers mean what they appear to.

**Requests per second per core, not CPU per process.** Roughly a third of a
proxy's core goes to kernel softirq handling loopback packets, and that work
appears in *no* process's CPU accounting. Per-process figures made this proxy
look 22% cheaper than nginx; per core the honest answer is 7%.

### Plaintext

| | req/s per core |
| --- | --- |
| origin, nothing in front | 74,000–75,000 |
| haproxy in **L4 mode** — no HTTP parsing at all | 39,100–39,400 |
| **`wreath.edge.serve()`** | **33,300–33,900** |
| nginx | 30,900–31,700 |
| haproxy, HTTP mode | 26,300–27,300 |
| `ReverseProxy` (ASGI) | 8,300 |

Two rows matter more than the ranking. **The origin's row is not a target a
proxy can approach**: forwarding is two message cycles and twice the packets, so
even haproxy doing *zero* HTTP work tops out at 53% of it. And the L4 row is the
real ceiling for a userspace proxy here — `serve()` is at 85% of it while
parsing two heads, transforming headers and selecting an upstream.

### TLS, which is what an edge actually does

| | req/s per core |
| --- | --- |
| **`wreath.edge.serve()`, `ssl=`** | **37,100–38,200** |
| nginx, TLS-terminating proxy | 36,800–37,600 |

Parity — ahead in two runs of three. Getting there needed TLS in C: through
`asyncio.sslproto` the same work costs 2.14× more, because a TLS connection left
the native transport entirely. Pass
[`metal_tls_context`](../reference/reactor.md); an ordinary `ssl.SSLContext`
still works and still takes the slow path.

### Body size

| body | `edge.serve()` | haproxy | nginx |
| --- | --- | --- | --- |
| 1 KB | 32,200 | 30,100 | 32,500 |
| 64 KB | 8,650 | **10,394** | 8,008 |
| 1 MB | 611 | **861** | 473 |

No cliff, and ahead of nginx throughout. haproxy pulls away on large bodies
because it `splice()`s them kernel-side while this copies through userspace —
worth about 40% at 1 MB, and not yet done.

### Run it on `metal_event_loop()`

Worth about +50%, for one argument:

```python
from wreath.reactor import metal_event_loop

asyncio.run(main(), loop_factory=metal_event_loop)
```

Use `metal_event_loop()` rather than building an `EventLoop` yourself: with the
native pieces switched off it is slower than stock asyncio, so a half-configured
one is worse than none.

### Two traps these numbers are pinned against

Both cost a day each here, and both make a benchmark read as a result when it is
measuring the harness.

- **The load generator is not free.** `oha` spent 22.5 CPU-µs per request
  driving a server that costs 9.9 — more than twice the thing under test.
  `h2load` is closer to parity. Either way you need more cores generating than
  serving, and any `req/s` figure taken without checking is the generator's.
- **Nothing looked saturated until it was pinned.** Every process sat at
  0.6–0.7 of a core at every concurrency from 32 to 512, while the machine
  burned 4–5 cores. The missing capacity was kernel softirq on loopback,
  invisible per process. Pinned to one core each, both origin and proxy sit at
  0.99 — they were saturated the whole time.

Finally, what this table does not say: one small response over loopback is the
shape that isolates per-request cost, and not the shape of production traffic.
Real network latency and connection churn are unmeasured here.

## What this does not do yet

Said plainly, because a proxy that is quiet about its limits is the dangerous
kind:

- **Request bodies are buffered** under `max_body` and refused with a 413 above
  it, on both paths. Responses stream in both directions on `serve()`; on
  `ReverseProxy` the request half needs `wreath.http_client`'s write path to
  accept an async iterator, which it does not yet.
- **No WebSocket or other upgrade.** `Upgrade` is hop-by-hop and dropped, so an
  upgrade request reaches the origin as an ordinary one.
- **`serve()` does not retry onto a second upstream.** A failed attempt is a
  502. Passive ejection still applies, so a failing origin stops being chosen,
  but the request that found it is not re-sent. `ReverseProxy` has the attempt
  loop.
- **Large bodies are copied through userspace.** haproxy `splice()`s them
  kernel-side and is about 40% cheaper at 1 MB as a result.
- **`serve()` is HTTP/1.1 on both sides.** `ReverseProxy` inherits whatever the
  server in front of it negotiated.
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
