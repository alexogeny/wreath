---
description: Build a tenant-bound automation platform over signed webhooks, durable workflows, live run updates and replayable evidence.
keywords: automation workflows webhooks triggers sockets user hooks durable replay multi-tenant
---

```hero
eyebrow: Story 03 · customer-programmable automation
title: Let customers wire the world together.
lede: A payment arrives, a schedule fires or a socket changes state. Each event becomes one durable run through user-authored steps—with retries, live progress and enough evidence to explain every effect.
signal: signed triggers
signal: durable workflows
signal: sandboxed hooks
signal: replayable runs
action: Build the event path -> #build-the-event-path
action: Lift it into production -> #make-the-backplane-durable
```

## The scene

A customer draws a workflow: when a payment succeeds, enrich the account, call their
JavaScript hook, post a message and update their CRM. Another customer listens to a
device socket and fans the same event into a completely different graph. Schedules,
manual runs and partner webhooks all enter the same system.

The canvas is not the difficult part. The difficult part is the backplane underneath:

- a provider retries an event while the first attempt still runs;
- a worker dies after the hook responds but before the next step starts;
- two customers use the same provider event identifier;
- a workflow changes while older instances are still resumable;
- a hook loops an event back into the platform;
- support needs to explain why a particular customer received a particular effect.

> The invariant: one authenticated trigger creates one tenant-bound run; every
> completed step stays completed across retries and restarts, and every external
> effect carries an idempotency key derived from that run.

## The system shape

```text
provider webhook ─┐
schedule tick ─────┼─→ verified trigger → tenant inbox → durable run ledger
socket event ──────┘                                      │
                                                         ├─→ connector action
workflow editor ←──── live room ←──── run progress ←─────┼─→ sandboxed user hook
                                                         └─→ signed outbound event
                                                                  │
recording + correlation + workflow state ─────────────────────────┘
```

The webhook inbox owns deduplication. The workflow store owns step completion. Jobs own
retry and lease. Rooms own only live fan-out. The hook runner owns untrusted code. That
separation is what makes each promise honest.

## Build the event path

This runnable slice binds one provider secret to one account. It verifies the exact
body, validates a typed event, deduplicates the provider event, executes an idempotent
workflow instance and broadcasts completion to the account's live run room.

```python title="app.py"
from dataclasses import dataclass

from wreath import Wreath
from wreath.protobuf import encode, field, message
from wreath.rooms import RoomRegistry
from wreath.webhooks import (
    HMACWebhookVerifier,
    LocalReplayStore,
    WebhookContext,
)
from wreath.websocket import WebSocket
from wreath.workflows import InMemoryWorkflowStore, Workflow

ACCOUNT_ID = "acct-lumen"
WEBHOOK_KEYS = {"current": b"local-automation-webhook-secret-001"}


@dataclass
class PaymentSucceeded:
    workflow_id: str
    customer_id: str
    amount_cents: int


@dataclass(frozen=True)
class WorkflowSpec:
    hook: str
    live_channel: str


@message
class RunUpdate:
    run_id: str = field(1)
    state: str = field(2)
    channel: str = field(3)


workflow_specs = {
    (ACCOUNT_ID, "welcome-paid-customer"): WorkflowSpec(
        hook="customer-welcome.js",
        live_channel="billing",
    )
}
effects: list[dict] = []
workflow_store = InMemoryWorkflowStore()
run_rooms = RoomRegistry()

app = Wreath()
source = app.webhooks("billing").source(
    "lumen-payments",
    path="/hooks/lumen/payments",
    verifier=HMACWebhookVerifier(WEBHOOK_KEYS, max_age=300),
    replay=LocalReplayStore(max_entries=1_000, ttl=300),
)


@app.websocket("/accounts/{account_id}/runs")
async def run_updates(websocket: WebSocket) -> None:
    account_id = websocket.path_params["account_id"]
    await websocket.accept()
    await run_rooms.join(account_id, websocket)
    try:
        async for _message in websocket:
            pass
    finally:
        await run_rooms.leave(account_id, websocket)


@source.event("payment.succeeded", payload=PaymentSucceeded)
async def payment_succeeded(
    context: WebhookContext,
    event: PaymentSucceeded,
) -> None:
    spec = workflow_specs[(ACCOUNT_ID, event.workflow_id)]
    run_key = f"{ACCOUNT_ID}:{context.envelope.id}"
    automation = Workflow("payment_succeeded")

    @automation.step
    async def invoke_customer_hook(step) -> str:
        invocation = {
            "idempotency_key": f"{run_key}:invoke_customer_hook",
            "account_id": ACCOUNT_ID,
            "hook": spec.hook,
            "customer_id": event.customer_id,
            "amount_cents": event.amount_cents,
        }
        effects.append(invocation)
        return invocation["idempotency_key"]

    @automation.step
    async def publish_completion(step) -> dict:
        update = RunUpdate(
            run_id=run_key,
            state="completed",
            channel=spec.live_channel,
        )
        await run_rooms.broadcast(ACCOUNT_ID, encode(update))
        return {
            "run_id": update.run_id,
            "state": update.state,
            "channel": update.channel,
        }

    await automation.run(store=workflow_store, key=run_key)
```

