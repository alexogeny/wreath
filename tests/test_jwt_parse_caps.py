from __future__ import annotations

import base64
import json
import re

import pytest

from wreath._auth.jwt import _parse_compact

# Mirrors of the module's own caps. Duplicated deliberately: importing them would make
# a test that still passes if a cap were lowered to zero, and the point of a bound is
# the number.
MAX_SEGMENT_BYTES = 16 * 1024
MAX_TOKEN_BYTES = 1 << 20
#: The longest base64url segment that is *accepted*. Both twins refuse when
#: `len // 4 * 3 > MAX_SEGMENT_BYTES`, so this is the largest length that does not.
MAX_SEGMENT_B64_ACCEPTED = 21847
assert MAX_SEGMENT_B64_ACCEPTED // 4 * 3 <= MAX_SEGMENT_BYTES
assert (MAX_SEGMENT_B64_ACCEPTED + 1) // 4 * 3 > MAX_SEGMENT_BYTES


def _segment(payload: object) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


#: Every malformed token, with the guard that must refuse it and the words it uses.
#:
#: "Exactly two dots" (wrong separator count) is deliberately a different message
#: from "has an empty segment" (right count, empty part): they are different
#: diagnoses, and answering both with one told the caller less.
MALFORMED = [
    # Past the whole-token ceiling, before any splitting happens.
    (
        "a." + "b" * MAX_TOKEN_BYTES + ".c",
        "token over the total cap",
        "compact JWT exceeds maximum size",
    ),
    # Wrong segment count, both directions.
    ("a.b", "two segments", "compact JWT must have exactly two dots"),
    ("a", "no dots at all", "compact JWT must have exactly two dots"),
    ("a.b.c.d", "four segments", "compact JWT must have exactly two dots"),
    # Right count, one segment empty. An empty signature is the classic `alg=none`
    # shape, so this refusal has a history.
    ("a.b.", "empty signature", "compact JWT has an empty segment"),
    (".b.c", "empty header", "compact JWT has an empty segment"),
    ("a..c", "empty payload", "compact JWT has an empty segment"),
    ("..", "all three empty", "compact JWT has an empty segment"),
    # An oversized segment inside a token under the total cap: the guard the source
    # comment singles out, a giant segment reaching the JSON parser. The one message
    # the two twins share.
    (
        "a." + "b" * (MAX_SEGMENT_B64_ACCEPTED + 1) + ".c",
        "segment over the per-segment cap",
        "JWT segment exceeds size cap",
    ),
    # Outside unpadded base64url. "=" matters most: accepting padding would give one
    # token two spellings, and a signature covers the bytes that were sent.
    ("a=.b.c", "base64 padding", "a compact JWT segment must be unpadded base64url"),
    ("a+b.c.d", "standard-base64 plus", "a compact JWT segment must be unpadded base64url"),
    ("a/b.c.d", "standard-base64 slash", "a compact JWT segment must be unpadded base64url"),
    ("a b.c.d", "a space", "a compact JWT segment must be unpadded base64url"),
    ("aé.b.c", "a non-ASCII character", "a compact JWT segment must be unpadded base64url"),
]


