from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from wreath._auth.requirements import (
    AuthRequirement,
    OAuthStepUpRequirement,
    PolicyRequirement,
    SetRequirement,
    merge_requirements,
)


@pytest.mark.parametrize("count", [0, 1, 2, 8])
def test_neutral_merges_share_immutable_result(count):
    inputs = [AuthRequirement() for _ in range(count)]
    results = [merge_requirements(*inputs) for _ in range(100)]
    assert all(result == AuthRequirement() for result in results)
    assert len({id(result) for result in results}) == 1
    result: Any = results[0]
    with pytest.raises(FrozenInstanceError):
        result.authenticated = True


@pytest.mark.parametrize("field", ["public", "authenticated", "identify"])
@pytest.mark.parametrize("value", [0, 0.0, None, "", (), []])
def test_false_like_field_values_are_not_canonicalized(field, value):
    fields: dict[str, Any] = {field: value}
    result = merge_requirements(AuthRequirement(**fields))
    assert getattr(result, field) is value


def test_subclass_access_property_is_still_evaluated():
    visited = []

    class Requirement(AuthRequirement):
        @property
        def access_level(self):
            visited.append("access")
            return 0

    result = merge_requirements(Requirement(), Requirement())
    assert type(result) is AuthRequirement
    assert result == AuthRequirement()
    assert visited == ["access", "access"]


@pytest.mark.parametrize(
    "fields",
    [
        {"public": True},
        {"authenticated": True},
        {"identify": True},
        {"role_checks": (SetRequirement(frozenset({"admin"}), "all"),)},
        {"permission_checks": (SetRequirement(frozenset({"read"}), "any"),)},
        {"policies": (PolicyRequirement("read", "document"),)},
        {"second_factor": 30},
        {"oauth_step_up": OAuthStepUpRequirement(max_age=0)},
    ],
)
def test_non_neutral_fields_remain_present(fields):
    requirement = AuthRequirement(**fields)
    assert merge_requirements(AuthRequirement(), requirement) == requirement


def test_merge_refusals_are_not_bypassed():
    with pytest.raises(ValueError, match="public.*cannot be combined"):
        merge_requirements(AuthRequirement(public=True), AuthRequirement(authenticated=True))
    with pytest.raises(ValueError, match="second_factor.*cannot be combined"):
        merge_requirements(
            AuthRequirement(second_factor=10),
            AuthRequirement(oauth_step_up=OAuthStepUpRequirement(max_age=20)),
        )
    with pytest.raises(ValueError, match="no authentication class in common"):
        merge_requirements(
            AuthRequirement(oauth_step_up=OAuthStepUpRequirement(acr_values=("a",))),
            AuthRequirement(oauth_step_up=OAuthStepUpRequirement(acr_values=("b",))),
        )


@pytest.mark.parametrize("field", ["role_checks", "permission_checks", "policies"])
def test_merge_still_validates_subclass_contents(field):
    class Unvalidated(AuthRequirement):
        def __post_init__(self):
            pass

        @property
        def access_level(self):
            return 0

    fields: dict[str, Any] = {field: ("invalid",)}
    with pytest.raises(TypeError, match=field):
        merge_requirements(Unvalidated(**fields))
