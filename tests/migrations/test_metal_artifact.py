"""Immutable migration artifacts are built and verified by Wreath-metal."""

from __future__ import annotations

import hashlib

import pytest

from wreath.migrations import (
    _build_native_artifact,
    _load_native_artifact,
    _verify_native_chain,
)

MIGRATION_ID = bytes.fromhex("00112233445566778899aabbccddeeff")
PARENT = bytes(32)
SOURCE = b"s" * 32
TARGET = b"t" * 32
EMPTY_TAPE = b"WMO1\x01\x00\x00\x00\x00\x00\x00\x00"
EMPTY_PLAN = b"WMP1\x01\x00\x00\x00\x00\x00\x00\x00"
EMPTY_SQL = b"WMS1\x01\x00\x00\x00\x00\x00\x00\x00"


def test_native_artifact_round_trips_verified_metadata_and_tape() -> None:
    artifact = _build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=PARENT,
        source_fingerprint=SOURCE,
        target_fingerprint=TARGET,
        operation_tape=EMPTY_TAPE,
        named_plan=EMPTY_PLAN,
        sql_tape=EMPTY_SQL,
    )

    assert artifact.data[:4] == b"WMA1"
    assert artifact.checksum == hashlib.sha256(
        artifact.data[:136] + bytes(32) + artifact.data[168:]
    ).digest()
    loaded = _load_native_artifact(artifact.data)
    assert loaded == artifact
    assert loaded.migration_id == MIGRATION_ID
    assert loaded.parent_checksum == PARENT
    assert loaded.source_fingerprint == SOURCE
    assert loaded.target_fingerprint == TARGET
    assert loaded.operation_tape == EMPTY_TAPE
    assert loaded.named_plan == EMPTY_PLAN
    assert loaded.sql_tape == EMPTY_SQL


def test_native_chain_verifies_parent_and_schema_continuity_in_one_call() -> None:
    first = _build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=PARENT,
        source_fingerprint=SOURCE,
        target_fingerprint=TARGET,
        operation_tape=EMPTY_TAPE,
        named_plan=EMPTY_PLAN,
        sql_tape=EMPTY_SQL,
    )
    second = _build_native_artifact(
        migration_id=b"2" * 16,
        parent_checksum=first.checksum,
        source_fingerprint=first.target_fingerprint,
        target_fingerprint=b"u" * 32,
        operation_tape=EMPTY_TAPE,
        named_plan=EMPTY_PLAN,
        sql_tape=EMPTY_SQL,
    )

    chain = _verify_native_chain(
        (first.data, second.data), expected_parent=PARENT, expected_source=SOURCE
    )

    assert chain.migration_count == 2
    assert chain.checksum == second.checksum
    assert chain.target_fingerprint == second.target_fingerprint

    with pytest.raises(ValueError, match="parent mismatch"):
        _verify_native_chain(
            (second.data, first.data), expected_parent=PARENT, expected_source=SOURCE
        )
    with pytest.raises(ValueError, match="source mismatch"):
        _verify_native_chain(
            (first.data, second.data), expected_parent=PARENT, expected_source=b"x" * 32
        )


def test_native_artifact_generation_is_byte_deterministic() -> None:
    arguments = {
        "migration_id": MIGRATION_ID,
        "parent_checksum": PARENT,
        "source_fingerprint": SOURCE,
        "target_fingerprint": TARGET,
        "operation_tape": EMPTY_TAPE,
        "named_plan": EMPTY_PLAN,
        "sql_tape": EMPTY_SQL,
    }

    assert _build_native_artifact(**arguments).data == _build_native_artifact(**arguments).data


def test_artifact_checksum_detects_any_mutation() -> None:
    artifact = _build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=PARENT,
        source_fingerprint=SOURCE,
        target_fingerprint=TARGET,
        operation_tape=EMPTY_TAPE,
        named_plan=EMPTY_PLAN,
        sql_tape=EMPTY_SQL,
    )
    corrupted = bytearray(artifact.data)
    corrupted[-1] ^= 1

    with pytest.raises(ValueError, match="checksum"):
        _load_native_artifact(bytes(corrupted))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("migration_id", b"short"),
        ("parent_checksum", b"short"),
        ("source_fingerprint", b"short"),
        ("target_fingerprint", b"short"),
        ("operation_tape", b"not a tape"),
        ("named_plan", b"not a plan"),
        ("sql_tape", b"not sql"),
    ],
)
def test_artifact_rejects_invalid_fixed_fields(field: str, value: bytes) -> None:
    arguments = {
        "migration_id": MIGRATION_ID,
        "parent_checksum": PARENT,
        "source_fingerprint": SOURCE,
        "target_fingerprint": TARGET,
        "operation_tape": EMPTY_TAPE,
        "named_plan": EMPTY_PLAN,
        "sql_tape": EMPTY_SQL,
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        _build_native_artifact(**arguments)


def test_every_artifact_byte_is_covered_by_structure_or_checksum() -> None:
    artifact = _build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=PARENT,
        source_fingerprint=SOURCE,
        target_fingerprint=TARGET,
        operation_tape=EMPTY_TAPE,
        named_plan=EMPTY_PLAN,
        sql_tape=EMPTY_SQL,
    ).data

    for index in range(len(artifact)):
        corrupted = bytearray(artifact)
        corrupted[index] ^= 1
        with pytest.raises(ValueError):
            _load_native_artifact(bytes(corrupted))


def test_artifact_loader_rejects_truncation_and_unknown_format() -> None:
    with pytest.raises(ValueError):
        _load_native_artifact(b"")
    with pytest.raises(ValueError):
        _load_native_artifact(b"BAD!" + bytes(200))
