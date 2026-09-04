from __future__ import annotations

from types import SimpleNamespace

import pytest

from wreath._agents.identity import principal_id, principal_partition_id
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


def test_principal_partition_distinguishes_namespaces_and_identity_types() -> None:
    unnamespaced = Identity("3:foox", namespace="")
    namespaced = Identity("x", namespace="foo")
    user = Identity("same", type="User")
    service = Identity("same", type="Service")

    assert principal_partition_id(unnamespaced) != principal_partition_id(namespaced)
    assert principal_partition_id(user) != principal_partition_id(service)


def test_principal_partition_uses_nested_verified_identity_facts() -> None:
    identity = Identity("user-1", type="Service", namespace="issuer")

    assert principal_partition_id(SimpleNamespace(identity=identity)) == principal_partition_id(
        identity
    )
    assert principal_partition_id(
        SimpleNamespace(id="spoofed-wrapper-id", identity=identity)
    ) == principal_partition_id(identity)


@pytest.mark.parametrize(
    ("principal", "message"),
    [
        (SimpleNamespace(id="user-1", namespace=7), "namespace must be a string"),
        (SimpleNamespace(id="user-1", type=7), "type must be a string"),
    ],
)
def test_principal_partition_refuses_non_string_identity_facts(
    principal: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        principal_partition_id(principal)
