"""Strict, unpadded base64url, both directions, vectorised where the build has it.

One module for base64 so there is one answer to "how does wreath encode this".
The decode half came first and the encode half followed the same evidence; the
whole argument for the decode half is below, and the encode half's numbers are
recorded on `b64url_encode` itself.


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
from typing import Any

from ._native import _core

#: Resolved once, with `getattr` rather than an attribute access, so a `_core`
#: built before `jose.c` still imports and simply takes the twin below.
_native_b64url = (
    getattr(_core, "jose_b64url_decode", None) if _core is not None else None
)

#: `codecs.c`'s encoder, which takes the alphabet and the padding as flags and
#: runs `wreath_b64_encode` -- AVX2 where the build has it. Resolved the same
#: way and for the same reason as the decoder above.
_native_encode = getattr(_core, "b64encode", None) if _core is not None else None

#: The alphabet, and nothing else -- no `=`, so padded input is refused by both
#: twins rather than accepted by one.
#:
#: Exported because `wreath._auth.jwt` needs the same set for a *segment*
#: charset check that this module's decoder does not perform for it, and two
#: frozensets spelling one alphabet is how they drift apart later.
B64URL_ALPHABET = frozenset(
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
    if not B64URL_ALPHABET.issuperset(data):
        raise ValueError("invalid base64url")
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except binascii.Error:
        # A length one more than a multiple of four: every character is in the
        # alphabet and it is still not a base64 string. Native reports it the
        # same way, so the twins agree on the message as well as the type.
        raise ValueError("invalid base64url") from None


def _b64url_encode_native(raw: bytes, _encode: Any = _native_encode) -> str:
    # The accelerator is captured as a default rather than read from the module
    # globals on every call: it is bound once at definition, which is one
    # dictionary lookup fewer per encode and, incidentally, the only spelling
    # `ty` accepts -- it cannot narrow a module-level `Any | None` across a
    # function boundary, and an `assert` would vanish under `python -O`.
    return _encode(raw, urlsafe=True, pad=False)


def _b64url_encode_pure(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_encode_pure(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# The arm is chosen once at import rather than per call, which is the idiom the
# rest of the tree uses and the reason it matters here: `binding` encodes on the
# response path and `rooms` once per room per broadcast, and both used to bind
# `_core.b64encode` directly. Routing them through a module-level function that
# re-tests `_native_encode` on every call would have added a Python frame to
# each of those, which is the opposite of the point.
#
# `b64_encode` *is* the native callable when there is one: `b64encode(data)`
# already defaults to `urlsafe=False, pad=True`, so no wrapper is needed and
# none is added. `b64url_encode` needs the two flags, so it keeps its wrapper --
# and the measurement below was taken through that wrapper, not around it.
if _native_encode is not None:
    #: Unpadded base64url. Five modules each kept a copy of this three-step
    #: stdlib chain -- `_userkit._b64`, `_webauthn.b64url_encode`,
    #: `_webpush._b64`, and an inline spelling in the session cookie and in
    #: PKCE -- while the native primitive with exactly these flags was already
    #: built and exposed.
    #:
    #: Measured against that chain over three runs, with an A/A floor of
    #: 0.0-1.1%: 8.5-14.4% faster on 16 bytes, 17.0-19.6% on 32, 21.6-33.7% on
    #: 64, 56.6-58.5% on 256, and 86.1-86.2% on 4 KiB. The floor is what makes
    #: the small end meaningful rather than noise; the large end is the AVX2 arm.
    b64url_encode = _b64url_encode_native
    #: Standard, padded base64: a value going into a JSON string rather than
    #: into a URL or a header -- the stream chunk transport, the room fan-out,
    #: and `binding`'s `bytes` response fields.
    b64_encode = _native_encode
else:  # pragma: no cover - exercised by the WREATH_PURE parity run
    b64url_encode = _b64url_encode_pure
    b64_encode = _b64_encode_pure

# The twins are not a strictness boundary, unlike the decode direction:
# `urlsafe_b64encode` is total over `bytes` and has exactly one answer, so the
# two arms cannot disagree about a *value* the way they could about which
# inputs to refuse. `tests/test_b64.py` differentially tests them anyway,
# naming both functions directly rather than monkeypatching the selection --
# a test that patches the arm is asserting the patch ran.


__all__ = [
    "B64URL_ALPHABET",
    "MAX_INPUT_BYTES",
    "b64_encode",
    "b64url_decode",
    "b64url_encode",
]
