"""Native/pure parity for CSRF token minting and validation.

The C twin owns the glue around the digest (message layout, base64, parsing,
constant-time compare). A divergence here is an authentication bug, not a
performance regression, so these compare the two implementations directly
rather than trusting either.
"""

from __future__ import annotations

import base64
import hmac
import os

import pytest

from wreath._native import _core
from wreath._pure import security as pure

pytestmark = pytest.mark.skipif(_core is None, reason="native _core is not built")

SECRET = b"k" * 32
NOW = 1_700_000_000
MAX_AGE = 7200


def _native() -> tuple[object, object, object]:
    assert _core is not None
    return _core.csrf_sign, _core.csrf_new_token, _core.csrf_validate


def _nonce(seed: bytes = b"n" * 32) -> str:
    return base64.urlsafe_b64encode(seed).rstrip(b"=").decode("ascii")


@pytest.mark.parametrize(
    "issued", [0, 1, 999, NOW, 2**31 - 1, 2**31, 2**40]
)
def test_signing_agrees_with_the_pure_twin(issued: int) -> None:
    sign, _new, _validate = _native()
    nonce = _nonce()
    assert sign(SECRET, issued, nonce) == pure.csrf_sign(SECRET, issued, nonce)


def test_signing_agrees_for_random_nonces() -> None:
    sign, _new, _validate = _native()
    for _ in range(200):
        nonce = _nonce(os.urandom(32))
        assert sign(SECRET, NOW, nonce) == pure.csrf_sign(SECRET, NOW, nonce)


def test_signing_agrees_for_random_secrets() -> None:
    sign, _new, _validate = _native()
    nonce = _nonce()
    for _ in range(100):
        secret = os.urandom(32)
        assert sign(secret, NOW, nonce) == pure.csrf_sign(secret, NOW, nonce)


def test_the_signature_is_the_hmac_the_python_twin_would_compute() -> None:
    # Anchored to hashlib rather than to either implementation, so both can be
    # wrong together and still be caught.
    sign, _new, _validate = _native()
    nonce = _nonce()
    token = sign(SECRET, NOW, nonce)
    message = f"v1.{NOW}.{nonce}"
    expected = base64.urlsafe_b64encode(
        hmac.digest(SECRET, message.encode("ascii"), "sha256")
    ).rstrip(b"=").decode("ascii")
    assert token == f"{message}.{expected}"


def test_each_implementation_accepts_the_other_s_tokens() -> None:
    _sign, new, validate = _native()
    native_token = new(SECRET, NOW)
    pure_token = pure.csrf_new_token(SECRET, NOW)
    assert validate(SECRET, pure_token, NOW, MAX_AGE) == (True, NOW)
    assert pure.csrf_validate(SECRET, native_token, NOW, MAX_AGE) == (True, NOW)


def test_a_minted_token_has_the_documented_shape() -> None:
    _sign, new, _validate = _native()
    parts = new(SECRET, NOW).split(".")
    assert len(parts) == 4
    assert parts[0] == "v1"
    assert parts[1] == str(NOW)
    assert len(parts[2]) == 43 and len(parts[3]) == 43


def test_minting_twice_gives_different_nonces() -> None:
    _sign, new, _validate = _native()
    assert new(SECRET, NOW) != new(SECRET, NOW)


def test_a_wrong_secret_is_rejected() -> None:
    _sign, new, validate = _native()
    token = new(SECRET, NOW)
    assert validate(b"j" * 32, token, NOW, MAX_AGE)[0] is False
    assert pure.csrf_validate(b"j" * 32, token, NOW, MAX_AGE)[0] is False


