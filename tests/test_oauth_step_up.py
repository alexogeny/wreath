from __future__ import annotations

from enum import StrEnum
from types import SimpleNamespace
from typing import Any

import pytest

from wreath import Wreath
from wreath._auth.requirements import (
    AuthRequirement,
    OAuthStepUpRequirement,
    SetRequirement,
    _merge_oauth_step_up,
    add_authenticated,
    add_identify,
    add_oauth_step_up,
    add_public,
    add_second_factor,
    merge_requirements,
    requirement_for,
    second_factor_age,
)
from wreath.auth import BearerTokenBackend, Identity, oauth_step_up, second_factor
from wreath.authorization import AuthorizationVocabulary
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


@pytest.mark.parametrize("value", [1, b"loa", "café", "loa\x00", 'loa"2', "loa\\2"])
def test_oauth_step_up_refuses_each_unsafe_authentication_class(value: object) -> None:
    with pytest.raises(ValueError, match="non-empty ASCII"):
        OAuthStepUpRequirement(acr_values=(value,))


@pytest.mark.parametrize("claims", [None, [], "claims", 1])
def test_oauth_step_up_refuses_non_mapping_claims(claims: object) -> None:
    identity = SimpleNamespace(claims=claims)

    assert OAuthStepUpRequirement(max_age=60).satisfied_by(identity, now=1000) is False


@pytest.mark.parametrize("stamp", [None, True, False, "900", object()])
def test_oauth_step_up_refuses_each_invalid_authentication_time(stamp: object) -> None:
    identity = SimpleNamespace(claims={"auth_time": stamp})

    assert OAuthStepUpRequirement(max_age=60).satisfied_by(identity, now=1000) is False


def test_oauth_step_up_requires_current_time_for_recency_check() -> None:
    identity = SimpleNamespace(claims={"auth_time": 900})

    assert OAuthStepUpRequirement(max_age=60).satisfied_by(identity) is False


def test_oauth_step_up_refuses_boolean_authentication_time_at_epoch() -> None:
    identity = SimpleNamespace(claims={"auth_time": False})

    assert OAuthStepUpRequirement(max_age=60).satisfied_by(identity, now=0) is False


def test_future_authentication_times_never_satisfy_recency_checks() -> None:
    identity = SimpleNamespace(claims={"auth_time": 1001.0, "second_factor_at": 1001.0})

    assert OAuthStepUpRequirement(max_age=60).satisfied_by(identity, now=1000.0) is False
    assert second_factor_age(identity, 1000.0) is None


@pytest.mark.parametrize(
    "stamp",
    [float("nan"), float("inf"), float("-inf"), 10**1000],
)
def test_invalid_numeric_authentication_times_never_satisfy_recency_checks(
    stamp: int | float,
) -> None:
    identity = SimpleNamespace(claims={"auth_time": stamp, "second_factor_at": stamp})

    assert OAuthStepUpRequirement(max_age=60).satisfied_by(identity, now=1000.0) is False
    assert second_factor_age(identity, 1000.0) is None


class FirstActions(StrEnum):
    READ = "read"
    SHARED = "shared"


class SecondActions(StrEnum):
    WRITE = "write"
    SHARED = "shared"


class EmptyAction(StrEnum):
    EMPTY = ""


def test_authorization_vocabulary_requires_at_least_one_enum() -> None:
    with pytest.raises(ValueError, match="at least one StrEnum"):
        AuthorizationVocabulary()


@pytest.mark.parametrize("enum", [1, object(), str, int])
def test_authorization_vocabulary_requires_str_enum_classes(enum: object) -> None:
    with pytest.raises(TypeError, match="built from StrEnum classes"):
        AuthorizationVocabulary(enum)


def test_authorization_vocabulary_refuses_empty_action() -> None:
    with pytest.raises(ValueError, match="actions cannot be empty"):
        AuthorizationVocabulary(EmptyAction)


def test_authorization_vocabulary_refuses_duplicate_action_across_enums() -> None:
    with pytest.raises(ValueError, match="actions must be unique: shared"):
        AuthorizationVocabulary(FirstActions, SecondActions)


def test_authorization_vocabulary_accepts_unique_actions() -> None:
    assert AuthorizationVocabulary(FirstActions).actions == ("read", "shared")


@pytest.mark.parametrize(
    ("requirement", "declares"),
    [
        (AuthRequirement(), False),
        (AuthRequirement(public=True), True),
        (AuthRequirement(identify=True), True),
        (AuthRequirement(authenticated=True), True),
    ],
)
def test_auth_requirement_declaration_modes_are_distinct(
    requirement: AuthRequirement,
    declares: bool,
) -> None:
    assert requirement.declares_access is declares


@pytest.mark.parametrize(
    ("check", "level"),
    [
        (SetRequirement(frozenset({"admin"}), "all"), 2),
        (SetRequirement(frozenset({"admin"}), "any"), 1),
        (SetRequirement(frozenset({"editor"}), "all"), 1),
    ],
)
def test_auth_requirement_admin_level_requires_exact_all_admin_check(
    check: SetRequirement,
    level: int,
) -> None:
    assert AuthRequirement(role_checks=(check,)).access_level == level


def test_oauth_step_up_merge_handles_absent_right_requirement() -> None:
    left = OAuthStepUpRequirement(max_age=60)

    assert _merge_oauth_step_up(left, None) is left


@pytest.mark.parametrize(
    ("left", "right", "max_age", "acr_values"),
    [
        (
            OAuthStepUpRequirement(acr_values=("loa",)),
            OAuthStepUpRequirement(max_age=60),
            60,
            ("loa",),
        ),
        (
            OAuthStepUpRequirement(max_age=60),
            OAuthStepUpRequirement(acr_values=("loa",)),
            60,
            ("loa",),
        ),
    ],
)
def test_oauth_step_up_merge_preserves_each_one_sided_constraint(
    left: OAuthStepUpRequirement,
    right: OAuthStepUpRequirement,
    max_age: int,
    acr_values: tuple[str, ...],
) -> None:
    merged = _merge_oauth_step_up(left, right)

    assert merged == OAuthStepUpRequirement(max_age=max_age, acr_values=acr_values)


def test_protected_requirement_refuses_predeclared_public_endpoint() -> None:
    def endpoint() -> None:
        return None

    add_public(endpoint)

    with pytest.raises(ValueError, match="public.*authentication"):
        add_authenticated(endpoint)


def test_public_requirement_refuses_identifying_endpoint() -> None:
    def endpoint() -> None:
        return None

    add_identify(endpoint)

    with pytest.raises(ValueError, match="public.*authentication"):
        add_public(endpoint)


def test_session_step_up_refuses_existing_oauth_step_up_directly() -> None:
    def endpoint() -> None:
        return None

    add_oauth_step_up(endpoint, OAuthStepUpRequirement(max_age=60))

    with pytest.raises(ValueError, match="cannot be combined"):
        add_second_factor(endpoint, 60)


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


@pytest.mark.parametrize("max_age", [float("nan"), float("inf")])
def test_session_second_factor_refuses_non_finite_freshness_windows(
    max_age: float,
) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        second_factor(max_age=max_age)


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
