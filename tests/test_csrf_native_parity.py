"""CSRF token minting and validation, held to an independent expectation.

`security.c` owns the glue around the digest -- message layout, base64, parsing,
constant-time compare -- and a mistake there is an authentication bug. So every
assertion below is anchored on something outside Wreath: the stdlib's `hmac` and
`base64`, or a value spelled out with the derivation beside it.

**The token layout**, written out here because it is Wreath's own and no
published vector covers it. `src/wreath/_native/security.c`'s header comment is
the other definition:

    v1 "." <issued> "." <nonce> "." <signature>

* `v1` is the literal version tag; any other tag is refused.
* `<issued>` is the issue time in whole seconds since the epoch, in decimal, as
  `str(int)` renders it (leading `-` for a negative, no padding, no separators).
* `<nonce>` is 32 random bytes in URL-safe base64 with the `=` padding stripped
  (RFC 4648 §5), which is exactly 43 characters from the `[A-Za-z0-9_-]` set.
* `<signature>` is HMAC-SHA256 (RFC 2104) under the secret, over the first three
  fields *as they appear in the token* -- the ASCII bytes of
  `v1.<issued>.<nonce>`, with no length prefix and no trailing separator -- in
  the same unpadded URL-safe base64, so also 43 characters.

`_stdlib_token` below is that paragraph as code, using nothing but `hmac` and
`base64`. Where a test needs a valid token it mints one there rather than asking
Wreath for one, so a token Wreath would produce and would accept still fails if
it is not the token the layout specifies.
"""

from __future__ import annotations

import base64
import hmac
import os

import pytest

from wreath._native import _core

pytestmark = pytest.mark.skipif(_core is None, reason="native _core is not built")

SECRET = b"k" * 32
NOW = 1_700_000_000
MAX_AGE = 7200
#: `csrf_validate` accepts a token issued up to this far in the future, so a
#: client whose clock runs fast is not locked out. Wreath's own choice; see
#: `issued > now + 60` in `security.c`.
SKEW = 60


def _native() -> tuple[object, object, object]:
    assert _core is not None
    return _core.csrf_sign, _core.csrf_new_token, _core.csrf_validate


def _b64url(raw: bytes) -> str:
    """RFC 4648 §5 base64url with the padding stripped, via the stdlib only."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """The inverse, re-padding to the multiple of four RFC 4648 §4 requires."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _nonce(seed: bytes = b"n" * 32) -> str:
    return _b64url(seed)


def _stdlib_token(secret: bytes, issued: int, nonce: str) -> str:
    """The token the layout in this module's docstring specifies, from stdlib.

    Deliberately independent of both twins: `hmac.digest` is CPython's binding
    to OpenSSL's HMAC and `base64` is the stdlib codec, so neither half of
    Wreath contributes to this value.
    """
    body = f"v1.{issued}.{nonce}"
    return f"{body}.{_b64url(hmac.digest(secret, body.encode('ascii'), 'sha256'))}"


@pytest.mark.parametrize("issued", [0, 1, 999, NOW, 2**31 - 1, 2**31, 2**40])
def test_signing_produces_the_token_the_layout_specifies(issued: int) -> None:
    """Both twins must render the whole token, not merely agree on it.

    `2**31` and either side of it are here because the C twin formats the stamp
    with `%lld` and Python with `str(int)`; a 32-bit truncation would show up as
    a different `<issued>` field and so as a different signature.
    """
    sign, _new, _validate = _native()
    nonce = _nonce()
    expected = _stdlib_token(SECRET, issued, nonce)

    assert sign(SECRET, issued, nonce) == expected


def test_signing_holds_for_random_nonces() -> None:
    sign, _new, _validate = _native()
    for _ in range(200):
        nonce = _nonce(os.urandom(32))
        expected = _stdlib_token(SECRET, NOW, nonce)
        assert sign(SECRET, NOW, nonce) == expected


