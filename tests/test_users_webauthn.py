from __future__ import annotations

import hashlib
import json
import struct
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from wreath import Wreath
from wreath._secondfactor import (
    MAX_USER_HANDLE_BYTES,
    InMemorySecondFactorStore,
    MemoryChallengeStore,
    SecondFactor,
    _descriptors,
    _webauthn_credential,
    begin_webauthn_assertion,
    begin_webauthn_registration,
    confirm_webauthn_registration,
    verify_webauthn_assertion,
)
from wreath._webauthn import (
    WebAuthnError,
    b64url_decode,
    b64url_encode,
    cbor_decode,
    check_client_data,
    default_origins,
    der_signature_to_raw,
    pack_credential,
    parse_attestation_object,
    parse_authenticator_data,
    parse_cose_key,
    unpack_credential,
)
from wreath.policy import HttpPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.testing import TestClient
from wreath.users import (
    InMemoryUserStore,
    hash_password,
    second_factor_router,
    user_router,
)


class _Revocations:
    async def delete_for(self, _subject: str) -> int:
        return 0


_REVOCATIONS = _Revocations()

RP_ID = "example.test"
ORIGIN = "https://example.test"
PASSWORD = "correct horse battery staple"

#: One scrypt, not one per seeded user. `hash_password` is deliberately slow --
#: 60 ms measured, and it is the same `PASSWORD` every time -- so re-deriving it
#: per test spent 1.3s of this file on a value that never varies. What the tests
#: exercise is `verify_password` on the login path, which still runs for real
#: against this hash; only the setup is memoised.
#:
#: Sharing one salt across seeded users is safe *here* because nothing in this
#: file reads a stored hash. A test that asserted two users hash differently
#: would have to call `hash_password` itself, which is what it is asserting about.
PASSWORD_HASH = hash_password(PASSWORD)


def _cbor(value: Any) -> bytes:
    """Just enough canonical CBOR to build what an authenticator emits."""
    if isinstance(value, bool):
        raise TypeError("no booleans are needed here")
    if isinstance(value, int):
        major, argument = (0, value) if value >= 0 else (1, -1 - value)
        return _cbor_head(major, argument)
    if isinstance(value, bytes):
        return _cbor_head(2, len(value)) + value
    if isinstance(value, str):
        packed = value.encode("utf-8")
        return _cbor_head(3, len(packed)) + packed
    if isinstance(value, dict):
        out = _cbor_head(5, len(value))
        for key, item in value.items():
            out += _cbor(key) + _cbor(item)
        return out
    raise TypeError(f"unsupported: {value!r}")


def _cbor_head(major: int, argument: int) -> bytes:
    if argument < 24:
        return bytes([major << 5 | argument])
    if argument < 256:
        return bytes([major << 5 | 24, argument])
    if argument < 65536:
        return bytes([major << 5 | 25]) + argument.to_bytes(2, "big")
    return bytes([major << 5 | 26]) + argument.to_bytes(4, "big")


class _Authenticator:
    """A key pair plus the framing a real authenticator would put around it."""

    def __init__(self, *, algorithm: str = "es256", credential_id: bytes = b"cred-id-0") -> None:
        self.algorithm = algorithm
        self.credential_id = credential_id
        self.aaguid = b"\x00" * 16
        if algorithm == "es256":
            self._key: Any = ec.generate_private_key(ec.SECP256R1())
            numbers = self._key.public_key().public_numbers()
            self.cose = _cbor(
                {
                    1: 2,  # kty: EC2
                    3: -7,  # alg: ES256
                    -1: 1,  # crv: P-256
                    -2: numbers.x.to_bytes(32, "big"),
                    -3: numbers.y.to_bytes(32, "big"),
                }
            )
        else:
            self._key = ed25519.Ed25519PrivateKey.generate()
            self.cose = _cbor(
                {
                    1: 1,  # kty: OKP
                    3: -8,  # alg: EdDSA
                    -1: 6,  # crv: Ed25519
                    -2: self._key.public_key().public_bytes_raw(),
                }
            )

    def _sign(self, message: bytes) -> bytes:
        if self.algorithm == "es256":
            return self._key.sign(message, ec.ECDSA(hashes.SHA256()))
        return self._key.sign(message)

    def client_data(
        self,
        *,
        ceremony: str,
        challenge: bytes,
        origin: str = ORIGIN,
        cross_origin: bool = False,
    ) -> bytes:
        return json.dumps(
            {
                "type": "webauthn.create" if ceremony == "register" else "webauthn.get",
                "challenge": b64url_encode(challenge),
                "origin": origin,
                "crossOrigin": cross_origin,
            }
        ).encode("utf-8")

    def auth_data(
        self,
        *,
        rp_id: str = RP_ID,
        sign_count: int = 0,
        user_present: bool = True,
        user_verified: bool = True,
        attested: bool = False,
    ) -> bytes:
        flags = 0
        if user_present:
            flags |= 0x01
        if user_verified:
            flags |= 0x04
        if attested:
            flags |= 0x40
        out = hashlib.sha256(rp_id.encode("utf-8")).digest()
        out += bytes([flags]) + struct.pack(">I", sign_count)
        if attested:
            out += self.aaguid + struct.pack(">H", len(self.credential_id))
            out += self.credential_id + self.cose
        return out

    def _framed(self, ceremony: str, challenge: bytes, options: dict[str, Any]) -> bytes:
        return self.client_data(
            ceremony=options.pop("ceremony_type", ceremony),
            challenge=challenge,
            origin=options.pop("origin", ORIGIN),
            cross_origin=options.pop("cross_origin", False),
        )

    def register(self, challenge: bytes, *, fmt: str = "none", **options: Any) -> dict[str, bytes]:
        client_data = self._framed("register", challenge, options)
        auth_data = self.auth_data(attested=True, **options)
        attestation = _cbor({"fmt": fmt, "attStmt": {}, "authData": auth_data})
        return {"client_data": client_data, "attestation_object": attestation}

    def assertion(self, challenge: bytes, **options: Any) -> dict[str, bytes]:
        client_data = self._framed("assert", challenge, options)
        auth_data = self.auth_data(**options)
        signature = self._sign(auth_data + hashlib.sha256(client_data).digest())
        return {
            "client_data": client_data,
            "authenticator_data": auth_data,
            "signature": signature,
        }


async def _enrol(store: Any, device: _Authenticator, user_id: str = "user-1") -> Any:
    begun = begin_webauthn_registration(user_id=user_id, account="ann@example.test", rp_id=RP_ID)
    minted = device.register(begun.challenge)
    return await confirm_webauthn_registration(
        store,
        user_id,
        challenge=begun.challenge,
        rp_id=RP_ID,
        origins=(ORIGIN,),
        **minted,
    )


@pytest.mark.parametrize("algorithm", ["es256", "ed25519"])
async def test_a_registration_and_assertion_round_trip(algorithm: str) -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator(algorithm=algorithm)
    credential, _ = await _enrol(store, device)
    assert credential.kind == "webauthn"

    begun = begin_webauthn_assertion([credential], rp_id=RP_ID)
    minted = device.assertion(begun.challenge, sign_count=1)
    result = await verify_webauthn_assertion(
        store,
        "user-1",
        challenge=begun.challenge,
        credential_id=device.credential_id,
        rp_id=RP_ID,
        origins=(ORIGIN,),
        **minted,
    )
    assert result.credential.id == credential.id
    assert result.counter == 1
    assert result.user_verified is True


async def test_an_rs256_authenticator_is_refused_by_name() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    device.cose = _cbor({1: 3, 3: -257, -1: b"\x00" * 256, -2: b"\x01\x00\x01"})
    with pytest.raises(WebAuthnError, match="-257"):
        await _enrol(store, device)


async def _assert(
    store: Any,
    device: _Authenticator,
    credential: Any,
    *,
    user_id: str = "user-1",
    challenge: bytes | None = None,
    rp_id: str = RP_ID,
    origins: tuple[str, ...] = (ORIGIN,),
    credential_id: bytes | None = None,
    require_user_verification: bool = False,
    rp_id_signed: str = RP_ID,
    at: float | None = None,
    **options: Any,
) -> Any:
    begun = begin_webauthn_assertion([credential], rp_id=RP_ID)
    presented = begun.challenge if challenge is None else challenge
    minted = device.assertion(presented, rp_id=rp_id_signed, **options)
    return await verify_webauthn_assertion(
        store,
        user_id,
        challenge=begun.challenge,
        credential_id=device.credential_id if credential_id is None else credential_id,
        rp_id=rp_id,
        origins=origins,
        require_user_verification=require_user_verification,
        at=at,
        **minted,
    )


