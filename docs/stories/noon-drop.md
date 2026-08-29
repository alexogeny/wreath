---
description: Design a flash-sale system that remains coherent through retries, double-clicks and webhook redelivery.
keywords: flash sale ticket drop edge rate limit idempotency webhook outbox inventory concurrency canary
---

```hero
eyebrow: Story 07 · one scarce outcome, many equivalent attempts
title: Survive the noon drop.
lede: Fifty thousand tickets go on sale at once. People double-click, refresh and retry from another device while payment events arrive more than once.
signal: native edge
signal: admission control
signal: idempotent writes
signal: webhook inbox and outbox
action: Follow one purchase -> #one-purchase-many-deliveries
action: Browse delivery surfaces -> ../reference/index.md#boundaries-and-delivery
```

## The scene

At 11:59 the catalogue is mostly reads. At noon it becomes contention for scarce
inventory. A new application build is receiving a small share of traffic. Payment
providers will retry, browsers will retry, and a person may genuinely initiate the
same intention from two devices.

The product needs one answer it can explain, not a mythical network where every
message arrives once.

## One purchase, many deliveries

Click “reserve” twice while the first response is delayed. Redeliver the payment
webhook. Retry the browser request through another edge connection. The system returns
the existing reservation, processes the provider event once and records every attempt
that collapsed into that outcome.

> The invariant: equivalent attempts converge on one reservation, while distinct
> attempts still compete honestly for the remaining inventory.

## The system shape

```text
crowd ──> edge traffic policy ──> admission ──> idempotent reservation
                                                  │
payment provider ──> verified inbox ──> fulfilment workflow ──> signed outbox
```

| Pressure | Wreath surface | Defence |
|---|---|---|
| origin overload | `wreath.edge`, `wreath.policy.admission`, `wreath.policy.ratelimit` | route, bound and shed before expensive work |
| duplicate browser writes | `wreath.policy.idempotency` | stable result per declared intention |
| scarce inventory | `wreath.postgres` transactions | one accepted claim per unit |
| provider redelivery | `wreath.webhooks` | verified, replay-protected durable inbox |
| downstream delivery | webhook outbox and `wreath.workflows` | retryable effects with visible state |
| public catalogue traffic | `wreath.response_cache` | cache and purge by explicit tags |
| a bad canary | `wreath.policy.traffic`, `wreath.recording` | bounded rollout and evidence |

## Build it in four acts

### 1. Make reservation one transaction

Claim inventory and create the reservation together. Return “sold out” from the first
point it is knowable. Race several distinct buyers and prove the available count never
goes negative.

### 2. Give intention a key

Attach idempotency to the purchase operation. Repeat the same request concurrently and
return the original result. Treat reusing a key with different inputs as a client bug:
the server replays the original outcome, which is why the response includes exactly
what was reserved.

### 3. Treat webhooks as delivery

Verify the provider signature and freshness before parsing business fields. Store the
event before acknowledging it. Process it through a claimable inbox and place outbound
notifications in an outbox after the reservation changes.

### 4. Put the edge under pressure

Cache the catalogue, rate-limit expensive paths and send a small traffic slice to the
new build. Record a failed request, remove the slice and replay the exact input against
the candidate fix.

## Implement the reservation boundary

This runnable cut uses an in-process inventory ledger and Wreath's idempotency policy.
The bearer identity matters: idempotency keys are scoped to an authenticated principal,
so Alice can never receive Bob's stored response.

```python title="app.py"
import asyncio
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.binding import Body
from wreath.exceptions import Conflict
from wreath.policy import HttpPolicy, IdempotencyPolicy
from wreath.response import JSONResponse


@dataclass
class ReservationRequest:
    ticket_type: str
    quantity: int


inventory = {"balcony": 2, "floor": 1}
reservations: list[dict] = []
inventory_lock = asyncio.Lock()

app = Wreath(http_policy=HttpPolicy(idempotency=IdempotencyPolicy()))
identities = {
    "alice-token": Identity("alice"),
    "bob-token": Identity("bob"),
}


def verify_token(token: str) -> Identity | None:
    return identities.get(token)


app.configure_auth(BearerTokenBackend(verify_token))


@app.post("/reservations")
@authenticated()
async def reserve(
    request: Request,
    command: Annotated[ReservationRequest, Body()],
):
    if command.quantity < 1:
        raise Conflict("quantity must be positive")
    async with inventory_lock:
        available = inventory.get(command.ticket_type, 0)
        if available < command.quantity:
            raise Conflict(
                f"only {available} {command.ticket_type} ticket(s) remain"
            )
        inventory[command.ticket_type] = available - command.quantity
        reservation = {
            "id": f"r-{len(reservations) + 1}",
            "buyer": request.identity.id,
            "ticket_type": command.ticket_type,
            "quantity": command.quantity,
        }
        reservations.append(reservation)
    return JSONResponse(reservation, status=201)
```