The in-memory stores state their limit plainly: this cut proves the contracts inside
one process. The production lift replaces owners, not semantics.

### Test retry and live progress together

```python title="test_app.py"
from wreath.negotiation import JSON
from wreath.protobuf import decode
from wreath.testing import TestClient
from wreath.temporal import now
from wreath.webhooks import HMACWebhookSigner, WebhookEnvelope

from app import ACCOUNT_ID, WEBHOOK_KEYS, RunUpdate, app, effects


def signed_event(event_id: str, body: bytes) -> tuple[dict[str, str], bytes]:
    envelope = WebhookEnvelope(
        id=event_id,
        type="payment.succeeded",
        version="1",
        timestamp=now(),
        content_type="application/json",
        body=body,
    )
    signer = HMACWebhookSigner(WEBHOOK_KEYS, key_id="current")
    headers = {
        name.decode("ascii"): value.decode("ascii")
        for name, value in signer.headers(envelope)
    }
    headers["content-type"] = "application/json"
    return headers, body


async def test_one_provider_event_creates_one_run_and_one_effect() -> None:
    effects.clear()
    body = JSON.encode(
        {
            "workflow_id": "welcome-paid-customer",
            "customer_id": "cus-42",
            "amount_cents": 12900,
        }
    )
    headers, body = signed_event("evt-paid-42", body)

    async with TestClient(app) as client:
        async with client.websocket(f"/accounts/{ACCOUNT_ID}/runs") as socket:
            accepted = await client.post(
                "/hooks/lumen/payments",
                headers=headers,
                content=body,
            )
            update = decode(RunUpdate, await socket.receive_bytes())
            duplicate = await client.post(
                "/hooks/lumen/payments",
                headers=headers,
                content=body,
            )

    assert accepted.status == 204
    assert duplicate.status == 409
    assert update.run_id == "acct-lumen:evt-paid-42"
    assert update.state == "completed"
    assert update.channel == "billing"
    assert effects == [
        {
            "idempotency_key": "acct-lumen:evt-paid-42:invoke_customer_hook",
            "account_id": "acct-lumen",
            "hook": "customer-welcome.js",
            "customer_id": "cus-42",
            "amount_cents": 12900,
        }
    ]


async def test_a_bad_signature_never_becomes_a_run() -> None:
    effects.clear()
    body = b'{"workflow_id":"welcome-paid-customer","customer_id":"x","amount_cents":1}'
    headers, body = signed_event("evt-bad-43", body)
    headers["wreath-webhook-signature"] = "v1=bad"

    async with TestClient(app) as client:
        response = await client.post(
            "/hooks/lumen/payments",
            headers=headers,
            content=body,
        )

    assert response.status == 401
    assert effects == []
```