async def test_a_registration_will_not_answer_an_assertion_type() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    begun = begin_webauthn_registration(user_id="user-1", account="ann@example.test", rp_id=RP_ID)
    minted = device.register(begun.challenge)
    minted["client_data"] = device.client_data(ceremony="assert", challenge=begun.challenge)
    with pytest.raises(WebAuthnError, match="client data type"):
        await confirm_webauthn_registration(
            store,
            "user-1",
            challenge=begun.challenge,
            rp_id=RP_ID,
            origins=(ORIGIN,),
            **minted,
        )
    assert await store.credentials("user-1") == []


async def test_an_assertion_will_not_answer_a_registration_type() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="client data type"):
        await _assert(store, device, credential, ceremony_type="register")


async def test_an_assertion_answering_a_different_challenge_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="different challenge"):
        await _assert(store, device, credential, challenge=b"n" * 32)


async def test_an_assertion_collected_at_another_origin_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="origin"):
        await _assert(store, device, credential, origin="https://phish.test")


async def test_an_assertion_signed_for_another_rp_id_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="different RP ID"):
        await _assert(store, device, credential, rp_id_signed="evil.test")


async def test_a_registration_signed_for_another_rp_id_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    begun = begin_webauthn_registration(user_id="user-1", account="ann@example.test", rp_id=RP_ID)
    minted = device.register(begun.challenge, rp_id="evil.test")
    with pytest.raises(WebAuthnError, match="different RP ID"):
        await confirm_webauthn_registration(
            store,
            "user-1",
            challenge=begun.challenge,
            rp_id=RP_ID,
            origins=(ORIGIN,),
            **minted,
        )
    assert await store.credentials("user-1") == []


async def test_a_cross_origin_ceremony_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="cross-origin"):
        await _assert(store, device, credential, cross_origin=True)


async def test_an_assertion_with_no_user_presence_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="user presence"):
        await _assert(store, device, credential, user_present=False)


async def test_a_registration_with_no_user_presence_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    begun = begin_webauthn_registration(user_id="user-1", account="ann@example.test", rp_id=RP_ID)
    minted = device.register(begun.challenge, user_present=False)
    with pytest.raises(WebAuthnError, match="user presence"):
        await confirm_webauthn_registration(
            store,
            "user-1",
            challenge=begun.challenge,
            rp_id=RP_ID,
            origins=(ORIGIN,),
            **minted,
        )
    assert await store.credentials("user-1") == []


async def test_an_attestation_that_is_not_none_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    begun = begin_webauthn_registration(user_id="user-1", account="ann@example.test", rp_id=RP_ID)
    minted = device.register(begun.challenge, fmt="packed")
    with pytest.raises(WebAuthnError, match="packed"):
        await confirm_webauthn_registration(
            store,
            "user-1",
            challenge=begun.challenge,
            rp_id=RP_ID,
            origins=(ORIGIN,),
            **minted,
        )


async def test_a_tampered_signature_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    begun = begin_webauthn_assertion([credential], rp_id=RP_ID)
    minted = device.assertion(begun.challenge, sign_count=1)
    minted["signature"] = minted["signature"][:-1] + bytes([minted["signature"][-1] ^ 1])
    with pytest.raises(WebAuthnError):
        await verify_webauthn_assertion(
            store,
            "user-1",
            challenge=begun.challenge,
            credential_id=device.credential_id,
            rp_id=RP_ID,
            origins=(ORIGIN,),
            **minted,
        )


async def test_authenticator_data_is_covered_by_the_signature() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    begun = begin_webauthn_assertion([credential], rp_id=RP_ID)
    minted = device.assertion(begun.challenge, sign_count=1)
    minted["authenticator_data"] = device.auth_data(sign_count=99)
    with pytest.raises(WebAuthnError, match="signature did not verify"):
        await verify_webauthn_assertion(
            store,
            "user-1",
            challenge=begun.challenge,
            credential_id=device.credential_id,
            rp_id=RP_ID,
            origins=(ORIGIN,),
            **minted,
        )


async def test_another_users_credential_never_answers_for_this_one() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device, user_id="user-2")
    with pytest.raises(WebAuthnError, match="no such credential"):
        await _assert(store, device, credential, user_id="user-1")


async def test_a_counter_that_goes_backwards_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    assert (await _assert(store, device, credential, sign_count=7)).counter == 7
    with pytest.raises(WebAuthnError, match="clone"):
        await _assert(store, device, credential, sign_count=6)


async def test_a_counter_that_stands_still_above_zero_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    await _assert(store, device, credential, sign_count=7)
    with pytest.raises(WebAuthnError, match="clone"):
        await _assert(store, device, credential, sign_count=7)


async def test_a_counter_dropping_to_zero_from_above_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    await _assert(store, device, credential, sign_count=7)
    with pytest.raises(WebAuthnError, match="clone"):
        await _assert(store, device, credential, sign_count=0)


async def test_an_authenticator_that_always_reports_zero_keeps_working() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    for _ in range(3):
        result = await _assert(store, device, credential, sign_count=0)
        assert result.counter == 0


class _Overtaken:
    """A store whose counter moves between the read and the write.

    That is what a concurrent assertion looks like from inside one request: the
    credential read at the top carries the old count, and by the time the
    signature has been checked another request has already recorded the same one
    or a newer one. The store's conditional advance is the only thing that can
    see it, so this stands in for the second request rather than running one.
    """

    def __init__(self, inner: Any, overtake_to: int) -> None:
        self._inner = inner
        self._overtake_to = overtake_to
        self.touches: list[int] = []

    async def credentials(self, user_id: str) -> Any:
        rows = await self._inner.credentials(user_id)
        for row in rows:
            if row.kind == "webauthn":
                await self._inner.touch(row.id, counter=self._overtake_to, at=datetime.now(UTC))
        return rows

    async def add(self, credential: Any) -> Any:
        return await self._inner.add(credential)

    async def remove(self, user_id: str, credential_id: str) -> None:
        await self._inner.remove(user_id, credential_id)

    async def touch(self, credential_id: str, *, counter: int, at: Any) -> bool:
        self.touches.append(counter)
        return await self._inner.touch(credential_id, counter=counter, at=at)


async def test_an_assertion_that_loses_the_counter_advance_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    overtaken = _Overtaken(store, overtake_to=9)
    with pytest.raises(WebAuthnError, match="clone"):
        await _assert(overtaken, device, credential, sign_count=5)
    assert overtaken.touches == [5]
    assert (await store.credentials("user-1"))[0].counter == 9


async def test_a_zero_counter_that_cannot_advance_is_not_a_refusal() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    result = await _assert(store, device, credential, sign_count=0)
    assert result.counter == 0
    assert (await store.credentials("user-1"))[0].counter == 0


def test_both_ceremonies_ask_for_user_verification() -> None:
    registration = begin_webauthn_registration(
        user_id="user-1", account="ann@example.test", rp_id=RP_ID
    )
    assert registration.options["authenticatorSelection"]["userVerification"] == "preferred"
    assertion = begin_webauthn_assertion([], rp_id=RP_ID)
    assert assertion.options["userVerification"] == "preferred"


async def test_the_user_verification_outcome_is_recorded() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    verified = await _assert(store, device, credential, sign_count=1, user_verified=True)
    assert verified.user_verified is True
    touched = await _assert(store, device, credential, sign_count=2, user_verified=False)
    assert touched.user_verified is False


async def test_user_verification_can_be_required() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="requires user verification"):
        await _assert(
            store,
            device,
            credential,
            user_verified=False,
            require_user_verification=True,
        )


async def test_user_verification_can_be_required_at_registration() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    begun = begin_webauthn_registration(user_id="user-1", account="ann@example.test", rp_id=RP_ID)
    minted = device.register(begun.challenge, user_verified=False)

    with pytest.raises(WebAuthnError, match="registration requires user verification"):
        await confirm_webauthn_registration(
            store,
            "user-1",
            challenge=begun.challenge,
            rp_id=RP_ID,
            origins=(ORIGIN,),
            require_user_verification=True,
            **minted,
        )
    assert await store.credentials("user-1") == []


async def test_registering_the_same_credential_twice_is_refused() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    await _enrol(store, device)
    with pytest.raises(WebAuthnError, match="already registered"):
        await _enrol(store, device)


async def test_a_non_webauthn_factor_cannot_claim_a_credential_id() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    await store.add(
        SecondFactor(
            id="totp-1",
            user_id="user-1",
            kind="totp",
            label="Phone",
            created_at=datetime.now(UTC),
            last_used_at=None,
            material=pack_credential(device.credential_id, device.cose, user_verified=True),
        )
    )

    credential, _ = await _enrol(store, device)

    assert credential.kind == "webauthn"


