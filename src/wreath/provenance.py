"""Stored-artifact provenance: chained Ed25519 signatures and quorum checks.

HTTP Message Signatures authenticate an HTTP message.  This module owns the
different problem of proving that bytes stored today are the bytes a set of
people approved, even when they are checked much later and outside HTTP.

Hashing, base64url and Ed25519 run in Wreath's native/data kernels.  Python owns
only the immutable envelope and its canonical serialization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Self

from ._auth._ecverify import verify_ed25519
from ._b64 import b64url_decode, b64url_encode
from ._native import _core

__all__ = [
    "ArtifactChanged",
    "Attestation",
    "InvalidProvenance",
    "Provenance",
    "ProvenanceKey",
    "VerifiedProvenance",
]

_DOMAIN = b"WREATH-PROVENANCE-v1\x00"
_EMPTY_CHAIN = bytes(32)
_MAX_SIGNATORIES = 64


class InvalidProvenance(ValueError):
    """A provenance envelope, signature, key, or quorum is invalid."""


class ArtifactChanged(InvalidProvenance):
    """The supplied artifact no longer has the digest the signers approved."""


@dataclass(frozen=True, slots=True)
class ProvenanceKey:
    """One Ed25519 identity, optionally able to sign.

    `sign` is a callback so production keys can stay in an HSM or KMS.
    `from_seed` is the dependency-free local/test spelling and delegates all
    secret-scalar work to Wreath's native RFC 8032 kernel.
    """

    key_id: str
    public: bytes
    sign: Callable[[bytes], bytes] | None = None

    def __post_init__(self) -> None:
        if not self.key_id or not isinstance(self.key_id, str):
            raise InvalidProvenance("provenance key_id must be a non-empty string")
        if not isinstance(self.public, bytes) or len(self.public) != 32:
            raise InvalidProvenance(f"provenance public key {self.key_id!r} must be 32 bytes")
        if self.sign is not None and not callable(self.sign):
            raise InvalidProvenance("provenance sign must be callable or None")

    @classmethod
    def from_seed(cls, key_id: str, seed: bytes) -> Self:
        """Build a local signing identity from one 32-byte Ed25519 seed."""
        if not isinstance(seed, bytes) or len(seed) != 32:
            raise InvalidProvenance("an Ed25519 seed must be exactly 32 bytes")
        public = _core.curve_ed_public_key(seed)

        def sign(message: bytes, _seed: bytes = seed) -> bytes:
            return _core.curve_ed_sign(_seed, message)

        return cls(key_id, public, sign)

    @classmethod
    def verifier(cls, key_id: str, public: bytes) -> Self:
        """Build a verification-only identity from its public key."""
        return cls(key_id, public)


@dataclass(frozen=True, slots=True)
class Attestation:
    """One signatory's Ed25519 signature over the artifact and prior chain."""

    key_id: str
    signature: bytes

    def __post_init__(self) -> None:
        if not self.key_id or not isinstance(self.key_id, str):
            raise InvalidProvenance("attestation key_id must be a non-empty string")
        if not isinstance(self.signature, bytes) or len(self.signature) != 64:
            raise InvalidProvenance(
                f"attestation by {self.key_id!r} must carry a 64-byte Ed25519 signature"
            )


@dataclass(frozen=True, slots=True)
class VerifiedProvenance:
    """The verified signatory set and the quorum it satisfied."""

    digest: str
    signatories: tuple[str, ...]
    quorum: int


