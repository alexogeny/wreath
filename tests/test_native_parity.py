"""Native-vs-pure parity for the non-router accelerators.

Each C function must produce output identical to its pure-Python twin. When
the extension is not built, the native side is skipped and only the pure twin
is exercised for correctness.
"""

from __future__ import annotations

import json as stdlib_json
import math
import random
from collections.abc import Callable

import pytest

from wreath._native import _core
from wreath._pure import codecs as pure_codecs
from wreath._pure import headers as pure_headers
from wreath._pure import http as pure_http
from wreath._pure import json as pure_json
from wreath._pure import multipart as pure_multipart
from wreath._pure import ws as pure_ws

native = pytest.mark.skipif(_core is None, reason="native extension not built")


# --- headers --------------------------------------------------------------

HEADER_LIST = [(b"host", b"x"), (b"accept", b"a"), (b"accept", b"b")]


def test_find_header_pure() -> None:
    assert pure_headers.find_header(HEADER_LIST, b"accept") == b"a"
    assert pure_headers.find_header(HEADER_LIST, b"missing") is None


@native
def test_find_header_parity() -> None:
    for name in (b"accept", b"host", b"missing"):
        assert _core.find_header(HEADER_LIST, name) == pure_headers.find_header(
            HEADER_LIST, name
        )
    assert _core.build_header_map(HEADER_LIST) == pure_headers.build_header_map(
        HEADER_LIST
    )


# --- codecs ---------------------------------------------------------------

QUERY_SAMPLES = [
    b"", b"a=1", b"a=1&b=2", b"a=%C3%A9&b=x+y", b"flag&k=", b"a=1&&b=2", b"%zz=%",
    b"name=%E2%9C%93&n=1", b"a%20b=c%2Fd",
]
COOKIE_SAMPLES = [
    b"", b"a=1", b"a=1; b=2", b" a = 1 ; b=2 ", b"a=1; a=2", b"=nope; ok=1", b"bare",
]


def test_codecs_pure_roundtrip() -> None:
    assert pure_codecs.percent_decode(b"a%20b+c", plus_as_space=True) == b"a b c"
    assert pure_codecs.parse_qs(b"a=1&b=2") == [("a", "1"), ("b", "2")]


@native
@pytest.mark.parametrize("sample", QUERY_SAMPLES)
def test_parse_qs_parity(sample: bytes) -> None:
    assert _core.parse_qs(sample) == pure_codecs.parse_qs(sample)


@native
@pytest.mark.parametrize("sample", COOKIE_SAMPLES)
def test_parse_cookies_parity(sample: bytes) -> None:
    assert _core.parse_cookies(sample) == pure_codecs.parse_cookies(sample)


@native
def test_percent_decode_parity() -> None:
    rng = random.Random(1)
    alphabet = b"abc%20+/=&AZ09" + bytes(range(0x80, 0x88))
    for _ in range(500):
        data = bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        for plus in (False, True):
            assert _core.percent_decode(data, plus_as_space=plus) == (
                pure_codecs.percent_decode(data, plus_as_space=plus)
            )


# --- websocket ------------------------------------------------------------

@native
def test_ws_mask_parity_and_involution() -> None:
    rng = random.Random(2)
    key = bytes(rng.randint(0, 255) for _ in range(4))
    for size in (0, 1, 7, 8, 9, 64, 1000):
        data = bytes(rng.randint(0, 255) for _ in range(size))
        masked = _core.ws_mask(data, key)
        assert masked == pure_ws.ws_mask(data, key)
        assert _core.ws_mask(masked, key) == data


@native
def test_ws_parse_frame_parity() -> None:
    rng = random.Random(3)
    for _ in range(300):
        payload = bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 300)))
        key = bytes(rng.randint(0, 255) for _ in range(4))
        n = len(payload)
        if n < 126:
            header = bytes([0x81, 0x80 | n]) + key
        else:
            header = bytes([0x81, 0x80 | 126, n >> 8, n & 0xFF]) + key
        frame = header + _core.ws_mask(payload, key)
        assert _core.ws_parse_frame(frame) == pure_ws.ws_parse_frame(frame)
        assert _core.ws_parse_frame(frame[:1]) is None


# --- multipart ------------------------------------------------------------

def _multipart_body(boundary: bytes) -> bytes:
    d = b"--" + boundary
    return (
        d + b"\r\ncontent-disposition: form-data; name=\"field\"\r\n\r\nvalue\r\n"
        + d + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\""
        b"\r\ncontent-type: text/plain\r\n\r\nline1\r\n\r\nline2\r\n"
        + d + b"--\r\n"
    )


@native
def test_multipart_parity() -> None:
    body = _multipart_body(b"BOUNDARY")
    assert _core.multipart_parse(body, b"BOUNDARY") == pure_multipart.multipart_parse(
        body, b"BOUNDARY"
    )


