from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._auth.requirements import merge_requirements, requirement_for
from wreath.auth import BearerTokenBackend, Identity, oauth_step_up, second_factor
from wreath.testing import TestClient


def _app(claims: dict[str, Any], **requirement: Any) -> Wreath:
    app = Wreath()
    identity = Identity("ada", claims=claims)
    app.configure_auth(BearerTokenBackend(lambda token: identity if token == "token" else None))

    @app.get("/transfer")
    @oauth_step_up(**requirement)
    async def transfer(request: Any) -> dict[str, bool]:
        return {"transferred": True}

    return app


@pytest.mark.asyncio
async def test_rfc_9470_challenges_for_more_recent_authentication(monkeypatch: Any) -> None:
    monkeypatch.setattr("wreath.app._wall_clock", lambda: 1_700_000_010.0)
    app = _app({"auth_time": 1_700_000_000}, max_age=5)

    async with TestClient(app) as client:
        response = await client.get("/transfer", headers={"authorization": "Bearer token"})

    assert response.status == 401
    assert response.header("www-authenticate") == (
        'Bearer error="insufficient_user_authentication", '
        'error_description="More recent authentication is required", max_age="5"'
    )


@pytest.mark.asyncio
async def test_a_recent_oauth_authentication_satisfies_max_age(monkeypatch: Any) -> None:
    monkeypatch.setattr("wreath.app._wall_clock", lambda: 1_700_000_004.0)
    app = _app({"auth_time": 1_700_000_000}, max_age=5)

    async with TestClient(app) as client:
        response = await client.get("/transfer", headers={"authorization": "Bearer token"})

    assert response.status == 200


@pytest.mark.asyncio
async def test_rfc_9470_challenges_for_an_acceptable_authentication_class() -> None:
    app = _app({"acr": "urn:example:loa:1"}, acr_values=("urn:example:loa:3", "myACR"))

    async with TestClient(app) as client:
        response = await client.get("/transfer", headers={"authorization": "Bearer token"})

    assert response.status == 401
    assert response.header("www-authenticate") == (
        'Bearer error="insufficient_user_authentication", '
        'error_description="A different authentication level is required", '
        'acr_values="urn:example:loa:3 myACR"'
    )


@pytest.mark.asyncio
async def test_one_of_the_declared_authentication_classes_satisfies_the_route() -> None:
    app = _app({"acr": "myACR"}, acr_values=("urn:example:loa:3", "myACR"))

    async with TestClient(app) as client:
        response = await client.get("/transfer", headers={"authorization": "Bearer token"})

    assert response.status == 200


@pytest.mark.asyncio
async def test_both_oauth_step_up_requirements_must_be_met(monkeypatch: Any) -> None:
    monkeypatch.setattr("wreath.app._wall_clock", lambda: 1_700_000_010.0)
    app = _app(
        {"auth_time": 1_700_000_000, "acr": "myACR"},
        max_age=5,
        acr_values=("myACR",),
    )

    async with TestClient(app) as client:
        response = await client.get("/transfer", headers={"authorization": "Bearer token"})

    assert response.status == 401
    assert response.header("www-authenticate") == (
        'Bearer error="insufficient_user_authentication", '
        'error_description="More recent authentication and a different authentication '
        'level are required", max_age="5", acr_values="myACR"'
    )


@pytest.mark.asyncio
async def test_an_anonymous_request_gets_the_normal_bearer_challenge() -> None:
    app = _app({}, max_age=5)

    async with TestClient(app) as client:
        response = await client.get("/transfer")

    assert response.status == 401
    assert response.header("www-authenticate") == "Bearer"


def test_oauth_step_up_refuses_incomplete_or_ambiguous_declarations() -> None:
    with pytest.raises(ValueError, match="max_age or acr_values"):
        oauth_step_up()
    with pytest.raises(TypeError, match="non-negative integer"):
        oauth_step_up(max_age=True)
    with pytest.raises(TypeError, match="non-negative integer"):
        oauth_step_up(max_age=1.5)
    with pytest.raises(ValueError, match="non-negative integer"):
        oauth_step_up(max_age=-1)
    with pytest.raises(ValueError, match="non-empty ASCII"):
        oauth_step_up(acr_values=("",))
    with pytest.raises(ValueError, match="non-empty ASCII"):
        oauth_step_up(acr_values=("has space",))
    with pytest.raises(ValueError, match="unique"):
        oauth_step_up(acr_values=("myACR", "myACR"))


def test_stacked_oauth_step_up_guards_keep_the_strictest_common_requirement() -> None:
    @oauth_step_up(max_age=60, acr_values=("two", "three"))
    @oauth_step_up(max_age=300, acr_values=("one", "two"))
    async def endpoint() -> None:
        return None

    requirement = requirement_for(endpoint).oauth_step_up
    assert requirement is not None
    assert requirement.max_age == 60
    assert requirement.acr_values == ("two",)


def test_merging_incompatible_authentication_classes_is_refused() -> None:
    from wreath._auth.requirements import AuthRequirement, OAuthStepUpRequirement

    first = AuthRequirement(oauth_step_up=OAuthStepUpRequirement(acr_values=("one",)))
    second = AuthRequirement(oauth_step_up=OAuthStepUpRequirement(acr_values=("two",)))

    with pytest.raises(ValueError, match="no authentication class in common"):
        merge_requirements(first, second)


def test_oauth_and_session_step_up_cannot_declare_two_remediation_flows() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):

        @oauth_step_up(max_age=60)
        @second_factor(max_age=60)
        async def endpoint() -> None:
            return None


@pytest.mark.asyncio
async def test_session_second_factor_guard_keeps_its_403_contract() -> None:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda token: Identity("ada", claims={}) if token == "token" else None)
    )

    @app.get("/session-step-up")
    @second_factor(max_age=300)
    async def session_step_up(request: Any) -> None:
        return None

    async with TestClient(app) as client:
        response = await client.get("/session-step-up", headers={"authorization": "Bearer token"})

    assert response.status == 403
    assert response.header("www-authenticate") is None
