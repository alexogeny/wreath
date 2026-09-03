from __future__ import annotations

from typing import Any

import pytest

from wreath import Request, Wreath
from wreath.policy import HttpPolicy, SignedRoutePolicy
from wreath.tenancy import Tenant, tenant_scope
from wreath.testing import TestClient
from wreath.tokens import ActionTokens, MemoryTokenLedger, TokenPurpose


def _tokens(clock, *, single_use: bool = False) -> ActionTokens:
    ledger = MemoryTokenLedger() if single_use else None
    return ActionTokens(
        {"current": b"s" * 32},
        current="current",
        purposes=(TokenPurpose("download", 60, single_use=single_use),),
        ledger=ledger,
        clock=clock,
    )


def test_signed_route_binds_tenant_before_consuming_single_use_token() -> None:
    tenant_a = Tenant("tenant_a", "tenant_a")
    tenant_b = Tenant("tenant_b", "tenant_b")
    policy = SignedRoutePolicy(
        _tokens(lambda: 1_000.0, single_use=True),
        "download",
        ("/download",),
    )
    with tenant_scope(tenant_a):
        signed = policy.sign("/download")
    query = signed.partition("?")[2].encode("ascii")

    def request(tenant: Tenant) -> Request:
        value = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/download",
                "query_string": query,
                "headers": [],
            },
            None,
        )
        value.state.tenant = tenant
        return value

    refused = policy._ingress_sync(request(tenant_b))
    assert refused is not None
    assert refused.status == 403
    assert policy._ingress_sync(request(tenant_a)) is None


@pytest.mark.asyncio
async def test_signed_route_binds_exact_path_method_and_query() -> None:
    now = 1_000.0
    policy = SignedRoutePolicy(
        _tokens(lambda: now),
        "download",
        ("/download",),
    )
    app = Wreath(http_policy=HttpPolicy(signed_routes=policy))

    @app.get("/download")
    async def download(request):
        return request.query_string.decode()

    signed = policy.sign("/download?file=report.pdf")
    async with TestClient(app) as client:
        accepted = await client.get(signed)
        changed = await client.get(signed.replace("report.pdf", "secrets.pdf"))
        missing = await client.get("/download?file=report.pdf")
        unrelated = await client.get("/not-protected")

    assert accepted.status == 200
    assert changed.status == 403
    assert missing.status == 403
    assert unrelated.status == 404


def test_signed_route_binds_the_raw_target_forwarded_by_a_proxy() -> None:
    policy = SignedRoutePolicy(_tokens(lambda: 1_000.0), "download", ("/download",))
    signed = policy.sign("/download")
    query = signed.partition("?")[2].encode("ascii")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/download",
            "raw_path": b"/down%6coad",
            "query_string": query,
            "headers": [],
        },
        None,
    )

    refusal = policy._ingress_sync(request)

    assert refusal is not None and refusal.status == 403


@pytest.mark.asyncio
async def test_signed_route_expiry_and_single_use_come_from_action_tokens() -> None:
    clock = [1_000.0]
    policy = SignedRoutePolicy(
        _tokens(lambda: clock[0], single_use=True),
        "download",
        ("/download",),
    )
    app = Wreath(http_policy=HttpPolicy(signed_routes=policy))

    @app.get("/download")
    async def download(request):
        return "ok"

    first_url = policy.sign("/download")
    expired_url = policy.sign("/download?file=old")
    async with TestClient(app) as client:
        first = await client.get(first_url)
        replay = await client.get(first_url)
        clock[0] += 61
        expired = await client.get(expired_url)

    assert first.status == 200
    assert replay.status == 403
    assert expired.status == 403


def test_signed_route_configuration_and_signing_are_fail_closed() -> None:
    tokens = _tokens(lambda: 1_000.0)
    with pytest.raises(ValueError, match="paths"):
        SignedRoutePolicy(tokens, "download", ())
    with pytest.raises(ValueError, match="paths"):
        SignedRoutePolicy(tokens, "download", ("relative",))
    with pytest.raises(ValueError, match="parameter"):
        SignedRoutePolicy(tokens, "download", ("/download",), parameter="bad name")

    policy = SignedRoutePolicy(tokens, "download", ("/download",))
    with pytest.raises(ValueError, match="protected paths"):
        policy.sign("/other")
    with pytest.raises(ValueError, match="already contains"):
        policy.sign("/download?signature=attacker")


def test_signed_route_reference_path_refuses_a_missing_signature(monkeypatch) -> None:
    policy = SignedRoutePolicy(_tokens(lambda: 1_000.0), "download", ("/download",))

    def unexpected_verification(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a missing signature reached token verification")

    monkeypatch.setattr(ActionTokens, "verify", unexpected_verification)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/download",
            "query_string": b"file=report.pdf",
            "headers": [],
        },
        None,
    )

    refusal = policy._ingress_sync(request)

    assert refusal is not None
    assert refusal.status == 403


def test_signed_route_policy_rejects_non_action_tokens() -> None:
    invalid: Any = object()
    with pytest.raises(TypeError, match="must be ActionTokens"):
        SignedRoutePolicy(invalid, "download", ("/download",))
