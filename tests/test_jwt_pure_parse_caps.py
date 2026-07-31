"""`_parse_compact`'s DoS guards and structural checks, in whichever mode is running.

**Why this file exists.** A mutation sweep over `src/wreath/_auth/jwt.py` reported the
size caps and structural checks on a compact JWT as `unreached` -- no test executed
them. A compact JWT is attacker-controlled input, so that is the worst place for it.

**Why they were unreached, and the trap in fixing it.** `_parse_compact` uses native
`jose_parse` when `_core` is built and only falls through to a Python branch when it
is not. The suite runs with `_core` built, so the Python branch never executes -- and
that branch exists precisely to hold the same caps when `_core` is absent:

    Enforce the same hard size caps as native jose_parse so the DoS guard
    (a giant segment fed to the JSON parser) holds under WREATH_PURE=1 and
    whenever _core is unavailable, not only on the native path.

The obvious fix is to select the pure branch by running a subprocess with
`WREATH_PURE=1`, following `test_http_client_protocol.py`. **That works as a test and
is useless as coverage.** `wreath mutant` applies a mutation in a forked child's
memory; `subprocess.run(sys.executable, ...)` starts a fresh interpreter that reads
pristine source from disk, so the mutation never reaches the code under test, the
assertions pass either way, and the mutant survives. Measured: a subprocess version of
this file left lines 311-321 `unreached`, exactly as before it was written.

So these tests are **in-process and mode-agnostic**. Each malformed token must be
refused whichever implementation is active, which is a twin-parity claim worth making
on its own. Run the suite normally and they cover native `jose_parse`; run it with
`WREATH_PURE=1` and the same tests cover the Python branch *and* become visible to
mutation testing. Anything only reachable under `WREATH_PURE=1` is unmutatable while
the extension is built, so the sweep has to be run in both modes -- the same argument
AGENTS.md makes for free-threading and the JIT being separately tested modes.

**The two twins answer identically, and that is now a checked property.** They did not
when this file was written: `MALFORMED` carried two columns, one wording per
implementation, and a comment recording that the difference was real. Exercising both
paths turned up three divergences behind that -- a size cap that differed by two
characters, a `binascii.Error` escaping the pure branch with a stdlib message, and an
empty token diagnosed two different ways -- so the twins were aligned on the clearer
wording of the two and the table collapsed to one expected message per token. Every
case below is therefore also a twin-parity assertion.

Pinning the *message* rather than just `ValueError` is what makes these bite: the caps
are ordered and overlapping, so deleting one `raise` often just falls through to the
next and still raises `ValueError`. A message-agnostic first draft left seven mutants
alive for exactly that reason.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys

import pytest

from wreath._auth import jwt as jwt_module
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


def test_which_implementation_this_run_is_exercising() -> None:
    """Not an assertion so much as a label, and it earns its place.

    Every test below is written to hold in both modes. If that ever stops being true
    the failure is much easier to read knowing which branch ran, and this is the one
    place that says so.
    """
    assert jwt_module._native_parse is None or callable(jwt_module._native_parse)


#: Which twin this interpreter loaded. Selected at import time from the environment,
#: so it is a property of the run rather than something a test can choose.
MODE = "pure" if jwt_module._native_parse is None else "native"

#: Every malformed token, with the refusal *each* implementation answers it with.
#:
#: **Asserting the message, not just `ValueError`, is what makes these tests bite.**
#: A message-agnostic version was tried and left seven mutants alive, because the caps
#: are ordered and overlapping: a token past the 1 MiB total cap necessarily has a
#: segment past the per-segment cap, so deleting the total-cap `raise` merely falls
#: through to the next one and still raises `ValueError`. Only the wording
#: distinguishes which guard fired.
#:
#: One expected message per token, asserted in whichever mode is running, so each row
#: is also a claim that the twins agree. They did not until exercising both paths
#: forced the question: native separated "exactly two dots" (wrong count) from "has an
#: empty segment" (right count, empty part) where the pure branch answered both with
#: one message, and the pure branch had the clearer wording for a bad segment. Both
#: were aligned on the better of the two rather than on whichever was already there.
MALFORMED = [
    # Past the whole-token ceiling, before any splitting happens.
    ("a." + "b" * MAX_TOKEN_BYTES + ".c", "token over the total cap",
     "compact JWT exceeds maximum size"),
    # Wrong segment count, both directions.
    ("a.b", "two segments",
     "compact JWT must have exactly two dots"),
    ("a", "no dots at all",
     "compact JWT must have exactly two dots"),
    ("a.b.c.d", "four segments",
     "compact JWT must have exactly two dots"),
    # Right count, one segment empty. An empty signature is the classic `alg=none`
    # shape, so this refusal has a history.
    ("a.b.", "empty signature",
     "compact JWT has an empty segment"),
    (".b.c", "empty header",
     "compact JWT has an empty segment"),
    ("a..c", "empty payload",
     "compact JWT has an empty segment"),
    ("..", "all three empty",
     "compact JWT has an empty segment"),
    # An oversized segment inside a token under the total cap: the guard the source
    # comment singles out, a giant segment reaching the JSON parser. The one message
    # the two twins share.
    ("a." + "b" * (MAX_SEGMENT_B64_ACCEPTED + 1) + ".c", "segment over the per-segment cap",
     "JWT segment exceeds size cap"),
    # Outside unpadded base64url. "=" matters most: accepting padding would give one
    # token two spellings, and a signature covers the bytes that were sent.
    ("a=.b.c", "base64 padding",
     "a compact JWT segment must be unpadded base64url"),
    ("a+b.c.d", "standard-base64 plus",
     "a compact JWT segment must be unpadded base64url"),
    ("a/b.c.d", "standard-base64 slash",
     "a compact JWT segment must be unpadded base64url"),
    ("a b.c.d", "a space",
     "a compact JWT segment must be unpadded base64url"),
    ("aé.b.c", "a non-ASCII character",
     "a compact JWT segment must be unpadded base64url"),
]


@pytest.mark.parametrize(
    "token,why,message",
    MALFORMED,
    ids=[case[1] for case in MALFORMED],
)
def test_a_malformed_compact_token_is_refused_by_the_right_guard(
    token: str, why: str, message: str
) -> None:
    """One expected message, whichever twin is running.

    That the message no longer depends on `MODE` is the assertion: a caller cannot
    tell from a refusal which build parsed the token.
    """
    with pytest.raises(ValueError, match=re.escape(message)):
        _parse_compact(token)


def test_the_per_segment_cap_is_reported_by_both_twins_in_the_same_words() -> None:
    """The DoS guard the source comment singles out, pinned separately.

    A giant segment reaching the JSON parser is the attack the cap exists for, so it
    is worth naming on its own rather than only as a row in the table.
    """
    with pytest.raises(ValueError, match="segment exceeds size cap"):
        _parse_compact("a." + "b" * (MAX_SEGMENT_B64_ACCEPTED + 1) + ".c")


def test_a_header_or_payload_that_is_not_a_json_object_is_refused() -> None:
    """`[1]` is valid JSON and is not a JWT header.

    Both positions, because either alone leaves the other arm of the `or` unexercised.
    This check is after the native/pure fork, so it runs in both modes identically.
    """
    for header, claims in (([1], {}), ({}, [1]), ("str", {}), (1, {})):
        token = f"{_segment(header)}.{_segment(claims)}.AAAA"
        with pytest.raises(ValueError, match="must be JSON objects"):
            _parse_compact(token)


def test_a_well_formed_token_parses_and_its_signing_input_is_the_first_two_segments() -> None:
    """The accepting side, without which refusing everything would pass.

    The `signing_input` assertion is the part that matters: it is what the signature is
    verified over, so if it were built from the wrong bytes every signature check would
    be wrong in a way no refusal test could see.
    """
    header = _segment({"alg": "HS256", "typ": "JWT"})
    claims = _segment({"sub": "u1"})
    token = f"{header}.{claims}.AAAA"

    parsed_header, parsed_claims, signing_input, signature = _parse_compact(token)

    assert parsed_header == {"alg": "HS256", "typ": "JWT"}
    assert parsed_claims == {"sub": "u1"}
    assert signing_input == f"{header}.{claims}".encode()
    assert signature == b"\x00\x00\x00"


def test_the_per_segment_cap_holds_at_exactly_the_boundary() -> None:
    """Both sides of the bound, to the character, in whichever mode is running.

    This used to stop eight characters short, because the twins disagreed about the
    last two and asserting inside that window would have pinned a discrepancy rather
    than a contract. Measured then, at `_MAX_SEGMENT_BYTES` = 16 KiB:

        length      native                pure
        <= 21847    reaches base64        reaches base64
        21848       refused (size cap)    reaches base64
        21849       refused (size cap)    reaches base64
        >= 21850    refused               refused

    Native refused when `b64len // 4 * 3 > max_seg`; the pure branch computed
    `(_MAX_SEGMENT_BYTES * 4) // 3 + 4` = 21849 and refused only what was strictly
    greater, so one token had two verdicts depending on which build was installed.
    The pure branch now evaluates native's expression itself, so there is no window
    left and the boundary is a contract both twins can be held to.

    The accepting side matters as much as the refusing one: without it the cap could
    be tightened to nothing and every test above would still pass. A segment of `a`s
    is not valid base64url content, so it fails *later* -- what is asserted here is
    only that it does not fail on the size cap.
    """
    with pytest.raises(ValueError) as accepted:
        _parse_compact("a" * MAX_SEGMENT_B64_ACCEPTED + ".b.c")
    assert "size cap" not in str(accepted.value)

    with pytest.raises(ValueError, match="size cap"):
        _parse_compact("a" * (MAX_SEGMENT_B64_ACCEPTED + 1) + ".b.c")


def test_the_two_twins_answer_a_bad_base64_length_the_same_way() -> None:
    """A segment whose characters are legal but whose length is not.

    The charset check cannot catch this -- every character is in the alphabet -- so
    the pure branch reached `base64.urlsafe_b64decode` and let `binascii.Error`
    escape carrying CPython's wording ("Invalid base64-encoded string: number of data
    characters ..."), where native answered with wreath's. Both are `ValueError`
    subclasses, so every caller caught it and nothing failed loudly; what differed was
    the message, and only in the fallback build.

    **One residue is invalid, not all of them.** An unpadded base64 segment may be
    0, 2 or 3 characters more than a multiple of four; only 1-more-than is impossible,
    because no number of base64 characters encodes to it. Both cases below are that
    residue, at two different lengths. The valid residues are covered by
    `test_a_valid_base64_length_gets_past_the_decoder` below -- keeping them apart is
    the point, since a check that refused every length would pass a test that only
    ever fed it bad ones.
    """
    for segment in ("b", "abcde"):
        assert len(segment) % 4 == 1, segment
        with pytest.raises(ValueError, match="must be unpadded base64url"):
            _parse_compact(f"{segment}.AAAA.AAAA")


def test_a_valid_base64_length_gets_past_the_decoder() -> None:
    """The accepting side of the same check, so it cannot be tightened to nothing.

    These decode; they then fail further along on UTF-8 or JSON, which is a different
    guard and a different message. What is asserted is only that the base64 refusal is
    not what answered.
    """
    for segment in ("ab", "abc", "abcd"):
        assert len(segment) % 4 != 1, segment
        with pytest.raises(ValueError) as raised:
            _parse_compact(f"{segment}.AAAA.AAAA")
        assert "unpadded base64url" not in str(raised.value), segment


def test_wreath_pure_selects_the_python_branch() -> None:
    """That the other mode exists and is reachable -- the reason this file is
    mode-agnostic rather than pinned to one implementation.

    This one *is* a subprocess, because import-time selection from the environment
    cannot be observed any other way. It is deliberately the only one: it asserts which
    branch a pure interpreter picks and nothing about behaviour, so it does not need to
    kill a mutant to be worth having. Run the mutation sweep with `WREATH_PURE=1` to
    measure the branch this proves is there.
    """
    environment = os.environ.copy()
    environment["WREATH_PURE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from wreath._auth import jwt;"
            " print('pure' if jwt._native_parse is None else 'native')",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "pure"


# --- _b64url_decode: strict in both twins ---------------------------------------
#
# Found by differentially testing the two twins over generated input, not by a failing
# test. Native `jose_b64url_decode` documents itself "strict, unpadded, URL-safe"; the
# pure twin was `base64.urlsafe_b64decode` with re-padding, which is none of those --
# it re-pads, translates `-`/`_` to `+`/`/` and then accepts `+`/`/` as input too, and
# discards characters outside the alphabet.
#
# `_parse_compact` was not affected: it charset-checks each segment first. The exposed
# callers were `key_from_jwk` and `peek_header`, which do not. So a JWKS whose key
# material carried padding or standard-base64 characters built a working verifier on a
# pure build and raised on a native one -- and since `-` and `+` decode to the same six
# bits, two spellings of one JWK yielded the same key.


def test_b64url_accepts_exactly_the_unpadded_urlsafe_alphabet() -> None:
    """The accepting side, so "reject everything" cannot pass the refusals below."""
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
    """Each refusal in whichever mode is running, which is the parity claim.

    `QU+D` is the one to keep in mind: the stdlib decodes it to the same bytes as
    `QU-D`, so without the charset check one key had two spellings -- the same
    "unbounded family of strings" hazard `_B64URL_SEGMENT`'s comment describes for
    token segments.
    """
    from wreath._auth.jwt import _b64url_decode

    with pytest.raises(ValueError):
        _b64url_decode(data)


def test_a_jwk_with_loose_base64_is_refused() -> None:
    """The shipped path the leniency actually reached.

    `key_from_jwk` hands `k` straight to `_b64url_decode`, so this is where a JWKS
    fetched from a third party met the difference between the two builds.
    """
    from wreath._auth.jwt import key_from_jwk

    clean = "A" * 43
    assert key_from_jwk({"kty": "oct", "k": clean}).secret == _b64url(clean)
    for loose in (clean + "=", clean[:-1] + "+", clean[:-1] + "/", clean + "\n"):
        with pytest.raises(ValueError):
            key_from_jwk({"kty": "oct", "k": loose})


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