async def test_registration_records_the_supplied_time() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    moment = 1_700_000_000.0
    begun = begin_webauthn_registration(user_id="user-1", account="ann@example.test", rp_id=RP_ID)

    credential, _ = await confirm_webauthn_registration(
        store,
        "user-1",
        challenge=begun.challenge,
        rp_id=RP_ID,
        origins=(ORIGIN,),
        at=moment,
        **device.register(begun.challenge),
    )

    expected = datetime.fromtimestamp(moment, UTC)
    assert credential.created_at == expected
    assert credential.last_used_at == expected


async def test_assertion_records_the_supplied_time() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    credential, _ = await _enrol(store, device)
    moment = 1_700_000_000.0

    result = await _assert(store, device, credential, sign_count=1, at=moment)

    assert result.credential.last_used_at == datetime.fromtimestamp(moment, UTC)


async def test_a_first_security_key_issues_recovery_codes() -> None:
    store = InMemorySecondFactorStore()
    _, codes = await _enrol(store, _Authenticator())
    assert len(codes) == 10
    rows = await store.credentials("user-1")
    assert sum(1 for row in rows if row.kind == "recovery") == 10


async def test_a_second_key_does_not_mint_a_second_set_of_codes() -> None:
    store = InMemorySecondFactorStore()
    await _enrol(store, _Authenticator())
    _, codes = await _enrol(store, _Authenticator(credential_id=b"cred-id-1"))
    assert codes == []
    rows = await store.credentials("user-1")
    assert sum(1 for row in rows if row.kind == "recovery") == 10
    assert sum(1 for row in rows if row.kind == "webauthn") == 2


def test_the_cbor_decoder_reads_what_an_authenticator_writes() -> None:
    value = {"fmt": "none", "attStmt": {}, "n": -3, "big": 70000, "raw": b"\x01\x02"}
    assert cbor_decode(_cbor(value)) == value


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("indefinite-length array", b"\x9f\x01\xff"),
        ("indefinite-length map", b"\xbf\x01\x01\xff"),
        ("a tag", b"\xc0\x01"),
        ("a float", b"\xfb\x00\x00\x00\x00\x00\x00\x00\x00"),
        ("a reserved header", b"\x1c"),
        ("a truncated length", b"\x1a\x00\x00"),
        ("a length past the end", b"\x58\x20\x01"),
        ("an array count past the end", b"\x98\x40\x01"),
        ("trailing bytes", b"\x01\x01"),
        ("a duplicate key", b"\xa2\x01\x01\x01\x02"),
        ("a byte-string key", b"\xa1\x41\x61\x01"),
    ],
)
def test_the_cbor_decoder_refuses_what_ctap2_never_emits(name: str, encoded: bytes) -> None:
    with pytest.raises(WebAuthnError):
        cbor_decode(encoded)


def test_the_cbor_decoder_bounds_its_own_recursion() -> None:
    deep = b"\x81" * 64 + b"\x01"
    with pytest.raises(WebAuthnError, match="too deep"):
        cbor_decode(deep)


def test_the_der_signature_parser_demands_minimal_integers() -> None:
    minimal = bytes([0x30, 0x06, 0x02, 0x01, 0x7F, 0x02, 0x01, 0x01])
    assert der_signature_to_raw(minimal) == (127).to_bytes(32, "big") + (1).to_bytes(32, "big")
    padded = bytes([0x30, 0x07, 0x02, 0x02, 0x00, 0x7F, 0x02, 0x01, 0x01])
    with pytest.raises(WebAuthnError, match="non-minimal"):
        der_signature_to_raw(padded)


@pytest.mark.parametrize(
    "encoded",
    [
        bytes([0x31, 0x06, 0x02, 0x01, 0x7F, 0x02, 0x01, 0x01]),  # not a SEQUENCE
        bytes([0x30, 0x06, 0x02, 0x01, 0xFF, 0x02, 0x01, 0x01]),  # negative r
        bytes([0x30, 0x07, 0x02, 0x01, 0x7F, 0x02, 0x01, 0x01, 0x00]),  # trailing byte
        bytes([0x30, 0x06, 0x02, 0x01, 0x00, 0x02, 0x01, 0x01]),  # r == 0
        bytes([0x30, 0x81, 0x06, 0x02, 0x01, 0x7F, 0x02, 0x01, 0x01]),  # long form
    ],
)
def test_the_der_signature_parser_refuses_malformed_signatures(encoded: bytes) -> None:
    with pytest.raises(WebAuthnError):
        der_signature_to_raw(encoded)


def test_a_public_key_off_the_curve_is_refused() -> None:
    off = _cbor({1: 2, 3: -7, -1: 1, -2: b"\x01" * 32, -3: b"\x02" * 32})
    with pytest.raises(WebAuthnError, match="not a point on P-256"):
        parse_cose_key(off)


def test_a_cose_key_must_match_its_own_algorithm() -> None:
    mismatched = _cbor({1: 1, 3: -7, -1: 6, -2: b"\x01" * 32})
    with pytest.raises(WebAuthnError, match="EC2 over P-256"):
        parse_cose_key(mismatched)


def test_authenticator_data_shorter_than_its_header_is_refused() -> None:
    with pytest.raises(WebAuthnError, match="too short"):
        parse_authenticator_data(b"\x00" * 36)


def test_authenticator_data_with_bytes_left_over_is_refused() -> None:
    device = _Authenticator()
    with pytest.raises(WebAuthnError, match="trailing bytes"):
        parse_authenticator_data(device.auth_data() + b"\x00")


def test_a_credential_id_longer_than_the_buffer_is_refused() -> None:
    device = _Authenticator()
    data = bytearray(device.auth_data(attested=True))
    data[53:55] = struct.pack(">H", 1000)
    with pytest.raises(WebAuthnError):
        parse_authenticator_data(bytes(data))


def test_stored_material_round_trips_and_refuses_a_truncated_row() -> None:
    packed = pack_credential(b"cred", b"key-bytes", user_verified=True)
    restored = unpack_credential(packed)
    assert restored.credential_id == b"cred"
    assert restored.public_key == b"key-bytes"
    assert restored.user_verified is True
    with pytest.raises(WebAuthnError, match="truncated"):
        unpack_credential(packed[:-1])
    with pytest.raises(WebAuthnError, match="not a webauthn credential"):
        unpack_credential(b"totp" + packed[4:])


def test_packing_refuses_material_the_header_cannot_describe() -> None:
    with pytest.raises(WebAuthnError, match="credential id must be 1..65535"):
        pack_credential(b"", b"key-bytes", user_verified=True)
    with pytest.raises(WebAuthnError, match="credential id must be 1..65535"):
        pack_credential(b"c" * 0x10000, b"key-bytes", user_verified=True)
    with pytest.raises(WebAuthnError, match="public key must be 1..65535"):
        pack_credential(b"cred", b"", user_verified=True)
    with pytest.raises(WebAuthnError, match="public key must be 1..65535"):
        pack_credential(b"cred", b"k" * 0x10000, user_verified=True)
    # The largest material the header *can* describe still round-trips, so the
    # bound is where it says it is rather than one byte either side.
    largest = pack_credential(b"c" * 0xFFFF, b"k" * 0xFFFF, user_verified=False)
    restored = unpack_credential(largest)
    assert restored.credential_id == b"c" * 0xFFFF
    assert restored.public_key == b"k" * 0xFFFF
    assert restored.user_verified is False


def test_a_begun_ceremony_keeps_its_challenge_out_of_its_repr() -> None:
    begun = begin_webauthn_registration(user_id="user-1", account="ann@example.test", rp_id=RP_ID)
    text = repr(begun)
    assert b64url_encode(begun.challenge) not in text
    assert "example.test" not in text


async def test_a_webauthn_credentials_repr_carries_no_key_material() -> None:
    store = InMemorySecondFactorStore()
    credential, _ = await _enrol(store, _Authenticator())
    assert "cred-id-0" not in repr(credential)
    assert credential.id in repr(credential)


def test_the_webauthn_wire_module_contains_no_assert_statements() -> None:
    import ast

    import wreath._webauthn as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert [node for node in ast.walk(tree) if isinstance(node, ast.Assert)] == []


