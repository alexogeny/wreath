"""`wreath._b64`: what is base64url, and what is only nearly base64url.

The module exists because `base64.urlsafe_b64decode` is lax in three ways that
matter to anything reading a value off the wire: it re-pads, it accepts `+`/`/`
as well as `-`/`_`, and it *discards* characters outside the alphabet instead of
refusing them. `wreath._auth.jwt` documents what that cost -- two spellings of
one JWK decoding to the same key -- and found it by differential testing rather
than from a failing test.

So the anchor here is the stdlib, used the other way round: every value in
`LAX_INPUTS` decodes to *something* through `urlsafe_b64decode`, and every one
of them has to be refused. Round-tripping against `urlsafe_b64encode` covers the
agreeing half, where the stdlib is exact.
"""

from __future__ import annotations

import base64
import binascii

import pytest

from wreath import _b64
from wreath._b64 import MAX_INPUT_BYTES, b64url_decode


def _encoded(n: int) -> tuple[str, bytes]:
    raw = bytes((i * 37 + 11) % 256 for i in range(n))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="), raw


# --- what it accepts ---------------------------------------------------------


def test_every_length_decodes_to_what_the_stdlib_encoded() -> None:
    """Lengths 0..256 cover all four residues mod 4 many times over, which is
    where a decoder's tail handling goes wrong."""
    for n in range(257):
        text, raw = _encoded(n)
        assert b64url_decode(text) == raw, f"wrong at length {n}"


# --- what it must refuse -----------------------------------------------------

#: Each of these decodes to *something* through `base64.urlsafe_b64decode`,
#: which is the point: they are not base64url, and the stdlib takes them anyway.
LAX_INPUTS = [
    pytest.param("QQ==", id="padded"),
    pytest.param("a+b/", id="standard-alphabet"),
    pytest.param("ab cd", id="space"),
    pytest.param("ab\ncd", id="newline"),
    pytest.param("ab.cd", id="dot"),
]

#: Refused by the stdlib too, for a different reason -- 1 more than a multiple
#: of 4 data characters encodes no whole byte. Kept separate because the claim
#: is different: here wreath must refuse with the *same* exception type it uses
#: for everything else, so a caller has one `except ValueError` rather than
#: needing to know `binascii.Error` exists.
UNDECODABLE_LENGTHS = [
    pytest.param("Q", id="length-1-mod-4"),
    pytest.param("abcde", id="length-5"),
]


@pytest.mark.parametrize("text", LAX_INPUTS)
def test_it_refuses_what_the_stdlib_would_take(text: str) -> None:
    # The stdlib call first, and not in a `raises`: it has to *succeed* for this
    # case to mean anything. A value that stopped being lax would fail here
    # rather than quietly turning the assertion below into a tautology.
    assert base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    with pytest.raises(ValueError):
        b64url_decode(text)


@pytest.mark.parametrize("text", UNDECODABLE_LENGTHS)
def test_an_impossible_length_is_a_value_error(text: str) -> None:
    with pytest.raises(binascii.Error):
        base64.urlsafe_b64decode(text)
    with pytest.raises(ValueError):
        b64url_decode(text)


def test_the_dash_and_plus_spellings_do_not_collide() -> None:
    """The JWK bug in one assertion: `-` and `+` carry the same six bits, so a
    decoder that accepts both maps two distinct strings onto one key."""
    assert b64url_decode("a-b_") == base64.urlsafe_b64decode("a-b_")
    with pytest.raises(ValueError):
        b64url_decode("a+b/")


def test_non_str_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        b64url_decode(b"QUJD")


def test_input_past_the_size_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        b64url_decode("A" * (MAX_INPUT_BYTES + 4))


def test_empty_decodes_to_empty() -> None:
    assert b64url_decode("") == b""


# --- the callers kept their contracts ----------------------------------------


def test_webauthn_still_accepts_a_padded_value() -> None:
    """Regression for the tightening this change deliberately did *not* make.

    `_webauthn`'s alphabet set contained `=`, so a padded value was accepted
    there before the shared decoder -- which is unpadded-only -- replaced it.
    Authentication input silently narrowing is not an acceptable side effect of
    a performance change, so `b64url_decode` strips padding first.
    """
    from wreath._webauthn import WebAuthnError
    from wreath._webauthn import b64url_decode as webauthn_decode

    assert webauthn_decode("QQ==") == b"A"
    assert webauthn_decode("QQ") == b"A"
    for bad in ("Q===", "Q=Q", "a+b/", ""):
        with pytest.raises(WebAuthnError):
            webauthn_decode(bad)


def test_a_session_cookie_round_trips() -> None:
    import time

    from wreath._json import dumps as json_dumps
    from wreath.policy.sessions import SessionPolicy

    middleware = SessionPolicy(secret="k" * 32)
    payload = {"uid": "ranger-1", "roles": ["ranger"]}
    raw = json_dumps(payload)
    cookie = middleware._sign(raw, int(time.time()))

    loaded = middleware._load(cookie)
    assert loaded is not None, "a cookie this middleware just signed must load"
    assert loaded[0] == payload
    assert loaded[1] == raw, "the exact payload bytes, not a re-serialisation"

    # The half of `_load` the decoder change is actually in: a body that is not
    # base64url must be refused rather than decoded to something.
    body, stamp, mac = cookie.split(".")
    assert middleware._load(f"{body}+.{stamp}.{mac}") is None


def test_a_password_record_round_trips() -> None:
    """`_userkit` splits on `$` and base64s each half; the decode is `_unb64`."""
    from wreath._userkit import hash_password, verify_password

    record = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", record)
    assert not verify_password("wrong", record)


# --- the encode half ---------------------------------------------------------

def test_the_encoders_match_the_stdlib_at_every_length() -> None:
    """Against `base64`, which is exact in this direction.

    Lengths 0..256 cover every residue mod 3 many times over, which is where an
    encoder's tail -- the one and two leftover bytes that decide how much
    padding there would have been -- goes wrong. `wreath_b64_encode` switches
    from its AVX2 body to a scalar tail inside this range, so the boundary is
    covered rather than assumed.
    """
    for n in range(257):
        raw = bytes((i * 37 + 11) % 256 for i in range(n))
        expected = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        assert _b64.b64url_encode(raw) == expected, f"unpadded base64url wrong at {n}"
        assert _b64.b64_encode(raw) == base64.b64encode(raw).decode("ascii"), (
            f"padded wrong at {n}"
        )


def test_the_encoders_round_trip_through_the_strict_decoder() -> None:
    """`b64url_encode` must produce something `b64url_decode` accepts.

    Not a tautology: the decoder refuses padding, so an encoder that left the
    `=` on would produce values its own module could not read back. That is
    exactly the shape of the bug the two halves living apart used to invite.
    """
    for n in range(130):
        raw = bytes((i * 53 + 7) % 256 for i in range(n))
        assert b64url_decode(_b64.b64url_encode(raw)) == raw


def test_b64url_encode_never_emits_padding_or_the_standard_alphabet() -> None:
    """The two properties every caller depends on."""
    for n in range(1, 130):
        raw = bytes((i * 91 + 3) % 256 for i in range(n))
        text = _b64.b64url_encode(raw)
        assert "=" not in text
        assert "+" not in text and "/" not in text
