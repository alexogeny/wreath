# Send a signed webhook

When you notify a partner's endpoint, they reject anything without a valid HMAC
signature — so every call has to carry a signed, timestamped envelope. Don't
hand-roll the scheme: build a `WebhookEnvelope`, sign it with an
`HMACWebhookSigner`, and post the signed headers alongside the exact body:

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
over the *exact* body — that the partner's `HMACWebhookVerifier` checks. Key
rotation is built in: sign with `key_id="current"` while a verifier still accepts
a `"previous"` key through the overlap.

## Retries you don't have to lose

A single POST can fail mid-flight. For at-least-once delivery, register the
partner as a `destination` with a Postgres outbox and let the dispatcher retry:

```python
from wreath.webhooks import PostgresWebhookOutbox

partners = app.webhooks("partners")
receiver = partners.destination(
    "shipping", client=client, path="/hooks",
    signer=signer, outbox=PostgresWebhookOutbox(),
)

conn = await db.acquire("write")
try:
    async with conn.transaction() as tx:
        await receiver.enqueue(tx, "order.shipped", {"order_id": order_id})
finally:
    await db.release("write", conn)
```

`enqueue` commits the delivery *in your transaction*, so the webhook is only sent
if the business write commits. The supervised `WebhookDispatcher` then claims due
rows with a fence token and retries transient failures (`429`, `503`, …) with
backoff — no double-delivery, nothing lost across a redeploy.