@pytest.mark.parametrize(
    "token",
    [
        "",
        "v1",
        "v1.1700000000",
        "v1.1700000000.short.short",
        "v2.1700000000." + "n" * 43 + "." + "s" * 43,
        "v1.notanumber." + "n" * 43 + "." + "s" * 43,
        "v1.1700000000." + "n" * 42 + "." + "s" * 43,
        "v1.1700000000." + "n" * 44 + "." + "s" * 43,
        "v1.1700000000." + "n" * 43 + "." + "s" * 44,
        "v1.1700000000." + "!" * 43 + "." + "s" * 43,
        "v1.1700000000." + "n" * 43 + "." + "s" * 43 + ".extra",
        "v1..." ,
        "v1.1700000000." + "n" * 43 + "." + "s" * 43 + "\x00",
        # **Interior** NULs, in the stamp. `strtoll` halts at one and reports the
        # prefix as consumed whole, so `*end == '\0'` reads as success while the
        # pure twin's `\Z`-anchored regex refuses. The corpus had a NUL only on
        # the end of the whole token, which `component_valid` rejects for a
        # different reason -- so the divergence had no case at all.
        "v1.1700000000\x00junk." + "n" * 43 + "." + "s" * 43,
        "v1.1700000000\x00." + "n" * 43 + "." + "s" * 43,
        "v1.\x001700000000." + "n" * 43 + "." + "s" * 43,
        "v1.99999999999999999999999999." + "n" * 43 + "." + "s" * 43,
        "v1.-1700000000." + "n" * 43 + "." + "s" * 43,
        "....",
        "v1.1700000000." + "n" * 43,
    ],
)
def test_malformed_tokens_are_rejected_identically(token: str) -> None:
    _sign, _new, validate = _native()
    assert validate(SECRET, token, NOW, MAX_AGE) == pure.csrf_validate(
        SECRET, token, NOW, MAX_AGE
    ), f"native and pure disagree on {token!r}"


@pytest.mark.parametrize(
    ("issued", "now"),
    [
        (NOW, NOW),            # fresh
        (NOW, NOW + 7200),     # exactly at max_age
        (NOW, NOW + 7201),     # expired by one second
        (NOW, NOW - 60),       # allowed clock skew
        (NOW, NOW - 61),       # beyond the skew allowance
        (NOW, NOW + 1),
    ],
)
def test_freshness_windows_agree(issued: int, now: int) -> None:
    sign, _new, validate = _native()
    token = sign(SECRET, issued, _nonce())
    assert validate(SECRET, token, now, MAX_AGE) == pure.csrf_validate(
        SECRET, token, now, MAX_AGE
    )


def test_an_expired_token_still_reports_when_it_was_issued() -> None:
    # `before` renews on this, so losing `issued` would turn renewal into a
    # rejection.
    sign, _new, validate = _native()
    token = sign(SECRET, NOW, _nonce())
    valid, issued = validate(SECRET, token, NOW + 99999, MAX_AGE)
    assert valid is False
    assert issued == NOW


