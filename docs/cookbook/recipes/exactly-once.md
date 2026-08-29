---
description: Turn repeated authenticated HTTP attempts into one stored application response.
keywords: recipe idempotency exactly once retry effect authentication PostgreSQL
---

# Exactly-once HTTP effects

“Exactly once” at an HTTP boundary means equivalent retries observe one committed
application outcome. Scope the key to the authenticated principal and store the first
response.

```python title="app.py"
from wreath import Request, Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.policy import HttpPolicy, IdempotencyPolicy
from wreath.response import JSONResponse

charges: list[dict] = []


def verify(token: str) -> Identity | None:
    return Identity("buyer-7") if token == "buyer-token" else None


app = Wreath(http_policy=HttpPolicy(idempotency=IdempotencyPolicy()))
app.configure_auth(BearerTokenBackend(verify))


@app.post("/charges")
@authenticated()
async def create_charge(request: Request) -> JSONResponse:
    charge = {"id": f"ch-{len(charges) + 1}", "buyer": request.identity.id}
    charges.append(charge)
    return JSONResponse(charge, status=201)
```

```python title="test_app.py"
from wreath.testing import TestClient

from app import app, charges


async def test_a_retry_observes_the_original_charge() -> None:
    charges.clear()
    headers = {
        "authorization": "Bearer buyer-token",
        "idempotency-key": "checkout-42",
    }
    async with TestClient(app) as client:
        first = await client.post("/charges", headers=headers)
        retry = await client.post("/charges", headers=headers)

    assert first.status == retry.status == 201
    assert first.json() == retry.json()
    assert dict(retry.headers)[b"idempotency-replayed"] == b"true"
    assert len(charges) == 1
```

`MemoryIdempotencyStore` is one-process evidence. Use `PostgresIdempotencyStore` for a
fleet, with retention longer than the caller's retry window. The handler still owns
its database transaction: an outbound provider call needs that provider's own
idempotency key or an outbox, because no local transaction can atomically commit a
remote side effect.

See [policy and hardening](../../guides/policy.md) and
[idempotency API](../../reference/policy.md).
