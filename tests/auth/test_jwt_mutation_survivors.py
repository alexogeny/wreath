from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any, cast

import pytest

import wreath._auth.jwt as jwt


def _segment(value: object) -> str:
    encoded = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


def _token(header: object = None, claims: object = None) -> str:
    header = {"alg": "HS256"} if header is None else header
    claims = {"sub": "person"} if claims is None else claims
    return f"{_segment(header)}.{_segment(claims)}.AA"


@pytest.mark.parametrize("algorithm", ["ES384", "ES512", "ES256K", "Ed448", "none"])
def test_every_deferred_algorithm_is_refused_as_known_but_unavailable(algorithm: str) -> None:
    with pytest.raises(jwt.UnsupportedAlgorithm, match="not supported in this build"):
        jwt.freeze_algorithms((algorithm,))


def test_the_deferred_algorithm_registry_names_every_known_unavailable_algorithm() -> None:
    assert jwt._DEFERRED == frozenset({"ES384", "ES512", "ES256K", "Ed448", "none"})


def test_peek_header_refuses_an_empty_segment_and_a_non_object() -> None:
    assert jwt.peek_header(".payload.signature") is None
    assert jwt.peek_header(f"{_segment(['HS256'])}.payload.signature") is None


class _FamilyImpostor:
    def __init__(self, family: str) -> None:
        self.family = family


@pytest.mark.parametrize("algorithm", ["HS256", "ES256", "EdDSA", "RS256"])
def test_signature_verification_refuses_a_matching_family_impostor(algorithm: str) -> None:
    family = jwt.FAMILY[algorithm]
    impostor = cast(jwt.JwtKey, _FamilyImpostor(family))
    assert not jwt._verify_signature(algorithm, impostor, b"message", b"signature")


def test_signature_verification_refuses_a_key_from_another_family() -> None:
    assert not jwt._verify_signature("HS256", jwt.RsaPublicKey(5, 3), b"message", b"signature")


@pytest.mark.parametrize("subject", [None, "", 42])
def test_default_identity_requires_a_non_empty_string_subject(subject: object) -> None:
    with pytest.raises(ValueError, match="string 'sub'"):
        jwt.default_identity({"sub": subject})


def test_default_identity_maps_each_role_source_and_string_roles() -> None:
    assert jwt.default_identity({"sub": "u", "roles": "admin editor"}).roles == frozenset(
        {"admin", "editor"}
    )
    assert jwt.default_identity({"sub": "u", "groups": ["staff"]}).roles == frozenset({"staff"})


def test_default_identity_maps_scope_to_permissions() -> None:
    identity = jwt.default_identity({"sub": "u", "scope": "read write"})
    assert identity.permissions == frozenset({"read", "write"})


def test_verify_jwt_refuses_a_non_string_algorithm_before_resolving_a_key() -> None:
    resolved = False

    def resolve(_header: Mapping[str, Any]) -> jwt.JwtKey | None:
        nonlocal resolved
        resolved = True
        return jwt.SymmetricKey(b"secret")

    assert (
        jwt.verify_jwt(
            _token({"alg": 256}),
            key_resolver=resolve,
            algorithms=cast(frozenset[str], frozenset({256})),
            issuer=None,
            audiences=(),
            leeway=0,
            required=(),
            identity=jwt.default_identity,
        )
        is None
    )
    assert not resolved


def test_verify_jwt_refuses_an_unhashable_algorithm_before_allowlist_membership() -> None:
    resolved = False

    def resolve(_header: Mapping[str, Any]) -> jwt.JwtKey | None:
        nonlocal resolved
        resolved = True
        return jwt.SymmetricKey(b"secret")

    assert (
        jwt.verify_jwt(
            _token({"alg": []}),
            key_resolver=resolve,
            algorithms=frozenset({"HS256"}),
            issuer=None,
            audiences=(),
            leeway=0,
            required=(),
            identity=jwt.default_identity,
        )
        is None
    )
    assert not resolved


def test_verify_jwt_refuses_an_allowlisted_algorithm_outside_the_supported_registry() -> None:
    resolved = False

    def resolve(_header: Mapping[str, Any]) -> jwt.JwtKey | None:
        nonlocal resolved
        resolved = True
        return jwt.SymmetricKey(b"secret")

    assert (
        jwt.verify_jwt(
            _token({"alg": "future"}),
            key_resolver=resolve,
            algorithms=frozenset({"future"}),
            issuer=None,
            audiences=(),
            leeway=0,
            required=(),
            identity=jwt.default_identity,
        )
        is None
    )
    assert not resolved


def test_verify_jwt_uses_the_explicit_validation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_now: list[int] = []
    monkeypatch.setattr(jwt, "_verify_signature", lambda *_args: True)

    def reason(
        _claims: object,
        *,
        now: int,
        leeway: int,
        issuer: str | None,
        audiences: tuple[str, ...],
        required: tuple[str, ...],
    ) -> int:
        observed_now.append(now)
        return 0

    monkeypatch.setattr(jwt, "_reason_valid", reason)
    identity = jwt.verify_jwt(
        _token(),
        key_resolver=lambda _header: jwt.SymmetricKey(b"secret"),
        algorithms=frozenset({"HS256"}),
        issuer=None,
        audiences=(),
        leeway=0,
        required=(),
        identity=jwt.default_identity,
        now=0,
    )

    assert identity is not None
    assert observed_now == [0]


def test_a_single_audience_is_one_entry_not_a_sequence_of_characters() -> None:
    assert jwt.freeze_audiences("service") == ("service",)
    assert jwt.freeze_audiences(["service", "admin"]) == ("service", "admin")


def test_key_coercion_distinguishes_bytes_strings_and_pem(monkeypatch: pytest.MonkeyPatch) -> None:
    assert jwt._coerce_key(bytearray(b"shared secret")) == jwt.SymmetricKey(b"shared secret")
    assert jwt._coerce_key("x" * jwt.MIN_HMAC_KEY_BYTES) == jwt.SymmetricKey(
        b"x" * jwt.MIN_HMAC_KEY_BYTES
    )

    sentinel = jwt.RsaPublicKey(5, 3)
    monkeypatch.setattr(jwt, "key_from_pem", lambda value: sentinel)
    assert jwt._coerce_key("  -----BEGIN PUBLIC KEY-----\nbody") is sentinel

    with pytest.raises(jwt.JwtError, match="unsupported key type.*int"):
        jwt._coerce_key(cast(jwt.JwtKey, 3))