# Everything above this line is signed by `_Authenticator`, which proves wreath
# agrees with `cryptography` about the wire formats and proves nothing at all
# about interoperability: the same module wrote the bytes and read them back.
# These two are not ours. They are transcribed, byte for byte, from the
# **W3C Web Authentication Level 3** specification's own test vectors:
#   Web Authentication: An API for accessing Public Key Credentials -- Level 3
#   W3C Candidate Recommendation Snapshot, 26 May 2026
#   Section 16.2, "ES256 Credential with No Attestation"
#   https://www.w3.org/TR/2026/CR-webauthn-3-20260526/#sctn-test-vectors-none-es256
# Section 16 states the use these are for in as many words: "Relying Party
# implementers may check that they can successfully validate the registration
# outputs given the same challenge input, and that they can successfully
# validate the authentication outputs given the same challenge input and the
# credential public key and credential ID from the associated registration
# example." The registration and the assertion below are the same credential,
# which is why the second can be verified against a key the first stored.
# The specification prints them as hex, so they are hex here: a transcription
# anyone can diff against the published document without re-encoding anything
# first. Section 16.1 fixes the RP ID as `example.org` and the origin as
# `https://example.org` for every vector in the section.
# Two properties of *this* vector are worth knowing before reading the
# assertions below, because both would otherwise look like defects:
# * The registration's clientDataJSON carries an `extraData` member the
#   specification put there deliberately, to check that a relying party does not
#   choke on a field it has never heard of. Wreath reads the three members it
#   cares about and ignores the rest, which is what makes this pass.
# * The signature counter is zero in both ceremonies, and user verification is
#   *not* set. Zero on both sides is "the authenticator does not implement a
#   counter", never a regression -- so this vector exercises that branch too.

_W3C_RP_ID = "example.org"
_W3C_ORIGIN = "https://example.org"

_W3C_REG_CHALLENGE = bytes.fromhex(
    "00c30fb78531c464d2b6771dab8d7b603c01162f2fa486bea70f283ae556e130"
)
_W3C_REG_CLIENT_DATA = bytes.fromhex(
    "7b2274797065223a22776562617574686e2e637265617465222c226368616c6c656e6765"
    "223a22414d4d507434557878475453746e63647134313759447742466938767049612d70"
    "77386f4f755657345441222c226f726967696e223a2268747470733a2f2f6578616d706c"
    "652e6f7267222c2263726f73734f726967696e223a66616c73652c226578747261446174"
    "61223a22636c69656e74446174614a534f4e206d617920626520657874656e6465642077"
    "697468206164646974696f6e616c206669656c647320696e20746865206675747572652c"
    "207375636820617320746869733a20426b5165446a646354427258426941774a544c4535"
    "51227d"
)
_W3C_REG_ATTESTATION = bytes.fromhex(
    "a363666d74646e6f6e656761747453746d74a068617574684461746158a4bfabc3743295"
    "8b063360d3ad6461c9c4735ae7f8edd46592a5e0f01452b2e4b559000000008446ccb9ab"
    "1db374750b2367ff6f3a1f0020f91f391db4c9b2fde0ea70189cba3fb63f579ba6122b33"
    "ad94ff3ec330084be4a5010203262001215820afefa16f97ca9b2d23eb86ccb64098d20d"
    "b90856062eb249c33a9b672f26df61225820930a56b87a2fca66334b03458abf879717c1"
    "2cc68ed73290af2e2664796b9220"
)
#: The specification publishes these alongside the blobs above, so they are an
#: independent statement of what the attestation object should decode to rather
#: than a copy of what wreath decoded it to.
_W3C_AAGUID = bytes.fromhex("8446ccb9ab1db374750b2367ff6f3a1f")
_W3C_CREDENTIAL_ID = bytes.fromhex(
    "f91f391db4c9b2fde0ea70189cba3fb63f579ba6122b33ad94ff3ec330084be4"
)

_W3C_ASSERT_CHALLENGE = bytes.fromhex(
    "39c0e7521417ba54d43e8dc95174f423dee9bf3cd804ff6d65c857c9abf4d408"
)
_W3C_ASSERT_AUTH_DATA = bytes.fromhex(
    "bfabc37432958b063360d3ad6461c9c4735ae7f8edd46592a5e0f01452b2e4b51900000000"
)
_W3C_ASSERT_CLIENT_DATA = bytes.fromhex(
    "7b2274797065223a22776562617574686e2e676574222c226368616c6c656e6765223a22"
    "4f63446e55685158756c5455506f334a5558543049393770767a7a59425039745a636858"
    "79617630314167222c226f726967696e223a2268747470733a2f2f6578616d706c652e6f"
    "7267222c2263726f73734f726967696e223a66616c73657d"
)
_W3C_ASSERT_SIGNATURE = bytes.fromhex(
    "3046022100f50a4e2e4409249c4a853ba361282f09841df4dd4547a13a87780218deffcd"
    "380221008480ac0f0b93538174f575bf11a1dd5d78c6e486013f937295ea13653e331e87"
)


async def _w3c_registered(store: Any) -> Any:
    return await confirm_webauthn_registration(
        store,
        "user-1",
        challenge=_W3C_REG_CHALLENGE,
        client_data=_W3C_REG_CLIENT_DATA,
        attestation_object=_W3C_REG_ATTESTATION,
        rp_id=_W3C_RP_ID,
        origins=(_W3C_ORIGIN,),
    )


async def test_the_w3c_recorded_registration_verifies() -> None:
    parsed = parse_attestation_object(_W3C_REG_ATTESTATION)
    assert parsed.aaguid == _W3C_AAGUID
    assert parsed.credential_id == _W3C_CREDENTIAL_ID
    assert parsed.sign_count == 0
    assert parsed.user_present is True
    assert parsed.user_verified is False
    assert parse_cose_key(parsed.public_key).algorithm == -7

    store = InMemorySecondFactorStore()
    credential, codes = await _w3c_registered(store)
    assert unpack_credential(credential.material).credential_id == _W3C_CREDENTIAL_ID
    assert len(codes) == 10


async def test_the_w3c_recorded_assertion_verifies() -> None:
    store = InMemorySecondFactorStore()
    credential, _ = await _w3c_registered(store)
    result = await verify_webauthn_assertion(
        store,
        "user-1",
        challenge=_W3C_ASSERT_CHALLENGE,
        credential_id=_W3C_CREDENTIAL_ID,
        client_data=_W3C_ASSERT_CLIENT_DATA,
        authenticator_data=_W3C_ASSERT_AUTH_DATA,
        signature=_W3C_ASSERT_SIGNATURE,
        rp_id=_W3C_RP_ID,
        origins=(_W3C_ORIGIN,),
    )
    assert result.credential.id == credential.id
    # Zero on both sides: the vector's authenticator does not report a counter,
    # and that must read as "not reported" rather than as a clone.
    assert result.counter == 0
    assert result.user_verified is False


async def test_the_w3c_recorded_assertion_is_bound_to_its_own_challenge() -> None:
    store = InMemorySecondFactorStore()
    await _w3c_registered(store)
    with pytest.raises(WebAuthnError, match="different challenge"):
        await verify_webauthn_assertion(
            store,
            "user-1",
            challenge=_W3C_REG_CHALLENGE,  # the registration's, not this one's
            credential_id=_W3C_CREDENTIAL_ID,
            client_data=_W3C_ASSERT_CLIENT_DATA,
            authenticator_data=_W3C_ASSERT_AUTH_DATA,
            signature=_W3C_ASSERT_SIGNATURE,
            rp_id=_W3C_RP_ID,
            origins=(_W3C_ORIGIN,),
        )


class _Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _CountingChallengeStore(MemoryChallengeStore):
    """A `MemoryChallengeStore` a test can count.

    The router keeps ceremony state here rather than in the session store, so
    "is the challenge parked / spent / gone" is a question about *this* object.
    Counting is all these tests need, and doing it by wrapping `put`/`discard`
    keeps the consuming statement -- the thing actually under test -- untouched.
    """

    def __init__(self) -> None:
        super().__init__()
        self.live: set[str] = set()

    async def put(self, handle: str, **kwargs: Any) -> None:
        await super().put(handle, **kwargs)
        self.live.add(handle)

    async def consume(self, handle: str, **kwargs: Any) -> dict[str, Any] | None:
        payload = await super().consume(handle, **kwargs)
        if payload is not None:
            self.live.discard(handle)
        return payload

    async def discard(self, handle: str) -> None:
        await super().discard(handle)
        self.live.discard(handle)


