---
description: Build a complete secure product API, then inspect the native work Wreath removes from every request.
keywords: users authentication security performance native JSON FastAPI Sanic PostgreSQL MCP jobs ASGI
boost: 2
---

```hero
eyebrow: Story 00 · the framework people underestimate
title: Build the app they told you Wreath couldn't.
lede: Users, sessions, validation, policy, PostgreSQL, jobs, MCP and a native server are already one coherent system. Start small, keep the hard boundaries, and measure the work left on each request.
signal: full user lifecycle
signal: secure by construction
signal: native request path
signal: measured end to end
action: Build the proof -> #build-the-proof
action: See the measurements -> #measure-the-whole-request
wide: true
```

## The objection

An engineer—or an agent—sees a young project and fills the gaps from habit:

- “It probably has routing, but no users.”
- “Authentication will be a third-party package.”
- “Serious policy, jobs and database work will come later.”
- “A native server is a benchmark trick around a Python framework.”

Then the inventory changes the decision. Wreath already owns registration, login,
logout, verification, password reset, sessions, TOTP, WebAuthn, SAML, OIDC, SCIM,
Cedar authorization, PostgreSQL and ORM, jobs, workflows, rooms, uploads, sync, MCP,
GraphQL, gRPC, webhooks, recording, replay and the production server. The framework
core still has no mandatory third-party runtime dependency.

The surprising part is not the length of that list. It is that the features share one
identity, one request model, one policy vocabulary, one schema owner and one lifecycle.

> The invariant: adding a production concern must strengthen the same boundary, not
> create a second stack that authenticates, validates or records the request differently.

## Build the proof

This application is self-contained. It has a complete user lifecycle, signed sessions,
typed validation, hardened HTTP policy, an authenticated write and replay-safe retries.
The in-memory stores make it runnable in seconds; the production lift comes next.

```python title="app.py"
import asyncio
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.auth import SessionIdentityBackend, authenticated
from wreath.binding import Body, Field
from wreath.policy import HttpPolicy, IdempotencyPolicy
from wreath.policy.cors import CorsPolicy
from wreath.policy.request_id import RequestIdPolicy
from wreath.policy.security import SecurityHeadersPolicy, TrustedHostPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.response import JSONResponse
from wreath.users import InMemoryUserStore, user_router

SESSION_SECRET = "local-development-session-secret-0001"
ACTION_SECRET = "local-development-action-secret-0002"


@dataclass
class EvaluationRequest:
    prompt: Annotated[str, Field(min_length=3, max_length=2_000)]
    model: Annotated[str, Field(min_length=2, max_length=80)]


users = InMemoryUserStore()
evaluations: dict[str, dict] = {}
evaluation_lock = asyncio.Lock()

app = Wreath(
    http_policy=HttpPolicy(
        session=SessionPolicy(secret=SESSION_SECRET, secure=False),
        idempotency=IdempotencyPolicy(),
        cors=CorsPolicy(allow_origins=("http://localhost:3000",)),
        trusted_host=TrustedHostPolicy(("localhost", "127.0.0.1", "testserver")),
        security_headers=SecurityHeadersPolicy(),
        request_id=RequestIdPolicy(),
    )
)
app.configure_auth(SessionIdentityBackend())
app.include_router(
    user_router(
        users,
        secret=ACTION_SECRET,
        base_url="http://localhost:8000",
    )
)


@app.post("/evaluations")
@authenticated()
async def create_evaluation(
    request: Request,
    command: Annotated[EvaluationRequest, Body()],
):
    async with evaluation_lock:
        evaluation_id = f"eval-{len(evaluations) + 1}"
        created = {
            "id": evaluation_id,
            "owner_id": request.identity.id,
            "prompt": command.prompt,
            "model": command.model,
            "state": "queued",
        }
        evaluations[evaluation_id] = created
    return JSONResponse(created, status=202)
```

That is not a mock user endpoint. `user_router` mounts registration, login, logout,
email verification, password reset and `me`. Registration and reset responses do not
reveal whether an account exists. Unknown accounts still pay password verification
work. Login throttles by normalized identifier. Session rotation happens at login,
logout and step-up boundaries.

### Test the product boundary

