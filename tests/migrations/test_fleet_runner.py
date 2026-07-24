"""The managed tenant fleet runner: pack a directory, resolve it in metal."""

from __future__ import annotations

import pytest

from wreath.migrations import (
    HISTORY_AMBIGUOUS,
    HISTORY_BLOCKED,
    HISTORY_UNKNOWN,
    HISTORY_VERIFIED,
    FleetResolution,
    TenantState,
    pack_tenant_directory,
    resolve_fleet,
)

TARGET_MIGRATION = 12
TARGET_CHECKSUM = 0xABCDEF
GENERATION = 5


def _resolve(states: list[TenantState]) -> FleetResolution:
    return resolve_fleet(
        states,
        target_migration=TARGET_MIGRATION,
        target_checksum=TARGET_CHECKSUM,
        directory_generation=GENERATION,
    )


def test_a_verified_tenant_at_target_is_current() -> None:
    state = TenantState(1, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_VERIFIED)
    result = _resolve([state])
    assert result.current == 1 and result.total == 1
    assert (result.apply, result.verify, result.ambiguous, result.blocked) == (0, 0, 0, 0)


def test_a_verified_tenant_behind_target_needs_apply() -> None:
    state = TenantState(1, TARGET_MIGRATION - 1, TARGET_CHECKSUM, GENERATION, HISTORY_VERIFIED)
    assert _resolve([state]).apply == 1


def test_a_verified_tenant_with_wrong_checksum_needs_apply() -> None:
    state = TenantState(1, TARGET_MIGRATION, 0x999, GENERATION, HISTORY_VERIFIED)
    assert _resolve([state]).apply == 1


def test_unknown_history_forces_catalog_verification() -> None:
    state = TenantState(1, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_UNKNOWN)
    assert _resolve([state]).verify == 1


def test_a_stale_generation_forces_verification_even_when_verified() -> None:
    state = TenantState(1, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION - 1, HISTORY_VERIFIED)
    assert _resolve([state]).verify == 1


def test_ambiguous_and_blocked_are_terminal() -> None:
    result = _resolve(
        [
            TenantState(1, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_AMBIGUOUS),
            TenantState(2, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_BLOCKED),
        ]
    )
    assert result.ambiguous == 1 and result.blocked == 1
    assert result.current == 0


def test_a_mixed_fleet_classifies_every_bucket_once() -> None:
    states = [
        TenantState(1, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_VERIFIED),
        TenantState(2, TARGET_MIGRATION - 3, TARGET_CHECKSUM, GENERATION, HISTORY_VERIFIED),
        TenantState(3, 0, 0, GENERATION, HISTORY_UNKNOWN),
        TenantState(4, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_AMBIGUOUS),
        TenantState(5, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_BLOCKED),
    ]
    result = _resolve(states)
    assert (result.current, result.apply, result.verify, result.ambiguous, result.blocked) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert result.total == 5


def test_an_empty_fleet_resolves_to_zero() -> None:
    result = _resolve([])
    assert result.total == 0


def test_a_large_fleet_resolves_in_one_call() -> None:
    states = [
        TenantState(i, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_VERIFIED)
        for i in range(10_000)
    ]
    assert _resolve(states).current == 10_000


# -- packing and validation ---------------------------------------------------


def test_pack_is_thirty_two_bytes_per_tenant() -> None:
    states = [
        TenantState(i, TARGET_MIGRATION, TARGET_CHECKSUM, GENERATION, HISTORY_VERIFIED)
        for i in range(4)
    ]
    assert len(pack_tenant_directory(states)) == 4 * 32


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tenant_id": -1},
        {"migration": -1},
        {"checksum": -1},
        {"generation": -1},
        {"status": 9},
        {"generation": 2**32},
        {"checksum": 2**64},
    ],
)
def test_invalid_tenant_state_is_rejected(kwargs: dict) -> None:
    base = {
        "tenant_id": 1,
        "migration": TARGET_MIGRATION,
        "checksum": TARGET_CHECKSUM,
        "generation": GENERATION,
        "status": HISTORY_VERIFIED,
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        TenantState(**base)


@pytest.mark.parametrize("bad", [-1, "x", 1.5])
def test_resolve_fleet_validates_targets(bad: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        resolve_fleet(
            [],
            target_migration=bad,
            target_checksum=TARGET_CHECKSUM,
            directory_generation=GENERATION,
        )
