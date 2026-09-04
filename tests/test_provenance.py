from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

import wreath.provenance as provenance_module
from wreath.provenance import (
    ArtifactChanged,
    Attestation,
    InvalidProvenance,
    Provenance,
    ProvenanceKey,
)

ARTIFACT = b"final report\n"
ALICE = ProvenanceKey.from_seed("alice", bytes(range(32)))
BOB = ProvenanceKey.from_seed("bob", bytes(range(1, 33)))


def test_multiple_signatories_round_trip_and_satisfy_quorum() -> None:
    signed = (
        Provenance.for_artifact(ARTIFACT, name="report.pdf", quorum=2)
        .countersign(ARTIFACT, ALICE)
        .countersign(ARTIFACT, BOB)
    )
    restored = Provenance.load(signed.dump())
    verified = restored.verify(
        ARTIFACT,
        {"alice": ALICE.public, "bob": BOB.public},
    )
    assert verified.signatories == ("alice", "bob")
    assert verified.quorum == 2


def test_a_changed_artifact_is_detected_before_signature_work() -> None:
    signed = Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, ALICE)
    with pytest.raises(ArtifactChanged, match="does not match"):
        signed.verify(ARTIFACT + b"changed", {"alice": ALICE.public})


def test_a_counter_signature_binds_the_preceding_chain() -> None:
    signed = (
        Provenance.for_artifact(ARTIFACT, quorum=1)
        .countersign(ARTIFACT, ALICE)
        .countersign(ARTIFACT, BOB)
    )
    tampered = dataclasses.replace(signed, attestations=(signed.attestations[1],))
    with pytest.raises(InvalidProvenance, match="bob.*does not verify"):
        tampered.verify(ARTIFACT, {"bob": BOB.public})


def test_the_signed_quorum_cannot_be_lowered_at_verification() -> None:
    signed = Provenance.for_artifact(ARTIFACT, quorum=2).countersign(ARTIFACT, ALICE)
    with pytest.raises(InvalidProvenance, match="only 1"):
        signed.verify(ARTIFACT, {"alice": ALICE.public})
    with pytest.raises(InvalidProvenance, match="signed quorum 2"):
        signed.verify(ARTIFACT, {"alice": ALICE.public}, quorum=1)


def test_a_seed_must_be_bytes_even_when_its_length_is_correct() -> None:
    seed: Any = bytearray(range(32))
    with pytest.raises(InvalidProvenance, match="seed must be exactly 32 bytes"):
        ProvenanceKey.from_seed("alice", seed)


def test_an_attestation_signature_must_be_bytes_of_the_exact_length() -> None:
    with pytest.raises(InvalidProvenance, match="64-byte"):
        Attestation("alice", b"x" * 63)
    signature: Any = bytearray(64)
    with pytest.raises(InvalidProvenance, match="64-byte"):
        Attestation("alice", signature)


def test_verification_quorum_refuses_a_non_integer() -> None:
    signed = Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, ALICE)
    quorum: Any = 1.5
    with pytest.raises(InvalidProvenance, match="quorum must be an integer"):
        signed.verify(ARTIFACT, {"alice": ALICE.public}, quorum=quorum)


def test_sidecar_refuses_duplicate_object_members() -> None:
    sidecar = Provenance.for_artifact(ARTIFACT).dump().replace(
        b'"version":1', b'"version":1,"version":1'
    )

    with pytest.raises(InvalidProvenance, match="duplicate provenance field 'version'"):
        Provenance.load(sidecar)


def test_sidecar_refuses_unknown_attestation_members() -> None:
    sidecar = (
        Provenance.for_artifact(ARTIFACT)
        .countersign(ARTIFACT, ALICE)
        .dump()
        .replace(b'{"key_id"', b'{"unknown":true,"key_id"')
    )

    with pytest.raises(InvalidProvenance, match="unknown attestation fields"):
        Provenance.load(sidecar)


def test_sidecar_refuses_a_non_array_attestation_collection() -> None:
    sidecar = Provenance.for_artifact(ARTIFACT).dump().replace(
        b'"attestations":[]', b'"attestations":{}'
    )

    with pytest.raises(InvalidProvenance, match="attestations must be an array"):
        Provenance.load(sidecar)


def test_sidecar_applies_the_signatory_bound_before_attestation_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(
        Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, ALICE).dump()
    )
    document["attestations"] *= 65

    def unexpected_construction(*args: Any, **kwargs: Any) -> None:
        pytest.fail("oversized sidecar reached attestation construction")

    monkeypatch.setattr(provenance_module, "Attestation", unexpected_construction)

    with pytest.raises(InvalidProvenance, match="at most 64 signatories"):
        Provenance.load(json.dumps(document).encode())


def test_sidecar_refuses_non_object_attestations_with_the_shape_error() -> None:
    document = json.loads(Provenance.for_artifact(ARTIFACT).dump())
    document["attestations"] = [None]

    with pytest.raises(InvalidProvenance, match="unknown attestation fields"):
        Provenance.load(json.dumps(document).encode())
