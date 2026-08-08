from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import AuthorizationDecision, authorize, roles
from wreath.middleware import (
    MiddlewareHooks,
    PipelineHooks,
)
from wreath.policy import CorsPolicy, HttpPolicy, SecurityHeadersPolicy, TrustedHostPolicy
from wreath.response import TextResponse
from wreath.testing import TestClient


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["decision", "trie"])
async def test_global_hooks_cover_miss_without_running_auth_or_route_middleware(
    mode: str,
) -> None:
    events: list[str] = []
    auth_calls = 0

    async def verify(token: str) -> Identity | None:
        nonlocal auth_calls
        auth_calls += 1
        return Identity(token)

    async def before(request: Any) -> None:
        events.append("global-before")

    async def after(request: Any, response: Any) -> Any:
        events.append(f"global-after:{request.state.route_outcome}")
        return response

    async def route_before(request: Any) -> None:
        events.append("route-before")

    app = Wreath(routing=mode)
    app.configure_auth(BearerTokenBackend(verify))
    app.add_global_middleware(MiddlewareHooks(before=before, after=after))
    app.add_middleware(MiddlewareHooks(before=route_before))

    @app.get("/private")
    @roles("admin")
    async def private(request: Any) -> str:
        events.append("handler")
        return "private"

    async with TestClient(app) as client:
        response = await client.get("/missing", headers={"Authorization": "Bearer admin"})

    assert response.status == 404
    assert auth_calls == 0
    assert events == ["global-before", "global-after:miss"]


@pytest.mark.asyncio
async def test_route_outcome_does_not_eagerly_create_public_request_state() -> None:
    state_before_hook: list[Any] = []
    state_before_after_hook: list[Any] = []
    outcomes: list[str] = []

    async def before(request: Any) -> None:
        state_before_hook.append(request._state)

    async def after(request: Any, response: Any) -> Any:
        state_before_after_hook.append(request._state)
        outcomes.append(request.state.route_outcome)
        return response

    app = Wreath()
    app.add_global_middleware(MiddlewareHooks(before=before, after=after))

    @app.get("/")
    async def endpoint(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/")

    assert response.status == 200
    assert state_before_hook == [None]
    assert state_before_after_hook == [None]
    assert outcomes == ["route"]


@pytest.mark.asyncio
async def test_public_route_skips_auth_and_classifies_once() -> None:
    auth_calls = 0

    async def verify(token: str) -> Identity | None:
        nonlocal auth_calls
        auth_calls += 1
        return Identity(token)

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/public")
    async def public(request: Any) -> str:
        return "public"

    async with TestClient(app) as client:
        response = await client.get("/public", headers={"Authorization": "Bearer supplied"})

    assert response.status == 200
    assert auth_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["decision", "trie"])
async def test_protected_denial_runs_global_finalizers_but_not_route_middleware(
    mode: str,
) -> None:
    events: list[str] = []

    async def verify(token: str) -> Identity | None:
        events.append("authenticate")
        return Identity(token, roles=frozenset({"user"}))

    async def global_after(request: Any, response: Any) -> Any:
        events.append("global-after")
        response.headers.append((b"x-finalized", b"yes"))
        return response

    async def route_before(request: Any) -> None:
        events.append("route-before")

    app = Wreath(routing=mode)
    app.configure_auth(BearerTokenBackend(verify))
    app.add_global_middleware(MiddlewareHooks(after=global_after))
    app.add_middleware(MiddlewareHooks(before=route_before))

    @app.get("/admin")
    @roles("admin")
    async def admin(request: Any) -> str:
        events.append("handler")
        return "admin"

    async with TestClient(app) as client:
        response = await client.get("/admin", headers={"Authorization": "Bearer user"})

    assert response.status == 403
    assert response.header("x-finalized") == "yes"
    assert events == ["authenticate", "global-after"]