class _MemorySessionStore:
    """A `wreath.session_store.SessionStore` twin: load / save / delete."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def load(self, sid: str) -> dict[str, Any] | None:
        return self.rows.get(sid)

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        self.rows[sid] = dict(data)

    async def save_if_present(self, sid: str, data: dict[str, Any], max_age: int) -> bool:
        if sid not in self.rows:
            return False
        await self.save(sid, data, max_age)
        return True

    async def delete(self, sid: str) -> None:
        self.rows.pop(sid, None)


def _cookie(response: Any) -> str:
    value = response.header("set-cookie")
    assert value is not None
    return value.split(";", 1)[0]


def _app(
    users: InMemoryUserStore,
    factors: InMemorySecondFactorStore,
    clock: _Clock,
    *,
    session_store: _MemorySessionStore | None = None,
    **options: Any,
) -> Wreath:
    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(
            session=SessionPolicy(
                secret="s" * 32,
                secure=False,
                store=session_store,
            )
        )
    )
    app.include_router(
        user_router(
            users,
            sessions=_REVOCATIONS,
            secret="u" * 32,
            second_factors=factors,
            clock=clock,
        )
    )
    options.setdefault("rp_id", RP_ID)
    options.setdefault("origins", (ORIGIN,))
    # No `pytest.warns` wrapper any more: a router built without `enrolments=`
    # no longer warns, because it no longer degrades. Ceremony state goes to a
    # `ChallengeStore` whose default is a real one.
    app.include_router(second_factor_router(users, factors, clock=clock, **options))

    @app.get("/session")
    async def show(request: Any) -> dict[str, Any]:
        return dict(request.state.session)

    @app.post("/adopt/{user_id}")
    async def adopt(request: Any, user_id: str) -> dict[str, Any]:
        """Sign somebody in the way an application that is not `user_router` does.

        `wreath._auth.oauth2` writes the principal onto the session itself, and
        knows nothing about a half-finished enrolment or a live challenge left
        on it. `user_router`'s own login and logout now clear both, so this is
        the path on which the ceremony's user binding is the last thing left to
        refuse a ceremony the previous holder began -- and a binding test that
        went through `/users/login` would be asserting a refusal whose subject
        the login had already removed (a check that has nothing to check).
        """
        request.state.session["principal"] = {"sub": user_id, "type": "User", "roles": []}
        return {"status": "adopted"}

    @app.post("/forget")
    async def forget(request: Any) -> dict[str, Any]:
        """Drop the identity and nothing else.

        Logout would clear the ceremony marker too, which is the whole state
        under test here: an application that writes the session itself can leave
        a marker behind with nobody to own it.
        """
        request.state.session.pop("principal", None)
        request.state.session.pop("pending_second_factor", None)
        return {"status": "forgotten"}

    return app


async def _seed(users: InMemoryUserStore, email: str = "ann@example.test") -> Any:
    return await users.create(email, PASSWORD_HASH)


async def _http_login(client: Any, email: str = "ann@example.test", cookie: str = "") -> Any:
    """Sign in, optionally *on an existing session*.

    Passing the cookie is what makes a session change hands rather than a fresh
    one being minted: login rotates the id but carries the contents over, so
    whatever the previous holder left behind is still there afterwards. That is
    the state the binding checks exist for, and a test that omits the cookie
    silently exercises the empty-session path instead.
    """
    headers = {"cookie": cookie} if cookie else {}
    return await client.post(
        "/users/login", json={"email": email, "password": PASSWORD}, headers=headers
    )


def _wire(minted: dict[str, bytes]) -> dict[str, str]:
    """What a browser's own script would post: base64url, because JSON has no bytes."""
    return {name: b64url_encode(raw) for name, raw in minted.items()}


async def _http_register(client: Any, device: _Authenticator, cookie: str, **options: Any) -> Any:
    begun = await client.post("/auth/2fa/webauthn/begin", headers={"cookie": cookie})
    assert begun.status == 200, begun.json()
    challenge = b64url_decode(begun.json()["challenge"])
    return begun, challenge, device.register(challenge, **options)


async def _http_assert(client: Any, device: _Authenticator, cookie: str, **options: Any) -> Any:
    begun = await client.post("/auth/2fa/webauthn/verify/begin", headers={"cookie": cookie})
    assert begun.status == 200, begun.json()
    challenge = b64url_decode(begun.json()["challenge"])
    return begun, device.assertion(challenge, **options)


async def _enrolled(client: Any, device: _Authenticator, cookie: str) -> str:
    """Register `device` over HTTP; returns the cookie afterwards."""
    begun, _, minted = await _http_register(client, device, cookie)
    cookie = _cookie(begun) or cookie
    done = await client.post(
        "/auth/2fa/webauthn/confirm", json=_wire(minted), headers={"cookie": cookie}
    )
    assert done.status == 200, done.json()
    return _cookie(done) or cookie


async def test_a_passkey_registers_and_then_finishes_a_pending_login() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    store = _MemorySessionStore()
    async with TestClient(_app(users, factors, clock, session_store=store)) as client:
        user = await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        await client.post("/users/logout", headers={"cookie": cookie})

        clock.now += 60
        pending = await _http_login(client)
        assert pending.json()["methods"] == ["recovery", "webauthn"]
        cookie = _cookie(pending)
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie
        prior_sid = next(iter(store.rows))
        done = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        assert done.status == 200, done.json()
        assert done.json()["email"] == "ann@example.test"
        rotated = _cookie(done)
        assert rotated != cookie
        rotated_sid = next(iter(store.rows))
        assert rotated_sid is not None and rotated_sid != prior_sid
        assert prior_sid not in store.rows
        session = (await client.get("/session", headers={"cookie": rotated})).json()
        assert session["principal"]["sub"] == user.id
        assert "pending_second_factor" not in session
        assert "pending_webauthn" not in session


async def test_a_discoverable_passkey_is_a_first_factor() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock, passkey_login=True)) as client:
        user = await _seed(users)
        cookie = _cookie(await _http_login(client))

        begun, _, minted = await _http_register(client, device, cookie)
        assert begun.json()["authenticatorSelection"] == {
            "userVerification": "required",
            "residentKey": "required",
            "requireResidentKey": True,
        }
        cookie = _cookie(begun) or cookie
        enrolled = await client.post(
            "/auth/2fa/webauthn/confirm",
            json=_wire(minted),
            headers={"cookie": cookie},
        )
        assert enrolled.status == 200, enrolled.json()
        factor_id = enrolled.json()["id"]
        assert factor_id != b64url_encode(device.credential_id)
        assert len(factor_id) == 64
        await client.post("/users/logout", headers={"cookie": _cookie(enrolled)})

        login = await client.post("/auth/2fa/webauthn/login/begin")
        assert login.status == 200, login.json()
        assert login.json()["allowCredentials"] == []
        assert login.json()["userVerification"] == "required"
        assertion = device.assertion(b64url_decode(login.json()["challenge"]), sign_count=1)
        completed = await client.post(
            "/auth/2fa/webauthn/login",
            json={"id": b64url_encode(device.credential_id), **_wire(assertion)},
            headers={"cookie": _cookie(login)},
        )
        assert completed.status == 200, completed.json()
        assert completed.json()["id"] == user.id
        session = (await client.get("/session", headers={"cookie": _cookie(completed)})).json()
        assert session["principal"]["sub"] == user.id
        assert session["principal"]["second_factor_uv"] is True