def test_multipart_pure_extracts_parts() -> None:
    parts = pure_multipart.multipart_parse(_multipart_body(b"X"), b"X")
    assert len(parts) == 2
    assert parts[0][1] == b"value"
    assert parts[1][1] == b"line1\r\n\r\nline2"


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


@native
@pytest.mark.parametrize("boundary", REPETITIVE_BOUNDARIES)
def test_multipart_parity_with_repetitive_boundaries(boundary: bytes) -> None:
    body = _multipart_body(boundary)
    assert _core.multipart_parse(body, boundary) == pure_multipart.multipart_parse(
        body, boundary
    )


@native
@pytest.mark.parametrize("boundary", REPETITIVE_BOUNDARIES)
def test_multipart_repetitive_boundary_round_trips(boundary: bytes) -> None:
    parts = _core.multipart_parse(_multipart_body(boundary), boundary)
    assert len(parts) == 2
    assert parts[0][1] == b"value"
    assert parts[1][1] == b"line1\r\n\r\nline2"


@native
def test_near_miss_boundaries_do_not_produce_spurious_parts() -> None:
    """A preamble full of almost-boundaries must not match, but the real one must.

    Every near miss shares a 39-byte prefix with the boundary, so a search that
    mishandles partial matches either invents a part or loses the real one.
    """
    boundary = b"a" * 40
    near_miss = b"--" + b"a" * 39 + b"b\r\n"
    body = near_miss * 200 + _multipart_body(boundary)
    parts = _core.multipart_parse(body, boundary)
    assert parts == pure_multipart.multipart_parse(body, boundary)
    assert len(parts) == 2
    assert parts[0][1] == b"value"
    assert parts[1][1] == b"line1\r\n\r\nline2"


@native
def test_body_without_the_boundary_is_rejected_by_both() -> None:
    boundary = b"a" * 40
    body = (b"--" + b"a" * 39 + b"b\r\n") * 50  # near misses only
    with pytest.raises(ValueError):
        _core.multipart_parse(body, boundary)
    with pytest.raises(ValueError):
        pure_multipart.multipart_parse(body, boundary)


# --- json -----------------------------------------------------------------

JSON_SAMPLES = [
    None, True, False, 0, -1, 2**70, 3.14, 1e300, "", "plain", "quote\"back\\slash",
    "tab\tnl\n", "unicode-é-✓", [], {}, [1, 2, 3], {"k": "v"},
    {"nested": {"a": [1, {"b": None}], "s": "xy"}}, ["mix", 1, 2.5, True, None],
]


def test_json_pure_matches_stdlib() -> None:
    for sample in JSON_SAMPLES:
        assert stdlib_json.loads(pure_json.json_dumps(sample)) == stdlib_json.loads(
            stdlib_json.dumps(sample)
        )


@native
@pytest.mark.parametrize("sample", JSON_SAMPLES)
def test_json_dumps_parity(sample: object) -> None:
    assert _core.json_dumps(sample) == pure_json.json_dumps(sample)


@native
def test_json_strictness_parity() -> None:
    for table in (_core.json_dumps, pure_json.json_dumps):
        with pytest.raises(ValueError):
            table(float("nan"))
        with pytest.raises(ValueError):
            table(float("inf"))
        with pytest.raises(TypeError):
            table({1: "int-key"})
        with pytest.raises(TypeError):
            table(object())


def test_json_dumps_integer_boundaries() -> None:
    # Regression for the two-digits-at-a-time integer writer: exercise every
    # digit-count transition and the long-long overflow fallback.
    values = [0, -0, 5, -5]
    for exp in range(1, 25):
        for base in (10**exp, 2**exp):
            values += [base - 1, base, base + 1, -(base - 1), -base, -(base + 1)]
    values += [2**63 - 1, -(2**63), 2**63, -(2**63) - 1, 2**100, -(2**100)]
    for value in values:
        expected = stdlib_json.dumps(value).encode()
        assert pure_json.json_dumps(value) == expected
        if _core is not None:
            assert _core.json_dumps(value) == expected


@native
def test_json_dumps_escape_positions() -> None:
    # Regression for the SWAR escape scanner: escapes at every offset within
    # and around the 8-byte window, for several string lengths.
    for length in (0, 1, 7, 8, 9, 15, 16, 17, 31, 64):
        base = "a" * length
        samples = [base]
        for pos in range(length):
            for special in ('"', "\\", "\n", "\x00", "\x1f"):
                samples.append(base[:pos] + special + base[pos + 1 :])
        samples.append(base + "é✓\U0001f600")
        for sample in samples:
            assert _core.json_dumps(sample) == pure_json.json_dumps(sample)


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
def test_json_loads_parity(document: str) -> None:
    _assert_loads_equal(_core.json_loads(document), pure_json.json_loads(document))
    _assert_loads_equal(
        _core.json_loads(document.encode()), pure_json.json_loads(document.encode())
    )
    _assert_loads_equal(
        _core.json_loads(bytearray(document.encode())),
        pure_json.json_loads(bytearray(document.encode())),
    )


