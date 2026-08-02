"""Web Push (RFC 8030/8291/8292) with no dependency at all.

Every other Python web-push implementation reaches for `pywebpush` and
`cryptography`. Wreath cannot, and it turns out not to need to: the whole
protocol is four primitives, three of which CPython already ships.

* **VAPID** (RFC 8292) is an ECDSA P-256 keypair and a signed JWT.
* **Message encryption** (RFC 8291) is ECDH on P-256, then HKDF-SHA256, then
  AES-128-GCM over the `aes128gcm` content coding of RFC 8188.
* **Delivery** is an HTTP POST to whatever endpoint the browser handed you.

`hashlib` and `hmac` cover SHA-256, HMAC and therefore HKDF. The elliptic-curve
arithmetic and AES-GCM are here because the stdlib has neither -- there is no
AES anywhere in CPython, which is the single reason this module is longer than
it looks like it should be. The AES-GCM below is now the *twin* of a hardware
implementation in `src/wreath/_native/aesgcm.c` rather than the only one; it
still ships, because a CPU without AES-NI still has to encrypt a push.

**What is not here, deliberately.** No key generation for the *client* half, no
subscription storage policy, and no retry loop: a push is delivered by a
`wreath.jobs` job, which already owns retries, backoff and dead-lettering.

Two things the protocol makes non-optional and implementations routinely skip:

* A `404` or `410` from the push service means the subscription is gone and the
  caller **must** stop using it. `PushResult.expired` says so, and the shipped
  channel prunes on it -- a store that never prunes is a slow leak plus a rising
  error rate nobody attributes.
* The payload is capped at 4096 octets *after* encryption. That is checked when
  the message is built rather than when it is sent, so an over-size notification
  is a programming error at declaration time instead of a delivery failure in a
  job an hour later.

The P-256 arithmetic used to duplicate the private half of
`wreath._auth._ecverify`, which is verify-only by design and kept its group
operations private, so a module that had to *sign* wrote its own. Both now share
`wreath._curves`. What stays here is what is specific to this protocol: SEC1
encoding in a `PushError` vocabulary, the hedged nonce, and low-S normalisation.
Every scalar this module multiplies is secret, so it calls the constant-shape
`p256_scalarmult_secret` and never the variable-time form a verifier uses.

## What this costs, measured

Medians of 15 interleaved rounds on a 12-core x86-64 box, 2026-08-02, against
an A/A control that put the noise floor at 2.0-11.1 us (0.02-0.09% of the pure
baseline). Two runs minutes apart reproduced the native arm exactly and the
facade arm to 0.02 us, which is what makes the ratio worth quoting at all.

| Operation | pure Python | AES-NI + PCLMULQDQ |
| --- | --- | --- |
| AES-128-GCM over 4000 B (the payload cap) | 11.58 ms | 4.03 us |
| ... entered through `aes128gcm_encrypt` | 11.58 ms | 4.15 us |
| AES-128-GCM over 200 B | 705 us | 0.44 us |

The AES was ablated rather than profiled, per `AGENTS.md`: of the 14.5 ms a
4000-byte record took, the block cipher was 81% and GHASH 31%. Both halves have
a hardware instruction -- `aesenc` for the cipher, `pclmulqdq` for GHASH's
carry-less multiply -- so `src/wreath/_native/aesgcm.c` is where they now run,
and this file keeps the Python as the twin `tests/test_aesgcm_parity.py` holds
byte-for-byte equal to it. The 0.12 us between the two rows is the argument
check and the dispatch, which is the whole cost of keeping the twin.

**That is the AES only, and saying more would overstate it.** Ablating one whole
push over a 3900-byte payload, in the same interleaved run:

| Arm | Cost |
| --- | --- |
| `encrypt()` end to end | 4.32 ms |
| ... its two P-256 scalar multiplications alone | 4.32 ms |
| ... its AES-128-GCM alone | 4.54 us |

So the curve *is* the push now: the two arms are indistinguishable at a 79.8 us
floor, and the AES is three orders of magnitude below either. A fan-out of
10,000 maximum-size pushes loses about two minutes of block cipher and keeps
about forty-three seconds of elliptic curve.

Read those two rows as belonging to different owners. The AES was worth moving
because one C call replaced an interpreter loop running over every byte; the
curve is a scalar multiplication in `wreath._curves`, shared with
`_auth/_ecverify` and `_dkim`, and whatever happens to it next happens there.

## What is constant time here, and what is not

**The pure code below is not, and never was.** `_SBOX` is a byte table indexed
by secret state, so an attacker who can observe the cache learns about the key;
`_gf_mul` branches on the bits of its operand. The native path *is* -- `aesenc`
and `pclmulqdq` are fixed-latency instructions with no data-dependent memory
access, and the tag comparison is branch-free on both paths (`hmac.compare_digest`
here, an XOR-accumulate in C).

Which one runs depends on the CPU, so "Wreath's AES-GCM is constant time" is not
a true sentence. It is true of a machine with AES-NI and PCLMULQDQ, and false of
one without, and `_core.aesgcm_arms()` is how you tell which you have.

The elliptic curve is a weaker claim than the native AES and a stronger one than
it used to be. The old `while k: if k & 1:` here leaked the scalar's bit length
through the iteration count and its Hamming weight through the branch;
`p256_scalarmult_secret` removes both, running a fixed 257-step ladder with
mask-selected registers. What it cannot remove is CPython's own big-integer
arithmetic, whose timing depends on operand magnitude, so **this is not
constant-time code and `_curves` does not claim it is** -- it is code whose
control flow the secret does not steer.
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

from ._b64 import b64url_decode
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

# --- NIST P-256 -------------------------------------------------------------

#: The curve constants, the group law and the two scalar multiplications all
#: live in `wreath._curves`, shared with `_auth/_ecverify` and `_dkim`. What
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
    module docstring says what that does and does not buy in pure Python.

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


# --- AES-128 ----------------------------------------------------------------
#
# Encryption only: GCM never runs the block cipher backwards, so there is no
# decryption path here to get wrong or to maintain.

_SBOX: Final = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_RCON: Final = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(value: int) -> int:
    value <<= 1
    return (value ^ 0x1B) & 0xFF if value & 0x100 else value


def _expand_key(key: bytes) -> list[list[int]]:
    """Expand a 16-byte key into eleven round keys.

    No length check: every caller has already been through
    `_check_gcm_parameters`, which refuses anything that is not 16 bytes with a
    message naming the algorithm. A second check here was a mutant survivor, and
    two spellings of one condition is how they drift apart.
    """
    words = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    for index in range(4, 44):
        word = list(words[index - 1])
        if index % 4 == 0:
            word = word[1:] + word[:1]
            word = [_SBOX[b] for b in word]
            word[0] ^= _RCON[index // 4 - 1]
        words.append([a ^ b for a, b in zip(words[index - 4], word, strict=True)])
    return [sum(words[r * 4 : r * 4 + 4], []) for r in range(11)]


def _encrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    state = [b ^ k for b, k in zip(block, round_keys[0], strict=True)]
    for rnd in range(1, 11):
        state = [_SBOX[b] for b in state]
        # ShiftRows, in column-major order: byte at (row, col) is state[col*4+row].
        state = [state[(i + (i % 4) * 4) % 16] for i in range(16)]
        if rnd != 10:
            mixed: list[int] = []
            for col in range(4):
                a = state[col * 4 : col * 4 + 4]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                mixed += [
                    a[0] ^ t ^ _xtime(a[0] ^ a[1]),
                    a[1] ^ t ^ _xtime(a[1] ^ a[2]),
                    a[2] ^ t ^ _xtime(a[2] ^ a[3]),
                    a[3] ^ t ^ _xtime(a[3] ^ a[0]),
                ]
            state = mixed
        state = [b ^ k for b, k in zip(state, round_keys[rnd], strict=True)]
    return bytes(state)


# --- GCM --------------------------------------------------------------------

_GCM_R: Final = 0xE1000000000000000000000000000000


def _gf_mul(x: int, y: int) -> int:
    """Multiply in GF(2^128) with GCM's bit ordering (NIST SP 800-38D §6.3)."""
    z = 0
    v = y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        v = (v >> 1) ^ _GCM_R if v & 1 else v >> 1
    return z


