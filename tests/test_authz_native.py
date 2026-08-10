from __future__ import annotations

from wreath._native import _core
from wreath.authorization import AuthorizationDecision


class EngineResult:
    allowed = True
    diagnostics = ("policy-1", 2)


def test_authorization_helpers_support_wide_masks_and_normalize_results() -> None:
    build_mask = _core.build_capability_mask
    normalize = _core.normalize_authorization_decision
    capabilities = {
        "authenticated": 1,
        "role:admin": 1 << 70,
        "permission:billing:read": 1 << 130,
    }

    assert build_mask(capabilities, {"admin"}, {"billing:read", "unknown"}) == (
        1 | (1 << 70) | (1 << 130)
    )
    assert normalize(True, AuthorizationDecision) == AuthorizationDecision(
        True, "cedar"
    )
    assert normalize(
        EngineResult(), AuthorizationDecision
    ) == AuthorizationDecision(True, "cedar", ("policy-1", "2"))
    assert normalize(object(), AuthorizationDecision) == AuthorizationDecision(
        False, "cedar"
    )
