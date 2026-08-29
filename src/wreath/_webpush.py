"""Web Push according to RFC 8030, RFC 8291, and RFC 8292.

VAPID uses ECDSA P-256; message encryption uses P-256 ECDH, HKDF-SHA256,
and AES-128-GCM with the RFC 8188 ``aes128gcm`` content coding. AES-GCM is
implemented in ``_native/aesgcm.c`` and dispatches to AES-NI/PCLMULQDQ when
the processor exposes them. The scalar kernel covers every other supported
Linux, macOS, and Windows target. Published NIST vectors and OpenSSL outputs
pin both instruction paths independently.

A 404 or 410 response expires a subscription, and the encrypted payload is
limited to 4096 octets. Both are protocol requirements enforced here. Retry,
storage, and dead-letter policy belong to the job and application layers.

Secret P-256 scalars use the fixed-width, fixed-step ladder in
``_native/curves.c``. Scalar bits steer neither control flow nor memory
indexing. The AES-NI/PCLMULQDQ kernel likewise has no data-dependent table
lookup; the scalar AES S-box does, so only the hardware path carries that
stronger timing property.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Final

from ._b64 import b64url_decode, b64url_encode
from ._curves import P256_G, P256_N, p256_on_curve, p256_scalarmult_secret
from ._native import _core

__all__ = [
    "PushError",
    "PushResult",
    "PushSubscription",
    "VapidKeys",
    "encrypt",
    "vapid_headers",
]


#: The curve constants, group law and scalar multiplication share the curve
#: library with `_auth/_ecverify` and `_dkim`. What
#: stays here is what is specific to *this* protocol: SEC1 encoding with a
#: `PushError` vocabulary, the hedged nonce, and low-S normalisation.
_N: Final = P256_N
#: The base point, split, because `tests/test_webpush_crypto.py` derives a public
#: key from it by hand to check this module against `_ecverify`.
_GX: Final = P256_G[0]
_GY: Final = P256_G[1]

#: RFC 8291 §4: the encrypted payload a push service must accept.
MAX_PAYLOAD_BYTES: Final = 4096
#: RFC 8292 §2: a VAPID token may not be valid for more than 24 hours.
MAX_VAPID_LIFETIME: Final = 24 * 3600


class PushError(Exception):
    """A subscription, key, or payload that cannot produce a valid push."""


Point = tuple[int, int] | None


def _mul(k: int, point: tuple[int, int]) -> Point:
    """`[k]point`. Every scalar this module multiplies is secret.

    The VAPID private key, the ECDSA nonce and the per-message ephemeral are all
    private, and disclosing any of them is total -- one recovered nonce yields
    the signing key. So this is `p256_scalarmult_secret`, whose iteration count
    and sequence of group operations do not depend on the scalar, and never the
    variable-time `_public` form a verifier is entitled to use. `_curves`'s
    module docstring says what that does and does not guarantee.

    Raises:
        ValueError: `k` is outside `[1, n)`. Callers guard for it and report a
            `PushError`; the two internal callers cannot reach it.
    """
    return p256_scalarmult_secret(k, point)


def _decode_point(raw: bytes) -> tuple[int, int]:
    """Read an uncompressed SEC1 point, checking it is actually on the curve.

    The client's public key arrives from a browser through the application, so
    it is attacker-controlled. A point that is not on P-256 is not a public key:
    it is a value that makes the ECDH below compute in a different, much smaller
    group, which is how an invalid-curve attack recovers a private key one
    subscription at a time.
    """
    if len(raw) != 65 or raw[0] != 0x04:
        raise PushError("a P-256 public key is 65 bytes beginning 0x04")
    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:], "big")
    if not p256_on_curve(x, y):
        raise PushError("the subscription's public key is not a point on P-256")
    return x, y


def _encode_point(point: Point) -> bytes:
    if point is None:
        raise PushError("cannot encode the point at infinity")
    x, y = point
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


# The complete cipher and authentication loops live in `_native/aesgcm.c`.
# Its scalar kernel is always present; AES-NI/PCLMULQDQ replaces it per
# call on capable x86 processors.


#: The GCM tag, in bytes. Fixed by the mode, not a policy of this module.
TAG_BYTES: Final = 16
#: NIST SP 800-38D §5.2.1.1 caps one key/nonce pair at 2^39 - 256 bits of
#: plaintext, past which the counter wraps and the mode stops being AES-GCM.
#: Web push refuses anything over 4096 bytes long before this matters; the bound
#: is enforced anyway, because this is the module's only unbounded input and
#: `python -O` deletes an `assert`.
MAX_GCM_PLAINTEXT_BYTES: Final = (1 << 36) - 32


def _check_gcm_parameters(key: bytes, nonce: bytes, length: int) -> None:
    """Refuse anything the mode is not defined for, before any of it runs.

    Shared by encryption and decryption so refusal wording cannot drift.
    """
    if len(key) != 16:
        raise PushError("AES-128 takes a 16-byte key")
    if len(nonce) != 12:
        raise PushError("this GCM profile takes a 96-bit nonce")
    if length > MAX_GCM_PLAINTEXT_BYTES:
        raise PushError(
            f"AES-GCM is defined for at most {MAX_GCM_PLAINTEXT_BYTES} bytes under "
            f"one key and nonce (NIST SP 800-38D §5.2.1.1); got {length}"
        )


def aes128gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """AES-128-GCM; returns ciphertext || 16-byte tag.

    Pinned against NIST SP 800-38D vectors and OpenSSL outputs by
    `tests/test_aesgcm_parity.py`.

    Raises:
        PushError: the key is not 16 bytes, the nonce is not 12, or the
            plaintext is longer than one key and nonce may cover.
    """
    _check_gcm_parameters(key, nonce, len(plaintext))
    return _core.aes128gcm_encrypt(key, nonce, plaintext, aad)


def aes128gcm_decrypt(key: bytes, nonce: bytes, message: bytes, aad: bytes = b"") -> bytes:
    """The plaintext of `ciphertext || tag`, or a refusal.

    Not used to deliver a push -- the recipient is a browser -- and here because
    a mode that only encrypts cannot be tested for the property that matters
    most: that a message someone has altered is *refused*. The tag comparison is
    constant time on both paths.

    Raises:
        PushError: the key or nonce is the wrong size, the message is shorter
            than its own tag, or the tag does not authenticate it.
    """
    if len(message) < TAG_BYTES:
        raise PushError(f"an AES-GCM message carries a {TAG_BYTES}-byte tag")
    _check_gcm_parameters(key, nonce, len(message) - TAG_BYTES)
    plaintext = _core.aes128gcm_decrypt(key, nonce, message, aad)
    if plaintext is None:
        raise PushError("the AES-GCM tag does not authenticate this message")
    return plaintext


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def _ecdsa_sign(private: int, digest: bytes) -> bytes:
    """Sign a SHA-256 digest, returning the 64-byte JOSE `r || s`.

    The nonce is *hedged*: derived by HMAC from the private key, the digest and
    fresh randomness together. A repeated nonce discloses the private key
    outright, and hedging means that holds only if `os.urandom` fails **and**
    HMAC-SHA256 is broken, rather than if either one is.
    """
    seed = private.to_bytes(32, "big") + digest + os.urandom(32)
    # The three retries below are required by FIPS 186-4 §6.4 and are each
    # reached with probability around 2^-128, so no test can drive them and
    # `wreath mutant` reports them as survivors. They are not redundant: without
    # them a once-in-the-universe nonce would emit a signature that discloses
    # the private key, and the loop is the specified handling rather than
    # defensive padding.
    while True:
        nonce = hmac.new(b"wreath-webpush-nonce", seed, hashlib.sha512).digest()
        signature = _core.curve_p256_sign(private, digest, nonce)
        if signature is not None:
            return signature
        seed = hashlib.sha256(seed).digest()


#: The shared encoder. This was a local copy of the stdlib chain, with the
#: `import base64` deferred into the body to keep it off the import path.
_b64 = b64url_encode


@dataclass(frozen=True, slots=True)
class VapidKeys:
    """The application server's identity to a push service (RFC 8292).

    One keypair, generated once and kept for the life of the application: the
    public half is what a browser pins when it subscribes, so rotating it
    invalidates every existing subscription.

    Args:
        private: The P-256 private scalar.
        subject: A `mailto:` or `https:` URL a push service operator can use to
            reach you about your traffic. Required by RFC 8292 -- some services
            reject a token without one.
    """

    private: int
    subject: str

    def __post_init__(self) -> None:
        if not 1 <= self.private < _N:
            raise PushError("a P-256 private key is in [1, n)")
        if not self.subject.startswith(("mailto:", "https://")):
            raise PushError(
                "the VAPID subject must be a mailto: or https: URL a push service "
                f"operator can contact you at; got {self.subject!r}"
            )

    @classmethod
    def generate(cls, subject: str) -> VapidKeys:
        """Mint a fresh keypair. Store `private_bytes`; it is not recoverable."""
        return cls(secrets.randbelow(_N - 1) + 1, subject)

    @classmethod
    def from_bytes(cls, private: bytes, subject: str) -> VapidKeys:
        """Rebuild from the 32 bytes `private_bytes` returned."""
        return cls(int.from_bytes(private, "big"), subject)

    @property
    def private_bytes(self) -> bytes:
        return self.private.to_bytes(32, "big")

    @property
    def public_bytes(self) -> bytes:
        """The uncompressed public key, which is what a browser subscribes to."""
        return _encode_point(_mul(self.private, P256_G))

    @property
    def application_server_key(self) -> str:
        """The base64url public key to hand `pushManager.subscribe` in a browser."""
        return _b64(self.public_bytes)


def vapid_headers(
    keys: VapidKeys, endpoint: str, *, lifetime: int = 12 * 3600, now: float | None = None
) -> dict[str, str]:
    """The `Authorization` header authenticating one push to `endpoint`.

    The token's audience is the push service's *origin*, not the full endpoint,
    and a token minted for one service is refused by another.

    Raises:
        PushError: `lifetime` exceeds the 24 hours RFC 8292 permits, or the
            endpoint has no origin.
    """
    if lifetime > MAX_VAPID_LIFETIME:
        raise PushError(
            f"a VAPID token may not live longer than {MAX_VAPID_LIFETIME} seconds "
            f"(RFC 8292 §2); got {lifetime}"
        )
    scheme, _, rest = endpoint.partition("://")
    if not rest:
        raise PushError(f"push endpoint {endpoint!r} has no scheme")
    audience = f"{scheme}://{rest.split('/', 1)[0]}"
    issued = int(time.time() if now is None else now)
    header = _b64(b'{"typ":"JWT","alg":"ES256"}')
    claims = _b64(
        json.dumps(
            {"aud": audience, "exp": issued + lifetime, "sub": keys.subject},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = _ecdsa_sign(keys.private, hashlib.sha256(signing_input).digest())
    token = f"{header}.{claims}.{_b64(signature)}"
    return {"Authorization": f"vapid t={token}, k={keys.application_server_key}"}


@dataclass(frozen=True, slots=True)
class PushSubscription:
    """What a browser's `PushSubscription.toJSON()` carries.

    Store it whole. `endpoint` identifies the push service *and* the
    subscription, so it is the natural key, and `p256dh`/`auth` are the material
    without which a payload cannot be encrypted for this recipient.
    """

    endpoint: str
    #: The client's public key, base64url, unpadded — 65 bytes decoded.
    p256dh: str
    #: The client's 16-byte shared authentication secret, base64url.
    auth: str

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> PushSubscription:
        """Build from the exact shape `PushSubscription.toJSON()` produces."""
        keys = payload.get("keys")
        if not isinstance(keys, dict):
            raise PushError("a push subscription needs a 'keys' object")
        endpoint = payload.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise PushError("a push subscription needs an https endpoint")
        return cls(endpoint, str(keys.get("p256dh", "")), str(keys.get("auth", "")))

    def client_key(self) -> bytes:
        return b64url_decode(self.p256dh)

    def auth_secret(self) -> bytes:
        secret = b64url_decode(self.auth)
        if len(secret) != 16:
            raise PushError("the subscription auth secret is 16 bytes")
        return secret


@dataclass(frozen=True, slots=True)
class PushResult:
    """What a push service said.

    `expired` is the one a caller must act on: `404` or `410` means the
    subscription is permanently gone and every future send to it will fail the
    same way. Delete it.
    """

    status: int
    expired: bool
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return 200 <= self.status < 300


def encrypt(
    subscription: PushSubscription,
    payload: bytes,
    *,
    salt: bytes | None = None,
    ephemeral: int | None = None,
) -> bytes:
    """Encrypt `payload` for one subscription, as the `aes128gcm` body.

    RFC 8291 over RFC 8188: an ephemeral P-256 keypair per message, ECDH against
    the subscription's key, HKDF twice, then one AES-128-GCM record whose header
    carries the salt and the ephemeral public key.

    Args:
        subscription: The recipient.
        payload: The cleartext, usually a JSON notification.
        salt: The 16-byte record salt. Generated when omitted; pass it only to
            reproduce a known-answer vector.
        ephemeral: The sender's per-message private scalar. Generated when
            omitted, and **must** be, outside a test: reusing one across two
            messages to the same recipient reuses the content key and nonce
            with it, which is the failure mode GCM does not survive. It exists
            so the RFC 8291 §5 vector can be reproduced exactly.

    Raises:
        PushError: the subscription's key is malformed or off-curve, or the
            encrypted result would exceed `MAX_PAYLOAD_BYTES`.
    """
    client_public = subscription.client_key()
    client_point = _decode_point(client_public)
    auth_secret = subscription.auth_secret()
    record_salt = secrets.token_bytes(16) if salt is None else salt
    if len(record_salt) != 16:
        raise PushError("the record salt is 16 bytes")

    scalar = secrets.randbelow(_N - 1) + 1 if ephemeral is None else ephemeral
    if not 1 <= scalar < _N:
        # Only reachable from a caller passing `ephemeral` explicitly, which is
        # the reproduce-a-vector escape hatch. `_mul` would refuse it too, with
        # a `ValueError` that does not say which argument was wrong.
        raise PushError("a P-256 ephemeral scalar is in [1, n)")
    server_public = _encode_point(_mul(scalar, P256_G))
    shared = _mul(scalar, client_point)
    if shared is None:
        # Unreachable at run time, and kept deliberately. P-256 has prime order,
        # `_decode_point` has already refused anything not on the curve, and the
        # scalar is drawn from [1, n), so the product cannot be the identity --
        # `wreath mutant` reports removing this raise as a survivor for exactly
        # that reason. It stays because `_mul` is typed `Point | None` and the
        # next line indexes it: deleting the guard moves the failure from a
        # named refusal to a `TypeError` on `None`.
        raise PushError("ECDH produced the point at infinity")
    shared_secret = shared[0].to_bytes(32, "big")

    # RFC 8291 §3.4. The key_info binds both public keys into the derivation, so
    # a secret computed against a different recipient cannot decrypt this.
    key_info = b"WebPush: info\x00" + client_public + server_public
    ikm = _hkdf(auth_secret, shared_secret, key_info, 32)
    cek = _hkdf(record_salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(record_salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # RFC 8188 §2: 0x02 is the delimiter on the last (here, only) record.
    ciphertext = aes128gcm_encrypt(cek, nonce, payload + b"\x02")
    body = (
        record_salt
        + (MAX_PAYLOAD_BYTES).to_bytes(4, "big")
        + bytes([len(server_public)])
        + server_public
        + ciphertext
    )
    if len(body) > MAX_PAYLOAD_BYTES:
        raise PushError(
            f"the encrypted push payload is {len(body)} bytes, over the "
            f"{MAX_PAYLOAD_BYTES}-byte limit every push service enforces; send an "
            "identifier and let the client fetch the detail"
        )
    return body


def declarative_payload(
    title: str,
    *,
    body: str = "",
    navigate: str,
    app_badge: int | None = None,
    mutable: bool = False,
) -> bytes:
    """A Declarative Web Push notification, as JSON bytes.

    The format Safari 18.4+ displays **without a service worker** -- the payload
    itself declares the notification, so there is no JavaScript to wake, none to
    get wrong, and none to fail silently on a locked phone. It is a W3C Working
    Draft with multi-vendor editorship, and it is not yet universal: send this
    shape and keep a service worker for the browsers that still need one. A
    client that does not understand it falls back to its `push` event, which is
    why `mutable` exists.

    Args:
        title: The notification title.
        body: The line under the title.
        navigate: Where tapping it goes. Required by the format -- a declarative
            notification with nowhere to go is not displayed at all.
        app_badge: The number to show on the app icon.
        mutable: Whether a service worker may rewrite this before display.
    """
    notification: dict[str, object] = {"title": title, "navigate": navigate}
    if body:
        notification["body"] = body
    if app_badge is not None:
        notification["app_badge"] = app_badge
    document: dict[str, object] = {"web_push": 8030, "notification": notification}
    if mutable:
        notification["mutable"] = True
    return json.dumps(document, separators=(",", ":")).encode("utf-8")
