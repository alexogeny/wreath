from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.policy import HttpPolicy, SignedRoutePolicy
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


def test_signed_route_policy_rejects_non_action_tokens() -> None:
    invalid: Any = object()
    with pytest.raises(TypeError, match="must be ActionTokens"):
        SignedRoutePolicy(invalid, "download", ("/download",))