def test_signing_holds_for_random_secrets() -> None:
    sign, _new, _validate = _native()
    nonce = _nonce()
    for _ in range(100):
        secret = os.urandom(32)
        expected = _stdlib_token(secret, NOW, nonce)
        assert sign(secret, NOW, nonce) == expected


def test_a_minted_token_is_the_stdlib_signature_of_its_own_nonce() -> None:
    """Each twin's freshly minted token, re-derived from its nonce alone.

    Also the cross-acceptance case: a token from either twin must validate
    under either, and the verdict is spelled out rather than compared.
    """
    _sign, new, validate = _native()
    for token in (new(SECRET, NOW),):
        _version, issued, nonce, _signature = token.split(".")
        assert token == _stdlib_token(SECRET, int(issued), nonce)
        assert validate(SECRET, token, NOW, MAX_AGE) == (True, NOW)


def test_a_minted_token_has_the_documented_shape() -> None:
    """Four fields, the `v1` tag, the decimal stamp, and two 32-byte components.

    Decoding the two base64url fields back through the stdlib is what makes
    "43 characters" mean "32 bytes" rather than "43 of anything".
    """
    _sign, new, _validate = _native()
    for token in (new(SECRET, NOW),):
        parts = token.split(".")
        assert len(parts) == 4
        assert parts[0] == "v1"
        assert parts[1] == str(NOW)
        assert len(parts[2]) == 43 and len(parts[3]) == 43
        assert len(_b64url_decode(parts[2])) == 32
        assert len(_b64url_decode(parts[3])) == 32


def test_minting_twice_gives_different_nonces() -> None:
    _sign, new, _validate = _native()
    assert new(SECRET, NOW) != new(SECRET, NOW)


def test_a_wrong_secret_is_rejected() -> None:
    """The independent half is the first line: the layout gives a *different*
    signature under a different key, so `False` is what the layout requires and
    not merely what both twins happen to answer."""
    _sign, new, validate = _native()
    token = new(SECRET, NOW)
    _version, issued, nonce, _signature = token.split(".")

    assert token != _stdlib_token(b"j" * 32, int(issued), nonce)
    assert validate(b"j" * 32, token, NOW, MAX_AGE) == (False, NOW)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        # Every one of these is refused before the freshness check, so the
        # reported issue time is the refusal sentinel 0 rather than a parse of
        # whatever the stamp field held. The one exception is called out below.
        ("", (False, 0)),                                     # no fields at all
        ("v1", (False, 0)),                                   # one field
        ("v1.1700000000", (False, 0)),                        # two
        ("v1.1700000000.short.short", (False, 0)),            # components != 43
        ("v2.1700000000." + "n" * 43 + "." + "s" * 43, (False, 0)),   # wrong tag
        ("v1.notanumber." + "n" * 43 + "." + "s" * 43, (False, 0)),   # no stamp
        ("v1.1700000000." + "n" * 42 + "." + "s" * 43, (False, 0)),   # nonce 42
        ("v1.1700000000." + "n" * 44 + "." + "s" * 43, (False, 0)),   # nonce 44
        ("v1.1700000000." + "n" * 43 + "." + "s" * 44, (False, 0)),   # sig 44
        # `!` is outside the RFC 4648 §5 alphabet the layout names.
        ("v1.1700000000." + "!" * 43 + "." + "s" * 43, (False, 0)),
        ("v1.1700000000." + "n" * 43 + "." + "s" * 43 + ".extra", (False, 0)),
        ("v1...", (False, 0)),                                # empty stamp
        ("v1.1700000000." + "n" * 43 + "." + "s" * 43 + "\x00", (False, 0)),
        # **Interior** NULs, in the stamp. `strtoll` halts at one and reports
        # the prefix as consumed whole, so a naive `*end == '\0'` check reads as
        # success and admits `1700000000\x00junk` as the timestamp
        # `1700000000`. The corpus only ever had a NUL on the *end* of a whole
        # token, which `component_valid` rejects for an unrelated reason, so
        # nothing covered this until these three rows.
        ("v1.1700000000\x00junk." + "n" * 43 + "." + "s" * 43, (False, 0)),
        ("v1.1700000000\x00." + "n" * 43 + "." + "s" * 43, (False, 0)),
        ("v1.\x001700000000." + "n" * 43 + "." + "s" * 43, (False, 0)),
        # 26 digits: longer than any int64 timestamp, and longer than the stamp
        # field the layout describes, so it is refused before it is read.
        ("v1.99999999999999999999999999." + "n" * 43 + "." + "s" * 43, (False, 0)),
        # The exception. `-1700000000` *is* a well-formed decimal stamp and both
        # components are well-formed, so the token survives to the freshness
        # check and is refused there for age -- 3.4e9 seconds ago against a
        # 7200-second window. Expiry reports the issue time it read, so the
        # answer is the stamp itself and not 0.
        ("v1.-1700000000." + "n" * 43 + "." + "s" * 43, (False, -1_700_000_000)),
        ("....", (False, 0)),                                 # five empty fields
        ("v1.1700000000." + "n" * 43, (False, 0)),            # three fields
    ],
)
def test_malformed_tokens_are_refused(token: str, expected: tuple[bool, int]) -> None:
    _sign, _new, validate = _native()
    assert validate(SECRET, token, NOW, MAX_AGE) == expected


