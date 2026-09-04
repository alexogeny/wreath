from __future__ import annotations

import pytest

from wreath import Wreath
from wreath._auth.models import Identity
from wreath._auth.requirements import add_authenticated
from wreath.policy import (
    HttpPolicy,
    RateLimitPolicy,
    TieredRateLimitPolicy,
    principal_key,
)
from wreath.testing import TestClient

pytestmark = pytest.mark.asyncio


def _app(middleware) -> Wreath:
    app = Wreath(http_policy=HttpPolicy(principal_rate_limit=middleware))

    @app.get("/llamas")
    @add_authenticated
    async def llamas(request) -> dict:
        return {"ok": True}

    return app


async def test_each_principal_gets_its_own_allowance() -> None:
    app = _app(TieredRateLimitPolicy(tiers={"pro": (9, 60.0)}, default=(1, 60.0)))
    async with TestClient(app) as client:
        ada = client.acting_as("ada")
        bo = client.acting_as("bo")

        assert (await ada.get("/llamas")).status == 200
        assert (await ada.get("/llamas")).status == 429
        assert (await bo.get("/llamas")).status == 200  # a separate allowance


async def test_the_global_limiter_refuses_to_key_on_the_principal() -> None:
    with pytest.raises(ValueError, match="before authentication"):
        RateLimitPolicy(limit=1, window=60.0, key=principal_key)


async def test_a_principal_id_cannot_collide_with_an_address() -> None:
    class _Anonymous:
        identity = None
        client = ("testclient", 5000)

    class _Named:
        identity = Identity("testclient")
        client = ("10.0.0.1", 5000)

    assert principal_key(_Anonymous()) == "ip:testclient"
    assert principal_key(_Named()) == "4:User0:testclient"


async def test_identity_type_and_id_boundaries_cannot_collide() -> None:
    class _First:
        identity = Identity("c", type="a:b")
        client = None

    class _Second:
        identity = Identity("b:c", type="a")
        client = None

    assert principal_key(_First()) != principal_key(_Second())


async def test_empty_identity_components_cannot_mimic_framed_components() -> None:
    class _EmptyType:
        identity = Identity("0:b", type="", namespace="a")
        client = None

    class _Typed:
        identity = Identity("b", type="a")
        client = None

    class _EmptyNamespace:
        identity = Identity("1:ab", type="x")
        client = None

    class _Namespaced:
        identity = Identity("b", type="x", namespace="a")
        client = None

    assert principal_key(_EmptyType()) != principal_key(_Typed())
    assert principal_key(_EmptyNamespace()) != principal_key(_Namespaced())


async def test_an_anonymous_caller_falls_back_to_its_address() -> None:
    class _Anonymous:
        identity = None
        client = ("203.0.113.7", 5000)

    assert principal_key(_Anonymous()) == "ip:203.0.113.7"


async def test_a_tier_gets_the_allowance_its_role_names() -> None:
    app = _app(TieredRateLimitPolicy(tiers={"pro": (3, 60.0)}, default=(1, 60.0)))
    async with TestClient(app) as client:
        free = client.acting_as("bo")
        pro = client.acting_as("ada", roles=["pro"])

        assert (await free.get("/llamas")).status == 200
        assert (await free.get("/llamas")).status == 429

        for _ in range(3):
            assert (await pro.get("/llamas")).status == 200
        assert (await pro.get("/llamas")).status == 429


async def test_the_most_generous_matching_tier_wins() -> None:
    app = _app(
        TieredRateLimitPolicy(tiers={"pro": (3, 60.0), "enterprise": (8, 60.0)}, default=(1, 60.0))
    )
    async with TestClient(app) as client:
        both = client.acting_as("ada", roles=["pro", "enterprise"])

        for _ in range(8):
            assert (await both.get("/llamas")).status == 200
        assert (await both.get("/llamas")).status == 429


async def test_an_unrecognised_role_gets_the_default() -> None:
    app = _app(TieredRateLimitPolicy(tiers={"pro": (5, 60.0)}, default=(1, 60.0)))
    async with TestClient(app) as client:
        other = client.acting_as("bo", roles=["llama-walker"])

        assert (await other.get("/llamas")).status == 200
        assert (await other.get("/llamas")).status == 429


async def test_tiers_do_not_share_a_bucket() -> None:
    app = _app(TieredRateLimitPolicy(tiers={"pro": (2, 60.0)}, default=(1, 60.0)))
    async with TestClient(app) as client:
        free = client.acting_as("ada")
        promoted = client.acting_as("ada", roles=["pro"])

        assert (await free.get("/llamas")).status == 200
        assert (await free.get("/llamas")).status == 429
        for _ in range(2):
            assert (await promoted.get("/llamas")).status == 200


