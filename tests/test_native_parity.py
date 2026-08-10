"""Independent-anchor tests for the non-router accelerators.

Every primitive here is driven against an expectation it did not produce. That
is the whole point of the file. It used to assert that two Wreath
implementations agreed with each other, which is weak evidence: two
implementations written by the same hand can be wrong in both halves and agree,
and the agreement then reads as proof. `AGENTS.md` makes the argument for
`wreath.edge`, which earned its native-only status by being checked against
haproxy and nginx rather than against itself; these are the same primitives one
layer down, and they get the same treatment.

The anchor for each section, so a reader can check a number rather than trust it:

* headers, http     -- values written out, against RFC 9110 and RFC 9112
* codecs            -- `urllib.parse.parse_qsl` and `urllib.parse.unquote_to_bytes`
* websocket         -- the RFC 6455 section 5.3 masking rule, transcribed, and
                       the section 5.7 worked examples that check the transcription
* multipart         -- the stdlib `email` package's own MIME parser
* json              -- the stdlib `json` module, and RFC 8259 where wreath is
                       deliberately stricter than it
* postgres codecs   -- `struct` against the PostgreSQL binary wire format

`_twins()` labels the implementation under test, so a parametrised failure names
the primitive as well as the input.
"""

from __future__ import annotations

import email.parser
import email.policy
import json as stdlib_json
import math
import random
import re
import struct
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from wreath._native import _core

native = pytest.mark.skipif(_core is None, reason="native extension not built")


def _twins(name: str) -> Iterator[tuple[str, Callable[..., object]]]:
    """One primitive, labelled, so a parametrised failure names what broke.

    Still a generator over a one-element sequence: the call sites read
    `for label, fn in _twins(...)`, and the label is what turns "assertion
    failed on input X" into "`json_dumps` failed on input X".
    """
    yield name, getattr(_core, name)


# --- headers --------------------------------------------------------------

HEADER_LIST = [(b"host", b"x"), (b"accept", b"a"), (b"accept", b"b")]


def test_find_header_returns_the_first_of_a_repeated_field() -> None:
    """RFC 9110 section 5.3: repeated field lines are ordered, so the first wins.

    `accept` appears twice with different values, which is what makes this an
    assertion rather than a tautology -- an implementation that returned the
    last match would still return *a* correct-looking header.
    """
    for label, find_header in _twins("find_header"):
        assert find_header(HEADER_LIST, b"accept") == b"a", label
        assert find_header(HEADER_LIST, b"host") == b"x", label
        assert find_header(HEADER_LIST, b"missing") is None, label


def test_build_header_map_keeps_the_first_of_a_repeated_field() -> None:
    """Same rule as above, collapsed into a mapping: `accept` must be `a`, not `b`."""
    for label, build_header_map in _twins("build_header_map"):
        assert build_header_map(HEADER_LIST) == {b"host": b"x", b"accept": b"a"}, label


# --- codecs ---------------------------------------------------------------

QUERY_SAMPLES = [
    b"", b"a=1", b"a=1&b=2", b"a=%C3%A9&b=x+y", b"flag&k=", b"a=1&&b=2", b"%zz=%",
    b"name=%E2%9C%93&n=1", b"a%20b=c%2Fd",
]
# `parse_cookies` has no stdlib counterpart that agrees: `http.cookies.SimpleCookie`
# implements the *response* `Set-Cookie` grammar, tolerates a bare token, and does
# not answer the duplicate-name question the same way. So these are spelled out,
# with the clause each one turns on.
#
# RFC 6265 section 4.2.1 is the request-header grammar:
#
#     cookie-string = cookie-pair *( ";" SP cookie-pair )
#     cookie-pair   = cookie-name "=" cookie-value
#     cookie-name   = token
#
# Whitespace is trimmed from both halves of a pair, per RFC 6265bis section
# 5.8.3 ("Remove any leading or trailing WSP characters from the name string and
# the value string"). This used to be trimmed only around the ";" separators and
# never around the "=", so `" a = 1 "` yielded the name `"a "` -- which RFC 9110
# section 5.6.2 says is not a `token` at all, SP not being a `tchar`. A
# comparison test could not see that: agreeing on a deviation looks exactly like
# agreeing on conformance.
COOKIE_EXPECTATIONS = [
    pytest.param(b"", {}, id="empty"),
    pytest.param(b"a=1", {"a": "1"}, id="one-pair"),
    pytest.param(b"a=1; b=2", {"a": "1", "b": "2"}, id="two-pairs"),
    pytest.param(b" a = 1 ; b=2 ", {"a": "1", "b": "2"}, id="whitespace-trimmed"),
    # RFC 6265 does not say what a server should do with a repeated cookie-name.
    # Wreath keeps the first, which matches how it resolves a repeated header
    # field above; written out because the RFC will not settle it.
    pytest.param(b"a=1; a=2", {"a": "1"}, id="duplicate-name-keeps-first"),
    # A `token` is `1*tchar`, so it cannot be empty and this is not a cookie-pair.
    pytest.param(b"=nope; ok=1", {"ok": "1"}, id="empty-name-dropped"),
    # cookie-pair requires the "="; a bare token is not one.
    pytest.param(b"bare", {}, id="no-equals-dropped"),
]


@pytest.mark.parametrize("sample", QUERY_SAMPLES)
def test_parse_qs_matches_urllib(sample: bytes) -> None:
    """`urllib.parse.parse_qsl` reads the same form encoding, and did not write ours.

    `keep_blank_values=True` because wreath reports `flag` and `k=` as present
    with an empty value rather than dropping them, which is what a handler
    binding an optional query parameter needs to see. The samples cover the
    cases the two could plausibly disagree on: a `+` that means a space, a `%zz`
    that is not an escape, an empty field between two `&`, and multi-byte UTF-8.
    """
    expected = urllib.parse.parse_qsl(sample.decode("latin-1"), keep_blank_values=True)
    for label, parse_qs in _twins("parse_qs"):
        assert parse_qs(sample) == expected, label


@pytest.mark.parametrize(("sample", "expected"), COOKIE_EXPECTATIONS)
def test_parse_cookies_follows_the_rfc_6265_cookie_pair_grammar(
    sample: bytes, expected: dict[str, str]
) -> None:
    for label, parse_cookies in _twins("parse_cookies"):
        assert parse_cookies(sample) == expected, label


