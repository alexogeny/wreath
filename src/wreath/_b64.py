"""Strict base64 codecs, vectorised where the CPU allows.

`base64.urlsafe_b64decode` is the wrong primitive for decoding anything that
arrived over the wire, for three separate reasons: it re-pads, it translates
`-`/`_` to `+`/`/` and then accepts `+`/`/` as *input* too, and it silently
discards characters outside the alphabet entirely. So two different spellings of
one value decode to the same bytes, and a value with a space in it decodes to
something rather than failing.

`jose.c` enforces those constraints and `simd.h` chooses a vector width per call.
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
    """Encode unpadded base64url."""
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
