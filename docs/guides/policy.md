---
description: Compose security, host, CORS, request identity and idempotency policy once.
keywords: guide policy hardening security headers trusted host CORS idempotency request ID
---

# Policy and hardening

First-class HTTP policy is compiled with the route table. Configure the boundary once;
do not scatter equivalent middleware decisions across handlers.

```python title="app.py"
from wreath import Request, Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.policy import HttpPolicy, IdempotencyPolicy
from wreath.policy.cors import CorsPolicy
from wreath.policy.request_id import RequestIdPolicy
from wreath.policy.security import SecurityHeadersPolicy, TrustedHostPolicy
from wreath.response import JSONResponse

created: list[dict] = []


def verify(token: str) -> Identity | None:
    return Identity("operator") if token == "operator-token" else None


app = Wreath(
    http_policy=HttpPolicy(
        trusted_host=TrustedHostPolicy(("api.example.test",)),
        cors=CorsPolicy(allow_origins=("https://console.example.test",)),
        security_headers=SecurityHeadersPolicy(),
        request_id=RequestIdPolicy(),
        idempotency=IdempotencyPolicy(),
    )
)
app.configure_auth(BearerTokenBackend(verify))


@app.post("/deployments")
@authenticated()
async def deploy(request: Request) -> JSONResponse:
    deployment = {"id": len(created) + 1, "actor": request.identity.id}
    created.append(deployment)
    return JSONResponse(deployment, status=202)
```

```python title="test_app.py"
from wreath.testing import TestClient

from app import app, created


async def test_a_retry_replays_the_first_authenticated_effect() -> None:
    created.clear()
    headers = {
        "host": "api.example.test",
        "authorization": "Bearer operator-token",
        "idempotency-key": "deploy-42",
    }
    async with TestClient(app) as client:
        first = await client.post("/deployments", headers=headers)
        retry = await client.post("/deployments", headers=headers)

    assert first.status == retry.status == 202
    assert retry.json() == first.json()
    assert dict(retry.headers)[b"idempotency-replayed"] == b"true"
    assert len(created) == 1


async def test_an_unknown_host_is_refused_before_the_handler() -> None:
    created.clear()
    async with TestClient(app) as client:
        response = await client.post(
            "/deployments",
            headers={
                "host": "attacker.example",
                "authorization": "Bearer operator-token",
                "idempotency-key": "deploy-43",
            },
        )
    assert response.status == 400
    assert created == []
```

Use PostgreSQL-backed rate-limit, idempotency and session stores across workers. The
[policy reference](../reference/policy.md) lists every policy and its bounded store.

## The middleware catalogue

People coming from another framework will search for “middleware”. In Wreath, the
standard cases below are immutable policy values. `HttpPolicy` fixes their order,
compiles the native parts with the route image, and lets OpenAPI describe the headers
and refusals that each configured instance adds.

| Familiar middleware job | `HttpPolicy` field | Wreath declaration |
|---|---|---|
| forwarded client, scheme and host | `proxy` | `ProxyPolicy` |
| allowed `Host` values | `trusted_host` | `TrustedHostPolicy` |
| maintenance mode | `maintenance` | `MaintenancePolicy` |
| AI crawler treatment | `ai_scraping` | `AIScrapingPolicy` |
| classified client traffic | `traffic` | `TrafficPolicy` |
| address-level throttling | `rate_limit` | `RateLimitPolicy` |
| authenticated plan or quota limits | `principal_rate_limit` | `TieredRateLimitPolicy` |
| expiring signed URLs | `signed_routes` | `SignedRoutePolicy` |
| bounded gzip request bodies | `request_decompression` | `RequestDecompressionPolicy` |
| correlation IDs | `request_id` | `RequestIdPolicy` |
| `Server-Timing` | `server_timing` | `ServerTimingPolicy` |
| cross-origin HTTP | `cors` | `CorsPolicy` |
| browser write protection | `csrf` | `CsrfPolicy` |
| CSP, HSTS and browser headers | `security_headers` | `SecurityHeadersPolicy` |
| WebSocket origins | `websocket_origin` | `WebSocketOriginPolicy` |
| signed or server-side sessions | `session` | `SessionPolicy` |
| replay-safe unsafe requests | `idempotency` | `IdempotencyPolicy` |
| `Cache-Control` and validators | `cache_control` | `CachePolicy` |
| gzip, Brotli and Zstandard responses | `compression` | `CompressionPolicy` |
| in-flight request admission | `concurrency` | `ConcurrencyPolicy` |
| handler time budgets | `deadline` | `DeadlinePolicy` |

There are two rate-limit positions because they know different facts. The ingress
limit runs before authentication and normally keys by client address. The principal
limit runs after identity is established, so it can enforce a plan or quota. Wreath
refuses a principal key in the ingress slot instead of silently putting every user in
one bucket.

## Application policy and route policy

Put universal boundaries in the application's `HttpPolicy`. Route decorators remain
the place for requirements that differ by operation: public versus authenticated,
roles, permissions, Cedar authorization, second-factor freshness, quotas and an
operation-specific idempotency contract. Generated OpenAPI sees both layers.

Use local memory stores only for one process. These declarations have PostgreSQL
owners when several workers must agree:

| Decision | One-process owner | Shared owner |
|---|---|---|
| idempotency replay | `MemoryIdempotencyStore` | `PostgresIdempotencyStore` |
| rate-limit buckets | `MemoryRateLimitStore` | `PostgresRateLimitStore` |
| revocable sessions | signed cookie | `PostgresSessionStore` |
| response invalidation | local cache backend | shared `response_cache` backend and tags |

## When custom middleware is actually custom

Use `wreath.middleware` for application behavior that has no first-class owner. Hook
middleware is compiled into a flat tape. An `applies_to` predicate sees static route
facts at startup, so route selection is not repeated on each request.

```python title="custom_middleware.py"
from wreath import Request, Wreath
from wreath.middleware import MiddlewareHooks, MiddlewareRoute

app = Wreath()


def is_api(route: MiddlewareRoute) -> bool:
    return route.path.startswith("/api/")


def attach_release(request: Request) -> None:
    request.state.release = "2026.08"


app.add_middleware(
    MiddlewareHooks(
        before_sync=attach_release,
        applies_to=is_api,
    )
)
```

Reach for legacy `(request, call_next)` middleware only when the handler call itself
must sit inside a context manager or `try/finally`. One legacy middleware makes that
route use nested calls instead of the compiled hook tape. Standard HTTP behavior is
deliberately refused by `add_middleware`; configure it through `HttpPolicy`.

## Refusals are part of the contract

CORS preflight, rate-limit `429`, maintenance `503`, deadline `504`, idempotency
conflicts, required headers and cache validators are included in generated operation
contracts. The declaration and the wire behavior come from the same configured
object, so the docs cannot advertise a generic limit while production enforces
another one.
