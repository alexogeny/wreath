"""Cedar string escapes retain their distinct parse contracts."""

from __future__ import annotations

import pytest

from wreath.authorization import CedarParseError, CedarPolicies, EntityUid


def test_braced_unicode_escape_is_decoded_before_evaluation() -> None:
    policies = CedarPolicies(
        r'permit(principal, action, resource) when { "\u{61}" == "a" };'
    )

    decision = policies.is_authorized(
        principal=EntityUid("User", "alice"),
        action=EntityUid("Action", "read"),
        resource=EntityUid("Document", "42"),
    )

    assert decision.allowed


def test_unknown_string_escape_is_refused_as_unknown() -> None:
    source = r'permit(principal, action, resource) when { "\q" == "q" };'

    with pytest.raises(CedarParseError) as excinfo:
        CedarPolicies(source)

    assert "unknown string escape \\q" in str(excinfo.value)