def test_percent_decode_matches_urllib_unquote() -> None:
    """`urllib.parse.unquote_to_bytes` is the stdlib's reading of RFC 3986 section 2.1.

    The `plus_as_space` arm is the `application/x-www-form-urlencoded` rule,
    where `+` means a space and `%2B` means a literal `+`. The substitution has
    to happen *before* the percent decoding for that to hold, and expressing the
    expectation in that order is what makes this a check rather than a
    restatement -- an implementation that decoded first would turn `%2B` into a
    space and this would catch it.

    The alphabet is chosen so roughly one character in five starts an escape and
    the high bytes make truncated multi-byte sequences common, which is where a
    hand-written decoder runs off the end of the buffer.
    """
    rng = random.Random(1)
    alphabet = b"abc%20+/=&AZ09" + bytes(range(0x80, 0x88))
    twins = list(_twins("percent_decode"))
    for _ in range(500):
        data = bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        for plus in (False, True):
            expected = urllib.parse.unquote_to_bytes(
                data.replace(b"+", b" ") if plus else data
            )
            for label, percent_decode in twins:
                assert percent_decode(data, plus_as_space=plus) == expected, (
                    label,
                    data,
                    plus,
                )


def test_percent_decode_distinguishes_an_escaped_plus_from_a_literal_one() -> None:
    """The one case the fuzz above proves but does not name."""
    for label, percent_decode in _twins("percent_decode"):
        assert percent_decode(b"a%20b+c", plus_as_space=True) == b"a b c", label
        assert percent_decode(b"a%2Bb+c", plus_as_space=True) == b"a+b c", label
        assert percent_decode(b"a%2Bb+c", plus_as_space=False) == b"a+b+c", label


# --- websocket ------------------------------------------------------------

def _rfc6455_mask(data: bytes, key: bytes) -> bytes:
    """RFC 6455 section 5.3 transcribed one octet at a time.

        j = i MOD 4
        transformed-octet-i = original-octet-i XOR masking-key-octet-j

    Deliberately the slow, obvious form: it is the reference the SWAR and
    vector arms in `ws.c` are checked against, so it must be readable as the
    spec text rather than fast. `test_ws_mask_reproduces_the_rfc_6455_worked_example`
    checks this transcription itself against a published vector, so a mistake
    here cannot quietly become the expectation.
    """
    return bytes(byte ^ key[index % 4] for index, byte in enumerate(data))


# RFC 6455 section 5.7, "Examples". Each is (frame bytes, (fin, opcode, payload,
# consumed)); `consumed` is the whole frame, since these are complete.
RFC6455_FRAMES = [
    pytest.param(
        bytes.fromhex("810548656c6c6f"),
        (True, 0x1, b"Hello", 7),
        id="single-frame-unmasked-text",
    ),
    pytest.param(
        bytes.fromhex("818537fa213d7f9f4d5158"),
        (True, 0x1, b"Hello", 11),
        id="single-frame-masked-text",
    ),
    pytest.param(
        bytes.fromhex("010348656c"), (False, 0x1, b"Hel", 5), id="fragment-1-of-2"
    ),
    pytest.param(
        bytes.fromhex("80026c6f"), (True, 0x0, b"lo", 4), id="fragment-2-of-2"
    ),
    pytest.param(
        bytes.fromhex("890548656c6c6f"), (True, 0x9, b"Hello", 7), id="unmasked-ping"
    ),
    pytest.param(
        bytes.fromhex("8a0548656c6c6f"), (True, 0xA, b"Hello", 7), id="unmasked-pong"
    ),
    # "256 bytes binary message in a single unmasked frame": 0x82 0x7E 0x0100 ...
    # This is the only vector that exercises the 16-bit extended length.
    pytest.param(
        bytes.fromhex("827e0100") + bytes(256),
        (True, 0x2, bytes(256), 260),
        id="binary-256-extended-length-16",
    ),
    # "64KiB binary message in a single unmasked frame": 0x82 0x7F 0x0000000000010000 ...
    # and the only one that exercises the 64-bit extended length.
    pytest.param(
        bytes.fromhex("827f0000000000010000") + bytes(65536),
        (True, 0x2, bytes(65536), 65546),
        id="binary-64kib-extended-length-64",
    ),
]


@pytest.mark.parametrize(("frame", "expected"), RFC6455_FRAMES)
def test_ws_parse_frame_decodes_the_rfc_6455_worked_examples(
    frame: bytes, expected: tuple[bool, int, bytes, int]
) -> None:
    """The published frames, byte for byte, from RFC 6455 section 5.7.

    These pin the header layout of section 5.2 -- FIN in bit 0, opcode in bits
    4-7, MASK in bit 8, then a 7-bit length or a 16- or 64-bit extended one,
    then the 4-byte key -- against numbers the working group wrote down, which
    is the one thing neither twin can have got wrong together.
    """
    for label, ws_parse_frame in _twins("ws_parse_frame"):
        assert ws_parse_frame(frame) == expected, label


def test_ws_mask_reproduces_the_rfc_6455_worked_example() -> None:
    """RFC 6455 section 5.7: masking "Hello" with key 0x37fa213d gives 0x7f9f4d5158.

    Asserted against `_rfc6455_mask` first, because that function is the anchor
    for the fuzz test below and an anchor nobody checked is just a third
    implementation.
    """
    key = bytes.fromhex("37fa213d")
    expected = bytes.fromhex("7f9f4d5158")
    assert _rfc6455_mask(b"Hello", key) == expected
    for label, ws_mask in _twins("ws_mask"):
        assert ws_mask(b"Hello", key) == expected, label


def test_ws_mask_matches_the_rfc_6455_definition_at_every_length() -> None:
    """Sizes chosen around the word boundaries the C arms switch on.

    0, 1 and 7 stay in the byte tail; 8 and 9 cross into the 64-bit SWAR body;
    1000 runs the vector arm long enough for a key-rotation bug to show, which
    is the classic defect here -- an implementation that reset `i MOD 4` at each
    block boundary is correct for every length divisible by 4 and wrong
    otherwise.
    """
    rng = random.Random(2)
    key = bytes(rng.randint(0, 255) for _ in range(4))
    twins = list(_twins("ws_mask"))
    for size in (0, 1, 7, 8, 9, 64, 1000):
        data = bytes(rng.randint(0, 255) for _ in range(size))
        expected = _rfc6455_mask(data, key)
        for label, ws_mask in twins:
            assert ws_mask(data, key) == expected, (label, size)
            # XOR is an involution, so unmasking is the same operation.
            assert ws_mask(expected, key) == data, (label, size)


