from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from wreath._auth._ecverify import verify_es256
from wreath._b64 import b64url_decode
from wreath._webpush import (
    _GX,
    _GY,
    MAX_PAYLOAD_BYTES,
    PushError,
    PushSubscription,
    VapidKeys,
    _ecdsa_sign,
    _mul,
    aes128gcm_encrypt,
    declarative_payload,
    encrypt,
    vapid_headers,
)

# RFC 8291 §5's receiver half. The sender half is generated per test.
RFC8291_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC8291_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC8291_PLAINTEXT = b"When I grow up, I want to be a watermelon"


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 64, 1000, 4000])
def test_aes_gcm_matches_an_independent_implementation(size: int) -> None:
    key, nonce, plaintext = os.urandom(16), os.urandom(12), os.urandom(size)
    assert aes128gcm_encrypt(key, nonce, plaintext) == AESGCM(key).encrypt(nonce, plaintext, None)


def test_aes_gcm_nist_known_answer() -> None:
    result = aes128gcm_encrypt(bytes(16), bytes(12), bytes(16))
    assert result == bytes.fromhex(
        "0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf"
    )


def test_aes_gcm_refuses_a_nonce_that_is_not_96_bits() -> None:
    with pytest.raises(PushError, match="96-bit nonce"):
        aes128gcm_encrypt(bytes(16), bytes(16), b"x")


def test_aes_gcm_refuses_a_key_that_is_not_128_bits() -> None:
    with pytest.raises(PushError, match="16-byte key"):
        aes128gcm_encrypt(bytes(32), bytes(12), b"x")


def test_ecdsa_signatures_verify_under_the_trees_own_verifier() -> None:
    private = int.from_bytes(os.urandom(32), "big") % (2**256) or 1
    public = _mul(private, (_GX, _GY))
    assert public is not None
    message = b"the quick brown fox"
    signature = _ecdsa_sign(private, hashlib.sha256(message).digest())
    assert verify_es256(public[0], public[1], message, signature)


def test_ecdsa_sign_retries_a_rejected_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    import wreath._webpush as webpush

    accepted = b"s" * 64
    results = iter((None, accepted))
    monkeypatch.setattr(webpush._core, "curve_p256_sign", lambda *_args: next(results))

    assert _ecdsa_sign(1, bytes(32)) == accepted


def test_encrypt_names_an_identity_shared_point(monkeypatch: pytest.MonkeyPatch) -> None:
    import wreath._webpush as webpush

    _, _, subscription = _receiver()
    multiply = webpush._mul
    calls = 0

    def identity_on_shared_secret(scalar: int, point: tuple[int, int]) -> object:
        nonlocal calls
        calls += 1
        return None if calls == 2 else multiply(scalar, point)

    monkeypatch.setattr(webpush, "_mul", identity_on_shared_secret)

    with pytest.raises(PushError, match="point at infinity"):
        encrypt(subscription, b"x", salt=bytes(16), ephemeral=1)


def test_ecdsa_signatures_verify_under_cryptography() -> None:
    keys = VapidKeys.generate("mailto:ops@example.com")
    digest = hashlib.sha256(b"payload").digest()
    signature = _ecdsa_sign(keys.private, digest)
    public = ec.derive_private_key(keys.private, ec.SECP256R1()).public_key()
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    public.verify(_der(r, s), b"payload", ec.ECDSA(hashes.SHA256()))


def _der(r: int, s: int) -> bytes:
    """Encode `r, s` as the DER sequence `cryptography` verifies."""
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    return encode_dss_signature(r, s)


def test_ecdsa_uses_a_fresh_nonce_for_every_signature() -> None:
    keys = VapidKeys.generate("mailto:ops@example.com")
    digest = hashlib.sha256(b"same").digest()
    assert _ecdsa_sign(keys.private, digest) != _ecdsa_sign(keys.private, digest)


def test_ecdsa_signatures_are_low_s() -> None:
    order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    keys = VapidKeys.generate("mailto:ops@example.com")
    for index in range(8):
        signature = _ecdsa_sign(keys.private, hashlib.sha256(str(index).encode()).digest())
        assert int.from_bytes(signature[32:], "big") <= order // 2


def test_vapid_token_audience_is_the_origin_not_the_endpoint() -> None:
    keys = VapidKeys.generate("mailto:ops@example.com")
    headers = vapid_headers(keys, "https://fcm.googleapis.com/fcm/send/abc123")
    token = headers["Authorization"].split("t=")[1].split(",")[0]
    claims = json.loads(b64url_decode(token.split(".")[1]))
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == "mailto:ops@example.com"


def test_the_token_expiry_is_issued_plus_lifetime_from_the_clock_given() -> None:
    keys = VapidKeys.generate("mailto:ops@example.com")
    headers = vapid_headers(keys, "https://push.example.net/x", lifetime=600, now=1_700_000_000)
    token = headers["Authorization"].split("t=")[1].split(",")[0]
    claims = json.loads(b64url_decode(token.split(".")[1]))
    assert claims["exp"] == 1_700_000_600


def test_vapid_lifetime_over_24_hours_is_refused() -> None:
    keys = VapidKeys.generate("mailto:ops@example.com")
    with pytest.raises(PushError, match="may not live longer"):
        vapid_headers(keys, "https://push.example.net/x", lifetime=25 * 3600)


def test_vapid_subject_must_be_contactable() -> None:
    with pytest.raises(PushError, match="mailto: or https: URL"):
        VapidKeys.generate("example.com")


