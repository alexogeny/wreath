from __future__ import annotations

from typing import Any

import pytest

from wreath.provenance import (
    ArtifactChanged,
    Attestation,
    InvalidProvenance,
    Provenance,
    ProvenanceKey,
)

ARTIFACT = b"approved artifact"
SEED = bytes(range(32))
SIGNER = ProvenanceKey.from_seed("signer", SEED)


def test_from_seed_accepts_an_exact_bytes_seed_at_call_time() -> None:
    key = ProvenanceKey.from_seed("runtime", SEED)

    assert key.key_id == "runtime"
    assert len(key.public) == 32


@pytest.mark.parametrize("seed", [b"x" * 31, b"x" * 33])
def test_from_seed_refuses_every_wrong_bytes_length(seed: bytes) -> None:
    with pytest.raises(InvalidProvenance, match="exactly 32 bytes"):
        ProvenanceKey.from_seed("runtime", seed)


@pytest.mark.parametrize("key_id", ["", None, 7])
def test_attestation_refuses_every_invalid_key_id(key_id: Any) -> None:
    with pytest.raises(InvalidProvenance, match="key_id must be a non-empty string"):
        Attestation(key_id, b"s" * 64)


def test_artifact_digest_refuses_non_bytes_like_values() -> None:
    artifact: Any = "artifact"
    with pytest.raises(TypeError, match="must be bytes-like, not str"):
        Provenance.for_artifact(artifact)


@pytest.mark.parametrize("digest", [b"d" * 31, b"d" * 33, bytearray(32)])
def test_provenance_refuses_invalid_digest_shapes(digest: Any) -> None:
    with pytest.raises(InvalidProvenance, match="32-byte SHA-256 digest"):
        Provenance(digest)


@pytest.mark.parametrize("quorum", [True, False, 0, -1, 1.0])
def test_provenance_refuses_invalid_signed_quorums(quorum: Any) -> None:
    with pytest.raises(InvalidProvenance, match="integer >= 1"):
        Provenance(b"d" * 32, quorum=quorum)


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        (None, "application/octet-stream"),
        (7, "application/octet-stream"),
        ("x", None),
        ("x", 7),
        ("x", ""),
    ],
)
def test_provenance_refuses_invalid_text_metadata(name: Any, media_type: Any) -> None:
    with pytest.raises(InvalidProvenance, match="name must be text"):
        Provenance(b"d" * 32, name=name, media_type=media_type)


def test_provenance_refuses_more_than_the_bounded_signatory_count() -> None:
    attestations = tuple(Attestation(str(index), b"s" * 64) for index in range(65))

    with pytest.raises(InvalidProvenance, match="at most 64 signatories"):
        Provenance(b"d" * 32, attestations=attestations)


def test_provenance_refuses_duplicate_signatory_ids() -> None:
    attestation = Attestation("duplicate", b"s" * 64)

    with pytest.raises(InvalidProvenance, match="may attest only once"):
        Provenance(b"d" * 32, attestations=(attestation, attestation))


def test_countersign_refuses_a_changed_artifact() -> None:
    provenance = Provenance.for_artifact(ARTIFACT)

    with pytest.raises(ArtifactChanged, match="changed after provenance was created"):
        provenance.countersign(ARTIFACT + b"!", SIGNER)


def test_countersign_refuses_a_duplicate_signatory() -> None:
    signed = Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, SIGNER)

    with pytest.raises(InvalidProvenance, match="already attested"):
        signed.countersign(ARTIFACT, SIGNER)


def test_countersign_refuses_a_verification_only_key() -> None:
    verifier = ProvenanceKey.verifier("signer", SIGNER.public)

    with pytest.raises(InvalidProvenance, match="verification-only"):
        Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, verifier)


def test_countersign_refuses_an_already_full_chain() -> None:
    attestations = tuple(Attestation(str(index), b"s" * 64) for index in range(64))
    provenance = Provenance.for_artifact(ARTIFACT)
    object.__setattr__(provenance, "attestations", attestations)
    key = ProvenanceKey("unused", SIGNER.public, lambda _statement: pytest.fail("signer called"))

    with pytest.raises(InvalidProvenance, match="at most 64 signatories"):
        provenance.countersign(ARTIFACT, key)


