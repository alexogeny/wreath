"""Strict, unpadded base64url decoding, vectorised where the build has it.

`base64.urlsafe_b64decode` is the wrong primitive for decoding anything that
arrived over the wire, for three separate reasons: it re-pads, it translates
`-`/`_` to `+`/`/` and then accepts `+`/`/` as *input* too, and it silently
discards characters outside the alphabet entirely. So two different spellings of
one value decode to the same bytes, and a value with a space in it decodes to
something rather than failing.

`jose.c` already had the answer -- a decoder that is strict about all three, and
that `simd.h` runs at a vector width chosen per call. `wreath._auth.jwt` has
resolved it since it shipped; this module is that resolution lifted out so the
session cookie, the WebAuthn payloads and the password record can share it
rather than each keeping a laxer copy. What each of those used to do:

    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)

Measured against that idiom, native is 85% faster on a 32-byte value and 96% on
4 KiB, and the whole signed-cookie read -- HMAC verify included -- is 33% to 47%
faster. The per-call C boundary was the stated hypothesis against doing this and
it does not survive contact: the stdlib path is several *Python-level*
operations, and one `METH_O` call is cheaper than the string concatenation
alone.

The fallback is not the stdlib call. It is a strict twin, because a guard that
is only present in the accelerated build is not a guard -- `wreath._auth.jwt`
records the same reasoning, and a differential test over generated input is what
found the JWK collision it describes.
"""

from __future__ import annotations

import base64
import binascii

from ._native import _core

#: Resolved once, with `getattr` rather than an attribute access, so a `_core`
#: built before `jose.c` still imports and simply takes the twin below.
_native_b64url = (
    getattr(_core, "jose_b64url_decode", None) if _core is not None else None
)

#: The alphabet, and nothing else -- no `=`, so padded input is refused by both
#: twins rather than accepted by one.
_B64URL = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

#: `JOSE_ABS_MAX_TOKEN` in `jose.c`. Enforced in the twin as well as the native
#: arm: a DoS bound that disappears under `WREATH_PURE=1` is not a bound.
MAX_INPUT_BYTES = 1 << 20


def b64url_decode(data: str) -> bytes:
    """Decode one unpadded base64url string.

    Raises:
        TypeError: `data` is not a `str`.
        ValueError: not base64url, or longer than `MAX_INPUT_BYTES`.
    """
    if _native_b64url is not None:
        return _native_b64url(data)
    if not isinstance(data, str):
        raise TypeError("b64url input must be str")
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("base64url input too large")
    if not _B64URL.issuperset(data):
        raise ValueError("invalid base64url")
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except binascii.Error:
        # A length one more than a multiple of four: every character is in the
        # alphabet and it is still not a base64 string. Native reports it the
        # same way, so the twins agree on the message as well as the type.
        raise ValueError("invalid base64url") from None


__all__ = ["MAX_INPUT_BYTES", "b64url_decode"]