def test_ws_parse_frame_recovers_the_payload_that_was_framed() -> None:
    """The payload is known before it is framed, so the parse is checked against it.

    The frame is built here from the RFC 6455 section 5.2 layout and the section
    5.3 masking rule rather than by calling `ws_mask`, so the parser is not
    being asked to undo its own work.
    """
    rng = random.Random(3)
    twins = list(_twins("ws_parse_frame"))
    for _ in range(300):
        payload = bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 300)))
        key = bytes(rng.randint(0, 255) for _ in range(4))
        n = len(payload)
        if n < 126:
            header = bytes([0x81, 0x80 | n]) + key
        else:
            header = bytes([0x81, 0x80 | 126, n >> 8, n & 0xFF]) + key
        frame = header + _rfc6455_mask(payload, key)
        expected = (True, 0x1, payload, len(frame))
        for label, ws_parse_frame in twins:
            assert ws_parse_frame(frame) == expected, label
            # One byte is short of the two-octet minimum header, so the frame is
            # not yet decidable and must be reported as incomplete, not refused.
            assert ws_parse_frame(frame[:1]) is None, label


# --- multipart ------------------------------------------------------------

def _multipart_body(boundary: bytes) -> bytes:
    d = b"--" + boundary
    return (
        d + b"\r\ncontent-disposition: form-data; name=\"field\"\r\n\r\nvalue\r\n"
        + d + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\""
        b"\r\ncontent-type: text/plain\r\n\r\nline1\r\n\r\nline2\r\n"
        + d + b"--\r\n"
    )


#: What `_multipart_body` contains, written out. The second part's body holds a
#: bare CRLF CRLF, which is the sequence that ends a header block -- a parser
#: that rescans for it inside the body loses the tail.
EXPECTED_PARTS = [
    ([(b"content-disposition", b'form-data; name="field"')], b"value"),
    (
        [
            (b"content-disposition", b'form-data; name="file"; filename="a.txt"'),
            (b"content-type", b"text/plain"),
        ],
        b"line1\r\n\r\nline2",
    ),
]


def _email_multipart_parse(
    body: bytes, boundary: bytes
) -> list[tuple[list[tuple[bytes, bytes]], bytes]]:
    """Parse the same body with the stdlib `email` package.

    An independent MIME implementation, older than this one and written from
    RFC 2046 directly. It needs the boundary in a `Content-Type` header rather
    than as an argument, so one is synthesised; nothing else is adjusted except
    lowering the header names, which is the normalisation wreath applies and
    `email` does not.
    """
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b'Content-Type: multipart/form-data; boundary="'
        + boundary
        + b'"\r\nMIME-Version: 1.0\r\n\r\n'
        + body
    )
    return [
        (
            [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in part.raw_items()
            ],
            part.get_payload(decode=True),
        )
        for part in message.iter_parts()
    ]


def test_the_email_anchor_agrees_with_the_written_out_parts() -> None:
    """Check the anchor before trusting it, on the one body whose content is fixed."""
    assert _email_multipart_parse(_multipart_body(b"BOUNDARY"), b"BOUNDARY") == (
        EXPECTED_PARTS
    )


def test_multipart_parse_matches_the_stdlib_email_parser() -> None:
    body = _multipart_body(b"BOUNDARY")
    expected = _email_multipart_parse(body, b"BOUNDARY")
    for label, multipart_parse in _twins("multipart_parse"):
        assert multipart_parse(body, b"BOUNDARY") == expected, label


# --- substring search: repetitive haystacks and overlapping prefixes --------
#
# Boundaries are attacker-supplied, so the delimiter search must stay correct on
# input built to maximise partial matches. These run against whatever wreath_memmem
# resolved to in this build (libc memmem on Linux/macOS/BSD); the portable
# fallback is validated separately by the diagnostic build below, because a
# result produced by glibc's memmem says nothing about the fallback.

REPETITIVE_BOUNDARIES = [
    pytest.param(b"a" * 60, id="all-same"),
    pytest.param(b"a" * 59 + b"b", id="overlapping-prefix"),
    pytest.param(b"ab" * 30, id="period-2"),
    pytest.param(b"aab" * 20, id="period-3"),
    pytest.param(b"a" * 30 + b"a" * 29 + b"b", id="long-run-then-break"),
    # RFC 2046 caps a boundary at 70 bytes, which makes the searched delimiter
    # ("--" + boundary + "--") 74 bytes: the longest needle this ever sees.
    pytest.param(b"-" * 70, id="max-length-delimiter"),
]


@pytest.mark.parametrize("boundary", REPETITIVE_BOUNDARIES)
def test_a_repetitive_boundary_still_splits_into_the_written_out_parts(
    boundary: bytes,
) -> None:
    """The parts do not depend on the boundary, so the expectation is the same one.

    That independence is the assertion: a search that mishandles overlapping
    prefixes produces a different split for `"a" * 59 + "b"` than for
    `"BOUNDARY"`, and comparing the two parsers to each other could never say so.
    """
    body = _multipart_body(boundary)
    assert _email_multipart_parse(body, boundary) == EXPECTED_PARTS
    for label, multipart_parse in _twins("multipart_parse"):
        assert multipart_parse(body, boundary) == EXPECTED_PARTS, label


def test_near_miss_boundaries_do_not_produce_spurious_parts() -> None:
    """A preamble full of almost-boundaries must not match, but the real one must.

    Every near miss shares a 39-byte prefix with the boundary, so a search that
    mishandles partial matches either invents a part or loses the real one --
    and the count is asserted as well as the content, because inventing a part
    and losing one are different defects with the same symptom otherwise.
    """
    boundary = b"a" * 40
    near_miss = b"--" + b"a" * 39 + b"b\r\n"
    body = near_miss * 200 + _multipart_body(boundary)
    assert _email_multipart_parse(body, boundary) == EXPECTED_PARTS
    for label, multipart_parse in _twins("multipart_parse"):
        assert multipart_parse(body, boundary) == EXPECTED_PARTS, label


def test_body_without_the_boundary_is_rejected_with_that_message() -> None:
    """The distinct message, not merely `ValueError`.

    Asserting only the type let the not-found refusal be deleted from the pure
    parser without the test noticing: parsing then ran off a `find` result of -1
    and failed a few lines later for an unrelated reason, which is the same
    exception type reporting a different fact.

    The stdlib anchor agrees that the body is broken but not on what to do about
    it: `email` records a `StartBoundaryNotFoundDefect` and hands back the whole
    thing as a single non-multipart payload, where wreath refuses. So the
    *diagnosis* is anchored -- an independent parser confirms there is no
    boundary here, which is what stops this from being a test that merely
    asserts wreath's own opinion -- and only the choice to raise is wreath's.
    """
    boundary = b"a" * 40
    body = (b"--" + b"a" * 39 + b"b\r\n") * 50  # near misses only

    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b'Content-Type: multipart/form-data; boundary="'
        + boundary
        + b'"\r\nMIME-Version: 1.0\r\n\r\n'
        + body
    )
    assert [type(defect).__name__ for defect in message.defects] == [
        "StartBoundaryNotFoundDefect",
        "MultipartInvariantViolationDefect",
    ]
    for _, multipart_parse in _twins("multipart_parse"):
        with pytest.raises(ValueError, match="multipart boundary not found"):
            multipart_parse(body, boundary)