def test_vapid_keys_round_trip_through_bytes() -> None:
    keys = VapidKeys.generate("mailto:ops@example.com")
    restored = VapidKeys.from_bytes(keys.private_bytes, keys.subject)
    assert restored.public_bytes == keys.public_bytes


def _receiver() -> tuple[ec.EllipticCurvePrivateKey, bytes, PushSubscription]:
    private = ec.derive_private_key(
        int.from_bytes(b64url_decode(RFC8291_UA_PRIVATE), "big"), ec.SECP256R1()
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    import base64

    subscription = PushSubscription(
        "https://push.example.net/x",
        base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii"),
        RFC8291_AUTH,
    )
    return private, public, subscription


def _decrypt(private: ec.EllipticCurvePrivateKey, ua_public: bytes, body: bytes) -> bytes:
    """The receiver half of RFC 8291, built only from `cryptography`."""
    salt, key_id_length = body[:16], body[20]
    as_public, ciphertext = body[21 : 21 + key_id_length], body[21 + key_id_length :]
    shared = private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    )
    prk_key = hmac.new(b64url_decode(RFC8291_AUTH), shared, hashlib.sha256).digest()
    ikm = HKDFExpand(hashes.SHA256(), 32, b"WebPush: info\x00" + ua_public + as_public).derive(
        prk_key
    )
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = HKDFExpand(hashes.SHA256(), 16, b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDFExpand(hashes.SHA256(), 12, b"Content-Encoding: nonce\x00").derive(prk)
    return AESGCM(cek).decrypt(nonce, ciphertext, None)


def test_rfc8291_payload_decrypts_under_an_independent_receiver() -> None:
    private, public, subscription = _receiver()
    body = encrypt(subscription, RFC8291_PLAINTEXT)
    assert _decrypt(private, public, body).rstrip(b"\x02") == RFC8291_PLAINTEXT


def test_rfc8291_section_5_is_reproduced_byte_for_byte() -> None:
    import base64

    salt = b64url_decode("DGv6ra1nlYgDCS1FRnbzlw")
    as_private = int.from_bytes(b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big")
    subscription = PushSubscription(
        "https://push.example.net/x",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        RFC8291_AUTH,
    )
    body = encrypt(subscription, RFC8291_PLAINTEXT, salt=salt, ephemeral=as_private)
    assert base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii") == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
        "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVB"
        "St2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


@pytest.mark.parametrize(
    "scalar",
    [
        0,
        -1,
        0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,  # n
        0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632552,  # n + 1
    ],
)
def test_an_ephemeral_scalar_outside_the_group_is_refused_by_name(scalar: int) -> None:
    _, _, subscription = _receiver()
    with pytest.raises(PushError, match=r"ephemeral scalar is in \[1, n\)"):
        encrypt(subscription, b"x", ephemeral=scalar)


def test_the_record_header_has_the_shape_rfc8188_specifies() -> None:
    _, _, subscription = _receiver()
    body = encrypt(subscription, b"x")
    assert len(body[:16]) == 16  # salt
    assert int.from_bytes(body[16:20], "big") == MAX_PAYLOAD_BYTES  # record size
    assert body[20] == 65  # key id length: an uncompressed P-256 point
    assert body[21] == 0x04


def test_two_pushes_to_one_subscription_use_different_keys() -> None:
    _, _, subscription = _receiver()
    first, second = encrypt(subscription, b"a"), encrypt(subscription, b"a")
    assert first[21:86] != second[21:86]
    assert first[:16] != second[:16]


def test_an_off_curve_subscription_key_is_refused() -> None:
    import base64

    bogus = b"\x04" + (1).to_bytes(32, "big") + (1).to_bytes(32, "big")
    subscription = PushSubscription(
        "https://push.example.net/x",
        base64.urlsafe_b64encode(bogus).rstrip(b"=").decode("ascii"),
        RFC8291_AUTH,
    )
    with pytest.raises(PushError, match="not a point on P-256"):
        encrypt(subscription, b"x")


def test_an_oversized_payload_is_refused_with_the_limit_named() -> None:
    _, _, subscription = _receiver()
    with pytest.raises(PushError, match="over the 4096-byte limit"):
        encrypt(subscription, b"x" * 4100)


def test_a_short_auth_secret_is_refused() -> None:
    _, _, subscription = _receiver()
    broken = PushSubscription(subscription.endpoint, subscription.p256dh, "AAAA")
    with pytest.raises(PushError, match="auth secret is 16 bytes"):
        encrypt(broken, b"x")


def test_subscription_from_json_requires_an_https_endpoint() -> None:
    with pytest.raises(PushError, match="https endpoint"):
        PushSubscription.from_json({"endpoint": "http://push.example.net/x", "keys": {}})


def test_declarative_payload_carries_the_web_push_marker() -> None:
    document = json.loads(declarative_payload("Title", body="Body", navigate="/photos/1"))
    assert document["web_push"] == 8030
    assert document["notification"] == {
        "title": "Title",
        "navigate": "/photos/1",
        "body": "Body",
    }


def test_an_endpoint_with_no_scheme_is_refused() -> None:
    keys = VapidKeys.generate("mailto:ops@example.com")
    with pytest.raises(PushError, match="has no scheme"):
        vapid_headers(keys, "push.example.net/x")


def test_a_salt_that_is_not_16_bytes_is_refused() -> None:
    _, _, subscription = _receiver()
    with pytest.raises(PushError, match="salt is 16 bytes"):
        encrypt(subscription, b"x", salt=b"short")