```bash
uv run wreath test -k automation
uv run wreath dev app:app
```

## Make the backplane durable

In production, the verified webhook handler and the queued job share one PostgreSQL
transaction. The inbox claim and job row either commit together or neither does. A job
key repeats the event identity inside the tenant, while the workflow store records each
completed step.

```python title="production.py"
from contextlib import asynccontextmanager
from dataclasses import dataclass

from wreath import Wreath
from wreath.config import Environment, Secret, read_osenv
from wreath.negotiation import JSON
from wreath.service_client import ServiceClient
from wreath.webhooks import HMACWebhookVerifier, PostgresWebhookInbox, WebhookContext


@dataclass(frozen=True)
class Settings:
    database_url: str
    hook_runner_url: str
    hook_runner_token: Secret[str]
    lumen_webhook_secret: Secret[str]


settings = Environment(read_osenv()).bind(Settings, prefix="AUTOMATION")

app = Wreath()
database = app.postgres("main", dsn=settings.database_url)
jobs = app.jobs("automation", database="main", concurrency=32, lease=60)
hook_http = app.http_client("hook-runner", base_url=settings.hook_runner_url)
hook_runner = ServiceClient(
    hook_http,
    token=settings.hook_runner_token.reveal(),
    base_path="/v1",
)


@asynccontextmanager
async def inbox_transaction():
    connection = await database.acquire("write")
    await connection.execute("BEGIN")
    try:
        yield connection
    except BaseException:
        await connection.execute("ROLLBACK")
        raise
    else:
        await connection.execute("COMMIT")
    finally:
        await database.release("write", connection)


source = app.webhooks("billing").source(
    "lumen-payments",
    path="/hooks/lumen/payments",
    verifier=HMACWebhookVerifier(
        {"current": settings.lumen_webhook_secret.reveal().encode()},
        max_age=300,
    ),
    inbox=PostgresWebhookInbox(),
    session_factory=inbox_transaction,
)


@source.event("payment.succeeded", payload=dict)
async def queue_payment(context: WebhookContext, payload: dict) -> None:
    account_id = "acct-lumen"
    await jobs.enqueue(
        "execute_run",
        account_id,
        context.envelope.id,
        payload,
        tx=context.session,
        tenant=account_id,
        key=f"{account_id}:{context.envelope.id}",
    )


@jobs.task("execute_run", retries=8, timeout=300)
async def execute_run(ctx, account_id: str, event_id: str, payload: dict) -> None:
    invocation_id = f"{account_id}:{event_id}:customer_hook"
    response = await hook_runner.post(
        "/invocations",
        body=JSON.encode(
            {
                "account_id": account_id,
                "event_id": event_id,
                "payload": payload,
                "fence": ctx.fence,
            }
        ),
        idempotency_key=invocation_id,
    )
    if response.status >= 500:
        raise RuntimeError("hook runner unavailable")
```

User JavaScript executes in the hook-runner service with per-invocation CPU, memory,
wall-clock, network and secret capabilities. The Wreath process never evaluates it.
The runner receives a stable idempotency key and the job fence, so a reclaimed worker
cannot publish a stale result.

## Replay without repeating the world

There are three different replay operations, and naming them prevents an expensive
mistake:

1. **Transport replay** feeds a captured request back through parsing and routing.
2. **Workflow resume** skips steps whose results are already committed.
3. **Business replay** creates a new run, with a new identity, after a person explicitly
   chooses whether external effects may run again.

Recording preserves bounded request evidence. Webhook envelopes carry correlation and
causation. Workflow instances preserve the declared step list and refuse a renamed
definition that cannot safely resume. Together they let support reconstruct a run
without quietly charging, emailing or invoking customer code twice.

The conventional paths continue in [realtime and durable work](../guides/realtime.md),
[webhook and protocol APIs](../reference/protocols.md), and
[testing and evidence](../guides/testing.md).