# --- json -----------------------------------------------------------------

JSON_SAMPLES = [
    None, True, False, 0, -1, 2**70, 3.14, 1e300, "", "plain", "quote\"back\\slash",
    "tab\tnl\n", "unicode-é-✓", [], {}, [1, 2, 3], {"k": "v"},
    {"nested": {"a": [1, {"b": None}], "s": "xy"}}, ["mix", 1, 2.5, True, None],
]


def _stdlib_dumps(value: object) -> bytes:
    """The stdlib encoder, configured the way wreath's output is defined.

    Compact separators and `ensure_ascii=False` are not incidental -- they are
    the wire format wreath emits, so this is the expectation rather than a
    normalisation of it. Byte-for-byte equality is therefore the right assertion
    and `loads(a) == loads(b)` would be a weaker one.
    """
    return stdlib_json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")



@pytest.mark.parametrize("sample", JSON_SAMPLES)
def test_json_dumps_matches_the_stdlib_encoder(sample: object) -> None:
    for label, json_dumps in _twins("json_dumps"):
        assert json_dumps(sample) == _stdlib_dumps(sample), label


def test_json_dumps_refuses_what_the_json_grammar_cannot_carry() -> None:
    """Each refusal by its own message, because they are different refusals.

    The stdlib is not the anchor here: `json.dumps` emits the bare words `NaN`
    and `Infinity` by default, which RFC 8259 section 6 has no production for
    (it admits only `number = [ minus ] int [ frac ] [ exp ]`). Wreath's refusal
    is a deliberate narrowing towards the RFC, so the stdlib's *permissiveness*
    is asserted below rather than borrowed -- otherwise a reader cannot tell
    whether these four are wreath's rules or Python's.

    Asserting only the exception type would pass on whichever branch fired,
    including a fallthrough that rejects everything.
    """
    assert stdlib_json.dumps(float("nan")) == "NaN"
    assert stdlib_json.dumps(float("inf")) == "Infinity"

    for _, json_dumps in _twins("json_dumps"):
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="JSON values must be finite numbers"):
                json_dumps(value)
        # RFC 8259 section 4: a member name is a `string`, so an int key has no
        # unambiguous rendering and is refused rather than stringified.
        with pytest.raises(TypeError, match="JSON object keys must be str, got int"):
            json_dumps({1: "int-key"})
        with pytest.raises(
            TypeError, match="object of type object is not JSON serializable"
        ):
            json_dumps(object())


def test_json_dumps_integer_boundaries() -> None:
    # Regression for the two-digits-at-a-time integer writer: exercise every
    # digit-count transition and the long-long overflow fallback. `json.dumps`
    # renders an int through `int.__repr__`, so the stdlib is an exact anchor
    # here including past 2**63 where the C writer changes strategy.
    values = [0, -0, 5, -5]
    for exp in range(1, 25):
        for base in (10**exp, 2**exp):
            values += [base - 1, base, base + 1, -(base - 1), -base, -(base + 1)]
    values += [2**63 - 1, -(2**63), 2**63, -(2**63) - 1, 2**100, -(2**100)]
    twins = list(_twins("json_dumps"))
    for value in values:
        expected = stdlib_json.dumps(value).encode()
        for label, json_dumps in twins:
            assert json_dumps(value) == expected, (label, value)


def test_json_dumps_escape_positions() -> None:
    # Regression for the SWAR escape scanner: escapes at every offset within
    # and around the 8-byte window, for several string lengths. Anchored on the
    # stdlib encoder, which escapes per RFC 8259 section 7 -- the two-character
    # forms for `"` `\` and the named controls, `\u00XX` for the rest.
    twins = list(_twins("json_dumps"))
    for length in (0, 1, 7, 8, 9, 15, 16, 17, 31, 64):
        base = "a" * length
        samples = [base]
        for pos in range(length):
            for special in ('"', "\\", "\n", "\x00", "\x1f"):
                samples.append(base[:pos] + special + base[pos + 1 :])
        samples.append(base + "é✓\U0001f600")
        for sample in samples:
            expected = _stdlib_dumps(sample)
            for label, json_dumps in twins:
                assert json_dumps(sample) == expected, (label, sample)


def test_json_dumps_escapes_exactly_the_characters_rfc_8259_requires() -> None:
    """Named because the sweep above proves it without ever saying which forms.

    RFC 8259 section 7: `"` and `\\` and the C0 controls must be escaped, and
    everything else -- including DEL and every non-ASCII character -- goes
    through as itself once `ensure_ascii` is off.
    """
    for label, json_dumps in _twins("json_dumps"):
        assert json_dumps('"') == b'"\\""', label
        assert json_dumps("\\") == b'"\\\\"', label
        assert json_dumps("\b\f\n\r\t") == b'"\\b\\f\\n\\r\\t"', label
        assert json_dumps("\x00\x1f") == b'"\\u0000\\u001f"', label
        # Not escaped: `/` has an optional escape the RFC does not require, and
        # DEL is not a C0 control.
        assert json_dumps("/\x7f") == b'"/\x7f"', label
        assert json_dumps("é✓\U0001f600") == '"é✓\U0001f600"'.encode(), label


# --- json decoding ----------------------------------------------------------

JSON_DOCUMENTS = [
    "null", "true", "false", "0", "-0", "1", "-1", "3.14", "-2.5e10", "1e400",
    "-1e400", "0.1e-3", "1E+2", "-0.0", "123456789012345678", "1234567890123456789",
    "123456789012345678901234567890", '""', '"plain"', '"\\u00e9"', '"é✓"',
    '"\\n\\t\\\\\\"\\/\\b\\f\\r"', '"\\ud83d\\ude00"', '"\\ud800"', '"\\udc00"',
    '"\\ud800x"', '"\\ud800\\ud800"', "[]", "{}", "[1,2,3]", '{"k":"v"}',
    '{"a":1,"a":2}', '{"nested":{"a":[1,{"b":null}],"s":"xy"}}',
    ' \t\r\n {"padded" : [ 1 , 2 ] } \n ', "NaN", "Infinity", "-Infinity",
    "[NaN, -Infinity]", '"' + "x" * 300 + '"',
]