async def test_a_custom_tier_function_can_read_anything() -> None:
    app = _app(
        TieredRateLimitPolicy(
            tiers={"enterprise": (4, 60.0)},
            default=(1, 60.0),
            tier=lambda request: "enterprise" if request.header("x-plan") == "enterprise" else None,
        )
    )
    async with TestClient(app) as client:
        caller = client.acting_as("ada").with_headers(**{"x-plan": "enterprise"})

        for _ in range(4):
            assert (await caller.get("/llamas")).status == 200
        assert (await caller.get("/llamas")).status == 429


async def test_a_limited_response_says_when_to_come_back() -> None:
    app = _app(TieredRateLimitPolicy(tiers={"pro": (5, 60.0)}, default=(1, 60.0)))
    async with TestClient(app) as client:
        caller = client.acting_as("bo")
        await caller.get("/llamas")
        limited = await caller.get("/llamas")

    assert limited.status == 429
    assert int(limited.header("retry-after")) >= 1


async def test_the_tiered_limiter_is_first_class_post_auth_policy() -> None:
    limiter = TieredRateLimitPolicy(tiers={"pro": (5, 60.0)}, default=(1, 60.0))
    policy = HttpPolicy(principal_rate_limit=limiter)
    assert policy.principal_rate_limit is limiter
    assert not hasattr(TieredRateLimitPolicy, "global_scope")


async def test_a_local_principal_limiter_uses_its_synchronous_stage() -> None:
    app = _app(RateLimitPolicy(limit=1, window=60.0))
    async with TestClient(app) as client:
        caller = client.acting_as("ada")
        assert (await caller.get("/llamas")).status == 200
        assert (await caller.get("/llamas")).status == 429


async def test_at_least_one_tier_is_required() -> None:
    with pytest.raises(ValueError, match="at least one tier"):
        TieredRateLimitPolicy(tiers={}, default=(1, 60.0))


# `TieredRateLimitPolicy` builds one `RateLimitPolicy` per tier.
# `wreath mutant` dropped `window=`, `cost=` and `exempt=` from that call and
# every test stayed green: every test above uses `window=60.0`, which is also
# `RateLimitPolicy`'s default, so the propagation was invisible. A tier
# declared "10 per day" silently becoming "10 per minute" is a 1440x weaker
# limit that no test could see.


async def test_a_tier_window_is_not_quietly_replaced_by_the_default() -> None:
    app = _app(
        TieredRateLimitPolicy(
            tiers={"pro": (1, 3600.0)},
            default=(1, 1800.0),
        )
    )
    async with TestClient(app) as client:
        pro = client.acting_as("ada", roles=["pro"])
        assert (await pro.get("/llamas")).status == 200
        refused = await pro.get("/llamas")
        assert refused.status == 429
        # One token refills in window/limit seconds. Far above the 60s default,
        # which is the number this would collapse to.
        assert int(refused.header("retry-after")) > 3000

        free = client.acting_as("bo")
        assert (await free.get("/llamas")).status == 200
        refused = await free.get("/llamas")
        assert refused.status == 429
        # The default tier carries its own window, distinct from both.
        assert 1500 < int(refused.header("retry-after")) <= 1800


async def test_the_cost_a_request_spends_reaches_every_tier() -> None:
    app = _app(
        TieredRateLimitPolicy(
            tiers={"pro": (4, 60.0)},
            default=(4, 60.0),
            cost=2.0,
        )
    )
    async with TestClient(app) as client:
        for who in (client.acting_as("ada", roles=["pro"]), client.acting_as("bo")):
            assert (await who.get("/llamas")).status == 200
            assert (await who.get("/llamas")).status == 200
            assert (await who.get("/llamas")).status == 429


async def test_an_exempt_request_is_not_limited_in_any_tier() -> None:
    app = _app(
        TieredRateLimitPolicy(
            tiers={"pro": (1, 60.0)},
            default=(1, 60.0),
            exempt=lambda request: request.header("x-internal") == "yes",
        )
    )
    async with TestClient(app) as client:
        for who in (client.acting_as("ada", roles=["pro"]), client.acting_as("bo")):
            for _ in range(5):
                response = await who.get("/llamas", headers={"x-internal": "yes"})
                assert response.status == 200
            # ... and the same caller without the header is limited normally.
            assert (await who.get("/llamas")).status == 200
            assert (await who.get("/llamas")).status == 429


async def test_an_anonymous_caller_on_a_public_route_gets_the_default_tier() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            principal_rate_limit=TieredRateLimitPolicy(tiers={"pro": (9, 60.0)}, default=(1, 60.0))
        )
    )

    @app.get("/public")
    async def public(request) -> dict:
        return {"ok": True}

    async with TestClient(app) as client:
        assert (await client.get("/public")).status == 200
        refused = await client.get("/public")
        assert refused.status == 429  # the default tier, not a crash