async def test_the_user_verification_outcome_reaches_the_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    store = _MemorySessionStore()
    async with TestClient(_app(users, factors, clock, session_store=store)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(
            client, device, cookie, sign_count=1, user_verified=False
        )
        cookie = _cookie(begun) or cookie
        prior_sid = next(iter(store.rows))
        done = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        assert done.json() == {"status": "second_factor_verified"}
        rotated = _cookie(done)
        assert rotated != cookie
        rotated_sid = next(iter(store.rows))
        assert rotated_sid is not None and rotated_sid != prior_sid
        assert prior_sid not in store.rows
        session = (await client.get("/session", headers={"cookie": rotated})).json()
        assert session["principal"]["second_factor_uv"] is False
        assert session["principal"]["second_factor_at"] == int(clock.now)


async def test_a_challenge_is_single_use() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    challenges = _CountingChallengeStore()
    async with TestClient(_app(users, factors, clock, challenges=challenges)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie
        body = {"id": b64url_encode(device.credential_id), **_wire(minted)}
        first = await client.post(
            "/auth/2fa/webauthn/verify", json=body, headers={"cookie": cookie}
        )
        assert first.status == 200
        assert challenges.live == set()
        replayed = await client.post(
            "/auth/2fa/webauthn/verify", json=body, headers={"cookie": cookie}
        )
        assert replayed.status == 400
        assert replayed.json() == {"error": "ceremony_expired"}


async def test_a_failed_ceremony_spends_its_challenge_too() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    challenges = _CountingChallengeStore()
    async with TestClient(_app(users, factors, clock, challenges=challenges)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(
            client, device, cookie, sign_count=1, origin="https://phish.test"
        )
        cookie = _cookie(begun) or cookie
        body = {"id": b64url_encode(device.credential_id), **_wire(minted)}
        refused = await client.post(
            "/auth/2fa/webauthn/verify", json=body, headers={"cookie": cookie}
        )
        assert refused.status == 401
        assert refused.json() == {"error": "invalid_assertion"}
        assert challenges.live == set()


async def test_beginning_again_abandons_the_previous_challenge() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    challenges = _CountingChallengeStore()
    async with TestClient(_app(users, factors, clock, challenges=challenges)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie
        again, _ = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(again) or cookie
        assert len(challenges.live) == 1
        stale = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        assert stale.status == 401
        assert stale.json() == {"error": "invalid_assertion"}


async def test_a_challenge_is_single_use_with_no_stores_configured_at_all() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie
        body = {"id": b64url_encode(device.credential_id), **_wire(minted)}
        first = await client.post(
            "/auth/2fa/webauthn/verify", json=body, headers={"cookie": cookie}
        )
        assert first.status == 200
        # The same cookie, replayed. It still carries the marker; what it cannot
        # carry is the challenge, which was spent by the statement above.
        replayed = await client.post(
            "/auth/2fa/webauthn/verify", json=body, headers={"cookie": cookie}
        )
        assert replayed.status == 400
        assert replayed.json() == {"error": "ceremony_expired"}


async def test_a_registration_challenge_cannot_answer_an_assertion() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        # Begin a *registration*, then try to finish it as an *assertion*.
        begun, _, _ = await _http_register(client, device, cookie)
        cookie = _cookie(begun) or cookie
        challenge = b64url_decode(begun.json()["challenge"])
        minted = device.assertion(challenge, sign_count=1)
        confused = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        assert confused.status == 400
        assert confused.json() == {"error": "ceremony_expired"}


async def test_a_verify_with_no_identity_on_the_session_is_refused() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie
        # Strip the identity without going through logout, which would also
        # clear the marker: an application that writes the session itself can
        # leave exactly this shape behind.
        stripped = await client.post("/forget", headers={"cookie": cookie})
        cookie = _cookie(stripped) or cookie
        refused = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        assert refused.status == 401
        assert refused.json() == {"error": "no_pending_second_factor"}


async def test_a_throttled_caller_cannot_spend_a_challenge() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    challenges = _CountingChallengeStore()
    async with TestClient(
        _app(users, factors, clock, challenges=challenges, max_verify_attempts=1)
    ) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie
        bad = {"id": b64url_encode(device.credential_id), **_wire(minted)}
        bad["signature"] = b64url_encode(b"\x00" * 64)

        first = await client.post("/auth/2fa/webauthn/verify", json=bad, headers={"cookie": cookie})
        assert first.status == 401
        # Now throttled. Begin a fresh ceremony and confirm the refusal does not
        # cost it: the challenge is still live afterwards.
        begun, minted = await _http_assert(client, device, cookie, sign_count=2)
        cookie = _cookie(begun) or cookie
        assert len(challenges.live) == 1
        throttled = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        assert throttled.status == 429
        assert len(challenges.live) == 1, "a throttled attempt spent the challenge"


async def test_a_challenge_expires() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock, webauthn_ttl=60.0)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie
        clock.now += 61
        late = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        assert late.status == 400
        assert late.json() == {"error": "ceremony_expired"}


async def test_a_challenge_is_bound_to_the_session_that_began_it() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    challenges = _CountingChallengeStore()
    async with TestClient(_app(users, factors, clock, challenges=challenges)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)

        # A second sign-in from somewhere else: same account, different session.
        elsewhere = _cookie(await _http_login(client))
        stolen = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": elsewhere},
        )
        assert stolen.status == 400
        assert stolen.json() == {"error": "no_ceremony_in_progress"}


async def test_a_begun_registration_does_not_survive_a_logout() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    challenges = _CountingChallengeStore()
    async with TestClient(_app(users, factors, clock, challenges=challenges)) as client:
        await _seed(users)
        bob = await _seed(users, "bob@example.test")
        cookie = _cookie(await _http_login(client))
        begun, _, minted = await _http_register(client, device, cookie)
        cookie = _cookie(begun) or cookie
        assert len(challenges.live) == 1

        gone = await client.post("/users/logout", headers={"cookie": cookie})
        cookie = _cookie(gone) or cookie
        assert challenges.live == set()
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "pending_webauthn" not in session

        cookie = _cookie(await _http_login(client, "bob@example.test", cookie))
        stolen = await client.post(
            "/auth/2fa/webauthn/confirm", json=_wire(minted), headers={"cookie": cookie}
        )
        assert stolen.status == 400
        assert stolen.json() == {"error": "no_ceremony_in_progress"}
        assert await factors.credentials(bob.id) == []


async def test_a_registration_is_bound_to_the_user_who_began_it() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    challenges = _CountingChallengeStore()
    async with TestClient(_app(users, factors, clock, challenges=challenges)) as client:
        await _seed(users)
        bob = await _seed(users, "bob@example.test")
        cookie = _cookie(await _http_login(client))
        begun, _, minted = await _http_register(client, device, cookie)
        cookie = _cookie(begun) or cookie
        rightful_cookie = cookie

        adopted = await client.post(f"/adopt/{bob.id}", headers={"cookie": cookie})
        cookie = _cookie(adopted) or cookie
        # The challenge is still there and still reachable from this session:
        # what refuses the confirmation is who it was begun for, nothing else.
        assert len(challenges.live) == 1
        stolen = await client.post(
            "/auth/2fa/webauthn/confirm", json=_wire(minted), headers={"cookie": cookie}
        )
        assert stolen.status == 400
        assert stolen.json() == {"error": "no_ceremony_in_progress"}
        assert await factors.credentials(bob.id) == []
        assert len(challenges.live) == 1

        completed = await client.post(
            "/auth/2fa/webauthn/confirm",
            json=_wire(minted),
            headers={"cookie": rightful_cookie},
        )
        assert completed.status == 200, completed.json()
        assert challenges.live == set()


async def test_a_pending_login_cannot_finish_somebody_elses_ceremony() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        bob = await _seed(users, "bob@example.test")
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        begun, minted = await _http_assert(client, device, cookie, sign_count=1)
        cookie = _cookie(begun) or cookie

        # Same browser, same session contents, a different person signed in --
        # by an application that writes the principal itself, since a sign-in
        # through `user_router` would have cleared the marker outright.
        adopted = await client.post(f"/adopt/{bob.id}", headers={"cookie": cookie})
        cookie = _cookie(adopted) or cookie
        stolen = await client.post(
            "/auth/2fa/webauthn/verify",
            json={"id": b64url_encode(device.credential_id), **_wire(minted)},
            headers={"cookie": cookie},
        )
        # 401 rather than 400: the marker *is* still there, and it is the
        # binding that refuses it.
        assert stolen.status == 401
        assert stolen.json() == {"error": "no_pending_second_factor"}
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert "second_factor_at" not in session.get("principal", {})


async def test_the_listing_shows_the_key_and_never_its_material() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        listed = await client.get("/auth/2fa", headers={"cookie": cookie})
        body = listed.json()
        assert [row["kind"] for row in body["factors"]] == ["webauthn"]
        assert body["recovery_codes_remaining"] == 10
        rendered = str(body)
        assert b64url_encode(device.credential_id) not in rendered
        assert b64url_encode(device.cose) not in rendered


async def test_verification_is_throttled_per_user() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    async with TestClient(_app(users, factors, clock, max_verify_attempts=2)) as client:
        await _seed(users)
        cookie = await _enrolled(client, device, _cookie(await _http_login(client)))
        statuses = []
        for _ in range(3):
            begun, minted = await _http_assert(
                client, device, cookie, sign_count=1, origin="https://phish.test"
            )
            cookie = _cookie(begun) or cookie
            refused = await client.post(
                "/auth/2fa/webauthn/verify",
                json={"id": b64url_encode(device.credential_id), **_wire(minted)},
                headers={"cookie": cookie},
            )
            statuses.append(refused.status)
        assert statuses == [401, 401, 429]


async def _enrol_totp(client: Any, clock: _Clock, cookie: str) -> str:
    """Run the TOTP enrolment over HTTP; returns the cookie afterwards."""
    from wreath._secondfactor import base32_to_secret, totp_code, totp_counter

    begun = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
    assert begun.status == 200, begun.json()
    cookie = _cookie(begun) or cookie
    code = totp_code(base32_to_secret(begun.json()["secret"]), totp_counter(clock.now))
    done = await client.post(
        "/auth/2fa/totp/confirm", json={"code": code}, headers={"cookie": cookie}
    )
    assert done.status == 200, done.json()
    return _cookie(done) or cookie


async def test_registering_a_further_passkey_does_not_stamp_the_session() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        # The victim enrols a TOTP factor, which stamps, and adds a passkey
        # while that stamp is still fresh -- the ordinary way a user ends up
        # with two factors, and the case the guard below must not cost.
        enrolled_at = int(clock.now)
        cookie = await _enrol_totp(client, clock, _cookie(await _http_login(client)))
        clock.now += 60

        cookie = await _enrolled(client, _Authenticator(credential_id=b"second"), cookie)
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        # A minute old, because the enrolment left it alone -- not absent,
        # because the first factor's confirmation did stamp it.
        assert session["principal"]["second_factor_at"] == enrolled_at


async def test_registering_a_further_passkey_does_not_satisfy_the_removal_guard() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        cookie = await _enrol_totp(client, clock, _cookie(await _http_login(client)))
        listed = (await client.get("/auth/2fa", headers={"cookie": cookie})).json()
        victim_factor = listed["factors"][0]["id"]

        # The session is stolen after the stamp has gone stale.
        clock.now += 3600
        assert (
            await client.delete(f"/auth/2fa/{victim_factor}", headers={"cookie": cookie})
        ).status == 403

        # The enrolment itself is refused, so there is no passkey to prove.
        refused = await client.post("/auth/2fa/webauthn/begin", headers={"cookie": cookie})
        assert refused.status == 403
        assert refused.json() == {"error": "second_factor_required"}
        assert (
            await client.delete(f"/auth/2fa/{victim_factor}", headers={"cookie": cookie})
        ).status == 403


async def test_a_stale_session_cannot_enrol_a_passkey_and_step_up_with_it() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        cookie = await _enrol_totp(client, clock, _cookie(await _http_login(client)))
        listed = (await client.get("/auth/2fa", headers={"cookie": cookie})).json()
        victim_factor = listed["factors"][0]["id"]

        clock.now += 3600
        begun = await client.post("/auth/2fa/webauthn/begin", headers={"cookie": cookie})
        assert begun.status == 403, begun.json()

        # And the assertion end has nothing of the attacker's to prove: the only
        # credential on the account is the victim's TOTP factor.
        stepped_up = await client.post(
            "/auth/2fa/webauthn/verify/begin", headers={"cookie": cookie}
        )
        assert stepped_up.status == 400
        assert stepped_up.json() == {"error": "no_second_factor_enrolled"}
        assert (
            await client.delete(f"/auth/2fa/{victim_factor}", headers={"cookie": cookie})
        ).status == 403


async def test_a_stale_session_cannot_enrol_totp_beside_a_passkey() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        cookie = await _enrolled(client, _Authenticator(), _cookie(await _http_login(client)))
        listed = (await client.get("/auth/2fa", headers={"cookie": cookie})).json()
        victim_factor = listed["factors"][0]["id"]

        clock.now += 3600
        refused = await client.post("/auth/2fa/totp/begin", headers={"cookie": cookie})
        assert refused.status == 403
        assert refused.json() == {"error": "second_factor_required"}
        assert (
            await client.delete(f"/auth/2fa/{victim_factor}", headers={"cookie": cookie})
        ).status == 403


async def test_enrolling_totp_beside_a_passkey_does_not_stamp_either() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        enrolled_at = int(clock.now)
        cookie = await _enrolled(client, _Authenticator(), _cookie(await _http_login(client)))

        clock.now += 60
        cookie = await _enrol_totp(client, clock, cookie)
        session = (await client.get("/session", headers={"cookie": cookie})).json()
        assert session["principal"]["second_factor_at"] == enrolled_at


async def test_a_first_factor_still_stamps_so_it_can_be_undone() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    async with TestClient(_app(users, factors, clock, step_up_ttl=300.0)) as client:
        await _seed(users)
        cookie = await _enrolled(client, _Authenticator(), _cookie(await _http_login(client)))
        listed = (await client.get("/auth/2fa", headers={"cookie": cookie})).json()
        removed = await client.delete(
            f"/auth/2fa/{listed['factors'][0]['id']}", headers={"cookie": cookie}
        )
        assert removed.status == 200


def _padded_cose(device: _Authenticator, padding: int) -> bytes:
    """`device`'s real COSE key with `padding` bytes hung off a label nobody reads.

    Attested credential data is **not signed** under `none` attestation, so this
    is something any caller who has begun a registration can post.
    """
    decoded = cbor_decode(device.cose)
    return _cbor({**decoded, 999: b"\x00" * padding})


def test_an_oversized_cose_key_is_refused_by_the_parser() -> None:
    device = _Authenticator()
    device.cose = _padded_cose(device, 2048)
    with pytest.raises(WebAuthnError, match="COSE public key"):
        parse_authenticator_data(device.auth_data(attested=True))


async def test_an_oversized_cose_key_is_a_400_and_not_a_500() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    device.cose = _padded_cose(device, 70_000)
    async with TestClient(_app(users, factors, clock)) as client:
        user = await _seed(users)
        cookie = _cookie(await _http_login(client))
        begun, _, minted = await _http_register(client, device, cookie)
        cookie = _cookie(begun) or cookie
        refused = await client.post(
            "/auth/2fa/webauthn/confirm", json=_wire(minted), headers={"cookie": cookie}
        )
        assert refused.status == 400
        assert refused.json() == {"error": "invalid_registration"}
        assert await factors.credentials(user.id) == []


async def test_a_key_within_the_bound_still_registers() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    device = _Authenticator()
    device.cose = _padded_cose(device, 64)
    async with TestClient(_app(users, factors, clock)) as client:
        user = await _seed(users)
        await _enrolled(client, device, _cookie(await _http_login(client)))
        assert any(row.kind == "webauthn" for row in await factors.credentials(user.id))


async def test_without_an_rp_id_there_are_no_passkey_routes() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))
    app.include_router(user_router(users, sessions=_REVOCATIONS, secret="u" * 32, clock=clock))
    app.include_router(second_factor_router(users, factors, clock=clock))
    async with TestClient(app) as client:
        assert (await client.post("/auth/2fa/webauthn/begin")).status == 404