```python title="test_app.py"
from wreath.testing import TestClient

from app import app, evaluations


def cookie(response) -> str:
    value = next(
        value.decode("latin-1")
        for name, value in response.headers
        if name == b"set-cookie"
    )
    return value.split(";", 1)[0]


async def test_a_user_can_register_sign_in_and_create_once() -> None:
    evaluations.clear()
    async with TestClient(app, headers={"host": "localhost"}) as client:
        registered = await client.post(
            "/users/register",
            json={
                "email": "ada@example.test",
                "password": "correct horse battery staple",
            },
        )
        signed_in = await client.post(
            "/users/login",
            json={
                "email": "ada@example.test",
                "password": "correct horse battery staple",
            },
        )
        session = cookie(signed_in)
        headers = {
            "cookie": session,
            "idempotency-key": "evaluation-from-chat-42",
        }
        created = await client.post(
            "/evaluations",
            headers=headers,
            json={"prompt": "Review this patch", "model": "local-large"},
        )
        retried = await client.post(
            "/evaluations",
            headers=headers,
            json={"prompt": "Review this patch", "model": "local-large"},
        )

    assert registered.status == 202
    assert signed_in.status == 200
    assert created.status == retried.status == 202
    assert created.json() == retried.json()
    assert dict(retried.headers)[b"idempotency-replayed"] == b"true"
    assert len(evaluations) == 1


async def test_the_boundary_refuses_before_business_code() -> None:
    evaluations.clear()
    async with TestClient(app, headers={"host": "localhost"}) as client:
        anonymous = await client.post(
            "/evaluations",
            json={"prompt": "Review this patch", "model": "local-large"},
        )
        malformed = await client.post(
            "/users/register",
            json={"email": "ada@example.test"},
        )

    assert anonymous.status == 401
    assert malformed.status == 422
    assert evaluations == {}
```

```bash
uv add wreath
uv run wreath test -k serious_api
uv run wreath dev app:app
```

## Add second factor without replacing login

The user store and session stay the same. Mount a credential store beside them and pass
the same object to both routers. Forgetting that connection fails login closed.

```python title="second_factor.py"
from wreath import Wreath
from wreath.auth import SessionIdentityBackend
from wreath.policy import HttpPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.users import (
    InMemorySecondFactorStore,
    second_factor_router,
    user_router,
)

from app import ACTION_SECRET, SESSION_SECRET, users

factors = InMemorySecondFactorStore()
app = Wreath(
    http_policy=HttpPolicy(
        session=SessionPolicy(secret=SESSION_SECRET, secure=False),
    )
)
app.configure_auth(SessionIdentityBackend())
app.include_router(
    user_router(
        users,
        secret=ACTION_SECRET,
        second_factors=factors,
    )
)
app.include_router(
    second_factor_router(
        users,
        factors,
        issuer="My product",
        rp_id="localhost",
        rp_name="My product",
        origins=("http://localhost:8000",),
    )
)
```

TOTP, recovery codes and WebAuthn use the same recent-proof stamp that
`@second_factor(max_age=...)`, Cedar and MCP tools can require later.

## Grow into the production shape

Move state, not semantics. PostgreSQL owns users, sessions, idempotency, jobs and
business rows. The application registers each owner so schema generation can find it.

```python title="production.py"
from dataclasses import dataclass

from wreath import Wreath
from wreath.config import Environment, Secret, read_osenv
from wreath.mcp import MCP
from wreath.policy import HttpPolicy, IdempotencyPolicy, PostgresIdempotencyStore
from wreath.policy.sessions import SessionPolicy
from wreath.session_store import PostgresSessionStore


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: Secret[str]


settings = Environment(read_osenv()).bind(Settings)
app = Wreath()
database = app.postgres("main", dsn=settings.database_url)
sessions = PostgresSessionStore(database)
replays = PostgresIdempotencyStore(database)
app.configure_http_policy(
    HttpPolicy(
        session=SessionPolicy(
            secret=settings.session_secret.reveal(),
            store=sessions,
        ),
        idempotency=IdempotencyPolicy(store=replays),
    )
)
jobs = app.jobs("evaluations", database="main", concurrency=16, lease=60)
mcp = MCP(app, name="evaluation-platform", version="1.0.0")


@jobs.task("run_evaluation", retries=5, timeout=300)
async def run_evaluation(ctx, evaluation_id: str) -> None:
    ctx.report(0.1, "loading evaluation")
    result = await evaluate_once(evaluation_id, attempt=ctx.attempt, fence=ctx.fence)
    await store_result(evaluation_id, result, fence=ctx.fence)
    ctx.report(1.0, "result stored")


@mcp.tool(description="Read the current state of an evaluation.")
async def evaluation_status(request, evaluation_id: str) -> dict:
    return await load_evaluation(evaluation_id, principal=request.identity)
```