@pytest.mark.parametrize(
    "token,why,message",
    MALFORMED,
    ids=[case[1] for case in MALFORMED],
)
def test_a_malformed_compact_token_is_refused_by_the_right_guard(
    token: str, why: str, message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        _parse_compact(token)


def test_the_per_segment_cap_is_reported_in_its_own_words() -> None:
    with pytest.raises(ValueError, match="segment exceeds size cap"):
        _parse_compact("a." + "b" * (MAX_SEGMENT_B64_ACCEPTED + 1) + ".c")


def test_a_header_or_payload_that_is_not_a_json_object_is_refused() -> None:
    for header, claims in (([1], {}), ({}, [1]), ("str", {}), (1, {})):
        token = f"{_segment(header)}.{_segment(claims)}.AAAA"
        with pytest.raises(ValueError, match="must be JSON objects"):
            _parse_compact(token)


def test_a_well_formed_token_parses_and_its_signing_input_is_the_first_two_segments() -> None:
    header = _segment({"alg": "HS256", "typ": "JWT"})
    claims = _segment({"sub": "u1"})
    token = f"{header}.{claims}.AAAA"

    parsed_header, parsed_claims, signing_input, signature = _parse_compact(token)

    assert parsed_header == {"alg": "HS256", "typ": "JWT"}
    assert parsed_claims == {"sub": "u1"}
    assert signing_input == f"{header}.{claims}".encode()
    assert signature == b"\x00\x00\x00"


def test_the_per_segment_cap_holds_at_exactly_the_boundary() -> None:
    with pytest.raises(ValueError) as accepted:
        _parse_compact("a" * MAX_SEGMENT_B64_ACCEPTED + ".b.c")
    assert "size cap" not in str(accepted.value)

    with pytest.raises(ValueError, match="size cap"):
        _parse_compact("a" * (MAX_SEGMENT_B64_ACCEPTED + 1) + ".b.c")


def test_a_bad_base64_length_is_refused_in_wreath_s_words() -> None:
    for segment in ("b", "abcde"):
        assert len(segment) % 4 == 1, segment
        with pytest.raises(ValueError, match="must be unpadded base64url"):
            _parse_compact(f"{segment}.AAAA.AAAA")


def test_a_valid_base64_length_gets_past_the_decoder() -> None:
    for segment in ("ab", "abc", "abcd"):
        assert len(segment) % 4 != 1, segment
        with pytest.raises(ValueError) as raised:
            _parse_compact(f"{segment}.AAAA.AAAA")
        assert "unpadded base64url" not in str(raised.value), segment


# Found by differentially testing the two twins over generated input, not by a failing
# test. Native `jose_b64url_decode` documents itself "strict, unpadded, URL-safe"; the
# pure twin was `base64.urlsafe_b64decode` with re-padding, which is none of those --
# it re-pads, translates `-`/`_` to `+`/`/` and then accepts `+`/`/` as input too, and
# discards characters outside the alphabet.
# `_parse_compact` was not affected: it charset-checks each segment first. The exposed
# callers were `key_from_jwk` and `peek_header`, which do not. So a JWKS whose key
# material carried padding or standard-base64 characters built a working verifier on a
# pure build and raised on a native one -- and since `-` and `+` decode to the same six
# bits, two spellings of one JWK yielded the same key.


def test_b64url_accepts_exactly_the_unpadded_urlsafe_alphabet() -> None:
    from wreath._auth.jwt import _b64url_decode

    assert _b64url_decode("") == b""
    assert _b64url_decode("QUJD") == b"ABC"
    # `-` and `_` are the base64url substitutions and must decode as themselves.
    assert _b64url_decode("-_-_") == base64.urlsafe_b64decode("-_-_")


@pytest.mark.parametrize(
    ("data", "why"),
    [
        ("QUJD=", "trailing padding"),
        ("QUJD==", "double padding"),
        ("QU+D", "standard-base64 plus"),
        ("QU/D", "standard-base64 slash"),
        ("QUJD\n", "trailing newline"),
        (" QUJD", "leading space"),
        ("QUJé", "outside the alphabet"),
        ("QUJDQ", "a length no base64 string can have"),
    ],
)
def test_b64url_refuses_everything_outside_that_alphabet(data: str, why: str) -> None:
    from wreath._auth.jwt import _b64url_decode

    with pytest.raises(ValueError):
        _b64url_decode(data)


def test_a_jwk_with_loose_base64_is_refused() -> None:
    from wreath._auth.jwt import key_from_jwk

    clean = "A" * 43
    assert key_from_jwk({"kty": "oct", "k": clean}).secret == _b64url(clean)
    for loose in (clean + "=", clean[:-1] + "+", clean[:-1] + "/", clean + "\n"):
        with pytest.raises(ValueError):
            key_from_jwk({"kty": "oct", "k": loose})


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
