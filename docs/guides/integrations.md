---
description: Call services and deliver or receive verified events without hiding retry and idempotency semantics.
keywords: guide HTTP client service client webhooks signatures email notifications retries circuit breaker
---

# Integration boundaries

An integration is more than an HTTP request. Name which side owns authentication,
timeouts, retries, replay detection, idempotency and the durable record before adding
the convenience wrapper.

| Boundary | Wreath owner | Use it for |
|---|---|---|
| outbound HTTP transport | `wreath.http_client` | pools, DNS, TLS, HTTP versions and limits |
| an authenticated service | `wreath.service_client` | bearer credentials, base paths, retries and circuit state |
| verified inbound events | `Wreath.webhooks()` | exact-body signatures, typed events and replay claims |
| durable outbound events | `wreath.webhooks` providers | attempts, signing, backoff and delivery records |
| HTTP message integrity | `wreath.signatures` | RFC 9421 request and response signatures |
| email | `wreath.email` | typed messages and bounded provider delivery |
| user-facing delivery | `wreath.notifications` | channels, preferences, digests and retries |

Register clients on the application so lifespan owns their pools and infrastructure
inference can see egress. Put one absolute timeout at the business boundary; stacked
client and job retries otherwise multiply into an accidental retry storm.

```python title="client.py"
from dataclasses import dataclass

from wreath import Wreath
from wreath.config import Environment, Secret, read_osenv
from wreath.service_client import ServiceClient


@dataclass(frozen=True)
class Settings:
    ledger_url: str
    ledger_token: Secret[str]


settings = Environment(read_osenv()).bind(Settings)
app = Wreath()
transport = app.http_client("ledger", base_url=settings.ledger_url)
ledger = ServiceClient(
    transport,
    token=settings.ledger_token.reveal(),
    base_path="/v1",
)
```

For incoming webhooks, verification happens over the exact received bytes before the
payload dataclass is constructed. A replay store claims the provider event id. That is
transport deduplication, not proof that the business effect happened once. Couple a
`PostgresWebhookInbox` claim and durable job enqueue in one transaction, then give the
effect its own idempotency key. The complete implementation is in
[let customers wire the world together](../stories/automation-backplane.md).

For outbound webhooks, persist the envelope before attempting delivery. Sign the bytes
that will be sent, preserve the event id across attempts, compute backoff once, and
record the terminal response. A `2xx` means the peer accepted the delivery; it does
not prove what the peer later did with it.

Use [exactly-once effects](../cookbook/recipes/exactly-once.md) for the transaction and
retry model, and [protocols and delivery reference](../reference/protocols.md) for the
complete client, webhook, signature, email and notification APIs.