@pytest.mark.parametrize("signature", [b"s" * 63, b"s" * 65, bytearray(64), None])
def test_countersign_refuses_invalid_signer_results(signature: Any) -> None:
    key = ProvenanceKey("broken", SIGNER.public, lambda _statement: signature)

    with pytest.raises(InvalidProvenance, match="signatures must be 64 bytes"):
        Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, key)


def test_countersign_reports_a_non_bytes_signer_result_type() -> None:
    key = ProvenanceKey("broken", SIGNER.public, lambda _statement: bytearray(64))

    with pytest.raises(InvalidProvenance) as raised:
        Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, key)

    assert str(raised.value) == (
        "signer 'broken' returned bytearray; Ed25519 signatures must be 64 bytes"
    )


def test_countersign_checks_the_signature_against_the_declared_public_key() -> None:
    key = ProvenanceKey("mismatch", bytes(reversed(SIGNER.public)), SIGNER.sign)

    with pytest.raises(InvalidProvenance, match="public key does not verify"):
        Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, key)


def test_verify_names_an_unnamed_changed_artifact() -> None:
    provenance = Provenance.for_artifact(ARTIFACT)

    with pytest.raises(ArtifactChanged) as raised:
        provenance.verify(ARTIFACT + b"!", {})

    assert str(raised.value) == "artifact '<unnamed>' does not match its signed digest"


def test_changed_artifact_errors_preserve_a_declared_name() -> None:
    provenance = Provenance.for_artifact(ARTIFACT, name="release.whl")

    with pytest.raises(ArtifactChanged) as countersign_error:
        provenance.countersign(ARTIFACT + b"!", SIGNER)
    with pytest.raises(ArtifactChanged) as verify_error:
        provenance.verify(ARTIFACT + b"!", {})

    assert str(countersign_error.value) == (
        "artifact 'release.whl' changed after provenance was created"
    )
    assert str(verify_error.value) == ("artifact 'release.whl' does not match its signed digest")


def test_verify_refuses_a_boolean_quorum() -> None:
    signed = Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, SIGNER)

    with pytest.raises(InvalidProvenance, match="quorum must be an integer"):
        signed.verify(ARTIFACT, {"signer": SIGNER.public}, quorum=True)


def test_verify_refuses_a_missing_signatory_key() -> None:
    signed = Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, SIGNER)

    with pytest.raises(InvalidProvenance, match="no public key supplied"):
        signed.verify(ARTIFACT, {})


def test_verify_accepts_a_provenance_key_object() -> None:
    signed = Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, SIGNER)

    verified = signed.verify(ARTIFACT, {"signer": SIGNER})

    assert verified.signatories == ("signer",)


@pytest.mark.parametrize("public", [b"p" * 31, b"p" * 33, bytearray(32), "p" * 32])
def test_verify_refuses_invalid_public_key_shapes(public: Any) -> None:
    signed = Provenance.for_artifact(ARTIFACT).countersign(ARTIFACT, SIGNER)

    with pytest.raises(InvalidProvenance, match="must be 32 bytes"):
        signed.verify(ARTIFACT, {"signer": public})


def test_load_refuses_unknown_fields_even_with_the_current_version() -> None:
    sidecar = Provenance.for_artifact(ARTIFACT).dump()
    changed = sidecar[:-1] + b',"unknown":true}'

    with pytest.raises(InvalidProvenance, match="unknown provenance fields or version"):
        Provenance.load(changed)


def test_load_refuses_unknown_versions_even_with_the_exact_fields() -> None:
    sidecar = Provenance.for_artifact(ARTIFACT).dump().replace(b'"version":1', b'"version":2')

    with pytest.raises(InvalidProvenance, match="unknown provenance fields or version"):
        Provenance.load(sidecar)