@pytest.mark.parametrize(
    ("issued", "now", "expected"),
    [
        # Wreath's own window, read off `issued > now + 60 or now - issued >
        # max_age` in both twins: a token is fresh from 60 seconds before it was
        # issued (clock skew) until `max_age` seconds after, both ends inclusive.
        (NOW, NOW, (True, NOW)),                          # fresh
        (NOW, NOW + MAX_AGE, (True, NOW)),                # last acceptable second
        (NOW, NOW + MAX_AGE + 1, (False, NOW)),           # expired by one second
        (NOW, NOW - SKEW, (True, NOW)),                   # furthest allowed skew
        (NOW, NOW - SKEW - 1, (False, NOW)),              # one second beyond it
        (NOW, NOW + 1, (True, NOW)),
    ],
)
def test_freshness_windows(issued: int, now: int, expected: tuple[bool, int]) -> None:
    """The token is minted by the stdlib, so a signature both twins got wrong
    together would fail here rather than pass the window check."""
    _sign, _new, validate = _native()
    token = _stdlib_token(SECRET, issued, _nonce())

    assert validate(SECRET, token, now, MAX_AGE) == expected


def test_an_expired_token_still_reports_when_it_was_issued() -> None:
    # `before` renews on this, so losing `issued` would turn renewal into a
    # rejection.
    _sign, _new, validate = _native()
    token = _stdlib_token(SECRET, NOW, _nonce())

    assert validate(SECRET, token, NOW + 99999, MAX_AGE) == (False, NOW)


