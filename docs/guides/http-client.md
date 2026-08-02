# Outbound HTTP and webhooks

Most services also *make* requests — to a payment provider, an internal API, a
partner's webhook endpoint. Wreath brings the tools for that into the same
circle, so you don't reach for a third dependency and a second mental model.

## User story: sign an outgoing webhook

> *As an API author, I notify a partner's endpoint whenever an order ships. They
> reject anything without a valid HMAC signature, so every call I make has to
> carry a signed, timestamped envelope — and I don't want to hand-roll the
> signing scheme.*

```python
from datetime import UTC, datetime
from wreath.webhooks import HMACWebhookSigner, WebhookEnvelope

client = app.http_client("partner", base_url="https://partner.example")
signer = HMACWebhookSigner({"current": settings.webhook_secret}, key_id="current")

async def notify_shipped(order_id: str, body: bytes) -> None:
    envelope = WebhookEnvelope(
        id=order_id,
        type="order.shipped",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=body,
    )
    await client.post("/hooks", headers=signer.headers(envelope), body=envelope.body)
```

`signer.headers(envelope)` returns the signed, timestamped headers — HMAC-SHA256
over the exact body — that the partner's `HMACWebhookVerifier` checks. Key
rotation is built in: sign with `key_id="current"` while a verifier still accepts
a `"previous"` key through the overlap.

## The outbound client

`wreath.http_client` is a dependency-free, lifespan-managed HTTP/1.1 client with
connection pooling, retries, redirect handling, and a destination policy that can
restrict where requests are even allowed to go:

```python
client = app.http_client("payments", base_url="https://api.example")

@app.get("/health-check")
async def health_check(request) -> dict:
    response = await client.get("/health")
    return {"upstream": response.status}
```

A client is named, carries its `base_url`, and makes requests against paths
resolved under it. Registered on the application like this, its connection pool
is opened during lifespan startup and closed at shutdown — nothing to leak
between requests. Outside an application, construct
`HTTPClient("payments", base_url=...)` directly and manage its lifecycle
yourself.

## Keep inbound URLs out of outbound origins

An `HTTPClient` is pinned to the origin in its `base_url`: request targets are
origin-relative, cross-origin redirects are refused, and every resolved address
is checked before a socket is opened. The default `DestinationPolicy` refuses
private, loopback, link-local, special, and non-global addresses. It also checks
the IPv4 destination embedded in the well-known NAT64 prefix, so translation
cannot turn a globally classified IPv6 answer into a route to loopback or a
cloud metadata service.

If a request chooses which upstream to call, map that input to preconfigured,
named clients. Do not construct a client directly from a caller-provided URL.
An internal service can opt into only the address class it needs without also
opening loopback or link-local destinations:

```python
from wreath.http_client import DestinationPolicy

billing = app.http_client(
    "billing",
    base_url="https://billing.internal",
    destination=DestinationPolicy(
        hosts=("billing.internal",),
        ports=frozenset({443}),
        allow_private=True,
    ),
)
```

## Rate limiting and retries

An outbound client can throttle itself and retry transient failures without a
wrapper library. Pass a `RatePolicy` and/or `RetryPolicy` when you register it —
both are forwarded straight through to the client:

```python
from wreath.http_client import RatePolicy, RetryPolicy

app.http_client(
    "payments",
    base_url="https://api.example.com",
    rate=RatePolicy(enabled=True, rate=20, capacity=40),   # 20 req/s, burst 40
    retry=RetryPolicy(attempts=3),                          # + backoff + Retry-After
)
```

`RatePolicy` is a continuous token bucket — the same native `TokenBucket` the
inbound rate-limiter uses — so a request *parks* until a token frees (up to
`max_wait`) rather than being rejected. `RetryPolicy` retries idempotent requests
on transient statuses (`408/425/429/500/502/503/504`) with exponential backoff
and bounded jitter, and honours a `Retry-After` header on `429`/`503`.

## Webhooks

Sending and receiving webhooks reliably is more than a POST — you need signing,
verification, and a way to not lose or double-deliver events. `wreath.webhooks`
provides HMAC signing and verification, an envelope format that carries
correlation and causation, and Postgres-backed inbox and outbox stores for
at-least-once delivery you can reason about:

```python
from wreath.webhooks import HMACWebhookSigner, HMACWebhookVerifier
```

**Reference:** [`wreath.http_client`](../reference/http_client.md),
[`wreath.webhooks`](../reference/webhooks.md).