@pytest.mark.asyncio
async def test_trusted_host_rejects_before_authentication() -> None:
    auth_calls = 0

    async def verify(token: str) -> Identity | None:
        nonlocal auth_calls
        auth_calls += 1
        return Identity(token, roles=frozenset({"admin"}))

    app = Wreath(
        http_policy=HttpPolicy(trusted_host=TrustedHostPolicy(("api.example",)))
    )
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/admin")
    @roles("admin")
    async def admin(request: Any) -> str:
        return "admin"

    async with TestClient(app) as client:
        response = await client.get(
            "/admin",
            headers={"Host": "evil.example", "Authorization": "Bearer admin"},
        )

    assert response.status == 400
    assert auth_calls == 0


@pytest.mark.asyncio
async def test_security_finalizer_covers_auth_denial() -> None:
    async def verify(token: str) -> Identity | None:
        return None

    app = Wreath(
        http_policy=HttpPolicy(
            security_headers=SecurityHeadersPolicy(
                content_security_policy="default-src 'none'"
            )
        )
    )
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/private")
    @roles("admin")
    async def private(request: Any) -> TextResponse:
        return TextResponse("private")

    async with TestClient(app) as client:
        response = await client.get("/private", headers={"Authorization": "Bearer wrong"})

    assert response.status == 401
    assert response.header("content-security-policy") == "default-src 'none'"


@pytest.mark.asyncio
async def test_protected_route_without_backend_has_routing_mode_parity() -> None:
    statuses: list[int] = []
    for mode in ("decision", "trie"):
        app = Wreath(routing=mode)

        @app.get("/private")
        @roles("admin")
        async def private(request: Any) -> str:
            return "private"

        async with TestClient(app) as client:
            statuses.append((await client.get("/private")).status)

    assert statuses == [401, 401]


@pytest.mark.asyncio
async def test_cors_finalizer_covers_auth_denial() -> None:
    async def verify(token: str) -> Identity | None:
        return None

    app = Wreath(
        http_policy=HttpPolicy(
            cors=CorsPolicy(allow_origins=["https://app.example"])
        )
    )
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/private")
    @roles("admin")
    async def private(request: Any) -> str:
        return "private"

    async with TestClient(app) as client:
        response = await client.get(
            "/private",
            headers={
                "Authorization": "Bearer wrong",
                "Origin": "https://app.example",
            },
        )

    assert response.status == 401
    assert response.header("access-control-allow-origin") == "https://app.example"


@pytest.mark.asyncio
async def test_global_security_finalizer_covers_static_files(tmp_path: Path) -> None:
    (tmp_path / "asset.txt").write_text("asset")
    app = Wreath(
        http_policy=HttpPolicy(
            security_headers=SecurityHeadersPolicy(
                content_security_policy="default-src 'none'"
            )
        )
    )
    app.static("/assets", str(tmp_path))

    async with TestClient(app) as client:
        response = await client.get("/assets/asset.txt")

    assert response.status == 200
    assert response.header("content-security-policy") == "default-src 'none'"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["decision", "trie"])
async def test_explicit_pipeline_stages_run_in_cost_order(mode: str) -> None:
    events: list[str] = []

    async def ingress(request: Any) -> None:
        events.append("ingress")

    async def pre_auth(request: Any) -> None:
        events.append("pre-auth")

    async def verify(token: str) -> Identity | None:
        events.append("authenticate")
        return Identity(token, roles=frozenset({"admin"}))

    async def identity(request: Any) -> None:
        events.append("identity")

    class Authorizer:
        async def authorize(self, request: Any, requirement: Any) -> AuthorizationDecision:
            events.append("policy")
            return AuthorizationDecision(True)

    async def action(request: Any) -> None:
        events.append("action")

    async def route(request: Any) -> None:
        events.append("route")

    async def finalizer(request: Any, response: Any) -> Any:
        events.append("finalize")
        return response

    app = Wreath(routing=mode)
    app.configure_auth(BearerTokenBackend(verify), Authorizer())
    app.add_global_middleware(
        PipelineHooks(
            before=ingress,
            pre_auth=pre_auth,
            identity=identity,
            action=action,
            after=finalizer,
        )
    )
    app.add_middleware(MiddlewareHooks(before=route))

    @app.get("/admin")
    @roles("admin")
    @authorize(action="read", resource="admin")
    async def admin(request: Any) -> str:
        events.append("handler")
        return "admin"

    async with TestClient(app) as client:
        response = await client.get("/admin", headers={"Authorization": "Bearer admin"})

    assert response.status == 200
    assert events == [
        "ingress",
        "pre-auth",
        "authenticate",
        "identity",
        "policy",
        "action",
        "route",
        "handler",
        "finalize",
    ]