The inventory check and decrement share one critical section. The idempotency policy
runs after authentication and before the handler: a retry after completion receives
the stored `201`, while a concurrent duplicate receives `409` with `Retry-After: 1`.

### Prove retries and honest competition

```python title="test_app.py"
import asyncio

from wreath.testing import TestClient

from app import app, inventory, reservations


def headers(token: str, key: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {token}",
        "idempotency-key": key,
    }


async def test_a_double_click_replays_one_reservation() -> None:
    inventory["floor"] = 1
    reservations.clear()
    async with TestClient(app) as client:
        first = await client.post(
            "/reservations",
            headers=headers("alice-token", "buy-floor-1"),
            json={"ticket_type": "floor", "quantity": 1},
        )
        retry = await client.post(
            "/reservations",
            headers=headers("alice-token", "buy-floor-1"),
            json={"ticket_type": "floor", "quantity": 1},
        )

    assert first.status == retry.status == 201
    assert first.json() == retry.json()
    assert dict(retry.headers)[b"idempotency-replayed"] == b"true"
    assert inventory["floor"] == 0
    assert len(reservations) == 1


async def test_distinct_buyers_compete_for_the_last_ticket() -> None:
    inventory["floor"] = 1
    reservations.clear()
    async with TestClient(app) as client:
        alice, bob = await asyncio.gather(
            client.post(
                "/reservations",
                headers=headers("alice-token", "alice-floor"),
                json={"ticket_type": "floor", "quantity": 1},
            ),
            client.post(
                "/reservations",
                headers=headers("bob-token", "bob-floor"),
                json={"ticket_type": "floor", "quantity": 1},
            ),
        )

    assert sorted((alice.status, bob.status)) == [201, 409]
    assert inventory["floor"] == 0
    assert len(reservations) == 1
```

```bash
uv run wreath test -k reservation
uv run wreath dev app:app
```

## Receive a payment event as a delivery

Wreath's webhook source verifies the exact bytes before binding the payload, rejects a
replayed event id, and dispatches only a declared event type.

```python title="payments.py"
from dataclasses import dataclass

from wreath.config import Environment, Secret, read_osenv
from wreath.webhooks import HMACWebhookVerifier, LocalReplayStore, WebhookContext

from app import app


@dataclass
class PaymentCaptured:
    reservation_id: str
    provider_charge_id: str


@dataclass(frozen=True)
class PaymentSettings:
    payment_webhook_secret: Secret[str]


settings = Environment(read_osenv()).bind(PaymentSettings)


hooks = app.webhooks("payments")
payments = hooks.source(
    "provider",
    path="/webhooks/payments",
    verifier=HMACWebhookVerifier(
        {"current": settings.payment_webhook_secret.reveal()},
        max_age=300,
    ),
    replay=LocalReplayStore(max_entries=20_000, ttl=300),
)


@payments.event("payment.captured", payload=PaymentCaptured)
async def payment_captured(
    context: WebhookContext,
    event: PaymentCaptured,
) -> None:
    await fulfil_once(
        reservation_id=event.reservation_id,
        event_id=context.envelope.id,
        charge_id=event.provider_charge_id,
    )
```

For several API replicas, replace both process-local stores: use
`PostgresIdempotencyStore` for request replays and `PostgresWebhookInbox` for provider
events. Register them on the application so `wreath schema sql` includes their tables.
The reservation itself should become one PostgreSQL transaction with a conditional
inventory update; the lock above teaches the invariant, but only the database can
enforce it across workers.

## The larger idea

Exactly-once delivery is not the promise. One explainable business outcome is. Wreath
brings the edge policy, transaction, idempotency record, webhook ledger and workflow
close enough that they can agree on what happened.

Next: [take the application somewhere the network disappears](field-operations.md),
or [choose a build path](../start/paths.md).
