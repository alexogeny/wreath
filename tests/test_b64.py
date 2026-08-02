"""`wreath._b64`, and the two twins agreeing about what is not base64url.

The module exists because `base64.urlsafe_b64decode` is lax in three ways that
matter to anything reading a value off the wire: it re-pads, it accepts `+`/`/`
as well as `-`/`_`, and it *discards* characters outside the alphabet instead of
refusing them. `wreath._auth.jwt` documents what that cost -- two spellings of
one JWK decoding to the same key -- and found it by differential testing rather
than from a failing test, which is why the differential test is the first one
here.

The strictness is only worth anything if both arms have it. A guard present in
the accelerated build and absent under `WREATH_PURE=1` is not a guard, so every
refusal below is asserted against *both*, and `pure` is a fixture rather than a
separate test file so the two can never drift apart.
"""

from __future__ import annotations

import base64

import pytest

from wreath import _b64
from wreath._b64 import MAX_INPUT_BYTES, b64url_decode

#: Read at import, before any fixture can null the arm out. A test that asks
#: `_b64._native_b64url is not None` *after* taking the `pure` fixture is asking
#: whether the fixture ran, and skips itself every time.
HAS_NATIVE = _b64._native_b64url is not None


@pytest.fixture
def pure(monkeypatch):
    """The module with its native arm removed, i.e. what `WREATH_PURE=1` runs."""
    monkeypatch.setattr(_b64, "_native_b64url", None)
    return b64url_decode


def _encoded(n: int) -> tuple[str, bytes]:
    raw = bytes((i * 37 + 11) % 256 for i in range(n))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="), raw


# --- the twins agree ---------------------------------------------------------


def test_both_arms_decode_every_length_identically(pure) -> None:
    """The differential test. Lengths 0..256 cover all four residues mod 4 many
    times over, which is where a decoder's tail handling goes wrong."""
    if not HAS_NATIVE:
        pytest.skip("no native _core to differentiate against")
    for n in range(257):
        text, raw = _encoded(n)
        assert pure(text) == raw, f"pure arm wrong at {n}"


def test_the_native_arm_matches_the_pure_arm(monkeypatch) -> None:
    native = _b64._native_b64url
    if native is None:
        pytest.skip("no native _core")
    for n in range(257):
        text, raw = _encoded(n)
        assert native(text) == raw, f"native arm wrong at {n}"


# --- what both arms must refuse ----------------------------------------------

#: Every one of these decodes to *something* through
#: `base64.urlsafe_b64decode`, which is the point.
LAX_INPUTS = [
    pytest.param("QQ==", id="padded"),
    pytest.param("a+b/", id="standard-alphabet"),
    pytest.param("ab cd", id="space"),
    pytest.param("ab\ncd", id="newline"),
    pytest.param("ab.cd", id="dot"),
    pytest.param("Q", id="length-1-mod-4"),
    pytest.param("abcde", id="length-5"),
]


@pytest.mark.parametrize("text", LAX_INPUTS)
def test_native_refuses_what_the_stdlib_would_take(text: str) -> None:
    if _b64._native_b64url is None:
        pytest.skip("no native _core")
    with pytest.raises(ValueError):
        b64url_decode(text)


@pytest.mark.parametrize("text", LAX_INPUTS)
def test_the_pure_twin_refuses_exactly_the_same_set(pure, text: str) -> None:
    with pytest.raises(ValueError):
        pure(text)


def test_the_dash_and_plus_spellings_do_not_collide(pure) -> None:
    """The JWK bug in one assertion: `-` and `+` carry the same six bits, so a
    decoder that accepts both maps two distinct strings onto one key."""
    for decode in filter(None, (b64url_decode, pure)):
        assert decode("a-b_") == base64.urlsafe_b64decode("a-b_")
        with pytest.raises(ValueError):
            decode("a+b/")


@pytest.mark.parametrize("arm", ["native", "pure"])
def test_non_str_is_a_type_error(monkeypatch, arm: str) -> None:
    if arm == "pure":
        monkeypatch.setattr(_b64, "_native_b64url", None)
    elif _b64._native_b64url is None:
        pytest.skip("no native _core")
    with pytest.raises(TypeError):
        b64url_decode(b"QUJD")


def test_the_size_cap_survives_losing_the_native_arm(pure) -> None:
    """A DoS bound that only exists in the accelerated build is not a bound."""
    oversize = "A" * (MAX_INPUT_BYTES + 4)
    with pytest.raises(ValueError):
        pure(oversize)
    if HAS_NATIVE:
        with pytest.raises(ValueError):
            b64url_decode(oversize)


def test_empty_decodes_to_empty(pure) -> None:
    assert pure("") == b""
    if HAS_NATIVE:
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
    from wreath.middleware.sessions import SessionMiddleware

    middleware = SessionMiddleware(secret="k" * 32)
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