def test_origins_without_an_rp_id_are_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="rp_id"):
        second_factor_router(InMemoryUserStore(), InMemorySecondFactorStore(), origins=(ORIGIN,))


def test_the_default_origin_set_is_https_only_off_loopback() -> None:
    assert default_origins("example.com") == ("https://example.com",)


@pytest.mark.parametrize(
    ("rp_id", "host"),
    [
        ("localhost", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("127.0.0.53", "127.0.0.53"),
        ("::1", "[::1]"),
    ],
)
def test_loopback_admits_http_in_the_default_origin_set(rp_id: str, host: str) -> None:
    assert default_origins(rp_id) == (f"https://{host}", f"http://{host}")


async def test_a_localhost_ceremony_on_a_port_verifies_by_default() -> None:
    store = InMemorySecondFactorStore()
    device = _Authenticator()
    begun = begin_webauthn_registration(
        user_id="user-1", account="ann@example.test", rp_id="localhost"
    )
    minted = device.register(begun.challenge, rp_id="localhost", origin="http://localhost:8000")
    credential, _ = await confirm_webauthn_registration(
        store,
        "user-1",
        challenge=begun.challenge,
        rp_id="localhost",
        origins=default_origins("localhost"),
        **minted,
    )
    assert credential.kind == "webauthn"


def _client_data(origin: str, challenge: bytes = b"c" * 32) -> bytes:
    return _Authenticator().client_data(ceremony="register", challenge=challenge, origin=origin)


def test_a_non_loopback_rp_id_still_refuses_http() -> None:
    with pytest.raises(WebAuthnError, match="origin"):
        check_client_data(
            _client_data("http://example.test"),
            expected_type="webauthn.create",
            challenge=b"c" * 32,
            origins=default_origins("example.test"),
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost.example.test",  # a name that merely begins with it
        "http://evil.localhost",  # a subdomain is a different host
        "http://notlocalhost",
        "http://localhost:8000.example.test",  # not a port at all
        "http://localhost:8000/../evil",  # not an origin at all
        "http://127.0.0.1:8000",  # loopback, but not the configured host
        "https://localhost:8000@evil.test",
    ],
)
def test_the_loopback_exception_widens_to_nothing_else(origin: str) -> None:
    with pytest.raises(WebAuthnError, match="origin"):
        check_client_data(
            _client_data(origin),
            expected_type="webauthn.create",
            challenge=b"c" * 32,
            origins=default_origins("localhost"),
        )