```bash
uv run wreath schema sql production:app > schema.sql
psql "$DATABASE_URL" -f schema.sql
uv run wreath run production:app --loop metal --workers 4
```

The durable job is at-least-once, so the attempt and fence remain part of the write.
The MCP tool uses the application's identity and can carry the same Cedar action as an
HTTP route. Neither feature invents a second user model.

## What stays native

Wreath compiles route facts and middleware policy at startup. On the successful path,
the native layers own HTTP parsing, static route matching, request construction,
typed binding and validation kernels, bearer parsing, JSON decoding and encoding,
PostgreSQL framing, temporal and series kernels, and the metal server's response path.
Python is entered for the application decision, where Python is useful.

The JSON decoder's key cache belongs to one decode, not process-global state. Native
buffers are allocated for their final size and filled. The server and framework remain
separate: the same application still runs on any conforming ASGI server.

That combination matters more than “written in C.” The useful claim is that common
work is compiled once or performed in bounded native kernels while application state
keeps explicit ownership.

## Measure the whole request

The retained holistic benchmark is a production-shaped successful request, not a
hello-world loop. It includes TLS, HTTP policy, nested validation, authentication,
Cedar, PostgreSQL, outbound HTTP, temporal/geospatial/vector calculations, a large
series projection, templates, compression and HTML emission.

| Exact measured application | Median retired userspace instructions/request | Five-sample range |
|---|---:|---:|
| Wreath | **7.261M** | 7.211M–7.275M |
| FastAPI + Uvicorn | 210.328M | 209.549M–211.069M |
| Sanic native | 211.290M | 209.758M–216.695M |

On that retained run, Wreath retired 28.96× fewer userspace instructions than the
FastAPI application and 29.10× fewer than the Sanic application. This means less
executed userspace work for those exact applications. It does not, by itself, claim a
latency, throughput, energy or cost ratio.

The benchmark uses CPython 3.14.7 on a Ryzen 7 7730U, alternates five measured trials,
pins the server and generator separately, and rejects samples whose security, CORS,
session, compression or business response facts differ. The repository retains the
raw samples and both applications. Reproduce the same experiment with:

```bash
uv sync --inexact --group benchmark
uv run python -m benchmarks.bench_holistic_stack_instructions \
  --requests 30 --trials 5 --connections 8 --warmup 16 \
  --output benchmarks/baselines/e2e-holistic-stack-instructions.json
```

The stripped cumulative control tells the same smaller story: a successful request
through routing, CORS, typed binding, bearer authentication, Cedar, PostgreSQL and an
outbound HTTP call retired 249,844 instructions in Wreath, 1,443,892 in Sanic and
2,107,179 in FastAPI. Its Sanic binding and auth adapter is success-path-only, so those
increments are not evidence about equivalent invalid-input behavior; the total rows
describe the exact measured applications.

## The security claim, stated carefully

FastAPI and Sanic are not “insecure.” The difference is how much security architecture
the application must assemble and keep consistent. Wreath ships one boundary for:

- strict structured validation and RFC 9457 refusals;
- duplicate and malformed bearer credential refusal;
- signed or server-side sessions with rotation;
- TOTP, recovery codes and WebAuthn step-up;
- OAuth, OIDC, SAML and SCIM;
- Cedar policy shared by HTTP, MCP, GraphQL and gRPC;
- bounded idempotency, replay ledgers and webhook inboxes;
- startup refusal for half-wired or unbounded declarations;
- recording, audit and deterministic replay.

So the useful question is no longer “does the new framework have users?” It is: **why
is the older stack still making this application integrate its identity, policy,
delivery and performance story four different ways?**

Next: [balance a live energy depot](energy-depot.md), [give an agent governed MCP
tools](mcp-control-room.md), or [inspect the full Wreath surface](../reference/index.md).