def test_a_tampered_signature_is_rejected() -> None:
    _sign, new, validate = _native()
    token = new(SECRET, NOW)
    head, _, signature = token.rpartition(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert validate(SECRET, f"{head}.{flipped}", NOW, MAX_AGE)[0] is False


def test_a_tampered_nonce_is_rejected() -> None:
    _sign, new, validate = _native()
    parts = new(SECRET, NOW).split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    assert validate(SECRET, ".".join(parts), NOW, MAX_AGE)[0] is False


def test_a_replayed_issued_time_is_rejected() -> None:
    # Moving `issued` invalidates the signature: it is inside the signed body.
    _sign, new, validate = _native()
    parts = new(SECRET, NOW).split(".")
    parts[1] = str(NOW + 1)
    assert validate(SECRET, ".".join(parts), NOW + 1, MAX_AGE)[0] is False


def test_a_nonce_of_the_wrong_length_is_refused_at_signing() -> None:
    sign, _new, _validate = _native()
    with pytest.raises(ValueError):
        sign(SECRET, NOW, "tooshort")
    with pytest.raises(ValueError):
        pure.csrf_sign(SECRET, NOW, "tooshort")


def test_secrets_of_any_length_agree() -> None:
    sign, _new, _validate = _native()
    nonce = _nonce()
    for size in (32, 33, 64, 65, 128, 200):
        secret = os.urandom(size)
        assert sign(secret, NOW, nonce) == pure.csrf_sign(secret, NOW, nonce)


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
def test_a_junk_issued_field_is_read_identically_by_both_twins(stamp: str) -> None:
    """The twins must agree on the *whole* result, `issued` included.

    `csrf_validate` reports the issue time alongside the verdict so a caller can
    renew rather than reject an expired token. The C twin parses that field with
    `strtoll` into a 24-byte buffer; `int()` accepts a strictly wider language --
    Unicode digits, `_` separators, trailing whitespace, unbounded length -- so
    the pure twin used to answer `(False, 123)` where the C twin answered
    `(False, 0)`, and accepted a zero-padded stamp the C twin refused outright.
    """
    _sign, new, validate = _native()
    parts = new(SECRET, NOW).split(".")
    parts[1] = stamp
    token = ".".join(parts)
    assert validate(SECRET, token, NOW, MAX_AGE) == pure.csrf_validate(
        SECRET, token, NOW, MAX_AGE
    )


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
def test_wildcard_host_matching_agrees_with_the_pure_twin(
    host: str, patterns: tuple[str, ...], expected: bool
) -> None:
    """`*.example` stands for at least one label, so `.example` is not a match.

    The pure twin excluded only the bare parent (`host != suffix[1:]`), which
    still admitted the empty-label host that the C twin's length comparison
    rejects. `TrustedHostPolicy` normalizes such a host away before it gets
    here, so this was a twin divergence rather than a live bypass -- but the
    pure matcher is what ships when the extension is not built.
    """
    assert _core is not None
    assert _core.host_allowed(host, patterns) is expected
    assert pure.host_allowed(host, patterns) is expected


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
    definition, so it has to agree with `hmac.digest` exactly -- including on
    the two branches the optimisation introduces: a key longer than the 64-byte
    block, which HMAC replaces with its own digest, and a shorter one, which it
    zero-pads.
    """
    sign, _new, _validate = _native()
    nonce = _nonce()
    token = sign(secret, NOW, nonce)
    message = f"v1.{NOW}.{nonce}".encode("ascii")
    expected = base64.urlsafe_b64encode(
        hmac.digest(secret, message, "sha256")
    ).rstrip(b"=").decode("ascii")

    assert token == f"v1.{NOW}.{nonce}.{expected}"


def test_switching_secrets_does_not_reuse_the_previous_key() -> None:
    """The failure mode a cached key schedule introduces, and the only new one.

    Signing with one secret and then another must not sign the second message
    with the first key's state. Interleaved and repeated, because a cache that
    is merely *stale* passes a single alternation.
    """
    sign, _new, _validate = _native()
    nonce = _nonce()
    message = f"v1.{NOW}.{nonce}".encode("ascii")
    secrets = [b"a" * 32, b"b" * 32, b"a" * 32, b"c" * 99, b"b" * 32, b"a" * 32]

    for secret in secrets:
        expected = base64.urlsafe_b64encode(
            hmac.digest(secret, message, "sha256")
        ).rstrip(b"=").decode("ascii")
        assert sign(secret, NOW, nonce).rsplit(".", 1)[1] == expected, secret


def test_two_equal_secrets_that_are_not_the_same_object_agree() -> None:
    """The cache compares key *bytes*, not identity: a fresh equal key hits it."""
    sign, _new, _validate = _native()
    nonce = _nonce()
    first = sign(b"s" * 32, NOW, nonce)
    second = sign(bytes(b"s" * 32), NOW, nonce)

    assert first == second


def test_a_token_minted_under_one_secret_is_refused_under_another() -> None:
    """End to end: the cache must not make a foreign token validate."""
    _sign, new_token, validate = _native()
    token = new_token(b"a" * 32, NOW)

    assert validate(b"a" * 32, token, NOW, MAX_AGE)[0] is True
    assert validate(b"b" * 32, token, NOW, MAX_AGE)[0] is False


def test_a_valid_token_with_a_nul_in_its_stamp_is_refused_by_both() -> None:
    """The sharper form: the signature still matches.

    The C twin rebuilds the signed message from the *parsed* `issued` value, so
    everything after the NUL vanishes before the HMAC is recomputed and the
    token verifies. A malformed-token corpus entry cannot show that -- the
    signature has to be real for the comparison to be reached at all.
    """
    _sign, new, validate = _native()
    parts = new(SECRET, NOW).split(".")
    tampered = ".".join([parts[0], parts[1] + "\x00rubbish", parts[2], parts[3]])
    assert pure.csrf_validate(SECRET, tampered, NOW, MAX_AGE) == (False, 0)
    assert validate(SECRET, tampered, NOW, MAX_AGE) == (False, 0)
