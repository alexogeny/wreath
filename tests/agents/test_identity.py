from __future__ import annotations

from types import SimpleNamespace

import pytest

from wreath._agents.identity import principal_id
from wreath.auth import Identity
from wreath.authorization import human


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        ("user-1", "user-1"),
        (SimpleNamespace(id="user-2"), "user-2"),
        (SimpleNamespace(subject="user-3"), "user-3"),
        (human(Identity("user-4")), "user-4"),
        (SimpleNamespace(identity=SimpleNamespace(subject="user-5")), "user-5"),
    ],
)
def test_principal_id_accepts_wreath_and_protocol_principal_shapes(
    principal: object, expected: str
) -> None:
    assert principal_id(principal) == expected


@pytest.mark.parametrize(
    "principal",
    ["", object(), SimpleNamespace(id=""), SimpleNamespace(identity=object())],
)
def test_principal_id_refuses_missing_or_empty_identity(principal: object) -> None:
    with pytest.raises(ValueError, match="agent principal"):
        principal_id(principal)