JSON_MALFORMED = [
    "", " ", "{", "[", '"', '"abc', "1.", "01", "-01", "-", "+1", ".5", "1.e3",
    "1e", "1e+", "[1,]", "[,1]", "[1 2]", '{"a":}', '{"a" 1}', '{"a":1,}',
    "{'a':1}", '{1:2}', "nul", "tru", "falsee-tail", "None", "infinity", "nan",
    '"\\x"', '"\\u12g4"', '"\\u123"', '"a\nb"', '"a\x00b"', "1 2", "{} []",
    "[1]]", "\xe9",
]


def _assert_loads_equal(got: object, want: object) -> None:
    assert type(got) is type(want)
    if isinstance(want, float) and math.isnan(want):
        assert math.isnan(got)  # type: ignore[arg-type]
        return
    # Comparing the canonical re-serialization catches what == alone would
    # miss (-0.0 vs 0.0, 1 vs True, nested NaN, key order) in one shot.
    assert stdlib_json.dumps(got, allow_nan=True) == stdlib_json.dumps(want, allow_nan=True)


@native
@pytest.mark.parametrize("document", JSON_DOCUMENTS)
def test_json_loads_matches_the_stdlib_decoder(document: str) -> None:
    """`json.loads` is the reference the C decoder was written against.

    All three input forms, because the C decoder takes a different path for each
    -- `str` reads code points directly, `bytes` runs the encoding sniff, and
    `bytearray` is the mutable buffer that must not be assumed contiguous-forever.
    """
    for form in (document, document.encode(), bytearray(document.encode())):
        _assert_loads_equal(_core.json_loads(form), stdlib_json.loads(form))


@pytest.mark.parametrize("document", JSON_MALFORMED)
def test_json_loads_rejects_what_the_stdlib_decoder_rejects(document: str) -> None:
    """The stdlib decides *which* documents are invalid; wreath must agree.

    The anchor is asserted first and deliberately: without it, a decoder that
    rejected every document -- including the valid ones -- would pass this test,
    and `pytest.raises(ValueError)` alone cannot tell the difference.
    `JSONDecodeError` rather than plain `ValueError` so the stdlib is refusing
    it as *unparseable* rather than failing for some unrelated reason.
    """
    with pytest.raises(stdlib_json.JSONDecodeError):
        stdlib_json.loads(document)
    if _core is not None:
        with pytest.raises(ValueError):
            _core.json_loads(document)


@native
def test_json_loads_sniffs_byte_encodings_exactly_like_the_stdlib() -> None:
    """RFC 4627 section 3's BOM-less detection, which `json.loads` still implements.

    The first two bytes of any JSON text are ASCII, so the pattern of NUL bytes
    identifies the encoding. Each of these decodes to the same document, which
    is what makes a wrong guess visible.
    """
    expected = {"k": [1, "é"]}
    for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
        data = '{"k": [1, "é"]}'.encode(encoding)
        assert stdlib_json.loads(data) == expected, encoding
        assert _core.json_loads(data) == expected, encoding
    bom = b'\xef\xbb\xbf{"k":1}'
    assert stdlib_json.loads(bom) == {"k": 1}
    assert _core.json_loads(bom) == {"k": 1}


@pytest.mark.parametrize("value", [memoryview(b"1"), 1], ids=["memoryview", "int"])
def test_json_loads_refuses_input_it_cannot_treat_as_a_document(value: Any) -> None:
    """The message, and the same message the stdlib gives.

    `memoryview` is the interesting one: it is buffer-like and reads like an
    obvious thing to accept, but `json.loads` refuses it and so does wreath. The
    stdlib's own wording is the anchor, so this asserts the whole sentence
    rather than the type -- a refusal test that checks only `TypeError` passes
    on any branch that happens to raise one.
    """
    with pytest.raises(TypeError) as stdlib_refusal:
        stdlib_json.loads(value)
    expected = str(stdlib_refusal.value)
    assert expected.startswith("the JSON object must be str, bytes or bytearray")

    if _core is not None:
        with pytest.raises(TypeError, match=re.escape(expected)):
            _core.json_loads(value)


@native
def test_json_loads_deep_nesting_raises_recursion_error() -> None:
    """Deep nesting must raise rather than crash -- on any stack, not just this one.

    The decoder detects exhaustion through CPython's own C-stack check, which
    since 3.12 measures the real stack rather than counting frames. So the depth
    that trips it is a function of the machine's stack limit and the per-frame
    cost of the build, neither of which is a property of the code under test. At
    the default 8 MiB limit the cliff here is ~104,000 levels at ~80 bytes each;
    a fixed 200,000 clears that by only 1.9x, and a runner with a larger stack or
    a build with cheaper frames sails straight past it and fails the test having
    proved nothing. That is what happened in CI.

    So the stack is made small and known rather than assumed. One thread with a
    1 MiB stack puts the cliff near 13,000 levels, and 100,000 clears it by
    roughly sevenfold -- enough margin that halving the per-frame cost would
    still leave the property intact.
    """
    depth = 100_000
    document = "[" * depth + "]" * depth
    outcomes: dict[str, BaseException | None] = {}

    def decode() -> None:
        try:
            _core.json_loads(document)
        except Exception as error:  # noqa: BLE001 -- asserted on below
            outcomes["json_loads"] = error
        else:
            outcomes["json_loads"] = None

    previous = threading.stack_size(1024 * 1024)
    try:
        worker = threading.Thread(target=decode)
        worker.start()
        worker.join()
    finally:
        threading.stack_size(previous)

    # Not `outcomes.get(...)`: a worker that died before running would leave the
    # dict empty, and a `get` returning None would read as "raised nothing".
    assert set(outcomes) == {"json_loads"}, "the decoder never ran"
    assert isinstance(outcomes["json_loads"], RecursionError), (
        f"the decoder did not refuse {depth:,} levels of nesting; "
        f"it raised {outcomes['json_loads']!r}"
    )


