from __future__ import annotations

import struct

import pytest

from wreath.migrations import FleetResolution, _resolve_managed_snapshot

_ROW = struct.Struct("<QQQIB3x")


def row(
    tenant: int,
    migration: int,
    checksum: int,
    generation: int,
    status: int,
) -> bytes:
    return _ROW.pack(tenant, migration, checksum, generation, status)


def test_managed_snapshot_classifies_packed_rows() -> None:
    snapshot = b"".join(
        (
            row(1, 7, 91, 3, 1),  # current
            row(2, 6, 80, 3, 1),  # apply
            row(3, 7, 91, 2, 1),  # verify stale directory generation
            row(4, 7, 91, 3, 0),  # verify unknown history
            row(5, 7, 91, 3, 2),  # ambiguous
            row(6, 7, 91, 3, 3),  # blocked
        )
    )

    assert _resolve_managed_snapshot(
        snapshot,
        target_migration=7,
        target_checksum=91,
        directory_generation=3,
    ) == FleetResolution(current=1, apply=1, verify=2, ambiguous=1, blocked=1)


def test_managed_snapshot_rejects_partial_rows() -> None:
    with pytest.raises(ValueError, match="multiple of 32"):
        _resolve_managed_snapshot(
            b"broken",
            target_migration=7,
            target_checksum=91,
            directory_generation=3,
        )


def test_managed_snapshot_rejects_non_buffer_input() -> None:
    with pytest.raises(TypeError):
        _resolve_managed_snapshot(  # type: ignore[arg-type]
            [1, 2, 3],
            target_migration=7,
            target_checksum=91,
            directory_generation=3,
        )


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("target_migration", True),
        ("target_migration", 2**64),
        ("target_checksum", True),
        ("target_checksum", 2**64),
        ("directory_generation", True),
        ("directory_generation", 2**32),
    ],
)
def test_managed_snapshot_refuses_unrepresentable_native_arguments(
    field: str, bad: object
) -> None:
    values = {
        "target_migration": 7,
        "target_checksum": 91,
        "directory_generation": 3,
    }
    values[field] = bad

    with pytest.raises(ValueError, match=field):
        _resolve_managed_snapshot(b"", **values)