def _ghash(h: int, data: bytes) -> int:
    y = 0
    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16].ljust(16, b"\x00")
        y = _gf_mul(y ^ int.from_bytes(block, "big"), h)
    return y


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

    Shared by both implementations and by both directions, so the native and
    pure paths refuse identical inputs in identical words -- which is the only
    way a differential test can compare their *failures* as well as their
    outputs.
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


def _ctr_pure(round_keys: list[list[int]], nonce: bytes, data: bytes) -> bytes:
    """XOR `data` with the AES-CTR keystream from counter 2 onwards.

    Its own inverse, which is why decryption needs no backwards block cipher.
    """
    out = bytearray()
    counter = int.from_bytes(nonce + b"\x00\x00\x00\x01", "big")
    for offset in range(0, len(data), 16):
        counter = (counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF)
        stream = _encrypt_block(round_keys, counter.to_bytes(16, "big"))
        chunk = data[offset : offset + 16]
        out += bytes(a ^ b for a, b in zip(chunk, stream, strict=False))
    return bytes(out)


def _gcm_tag_pure(
    round_keys: list[list[int]], nonce: bytes, ciphertext: bytes, aad: bytes
) -> bytes:
    """GHASH over the AAD and the ciphertext, masked with E(J0)."""
    h = int.from_bytes(_encrypt_block(round_keys, b"\x00" * 16), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    lengths = (len(aad) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    hashed = _ghash(
        h,
        aad
        + b"\x00" * (-len(aad) % 16)
        + ciphertext
        + b"\x00" * (-len(ciphertext) % 16)
        + lengths,
    )
    tag = hashed ^ int.from_bytes(_encrypt_block(round_keys, j0), "big")
    return tag.to_bytes(TAG_BYTES, "big")


def _aes128gcm_encrypt_pure(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    round_keys = _expand_key(key)
    ciphertext = _ctr_pure(round_keys, nonce, plaintext)
    return ciphertext + _gcm_tag_pure(round_keys, nonce, ciphertext, aad)


def _aes128gcm_decrypt_pure(
    key: bytes, nonce: bytes, message: bytes, aad: bytes
) -> bytes | None:
    """The plaintext, or None when the tag does not verify.

    None rather than an exception so that the C twin -- which cannot raise
    Wreath's error type without a Python call -- refuses in the same words. The
    caller below owns the message.
    """
    ciphertext, tag = message[:-TAG_BYTES], message[-TAG_BYTES:]
    round_keys = _expand_key(key)
    if not hmac.compare_digest(tag, _gcm_tag_pure(round_keys, nonce, ciphertext, aad)):
        return None
    return _ctr_pure(round_keys, nonce, ciphertext)


# The hardware path, bound once at import. `aesgcm_arms()` is empty on a CPU
# without AES-NI and PCLMULQDQ and on a build that could not compile them, and
# `_core` is None under WREATH_PURE=1 -- all three land on the twin above.
# `getattr` rather than a bare attribute because an in-place `.so` older than
# this module is a routine state in a development tree, not a broken install.
_native_gcm_encrypt = None
_native_gcm_decrypt = None
if _core is not None and getattr(_core, "aesgcm_arms", tuple)():
    _native_gcm_encrypt = _core.aes128gcm_encrypt
    _native_gcm_decrypt = _core.aes128gcm_decrypt


def aes128gcm_encrypt(
    key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b""
) -> bytes:
    """AES-128-GCM; returns ciphertext || 16-byte tag.

    Written out because CPython ships no AES at all. Runs on AES-NI and
    PCLMULQDQ where the CPU has them, and on the pure twin above otherwise;
    `tests/test_aesgcm_parity.py` holds the two byte-for-byte equal, and both
    are pinned against the NIST SP 800-38D vectors and against `cryptography`.

    Raises:
        PushError: the key is not 16 bytes, the nonce is not 12, or the
            plaintext is longer than one key and nonce may cover.
    """
    _check_gcm_parameters(key, nonce, len(plaintext))
    if _native_gcm_encrypt is not None:
        return _native_gcm_encrypt(key, nonce, plaintext, aad)
    return _aes128gcm_encrypt_pure(key, nonce, plaintext, aad)


def aes128gcm_decrypt(
    key: bytes, nonce: bytes, message: bytes, aad: bytes = b""
) -> bytes:
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
    if _native_gcm_decrypt is not None:
        plaintext = _native_gcm_decrypt(key, nonce, message, aad)
    else:
        plaintext = _aes128gcm_decrypt_pure(key, nonce, message, aad)
    if plaintext is None:
        raise PushError("the AES-GCM tag does not authenticate this message")
    return plaintext


# --- HKDF (RFC 5869) --------------------------------------------------------


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


# --- ECDSA P-256 signing ----------------------------------------------------


def _ecdsa_sign(private: int, digest: bytes) -> bytes:
    """Sign a SHA-256 digest, returning the 64-byte JOSE `r || s`.

    The nonce is *hedged*: derived by HMAC from the private key, the digest and
    fresh randomness together. A repeated nonce discloses the private key
    outright, and hedging means that holds only if `os.urandom` fails **and**
    HMAC-SHA256 is broken, rather than if either one is.
    """
    z = int.from_bytes(digest, "big")
    seed = private.to_bytes(32, "big") + digest + os.urandom(32)
    # The three retries below are required by FIPS 186-4 §6.4 and are each
    # reached with probability around 2^-128, so no test can drive them and
    # `wreath mutant` reports them as survivors. They are not redundant: without
    # them a once-in-the-universe nonce would emit a signature that discloses
    # the private key, and the loop is the specified handling rather than
    # defensive padding.
    while True:
        k = int.from_bytes(hmac.new(b"wreath-webpush-nonce", seed, hashlib.sha512).digest(), "big")
        k %= _N - 1
        k += 1
        point = _mul(k, P256_G)
        if point is None:
            seed = hashlib.sha256(seed).digest()
            continue
        r = point[0] % _N
        if r == 0:
            seed = hashlib.sha256(seed).digest()
            continue
        s = pow(k, -1, _N) * (z + r * private) % _N
        if s == 0:
            seed = hashlib.sha256(seed).digest()
            continue
        # Low-S, as JOSE and every modern verifier expect. Both forms are valid
        # ECDSA; publishing the high one makes signatures malleable for no gain.
        if s > _N // 2:
            s = _N - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# --- VAPID ------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


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


# --- subscriptions and encryption -------------------------------------------


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