def _artifact_digest(artifact: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(artifact, bytes | bytearray | memoryview):
        raise TypeError(f"provenance artifact must be bytes-like, not {type(artifact).__name__}")
    return hashlib.sha256(artifact).digest()


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate provenance field {key!r}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class Provenance:
    """An immutable approval chain bound to one exact stored artifact."""

    digest: bytes
    quorum: int = 1
    name: str = ""
    media_type: str = "application/octet-stream"
    attestations: tuple[Attestation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.digest, bytes) or len(self.digest) != 32:
            raise InvalidProvenance("provenance digest must be a 32-byte SHA-256 digest")
        if isinstance(self.quorum, bool) or not isinstance(self.quorum, int) or self.quorum < 1:
            raise InvalidProvenance("provenance quorum must be an integer >= 1")
        if (
            not isinstance(self.name, str)
            or not isinstance(self.media_type, str)
            or not self.media_type
        ):
            raise InvalidProvenance("provenance name must be text and media_type non-empty text")
        ids = tuple(attestation.key_id for attestation in self.attestations)
        if len(ids) > _MAX_SIGNATORIES:
            raise InvalidProvenance(f"provenance supports at most {_MAX_SIGNATORIES} signatories")
        if len(ids) != len(set(ids)):
            raise InvalidProvenance("a provenance key may attest only once")

    @classmethod
    def for_artifact(
        cls,
        artifact: bytes | bytearray | memoryview,
        *,
        quorum: int = 1,
        name: str = "",
        media_type: str = "application/octet-stream",
    ) -> Self:
        """Create an unsigned envelope for these exact bytes."""
        return cls(_artifact_digest(artifact), quorum, name, media_type)

    def matches(self, artifact: bytes | bytearray | memoryview) -> bool:
        """Whether the artifact is byte-for-byte the version this chain names."""
        return hmac.compare_digest(self.digest, _artifact_digest(artifact))

    def _statement(self, chain: bytes) -> bytes:
        metadata = {
            "algorithm": "ed25519",
            "chain": b64url_encode(chain),
            "digest": b64url_encode(self.digest),
            "media_type": self.media_type,
            "name": self.name,
            "quorum": self.quorum,
            "version": 1,
        }
        return _DOMAIN + _json(metadata)

    @staticmethod
    def _extend_chain(chain: bytes, attestation: Attestation) -> bytes:
        key_id = attestation.key_id.encode("utf-8")
        return hashlib.sha256(
            chain + len(key_id).to_bytes(4, "big") + key_id + attestation.signature
        ).digest()

    def _chain(self) -> bytes:
        chain = _EMPTY_CHAIN
        for attestation in self.attestations:
            chain = self._extend_chain(chain, attestation)
        return chain

    def countersign(
        self,
        artifact: bytes | bytearray | memoryview,
        key: ProvenanceKey,
    ) -> Self:
        """Append `key` after verifying the artifact still matches.

        Each new signature covers the complete preceding chain, so it is a
        counter-signature as well as an independent approval of the bytes.
        """
        if not self.matches(artifact):
            raise ArtifactChanged(
                f"artifact {self.name or '<unnamed>'!r} changed after provenance was created"
            )
        if any(item.key_id == key.key_id for item in self.attestations):
            raise InvalidProvenance(f"key {key.key_id!r} has already attested")
        if key.sign is None:
            raise InvalidProvenance(f"key {key.key_id!r} is verification-only")
        if len(self.attestations) >= _MAX_SIGNATORIES:
            raise InvalidProvenance(f"provenance supports at most {_MAX_SIGNATORIES} signatories")
        statement = self._statement(self._chain())
        signature = key.sign(statement)
        if not isinstance(signature, bytes) or len(signature) != 64:
            received = len(signature) if isinstance(signature, bytes) else type(signature).__name__
            raise InvalidProvenance(
                f"signer {key.key_id!r} returned {received}; Ed25519 signatures must be 64 bytes"
            )
        if not verify_ed25519(key.public, statement, signature):
            raise InvalidProvenance(
                f"signer {key.key_id!r} returned a signature that its public key does not verify"
            )
        return type(self)(
            self.digest,
            self.quorum,
            self.name,
            self.media_type,
            (*self.attestations, Attestation(key.key_id, signature)),
        )

    def verify(
        self,
        artifact: bytes | bytearray | memoryview,
        keys: Mapping[str, ProvenanceKey | bytes],
        *,
        quorum: int | None = None,
    ) -> VerifiedProvenance:
        """Verify the bytes, every counter-signature, and the required quorum."""
        if not self.matches(artifact):
            raise ArtifactChanged(
                f"artifact {self.name or '<unnamed>'!r} does not match its signed digest"
            )
        required = self.quorum if quorum is None else quorum
        if isinstance(required, bool) or not isinstance(required, int) or required < self.quorum:
            raise InvalidProvenance(
                f"verification quorum must be an integer >= the signed quorum {self.quorum}"
            )
        verified: list[str] = []
        chain = _EMPTY_CHAIN
        for attestation in self.attestations:
            key = keys.get(attestation.key_id)
            if key is None:
                raise InvalidProvenance(
                    f"no public key supplied for signatory {attestation.key_id!r}"
                )
            public = key.public if isinstance(key, ProvenanceKey) else key
            if not isinstance(public, bytes) or len(public) != 32:
                raise InvalidProvenance(f"public key for {attestation.key_id!r} must be 32 bytes")
            if not verify_ed25519(public, self._statement(chain), attestation.signature):
                raise InvalidProvenance(
                    f"provenance signature by {attestation.key_id!r} does not verify"
                )
            verified.append(attestation.key_id)
            chain = self._extend_chain(chain, attestation)
        if len(verified) < required:
            raise InvalidProvenance(
                f"provenance quorum is {required}, but only {len(verified)} "
                "distinct signature(s) verify"
            )
        return VerifiedProvenance(self.digest.hex(), tuple(verified), required)

    def dump(self) -> bytes:
        """Serialize the sidecar deterministically as UTF-8 JSON."""
        return _json(
            {
                "attestations": [
                    {"key_id": item.key_id, "signature": b64url_encode(item.signature)}
                    for item in self.attestations
                ],
                "digest": b64url_encode(self.digest),
                "media_type": self.media_type,
                "name": self.name,
                "quorum": self.quorum,
                "version": 1,
            }
        )

    @classmethod
    def load(cls, data: bytes | bytearray | memoryview) -> Self:
        """Parse a serialized sidecar, refusing unknown or malformed shapes."""
        try:
            document = json.loads(bytes(data), object_pairs_hook=_unique_object)
            if (
                set(document)
                != {"attestations", "digest", "media_type", "name", "quorum", "version"}
                or document["version"] != 1
            ):
                raise ValueError("unknown provenance fields or version")
            raw_attestations = document["attestations"]
            if not isinstance(raw_attestations, list):
                raise ValueError("provenance attestations must be an array")
            if len(raw_attestations) > _MAX_SIGNATORIES:
                raise ValueError(
                    f"provenance supports at most {_MAX_SIGNATORIES} signatories"
                )
            if any(
                not isinstance(item, dict) or set(item) != {"key_id", "signature"}
                for item in raw_attestations
            ):
                raise ValueError("unknown attestation fields")
            attestations = tuple(
                Attestation(item["key_id"], b64url_decode(item["signature"]))
                for item in raw_attestations
            )
            return cls(
                b64url_decode(document["digest"]),
                document["quorum"],
                document["name"],
                document["media_type"],
                attestations,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise InvalidProvenance(f"invalid provenance sidecar: {error}") from error
