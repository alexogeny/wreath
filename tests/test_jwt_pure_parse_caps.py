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

**The two twins do not word their refusals identically** (native: "must have exactly
two dots"; pure: "must have three non-empty segments"), so `MALFORMED` below carries
both spellings and each test asserts the one belonging to the active mode. Pinning the
*message* rather than just `ValueError` is what makes these bite: the caps are ordered
and overlapping, so deleting one `raise` often just falls through to the next and still
raises `ValueError`. A message-agnostic first draft left seven mutants alive for exactly
that reason.
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
MAX_SEGMENT_B64 = (MAX_SEGMENT_BYTES * 4) // 3 + 4


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
#: segment past the 21849 segment cap, so deleting the total-cap `raise` merely falls
#: through to the next one and still raises `ValueError`. Only the wording
#: distinguishes which guard fired.
#:
#: The table also records that the twins are **not** message-identical, and that native
#: is the more specific of the two: it separates "exactly two dots" (wrong count) from
#: "has an empty segment" (right count, empty part), where the Python branch answers
#: both with one message. That is a real difference in diagnostic quality, not a typo,
#: and it is the sort of thing that is invisible until both paths are exercised.
MALFORMED = [
    # Past the whole-token ceiling, before any splitting happens.
    ("a." + "b" * MAX_TOKEN_BYTES + ".c", "token over the total cap",
     "token length out of range", "compact JWT exceeds maximum size"),
    # Wrong segment count, both directions.
    ("a.b", "two segments",
     "compact JWT must have exactly two dots", "compact JWT must have three non-empty segments"),
    ("a", "no dots at all",
     "compact JWT must have exactly two dots", "compact JWT must have three non-empty segments"),
    ("a.b.c.d", "four segments",
     "compact JWT must have exactly two dots", "compact JWT must have three non-empty segments"),
    # Right count, one segment empty. An empty signature is the classic `alg=none`
    # shape, so this refusal has a history.
    ("a.b.", "empty signature",
     "compact JWT has an empty segment", "compact JWT must have three non-empty segments"),
    (".b.c", "empty header",
     "compact JWT has an empty segment", "compact JWT must have three non-empty segments"),
    ("a..c", "empty payload",
     "compact JWT has an empty segment", "compact JWT must have three non-empty segments"),
    ("..", "all three empty",
     "compact JWT has an empty segment", "compact JWT must have three non-empty segments"),
    # An oversized segment inside a token under the total cap: the guard the source
    # comment singles out, a giant segment reaching the JSON parser. The one message
    # the two twins share.
    ("a." + "b" * (MAX_SEGMENT_B64 + 1) + ".c", "segment over the per-segment cap",
     "JWT segment exceeds size cap", "JWT segment exceeds size cap"),
    # Outside unpadded base64url. "=" matters most: accepting padding would give one
    # token two spellings, and a signature covers the bytes that were sent.
    ("a=.b.c", "base64 padding",
     "invalid base64url in JWT segment", "a compact JWT segment must be unpadded base64url"),
    ("a+b.c.d", "standard-base64 plus",
     "invalid base64url in JWT segment", "a compact JWT segment must be unpadded base64url"),
    ("a/b.c.d", "standard-base64 slash",
     "invalid base64url in JWT segment", "a compact JWT segment must be unpadded base64url"),
    ("a b.c.d", "a space",
     "invalid base64url in JWT segment", "a compact JWT segment must be unpadded base64url"),
    ("aé.b.c", "a non-ASCII character",
     "invalid base64url in JWT segment", "a compact JWT segment must be unpadded base64url"),
]


@pytest.mark.parametrize(
    "token,why,native_message,pure_message",
    MALFORMED,
    ids=[case[1] for case in MALFORMED],
)
def test_a_malformed_compact_token_is_refused_by_the_right_guard(
    token: str, why: str, native_message: str, pure_message: str
) -> None:
    expected = pure_message if MODE == "pure" else native_message
    with pytest.raises(ValueError, match=re.escape(expected)):
        _parse_compact(token)


def test_the_per_segment_cap_is_reported_by_both_twins_in_the_same_words() -> None:
    """The one refusal whose message the two implementations share.

    Worth pinning separately because it is the DoS guard the comment singles out, and a
    shared message is a small contract between the twins that nothing else records.
    """
    assert MALFORMED[8][2] == MALFORMED[8][3] == "JWT segment exceeds size cap"
    with pytest.raises(ValueError, match="segment exceeds size cap"):
        _parse_compact("a." + "b" * (MAX_SEGMENT_B64 + 1) + ".c")


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


def test_a_segment_well_inside_the_cap_gets_past_the_size_check() -> None:
    """The bound from its accepting side, or it could be tightened to nothing.

    Such a segment is not valid base64url content, so it fails *later*; what this
    asserts is that it does not fail on the size cap.

    **It stops eight characters short of the cap on purpose. The two twins disagree
    about the last two.** Measured, at `_MAX_SEGMENT_BYTES` = 16 KiB:

        length      native                pure
        <= 21847    reaches base64        reaches base64
        21848       refused (size cap)    reaches base64
        21849       refused (size cap)    reaches base64
        >= 21850    refused               refused

    Native's bound is the base64 length of a 16 KiB payload (21848). The Python branch
    computes `(_MAX_SEGMENT_BYTES * 4) // 3 + 4` = 21849 and refuses only what is
    strictly greater, so two lengths are refused by one implementation and accepted by
    the other -- and the Python branch's own comment says it enforces "the same hard
    size caps as native jose_parse". It does not, by two characters.

    Nothing here asserts inside that window, because a test that did would be pinning
    a discrepancy rather than a contract. Two bytes on a 16 KiB cap is not a security
    hole, but which bound is intended is a decision, not a detail: aligning the Python
    branch to 21848 is the conservative direction and is a one-character change
    (`>=`), and it is deliberately left for a human.
    """
    with pytest.raises(ValueError) as raised:
        _parse_compact("a" * (MAX_SEGMENT_B64 - 8) + ".b.c")
    assert "size cap" not in str(raised.value)


def test_both_twins_agree_beyond_the_disputed_window() -> None:
    """Whatever the boundary is, two characters past it every implementation refuses.

    This is the part of the cap that is a contract rather than an accident, so it is
    the part worth a test that runs in both modes.
    """
    with pytest.raises(ValueError, match="size cap"):
        _parse_compact("a" * (MAX_SEGMENT_B64 + 1) + ".b.c")


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