@native
@pytest.mark.parametrize("document", JSON_MALFORMED)
def test_json_loads_malformed_parity(document: str) -> None:
    for loads in (_core.json_loads, pure_json.json_loads):
        with pytest.raises(ValueError):
            loads(document)


@native
def test_json_loads_input_types() -> None:
    # Exotic byte encodings are sniffed exactly like stdlib json.
    for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
        data = '{"k": [1, "é"]}'.encode(encoding)
        assert _core.json_loads(data) == pure_json.json_loads(data)
    bom = b'\xef\xbb\xbf{"k":1}'
    assert _core.json_loads(bom) == pure_json.json_loads(bom)
    for loads in (_core.json_loads, pure_json.json_loads):
        with pytest.raises(TypeError, match="str, bytes or bytearray"):
            loads(memoryview(b"1"))
        with pytest.raises(TypeError):
            loads(1)


@native
def test_json_loads_deep_nesting_raises_recursion_error() -> None:
    document = "[" * 200_000 + "]" * 200_000
    for loads in (_core.json_loads, pure_json.json_loads):
        with pytest.raises(RecursionError):
            loads(document)


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


@native
def test_json_fuzz_parity() -> None:
    rng = random.Random(20260714)
    for _ in range(1500):
        value = _random_json_value(rng, 0)
        assert _core.json_dumps(value) == pure_json.json_dumps(value)
        rendered = stdlib_json.dumps(
            value,
            ensure_ascii=rng.random() < 0.5,
            indent=rng.choice([None, None, 2]),
            separators=rng.choice([None, (",", ":"), (", ", ": ")]),
        )
        _assert_loads_equal(_core.json_loads(rendered), pure_json.json_loads(rendered))
        encoded = rendered.encode()
        _assert_loads_equal(_core.json_loads(encoded), pure_json.json_loads(encoded))


# --- http parser ----------------------------------------------------------

HTTP_SAMPLES = [
    b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
    b"POST /a/b?q=1 HTTP/1.0\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc",
    b"GET /x HTTP/1.1\r\nX-Empty:\r\nX-Pad:   v   \r\n\r\n",
]


def test_http_pure_parses() -> None:
    parsed = pure_http.http_parse_request(HTTP_SAMPLES[0])
    assert parsed is not None
    method, target, minor, headers, consumed = parsed
    assert method == "GET"
    assert target == b"/"
    assert minor == 1
    assert headers == [(b"host", b"x")]
    assert consumed == len(HTTP_SAMPLES[0])


@native
@pytest.mark.parametrize("sample", HTTP_SAMPLES)
def test_http_parity(sample: bytes) -> None:
    assert _core.http_parse_request(sample) == pure_http.http_parse_request(sample)


@native
def test_http_incomplete_parity() -> None:
    partial = b"GET / HTTP/1.1\r\nHost: x\r\n"
    assert _core.http_parse_request(partial) is None
    assert pure_http.http_parse_request(partial) is None


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

    # Cost per haystack byte must not grow with the needle: that is the
    # difference between the linear two-way search and the naive scan it
    # replaced, whose per-byte cost rises with needle length on this input.
    rates = [
        float(token.split("=", 1)[1])
        for line in run.stdout.splitlines()
        for token in line.split()
        if token.startswith("ns_per_haystack_byte=")
    ]
    assert len(rates) >= 4, run.stdout
    assert max(rates) < 3 * min(rates), f"fallback scaling is not flat: {rates}"


@native
@pytest.mark.parametrize(
    "limits",
    [
        (1, -1, -1),  # part count
        (-1, 4, -1),  # part header bytes
        (-1, -1, 3),  # part bytes
        (2, -1, -1),  # exactly at the part-count limit: accepted
        (-1, -1, 5),  # exactly at the part-bytes limit: accepted
        (1, 4, 3),  # several limits at once: the same one must win in both
    ],
)
def test_multipart_limit_parity(limits: tuple[int, int, int]) -> None:
    """Native and pure must accept and reject identically, with one message."""
    body = _multipart_body(b"BOUNDARY")

    def run(parser: Callable[..., object]) -> object:
        try:
            return parser(body, b"BOUNDARY", *limits)
        except ValueError as error:
            return type(error), str(error)

    assert run(_core.multipart_parse) == run(pure_multipart.multipart_parse)


def test_multipart_limits_default_to_unlimited() -> None:
    body = _multipart_body(b"X")
    assert pure_multipart.multipart_parse(body, b"X") == pure_multipart.multipart_parse(
        body, b"X", -1, -1, -1
    )
