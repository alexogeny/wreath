"""Rate limits keyed on the caller, with an allowance per plan.

Keying on the client address is right at ingress and wrong for an authenticated
API: behind a proxy or a carrier NAT it lumps unrelated callers into one bucket,
and it hands one caller a fresh allowance per device. The right key is who they
are — and the right *limit* is usually a function of what they pay for.

The constraint that shapes the design: `request.identity` is set during route
authorization, so a limiter that keys on the principal cannot be a global hook.
Global hooks run at ingress, before anybody has been identified. These tests pin
that, because getting it wrong yields a limiter that silently keys every request
the same way.
"""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath._auth.models import Identity
from wreath._auth.requirements import add_authenticated
from wreath.middleware import (
    RateLimitMiddleware,
    TieredRateLimitMiddleware,
    principal_key,
)
from wreath.testing import TestClient

pytestmark = pytest.mark.asyncio


def _app(middleware) -> Wreath:
    app = Wreath()
    app.add_middleware(middleware)

    @app.get("/llamas")
    @add_authenticated
    async def llamas(request) -> dict:
        return {"ok": True}

    return app


# --- the key ------------------------------------------------------------------


async def test_each_principal_gets_its_own_allowance() -> None:
    app = _app(TieredRateLimitMiddleware(tiers={"pro": (9, 60.0)}, default=(1, 60.0)))
    async with TestClient(app) as client:
        ada = client.acting_as("ada")
        bo = client.acting_as("bo")

        assert (await ada.get("/llamas")).status == 200
        assert (await ada.get("/llamas")).status == 429
        assert (await bo.get("/llamas")).status == 200      # a separate allowance


async def test_the_global_limiter_refuses_to_key_on_the_principal() -> None:
    """The trap: it runs before authentication, so it would bucket everyone
    together and look like it worked. A startup error beats that discovery."""
    with pytest.raises(ValueError, match="before authentication"):
        RateLimitMiddleware(limit=1, window=60.0, key=principal_key)


async def test_a_principal_id_cannot_collide_with_an_address() -> None:
    """`ip:` prefixed, so a user called `testclient` is not the anonymous bucket."""
    class _Anonymous:
        identity = None
        client = ("testclient", 5000)

    class _Named:
        identity = Identity("testclient")
        client = ("10.0.0.1", 5000)

    assert principal_key(_Anonymous()) == "ip:testclient"
    assert principal_key(_Named()) == "User:testclient"


async def test_an_anonymous_caller_falls_back_to_its_address() -> None:
    """One shared anonymous bucket would be a denial of service on yourself."""
    class _Anonymous:
        identity = None
        client = ("203.0.113.7", 5000)

    assert principal_key(_Anonymous()) == "ip:203.0.113.7"


# --- the tier -------------------------------------------------------------------


async def test_a_tier_gets_the_allowance_its_role_names() -> None:
    app = _app(TieredRateLimitMiddleware(
        tiers={"pro": (3, 60.0)}, default=(1, 60.0)
    ))
    async with TestClient(app) as client:
        free = client.acting_as("bo")
        pro = client.acting_as("ada", roles=["pro"])

        assert (await free.get("/llamas")).status == 200
        assert (await free.get("/llamas")).status == 429

        for _ in range(3):
            assert (await pro.get("/llamas")).status == 200
        assert (await pro.get("/llamas")).status == 429


async def test_the_most_generous_matching_tier_wins() -> None:
    """Holding two plans must not be worse than holding the better one."""
    app = _app(TieredRateLimitMiddleware(
        tiers={"pro": (3, 60.0), "enterprise": (8, 60.0)}, default=(1, 60.0)
    ))
    async with TestClient(app) as client:
        both = client.acting_as("ada", roles=["pro", "enterprise"])

        for _ in range(8):
            assert (await both.get("/llamas")).status == 200
        assert (await both.get("/llamas")).status == 429


async def test_an_unrecognised_role_gets_the_default() -> None:
    app = _app(TieredRateLimitMiddleware(
        tiers={"pro": (5, 60.0)}, default=(1, 60.0)
    ))
    async with TestClient(app) as client:
        other = client.acting_as("bo", roles=["llama-walker"])

        assert (await other.get("/llamas")).status == 200
        assert (await other.get("/llamas")).status == 429


async def test_tiers_do_not_share_a_bucket() -> None:
    """A promotion arrives with a full allowance, not the old plan's remainder."""
    app = _app(TieredRateLimitMiddleware(
        tiers={"pro": (2, 60.0)}, default=(1, 60.0)
    ))
    async with TestClient(app) as client:
        free = client.acting_as("ada")
        promoted = client.acting_as("ada", roles=["pro"])

        assert (await free.get("/llamas")).status == 200
        assert (await free.get("/llamas")).status == 429
        for _ in range(2):
            assert (await promoted.get("/llamas")).status == 200


async def test_a_custom_tier_function_can_read_anything() -> None:
    app = _app(TieredRateLimitMiddleware(
        tiers={"enterprise": (4, 60.0)},
        default=(1, 60.0),
        tier=lambda request: (
            "enterprise" if request.header("x-plan") == "enterprise" else None
        ),
    ))
    async with TestClient(app) as client:
        caller = client.acting_as("ada").with_headers(**{"x-plan": "enterprise"})

        for _ in range(4):
            assert (await caller.get("/llamas")).status == 200
        assert (await caller.get("/llamas")).status == 429


async def test_a_limited_response_says_when_to_come_back() -> None:
    app = _app(TieredRateLimitMiddleware(tiers={"pro": (5, 60.0)}, default=(1, 60.0)))
    async with TestClient(app) as client:
        caller = client.acting_as("bo")
        await caller.get("/llamas")
        limited = await caller.get("/llamas")

    assert limited.status == 429
    assert int(limited.header("retry-after")) >= 1


async def test_it_is_route_middleware_on_purpose() -> None:
    """Global hooks run before authentication, where there is no principal."""
    assert TieredRateLimitMiddleware.global_scope is False
    assert RateLimitMiddleware.global_scope is True     # ingress, keyed on address


async def test_at_least_one_tier_is_required() -> None:
    with pytest.raises(ValueError, match="at least one tier"):
        TieredRateLimitMiddleware(tiers={}, default=(1, 60.0))