@pytest.mark.asyncio
async def test_miss_stage_can_rate_limit_without_authentication() -> None:
    auth_calls = 0

    async def verify(token: str) -> Identity | None:
        nonlocal auth_calls
        auth_calls += 1
        return Identity(token)

    async def block_miss(request: Any) -> TextResponse:
        return TextResponse("slow down", status=429)

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))
    app.add_global_middleware(PipelineHooks(miss=block_miss))

    async with TestClient(app) as client:
        response = await client.get("/scanner-path")

    assert response.status == 429
    assert auth_calls == 0


async def _hook_order(kind: str) -> tuple[list[str], int]:
    """Drive three global hooks where the middle one short-circuits or raises."""
    events: list[str] = []

    async def outer_before(request: Any) -> None:
        events.append("outer-before")

    async def outer_after(request: Any, response: Any) -> Any:
        events.append("outer-after")
        return response

    async def culprit_before(request: Any) -> Any:
        events.append("culprit-before")
        if kind == "raise":
            raise RuntimeError("before blew up")
        return TextResponse("short-circuit")

    async def culprit_after(request: Any, response: Any) -> Any:
        events.append("culprit-after")
        return response

    async def inner_before(request: Any) -> None:
        events.append("inner-before")

    async def inner_after(request: Any, response: Any) -> Any:
        events.append("inner-after")
        return response

    app = Wreath()
    app.add_global_middleware(
        MiddlewareHooks(before=outer_before, after=outer_after), priority=0
    )
    app.add_global_middleware(
        MiddlewareHooks(before=culprit_before, after=culprit_after), priority=1
    )
    app.add_global_middleware(
        MiddlewareHooks(before=inner_before, after=inner_after), priority=2
    )

    @app.get("/")
    async def endpoint(request: Any) -> str:
        events.append("handler")
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/")
    return events, response.status


@pytest.mark.asyncio
async def test_a_before_that_raises_does_not_run_its_own_after() -> None:
    """A hook whose `before` never completed must not have its `after` paired.

    Design 22 item 12. `after` is cleanup for preconditions `before`
    establishes; running it against a `before` that raised part-way is
    guessing how far it got. `SessionPolicy` is the near-miss --
    `_after_stored` reads `state._session_loaded` by attribute while reading
    its siblings with `.get()`, so it is one inserted `await` away from
    turning an error response into an unrelated `AttributeError` 500.
    """
    events, status = await _hook_order("raise")

    assert "culprit-after" not in events
    # Hooks below it *did* complete, so they still unwind.
    assert events == ["outer-before", "culprit-before", "outer-after"]
    assert status == 500


@pytest.mark.asyncio
async def test_a_before_that_returns_a_response_still_runs_its_own_after() -> None:
    """The other half of the pair: returning a response is a completed `before`.

    Pinned so a future reading of the rule above cannot collapse both cases
    into `index` and silently drop a short-circuiting hook's `after`.
    """
    events, status = await _hook_order("short-circuit")

    assert events == ["outer-before", "culprit-before", "culprit-after", "outer-after"]
    assert status == 200
