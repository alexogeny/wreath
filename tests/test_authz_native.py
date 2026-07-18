from __future__ import annotations

# Optional native imports intentionally follow their pure twins for parity cases.
# ruff: noqa: I001

import pytest

from wreath.authorization import AuthorizationDecision
from wreath._pure.authz import (
    build_capability_mask as pure_build_capability_mask,
    normalize_authorization_decision as pure_normalize_decision,
)

try:
    from wreath._native import _core
except ImportError:  # pragma: no cover
    _core = None


class EngineResult:
    allowed = True
    diagnostics = ("policy-1", 2)


@pytest.mark.parametrize(
    ("build_mask", "normalize"),
    [
        pytest.param(pure_build_capability_mask, pure_normalize_decision, id="pure"),
        pytest.param(
            None if _core is None else _core.build_capability_mask,
            None if _core is None else _core.normalize_authorization_decision,
            id="native",
            marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
        ),
    ],
)
def test_authorization_helpers_support_wide_masks_and_normalize_results(
    build_mask, normalize
) -> None:
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
