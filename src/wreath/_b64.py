"""Strict, unpadded base64url, both directions, vectorised where the CPU allows.

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

`jose.c` is strict about all three, and `simd.h` runs it at a vector width
chosen per call. `wreath._auth.jwt` has resolved it since it shipped; this
module is that resolution lifted out so the session cookie, the WebAuthn
payloads and the password record share it rather than each keeping a laxer copy.
What each of those used to do:

    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)

Measured against that idiom, this is 85% faster on a 32-byte value and 96% on
4 KiB, and the whole signed-cookie read -- HMAC verify included -- is 33% to 47%
faster. The per-call C boundary was the stated hypothesis against doing this and
it does not survive contact: the stdlib path is several *Python-level*
operations, and one `METH_O` call is cheaper than the string concatenation
alone.
"""

from __future__ import annotations

from typing import Any

from ._native import _core

#: The alphabet, and nothing else -- no `=`, so padded input is refused.
#:
#: Exported because `wreath._auth.jwt` needs the same set for a *segment*
#: charset check that this module's decoder does not perform for it, and two
#: frozensets spelling one alphabet is how they drift apart later.
B64URL_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

#: `JOSE_ABS_MAX_TOKEN` in `jose.c`, restated here for callers that want to
#: refuse an oversized value before handing it over.
MAX_INPUT_BYTES = 1 << 20

#: Decode one unpadded base64url string to bytes.
#:
#: Raises `TypeError` for a non-`str`, and `ValueError` for anything that is not
#: base64url or is longer than `MAX_INPUT_BYTES`.
b64url_decode: Any = _core.jose_b64url_decode

#: `codecs.c`'s encoder, which takes the alphabet and the padding as flags and
#: runs `wreath_b64_encode` -- AVX2 where the CPU has it.
_b64encode = _core.b64encode


def b64url_encode(raw: bytes, _encode: Any = _b64encode) -> str:
    """Unpadded base64url.

    Five modules each kept a copy of the three-step stdlib chain this replaces
    -- `_userkit._b64`, `_webauthn.b64url_encode`, `_webpush._b64`, and an
    inline spelling in the session cookie and in PKCE -- while the primitive
    with exactly these flags was already built and exposed.

    Measured against that chain over three runs, with an A/A floor of 0.0-1.1%:
    8.5-14.4% faster on 16 bytes, 17.0-19.6% on 32, 21.6-33.7% on 64, 56.6-58.5%
    on 256, and 86.1-86.2% on 4 KiB. The floor is what makes the small end
    meaningful rather than noise; the large end is the AVX2 arm. The measurement
    was taken through this wrapper, not around it.

    `_encode` is captured as a default rather than read from the module globals
    on every call: bound once at definition, it is one dictionary lookup fewer
    per encode, and `binding` encodes on the response path while `rooms` encodes
    once per room per broadcast.
    """
    return _encode(raw, urlsafe=True, pad=False)


#: Standard, padded base64: a value going into a JSON string rather than into a
#: URL or a header -- the stream chunk transport, the room fan-out, and
#: `binding`'s `bytes` response fields. `b64encode(data)` already defaults to
#: `urlsafe=False, pad=True`, so this needs no wrapper and has none.
b64_encode = _b64encode


__all__ = [
    "B64URL_ALPHABET",
    "MAX_INPUT_BYTES",
    "b64_encode",
    "b64url_decode",
    "b64url_encode",
]