def _random_json_value(rng: random.Random, depth: int) -> object:
    kind = rng.randint(0, 7 if depth < 4 else 5)
    if kind == 0:
        return rng.choice([None, True, False])
    if kind == 1:
        return rng.choice(
            [0, 1, -1, 2**31, -(2**31), 2**63 - 1, -(2**63), 2**64, rng.randint(-(10**24), 10**24)]
        )
    if kind == 2:
        return rng.choice(
            [0.0, -0.0, 1.5, -2.25, 1e-300, 1e300, 3.141592653589793, rng.random() * 10**6]
        )
    if kind <= 5:
        alphabet = 'ab"\\\n\t\x00\x1f é✓\U0001f600퟿'
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
    if kind == 6:
        return [_random_json_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    return {
        f"k{index}-{rng.randint(0, 9)}": _random_json_value(rng, depth + 1)
        for index in range(rng.randint(0, 5))
    }


def test_json_fuzz_against_the_stdlib() -> None:
    """Both directions against `json`, over values built to hit the awkward cases.

    The generator emits the things a hand-written codec gets wrong: ints either
    side of the 32- and 64-bit boundaries, subnormal and huge floats, `-0.0`,
    strings carrying quotes, backslashes, NUL, a lone C1 control, astral
    code points and U+D7FF (the code point immediately below the surrogate
    range, where an off-by-one in a surrogate check shows up).

    The decoder is fed the stdlib's *own* rendering at randomised whitespace and
    escaping settings, so it is parsed in forms the encoder above never emits.
    """
    rng = random.Random(20260714)
    dumps_twins = list(_twins("json_dumps"))
    for _ in range(1500):
        value = _random_json_value(rng, 0)
        expected = _stdlib_dumps(value)
        for label, json_dumps in dumps_twins:
            assert json_dumps(value) == expected, label

        rendered = stdlib_json.dumps(
            value,
            ensure_ascii=rng.random() < 0.5,
            indent=rng.choice([None, None, 2]),
            separators=rng.choice([None, (",", ":"), (", ", ": ")]),
        )
        if _core is not None:
            _assert_loads_equal(_core.json_loads(rendered), stdlib_json.loads(rendered))
            encoded = rendered.encode()
            _assert_loads_equal(_core.json_loads(encoded), stdlib_json.loads(encoded))


# --- http parser ----------------------------------------------------------

# (method, target, minor version, headers, bytes consumed), written out against
# RFC 9112. `consumed` stops at the end of the head -- the CRLFCRLF is included
# and the body is not, because framing the body is the caller's job.
HTTP_EXPECTATIONS = [
    pytest.param(
        b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
        ("GET", b"/", 1, [(b"host", b"x")], 27),
        id="minimal-1.1",
    ),
    pytest.param(
        b"POST /a/b?q=1 HTTP/1.0\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc",
        # 54 of the 57 bytes: `abc` is the body and stays in the caller's buffer.
        ("POST", b"/a/b?q=1", 0, [(b"host", b"x"), (b"content-length", b"3")], 54),
        id="1.0-head-followed-by-a-body",
    ),
    pytest.param(
        b"GET /x HTTP/1.1\r\nX-Empty:\r\nX-Pad:   v   \r\n\r\n",
        # RFC 9112 section 5: the field name is case-insensitive and is
        # normalised to lower case here; section 5.1 makes the OWS around a
        # field value not part of it, so `X-Pad` is `v` and an absent value is
        # the empty string rather than a parse failure.
        ("GET", b"/x", 1, [(b"x-empty", b""), (b"x-pad", b"v")], 44),
        id="empty-and-padded-field-values",
    ),
]


@pytest.mark.parametrize(("sample", "expected"), HTTP_EXPECTATIONS)
def test_http_parse_request_yields_the_written_out_head(
    sample: bytes, expected: tuple[str, bytes, int, list[tuple[bytes, bytes]], int]
) -> None:
    for label, http_parse_request in _twins("http_parse_request"):
        assert http_parse_request(sample) == expected, label


def test_http_parse_request_reports_an_unterminated_head_as_incomplete() -> None:
    """`None` means "read more", which is a different answer from refusing.

    The head is one CRLF short of complete, so nothing about it is yet wrong;
    a parser that raised here would break every request split across two reads.
    """
    partial = b"GET / HTTP/1.1\r\nHost: x\r\n"
    for label, http_parse_request in _twins("http_parse_request"):
        assert http_parse_request(partial) is None, label


# --- portable substring fallback (diagnostic build) ------------------------

def _cc() -> str | None:
    import os
    import shutil

    for name in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return name
    return None


@pytest.mark.fuzz
def test_portable_memmem_fallback_is_correct_and_linear(tmp_path) -> None:
    """Compile and run the fallback with WREATH_FORCE_PORTABLE_MEMMEM.

    On this platform wreath_memmem binds to libc memmem, so nothing in a normal
    build ever executes the portable two-way path. This forces it, checks it
    exhaustively against a naive reference (every string over a binary alphabet
    up to length 12, plus repetitive haystacks with overlapping prefixes), and
    reports its scaling.
    """
    import subprocess
    import sysconfig
    from pathlib import Path

    cc = _cc()
    if cc is None:
        pytest.skip("no C compiler available for the diagnostic build")
    source = Path(__file__).parent.parent / "tools" / "memmem_fallback_check.c"
    if not source.exists():
        pytest.skip(f"{source} not present")
    binary = tmp_path / "memmem_fallback_check"
    include = sysconfig.get_paths()["include"]
    build = subprocess.run(
        [cc, "-O2", f"-I{include}", str(source), "-o", str(binary)],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"diagnostic build unavailable: {build.stderr.strip()[-300:]}")

    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, f"fallback check failed:\n{run.stdout}\n{run.stderr}"
    assert "0 failures" in run.stdout, run.stdout

    # Cost per haystack byte must stay flat as the attacker-controlled haystack
    # grows. Compare each needle independently: short needles use the SIMD path
    # and long needles use two-way, so their constant factors need not match.
    rates: dict[int, list[float]] = {}
    for line in run.stdout.splitlines():
        fields = dict(token.split("=", 1) for token in line.split() if "=" in token)
        if "needle" in fields and "ns_per_haystack_byte" in fields:
            rates.setdefault(int(fields["needle"]), []).append(
                float(fields["ns_per_haystack_byte"])
            )
    assert set(rates) == {4, 32, 64}, run.stdout
    assert all(len(samples) == 3 for samples in rates.values()), run.stdout
    for needle, samples in rates.items():
        assert max(samples) < 3 * min(samples), (
            f"fallback scaling is not flat for needle {needle}: {samples}"
        )


# (max_parts, max_part_header_bytes, max_part_bytes) against the outcome, derived
# from `_multipart_body`: two parts, the first with a 44-byte header block and a
# 5-byte body, the second with an 84-byte header block and a 14-byte body. A
# negative limit means no limit.
#
# The old comparison version of this table carried a case labelled "exactly at
# the part-bytes limit: accepted" for (-1, -1, 5), and it is a rejection -- the
# limit admits the first part's 5 bytes and then the *second* part's 14 exceed
# it. Both twins rejected it identically, so the comparison passed and the
# mislabelling survived. Writing the outcome down is what makes that visible.
MULTIPART_LIMITS = [
    pytest.param(
        (1, -1, -1),
        ("err", "multipart form has more than 1 parts"),
        id="part-count-exceeded-by-the-second-part",
    ),
    pytest.param(
        (-1, 4, -1),
        ("err", "multipart part headers exceed 4 bytes"),
        id="header-bytes-exceeded",
    ),
    pytest.param(
        (-1, -1, 3),
        ("err", "multipart part exceeds 3 bytes"),
        id="part-bytes-exceeded-by-the-first-part",
    ),
    pytest.param((2, -1, -1), ("ok", EXPECTED_PARTS), id="exactly-two-parts-accepted"),
    pytest.param(
        (-1, -1, 5),
        ("err", "multipart part exceeds 5 bytes"),
        id="part-bytes-exceeded-by-the-second-part",
    ),
    # Order matters when several limits are breached at once: the part-count
    # check runs first but does not fire on the first part (zero parts so far),
    # so the header-bytes check is the one that reports.
    pytest.param(
        (1, 4, 3),
        ("err", "multipart part headers exceed 4 bytes"),
        id="header-bytes-wins-over-part-bytes",
    ),
]


@pytest.mark.parametrize(("limits", "expected"), MULTIPART_LIMITS)
def test_multipart_limits_reject_the_written_out_case(
    limits: tuple[int, int, int], expected: tuple[str, object]
) -> None:
    """Which limit fires, and with which message -- not merely that one did.

    Every one of these refusals is a `ValueError` naming a byte count, so
    asserting the type would pass on whichever check happened to run first.
    """
    body = _multipart_body(b"BOUNDARY")

    def run(parser: Callable[..., object]) -> tuple[str, object]:
        try:
            return ("ok", parser(body, b"BOUNDARY", *limits))
        except ValueError as error:
            return ("err", str(error))

    for label, multipart_parse in _twins("multipart_parse"):
        assert run(multipart_parse) == expected, label


def test_multipart_limits_default_to_unlimited() -> None:
    """A caller who passes no limits gets the same parts as one who disables them."""
    body = _multipart_body(b"X")
    for label, multipart_parse in _twins("multipart_parse"):
        assert multipart_parse(body, b"X") == EXPECTED_PARTS, label
        assert multipart_parse(body, b"X", -1, -1, -1) == EXPECTED_PARTS, label


# --- PostgreSQL wire codecs: native vs pure ------------------------------------
#
# The two codecs encode every parameter the driver sends and decode every value it
# reads, so a disagreement between them is a value that changes with the call path.
# Nothing compared them directly; a generated differential sweep over the declared
# OIDs found three, all in `_pgdriver` and all fixed:
#
#   * `_encode_text` rounded an int through `float()` before sending it to a float
#     column, so 2**53+1 went on the wire as 9007199254740992 -- a different number,
#     decided client-side -- and anything past the float range raised OverflowError
#     where the native build sent the digits and let the server rule on them.
#   * `_encode_binary` let `struct.error` out for an over-range int. It does not
#     inherit from OverflowError, so a caller handling the native codec's failure did
#     not catch this one.
#   * The text fall-through encoded UTF-8, so a non-ASCII string was accepted as a
#     literal for a `time` column that cannot hold one.

_PG_OIDS = {
    "bool": 16, "bytea": 17, "int8": 20, "int2": 21, "int4": 23, "text": 25,
    "json": 114, "float4": 700, "float8": 701, "varchar": 1043, "date": 1082,
    "time": 1083, "timestamp": 1114, "timestamptz": 1184, "numeric": 1700,
    "uuid": 2950, "jsonb": 3802, "bit": 1560,
}


def _pg_twins():
    pytest.importorskip("wreath._native._postgres")
    from wreath import _pgdriver as pure
    from wreath._native import _postgres as native

    return native, pure


PG_FLOAT_VALUES = [2**53 + 1, 2**63 - 1, 0, 1, -1, 1.5, -0.0, float("inf"),
                   float("nan")]


@pytest.mark.parametrize("value", PG_FLOAT_VALUES, ids=repr)
@pytest.mark.parametrize("oid", [700, 701], ids=["float4", "float8"])
def test_float_binary_codec_matches_the_postgresql_wire_format(
    oid: int, value: object
) -> None:
    """`float4send`/`float8send` put IEEE 754 on the wire in network byte order.

    PostgreSQL's `src/backend/utils/adt/float.c` sends a `float4` with
    `pq_sendfloat4` and a `float8` with `pq_sendfloat8`, both of which are the
    raw IEEE 754 single/double bit pattern big-endian. `struct.pack(">f")` and
    `struct.pack(">d")` are exactly that, so the server's own format is the
    expectation rather than the other twin's output -- including the
    round-to-nearest that turns 2**53+1 into a `float4` and the quiet-NaN and
    infinity patterns, which is where a hand-rolled packer diverges.
    """
    native, pure = _pg_twins()
    expected = struct.pack(">f" if oid == 700 else ">d", value)
    for label, module in (("native", native), ("pure", pure)):
        assert module._encode_binary(value, oid) == expected, label


@pytest.mark.parametrize("value", PG_FLOAT_VALUES, ids=repr)
@pytest.mark.parametrize("oid", [700, 701], ids=["float4", "float8"])
def test_float_text_codec_sends_the_value_undamaged(oid: int, value: object) -> None:
    """The digits must survive, because rounding here is rounding the server never sees.

    This is the defect that made the sweep worth writing: `_pgdriver` ran an int
    through `float()` before formatting, so 2**53+1 -- the first integer a
    double cannot represent -- went on the wire as 9007199254740992. A different
    number, decided client-side, for a column that might have held it.

    So the anchor is not "what the other twin emits" but what the server must
    receive: for an int, its exact decimal digits; for a float, a decimal that
    reads back as the same double. Both are checked by reading the bytes back,
    which is what the server does.
    """
    native, pure = _pg_twins()
    for label, module in (("native", native), ("pure", pure)):
        encoded = module._encode_text(value, oid)
        assert encoded.decode("ascii"), label
        if isinstance(value, int):
            assert int(encoded) == value, label
        elif math.isnan(value):
            assert math.isnan(float(encoded)), label
        else:
            # `float()` is the reader; `copysign` catches -0.0, which compares
            # equal to 0.0 and would otherwise slip through undetected.
            read_back = float(encoded)
            assert read_back == value, label
            assert math.copysign(1.0, read_back) == math.copysign(1.0, value), label


@pytest.mark.parametrize("value", [10**400, -(10**400)], ids=["huge", "-huge"])
@pytest.mark.parametrize("oid", [700, 701], ids=["float4", "float8"])
def test_an_int_past_the_float_range_overflows_binary_and_survives_text(
    oid: int, value: int
) -> None:
    """Two contracts, and the exception type is half of one of them.

    Binary has nowhere to put it, so it must raise `OverflowError` -- and the
    type is the assertion, because `_pgdriver` used to let `struct.error` out
    and that does not inherit from `OverflowError`, so a caller handling
    `codec.c`'s failure did not catch this one.

    Text has somewhere to put it: the digits go on the wire and the *server*
    rules on them, which is the only place that judgement is correct.
    """
    native, pure = _pg_twins()
    for label, module in (("native", native), ("pure", pure)):
        with pytest.raises(OverflowError):
            module._encode_binary(value, oid)
        assert int(module._encode_text(value, oid)) == value, label


#: The OIDs that take a `str`, and the encoding each one's column uses. `text`,
#: `varchar`, `json` and `jsonb` hold UTF-8. `time` takes a bare literal and
#: PostgreSQL's time input is ASCII-only, so a non-ASCII character has to be
#: refused here rather than sent as UTF-8 for the server to choke on.
#:
#: Written out rather than compared between the twins: "both twins encode `time`
#: as ASCII" is a statement about wreath, whereas "PostgreSQL's `time` input
#: cannot hold a non-ASCII character" is a statement about the wire, and only the
#: second is worth asserting.
_STR_TEXT_ENCODING = {
    25: "utf-8",  # text
    114: "utf-8",  # json
    1043: "utf-8",  # varchar
    3802: "utf-8",  # jsonb
    1083: "ascii",  # time
}

#: The OIDs that refuse a `str` outright, by the message they must refuse it with.
_STR_TEXT_REFUSAL = {
    16: "bool codec requires bool",
    17: "bytea codec requires bytes",
    20: "integer codec requires int",
    21: "integer codec requires int",
    23: "integer codec requires int",
    700: "float codec requires int or float",
    701: "float codec requires int or float",
    1082: "date codec requires date",
    1114: "timestamp codec requires datetime",
    1184: "timestamp codec requires datetime",
    1700: "numeric codec requires Decimal or int",
    2950: "uuid codec requires UUID",
}

STR_SAMPLES = ["12:00:00", "é", "", "plain"]


def test_every_declared_oid_has_a_written_out_answer_for_a_str() -> None:
    """The completeness the old sweep got from parametrising over `_PG_OIDS`.

    Without it, adding an OID to the driver and forgetting it here narrows the
    coverage silently rather than failing. 1560 is `bit`, which takes a `str`
    but only one made of bits, and has its own test below.
    """
    assert set(_STR_TEXT_ENCODING) | set(_STR_TEXT_REFUSAL) | {1560} == set(
        _PG_OIDS.values()
    )


@pytest.mark.parametrize("value", STR_SAMPLES, ids=repr)
@pytest.mark.parametrize("oid", sorted(_STR_TEXT_ENCODING))
def test_a_str_parameter_is_encoded_with_its_columns_own_encoding(
    oid: int, value: str
) -> None:
    """`é` is the discriminating input, and the reason this is not a comparison.

    A codec that encoded UTF-8 where the column takes ASCII is invisible on
    `plain` and `12:00:00`, and that was a real defect: the text fall-through
    encoded UTF-8, so a non-ASCII string was accepted as a literal for a `time`
    column that cannot hold one.
    """
    native, pure = _pg_twins()
    encoding = _STR_TEXT_ENCODING[oid]
    for label, module in (("native", native), ("pure", pure)):
        if encoding == "ascii" and not value.isascii():
            with pytest.raises(UnicodeEncodeError):
                module._encode_text(value, oid)
        else:
            assert module._encode_text(value, oid) == value.encode(encoding), label


@pytest.mark.parametrize("value", STR_SAMPLES, ids=repr)
@pytest.mark.parametrize("oid", sorted(_STR_TEXT_REFUSAL))
def test_a_str_parameter_is_refused_by_the_oids_that_cannot_hold_one(
    oid: int, value: str
) -> None:
    """By the message, so a refusal cannot be credited to the wrong branch."""
    native, pure = _pg_twins()
    expected = _STR_TEXT_REFUSAL[oid]
    for module in (native, pure):
        with pytest.raises(TypeError, match=re.escape(expected)):
            module._encode_text(value, oid)


@pytest.mark.parametrize("value", STR_SAMPLES, ids=repr)
def test_the_bit_codec_takes_only_a_string_of_zeroes_and_ones(value: str) -> None:
    """`bit` is the one OID that takes a `str` but not an arbitrary one.

    The empty string is a zero-length bit string and is accepted; every other
    sample here holds a character that is not a bit.
    """
    native, pure = _pg_twins()
    for label, module in (("native", native), ("pure", pure)):
        if value == "":
            assert module._encode_text(value, 1560) == b"", label
        else:
            with pytest.raises(
                ValueError, match=re.escape("a bit string may hold only '0' and '1'")
            ):
                module._encode_text(value, 1560)


def test_loading_the_native_package_does_not_drag_in_asyncio() -> None:
    """`wreath._native` is a loader, and a loader should cost a dlopen.

    It eagerly imported `_client`, whose module init imports `asyncio` -- and
    with it `ssl`, `subprocess`, `logging`, `inspect` and `dataclasses`.
    Measured before this test existed: `import wreath._native._core` cost
    118 ms against 34 ms for `import wreath`, and 74 ms of the difference was
    that one transitive `asyncio`. Every child process the test suite spawns
    paid it, as does every xdist worker, for a module most of them never touch.

    Asserted in a subprocess because `sys.modules` in this one is already
    populated by every other test in the file, so the question cannot be asked
    here at all. `_core` is the module under test rather than the package: it
    is what the facades actually import, and importing it is what pulls the
    package `__init__` in behind it.
    """
    import subprocess

    code = (
        "import sys, wreath._native._core;"
        "print('asyncio' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False", (
        "wreath._native imported asyncio for a caller that only wanted _core"
    )


def test_the_lazily_loaded_extensions_are_still_reachable_by_name() -> None:
    """Laziness that loses a module is worse than the eager import it replaced.

    `_client`, `_reactor` and `_edge` moved behind a module `__getattr__`, and
    the three ways callers reach them -- `from ... import`, attribute access,
    and `getattr` with a default -- all have to keep working, including the
    `None` that means "this build has no such extension".
    """
    import wreath._native as native_package
    from wreath._native import _client, _edge, _reactor

    for name, value in (("_client", _client), ("_reactor", _reactor), ("_edge", _edge)):
        assert value is getattr(native_package, name)
        assert value is None or value.__name__ == f"wreath._native.{name}"
