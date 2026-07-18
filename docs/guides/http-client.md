# Outbound HTTP and webhooks

Most services also *make* requests — to a payment provider, an internal API, a
partner's webhook endpoint. Wreath brings the tools for that into the same
circle, so you don't reach for a third dependency and a second mental model.

## The outbound client

`wreath.http_client` is a dependency-free, lifespan-managed HTTP/1.1 client with
connection pooling, retries, redirect handling, and a destination policy that can
restrict where requests are even allowed to go:

```python
from wreath.http_client import HTTPClient

client = HTTPClient()
response = await client.get("https://api.example/health")
```

Open and close the client with your application lifespan so its connection pool
is managed cleanly rather than leaking between requests.

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