def test_a_named_origin_off_loopback_gets_no_port_tolerance() -> None:
    with pytest.raises(WebAuthnError, match="origin"):
        check_client_data(
            _client_data("https://example.test:8443"),
            expected_type="webauthn.create",
            challenge=b"c" * 32,
            origins=("https://example.test",),
        )


async def test_a_localhost_router_accepts_a_ported_ceremony_with_no_origins_named() -> None:
    users, factors, clock = InMemoryUserStore(), InMemorySecondFactorStore(), _Clock()
    await _seed(users)
    app = _app(users, factors, clock, rp_id="localhost", origins=())
    device = _Authenticator()
    async with TestClient(app) as client:
        cookie = _cookie(await _http_login(client))
        begun, _, minted = await _http_register(
            client, device, cookie, rp_id="localhost", origin="http://localhost:8000"
        )
        cookie = _cookie(begun) or cookie
        done = await client.post(
            "/auth/2fa/webauthn/confirm", json=_wire(minted), headers={"cookie": cookie}
        )
        assert done.status == 200, done.json()


def test_a_router_built_without_a_store_warns_about_nothing() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        second_factor_router(InMemoryUserStore(), InMemorySecondFactorStore())


def test_a_router_given_a_store_warns_about_nothing() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        second_factor_router(
            InMemoryUserStore(),
            InMemorySecondFactorStore(),
            enrolments=_MemorySessionStore(),
        )


def test_an_impossible_setting_fails_where_it_is_written() -> None:
    with pytest.raises(ValueError, match="user verification"):
        second_factor_router(
            InMemoryUserStore(),
            InMemorySecondFactorStore(),
            rp_id=RP_ID,
            user_verification="whenever",
        )


# `wreath mutant --operators guard.remove-raise` reported each of these
# UNREACHED across all 213 tests of the second-factor suite. The ceremony tests
# above all pass well-formed parameters, which is the right thing for them to
# do and the reason none of these refusals had ever fired. Each guards a
# WebAuthn security property that fails quietly rather than loudly when the
# parameter is wrong: an empty origin list accepts an assertion collected
# anywhere, an empty RP ID hashes to a value no authenticator will match, and a
# short challenge is a replayable one.


def test_a_ceremony_with_no_usable_origin_is_refused() -> None:
    from wreath._secondfactor import _webauthn_origins

    with pytest.raises(ValueError, match="non-empty origin"):
        _webauthn_origins(())
    with pytest.raises(ValueError, match="non-empty origin"):
        _webauthn_origins(("",))
    # One bad entry poisons the tuple: a caller who meant two origins and typed
    # one blank must not silently get a list that matches the empty string.
    with pytest.raises(ValueError, match="non-empty origin"):
        _webauthn_origins((ORIGIN, ""))
    assert _webauthn_origins((ORIGIN,)) == (ORIGIN,)


@pytest.mark.parametrize(
    "origins",
    [
        ("null",),
        ("http://example.test",),
        ("https://user@example.test",),
        ("https://example.test/path",),
        ("https://example.test:0",),
        (" https://example.test",),
        ("ftp://example.test",),
        ("ftp://localhost",),
        ("https://example..test",),
        ("https://-example.test",),
        ("https://example-.test",),
        ("https://exa_mple.test",),
        (f"https://{'a' * 64}.test",),
        (f"https://{'.'.join(['a' * 63] * 4)}",),
        (7,),
    ],
)
def test_a_ceremony_refuses_malformed_or_insecure_allowed_origins(origins: object) -> None:
    from wreath._secondfactor import _webauthn_origins

    with pytest.raises(ValueError, match="valid HTTPS origin or loopback HTTP origin"):
        _webauthn_origins(cast(Any, origins))


def test_a_ceremony_accepts_https_and_loopback_http_origins() -> None:
    from wreath._secondfactor import _webauthn_origins

    assert _webauthn_origins(("https://api-example.test:8443", "http://localhost:8000")) == (
        "https://api-example.test:8443",
        "http://localhost:8000",
    )


def test_a_ceremony_with_no_rp_id_is_refused() -> None:
    from wreath._secondfactor import _webauthn_rp_id

    with pytest.raises(ValueError, match="needs an RP ID"):
        _webauthn_rp_id("")
    assert _webauthn_rp_id(RP_ID) == RP_ID

    with pytest.raises(ValueError, match="needs an RP ID"):
        begin_webauthn_registration(user_id="u1", account="ann@example.test", rp_id="")
    with pytest.raises(ValueError, match="needs an RP ID"):
        begin_webauthn_assertion([], rp_id="")


def test_a_challenge_too_short_to_resist_replay_is_refused() -> None:
    from wreath._secondfactor import WEBAUTHN_CHALLENGE_BYTES, _webauthn_challenge

    with pytest.raises(ValueError, match="at least 16 bytes"):
        _webauthn_challenge(b"\x00" * 15)
    assert len(_webauthn_challenge(b"\x00" * 16)) == 16  # the floor is admitted
    assert len(_webauthn_challenge(None)) == WEBAUTHN_CHALLENGE_BYTES

    with pytest.raises(ValueError, match="at least 16 bytes"):
        begin_webauthn_registration(
            user_id="u1",
            account="ann@example.test",
            rp_id=RP_ID,
            challenge=b"\x00" * 8,
        )


def test_a_registration_with_no_account_name_is_refused() -> None:
    with pytest.raises(ValueError, match="needs an account name"):
        begin_webauthn_registration(user_id="u1", account="", rp_id=RP_ID)


@pytest.mark.parametrize(
    ("user_id", "user_verification", "message"),
    [
        ("", "preferred", "user handle"),
        ("é" * (MAX_USER_HANDLE_BYTES // 2 + 1), "preferred", "user handle"),
        ("u1", "whenever", "unknown user verification"),
    ],
)
def test_registration_refuses_invalid_handles_and_verification_policy(
    user_id: str,
    user_verification: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        begin_webauthn_registration(
            user_id=user_id,
            account="ann@example.test",
            rp_id=RP_ID,
            user_verification=user_verification,
        )


def test_registration_options_preserve_named_and_default_display_values() -> None:
    ordinary = begin_webauthn_registration(
        user_id="u1",
        account="ann@example.test",
        rp_id=RP_ID,
        challenge=b"c" * 32,
    ).options
    assert ordinary["rp"] == {"id": RP_ID, "name": RP_ID}
    assert ordinary["user"]["displayName"] == "ann@example.test"
    assert ordinary["authenticatorSelection"]["residentKey"] == "discouraged"

    named = begin_webauthn_registration(
        user_id="u1",
        account="ann@example.test",
        display_name="Ada",
        rp_id=RP_ID,
        rp_name="Wreath",
        discoverable=True,
        challenge=b"c" * 32,
    ).options
    assert named["rp"] == {"id": RP_ID, "name": "Wreath"}
    assert named["user"]["displayName"] == "Ada"
    assert named["authenticatorSelection"]["residentKey"] == "required"


def _stored_factor(identifier: str, kind: str, credential_id: bytes) -> SecondFactor:
    return SecondFactor(
        id=identifier,
        user_id="u1",
        kind=kind,
        label=identifier,
        created_at=datetime.now(UTC),
        last_used_at=None,
        material=pack_credential(credential_id, b"key", user_verified=False),
    )


def test_descriptors_include_only_decodable_webauthn_credentials() -> None:
    totp = _stored_factor("totp", "totp", b"not-a-passkey")
    passkey = _stored_factor("passkey", "webauthn", b"credential")
    assert _descriptors((totp, passkey)) == [
        {"type": "public-key", "id": b64url_encode(b"credential")}
    ]


async def test_credential_lookup_refuses_empty_and_wrong_kind_then_finds_a_later_match():
    store = InMemorySecondFactorStore()
    first = _stored_factor("first", "webauthn", b"other")
    wrong_kind = _stored_factor("totp", "totp", b"target")
    target = _stored_factor("target", "webauthn", b"target")
    for factor in (first, wrong_kind, target):
        await store.add(factor)

    with pytest.raises(WebAuthnError, match="names no credential"):
        await _webauthn_credential(store, "u1", b"")
    found, stored = await _webauthn_credential(store, "u1", b"target")
    assert found == target
    assert stored.credential_id == b"target"