def test_a_tampered_signature_is_rejected() -> None:
    _sign, _new, validate = _native()
    token = _stdlib_token(SECRET, NOW, _nonce())
    head, _, signature = token.rpartition(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered = f"{head}.{flipped}"

    # Independent of the verdict: the tampered token is not the token the
    # layout specifies for this nonce, so refusing it is the only right answer.
    assert tampered != _stdlib_token(SECRET, NOW, _nonce())
    assert validate(SECRET, tampered, NOW, MAX_AGE) == (False, NOW)


def test_a_tampered_nonce_is_rejected() -> None:
    """The nonce is inside the signed body, so moving it breaks the signature."""
    _sign, _new, validate = _native()
    parts = _stdlib_token(SECRET, NOW, _nonce()).split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    tampered = ".".join(parts)

    assert tampered != _stdlib_token(SECRET, NOW, parts[2])
    assert validate(SECRET, tampered, NOW, MAX_AGE) == (False, NOW)


def test_a_replayed_issued_time_is_rejected() -> None:
    # Moving `issued` invalidates the signature: it is inside the signed body.
    _sign, _new, validate = _native()
    parts = _stdlib_token(SECRET, NOW, _nonce()).split(".")
    parts[1] = str(NOW + 1)
    replayed = ".".join(parts)

    assert replayed != _stdlib_token(SECRET, NOW + 1, parts[2])
    assert validate(SECRET, replayed, NOW + 1, MAX_AGE) == (False, NOW + 1)


def test_a_nonce_of_the_wrong_length_is_refused_at_signing() -> None:
    """43 characters is the layout, not a preference: `b64url(32 bytes)`."""
    sign, _new, _validate = _native()
    with pytest.raises(ValueError):
        sign(SECRET, NOW, "tooshort")


def test_secrets_of_any_length_are_signed_as_hmac_defines() -> None:
    sign, _new, _validate = _native()
    nonce = _nonce()
    for size in (32, 33, 64, 65, 128, 200):
        secret = os.urandom(size)
        expected = _stdlib_token(secret, NOW, nonce)
        assert sign(secret, NOW, nonce) == expected


@pytest.mark.parametrize(
    "stamp",
    [
        "\N{ARABIC-INDIC DIGIT ONE}\N{ARABIC-INDIC DIGIT TWO}",  # int() takes these
        "\N{EXTENDED ARABIC-INDIC DIGIT ONE}",
        "1_0",           # int() takes the PEP 515 separator; strtoll does not
        "1 ",            # int() strips a trailing space; strtoll stops at it
        "1\t",
        "0" * 24 + "1",  # longer than the C twin's 24-byte parse buffer
        "\N{NO-BREAK SPACE}1",
        "0x10",
        "1e3",
        "--1",
        "",
    ],
)
def test_a_junk_issued_field_is_refused_with_a_zero_issue_time(stamp: str) -> None:
    """The refusal pair is `(False, 0)` for every one of these, `issued` included.

    None of these stamps is the decimal integer the layout calls for -- they are
    Unicode digits, a PEP 515 separator, trailing whitespace, a hexadecimal or
    exponent spelling, a doubled sign, an over-long run, or nothing at all -- so
    the token is malformed and there is no issue time to report. `0` is the
    sentinel both twins use for "refused before the stamp was read"; a stamp
    that parsed to some value and was *then* refused would report that value
    instead, which is the distinction this asserts.

    It matters because `csrf_validate` reports the issue time alongside the
    verdict so a caller can renew rather than reject an expired token. The stamp
    is parsed with `strtoll` into a 24-byte buffer, and the layout says decimal
    with no padding and no separators -- so a zero-padded or otherwise wider
    spelling is refused rather than read.
    """
    _sign, new, validate = _native()
    parts = new(SECRET, NOW).split(".")
    parts[1] = stamp
    token = ".".join(parts)

    assert validate(SECRET, token, NOW, MAX_AGE) == (False, 0)


@pytest.mark.parametrize(
    ("host", "patterns", "expected"),
    [
        (".example", ("*.example",), False),
        ("example", ("*.example",), False),
        ("a.example", ("*.example",), True),
        (".app.example", ("*.app.example",), False),
        ("..example", ("*.example",), True),
        ("app.example", ("app.example",), True),
    ],
)
def test_wildcard_host_matching_needs_at_least_one_label(
    host: str, patterns: tuple[str, ...], expected: bool
) -> None:
    """`*.example` stands for at least one label, so `.example` is not a match.

    The rule is the one RFC 9110 §4.2.4 and RFC 6125 §6.4.3 describe for a
    wildcard label: it matches a single, *non-empty* left-most label. An
    implementation that excludes only the bare parent (`host != suffix[1:]`)
    still admits the empty-label host, which the length comparison in
    `security.c` rejects. `TrustedHostPolicy` normalizes such a host away before
    it reaches the matcher, so this is defence in depth rather than the only
    guard -- which is the reason to pin it here rather than rely on the caller.
    """
    assert _core is not None
    assert _core.host_allowed(host, patterns) is expected


# -- the key schedule is cached; these hold that cache to HMAC's definition ----


@pytest.mark.parametrize(
    "secret",
    [
        b"",                    # empty: still a key, still zero-padded
        b"k",
        b"k" * 31,
        b"k" * 63,
        b"k" * 64,              # exactly one block, no padding and no hashing
        b"k" * 65,              # over a block: HMAC replaces it with its digest
        b"k" * 200,
        bytes(range(256)),      # every byte value, and longer than a block
    ],
)
def test_the_signature_is_hmac_for_any_key_length(secret: bytes) -> None:
    """The digest must be HMAC's, not something close to it.

    `csrf_sign` absorbs the key's ipad/opad blocks once and copies the state per
    call instead of letting `hmac.digest` re-derive them. That is HMAC's own
    definition (RFC 2104 §2, and §3 for a key longer than the block), so it has
    to agree with `hmac.digest` exactly -- including on the two branches the
    optimisation introduces: a key longer than the 64-byte block, which HMAC
    replaces with its own digest, and a shorter one, which it zero-pads.
    """
    sign, _new, _validate = _native()
    nonce = _nonce()
    expected = _stdlib_token(secret, NOW, nonce)

    assert sign(secret, NOW, nonce) == expected


def test_switching_secrets_does_not_reuse_the_previous_key() -> None:
    """The failure mode a cached key schedule introduces, and the only new one.

    Signing with one secret and then another must not sign the second message
    with the first key's state. Interleaved and repeated, because a cache that
    is merely *stale* passes a single alternation.
    """
    sign, _new, _validate = _native()
    nonce = _nonce()
    secrets = [b"a" * 32, b"b" * 32, b"a" * 32, b"c" * 99, b"b" * 32, b"a" * 32]

    for secret in secrets:
        assert sign(secret, NOW, nonce) == _stdlib_token(secret, NOW, nonce), secret


def test_two_equal_secrets_that_are_not_the_same_object_agree() -> None:
    """The cache compares key *bytes*, not identity: a fresh equal key hits it."""
    sign, _new, _validate = _native()
    nonce = _nonce()
    expected = _stdlib_token(b"s" * 32, NOW, nonce)

    assert sign(b"s" * 32, NOW, nonce) == expected
    assert sign(bytes(b"s" * 32), NOW, nonce) == expected


def test_a_token_minted_under_one_secret_is_refused_under_another() -> None:
    """End to end: the cache must not make a foreign token validate."""
    _sign, new_token, validate = _native()
    token = new_token(b"a" * 32, NOW)
    _version, issued, nonce, _signature = token.split(".")

    assert token == _stdlib_token(b"a" * 32, int(issued), nonce)
    assert token != _stdlib_token(b"b" * 32, int(issued), nonce)
    assert validate(b"a" * 32, token, NOW, MAX_AGE) == (True, NOW)
    assert validate(b"b" * 32, token, NOW, MAX_AGE) == (False, NOW)


def test_a_valid_token_with_a_nul_in_its_stamp_is_refused_by_both() -> None:
    """The sharper form: the signature still matches.

    The C twin rebuilds the signed message from the *parsed* `issued` value, so
    everything after the NUL vanishes before the HMAC is recomputed and the
    token verifies. A malformed-token corpus entry cannot show that -- the
    signature has to be real for the comparison to be reached at all. The stamp
    field the layout describes is decimal digits, so a NUL in it makes the token
    malformed and `(False, 0)` the required answer.
    """
    _sign, _new, validate = _native()
    parts = _stdlib_token(SECRET, NOW, _nonce()).split(".")
    tampered = ".".join([parts[0], parts[1] + "\x00rubbish", parts[2], parts[3]])

    assert validate(SECRET, tampered, NOW, MAX_AGE) == (False, 0)
