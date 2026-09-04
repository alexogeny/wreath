from __future__ import annotations

import json as stdlib_json
import subprocess
import sys
from collections.abc import Callable

import pytest

from wreath._native import _core

native = pytest.mark.skipif(_core is None, reason="native extension not built")


@native
def test_json_fallback_does_not_retain_an_ended_interpreter() -> None:
    snippet = (
        "from wreath._native import _core; "
        "value = chr(34) + chr(0xd800) + chr(34); "
        "assert _core.json_loads(value) == chr(0xd800)"
    )
    script = (
        "import _testcapi\n"
        f"snippet = {snippet!r}\n"
        "assert _testcapi.run_in_subinterp(snippet) == 0\n"
        "assert _testcapi.run_in_subinterp(snippet) == 0\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


# CPython caches, and shares process-wide, the single-character `bytes` objects
# and the small `int` objects. They are the reachable interpreter-global state a
# parser can plausibly write through, because they are what the C API returns
# instead of allocating when a result is one byte long or a small integer.


def _singleton_violations() -> list[str]:
    """Every shared singleton whose contents no longer match its own value.

    This asserts an *invariant*, not a before/after delta, and the distinction
    is the whole reason the helper is written this way. The obvious snapshot --
    `[bytes(bytearray([c])) for c in range(256)]` before and after -- looks like
    it samples the singletons and samples nothing: `bytearray([c])` holds the
    raw integer, so `bytes()` of it rebuilds the intended value from scratch
    every time and is correct by construction whatever happened to the cache.
    Two such snapshots are equal even while `bytes([65])` reads `b"a"`. That
    version of this helper was written first and passed against a build with the
    defect live.

    So each entry is read through the path that *returns the cache* and compared
    against a value derived independently of it.
    """
    bad: list[str] = []
    for code in range(256):
        singleton = bytes([code])  # returns the interpreter's cached object
        expected = bytes(bytearray([code]))  # allocated fresh; never the cache
        if singleton != expected:
            bad.append(f"bytes[{code}]: expected {expected!r}, got {singleton!r}")
    for value in range(-5, 257):
        if int(f"{value}") != value:  # the small-int cache, read via a fresh parse
            bad.append(f"int[{value}]: got {int(f'{value}')}")
    for code in range(256):
        # Latin-1 single-character `str` values are cached the same way.
        singleton = chr(code)
        expected = str(bytes(bytearray([code])), "latin-1")
        if singleton != expected:
            bad.append(f"str[{code}]: expected {expected!r}, got {singleton!r}")
    return bad


def assert_no_interpreter_mutation(label: str, call: Callable[[], object]) -> object:
    """Run *call* and fail if it corrupted any shared singleton.

    Returns whatever *call* returned, so a caller can also assert the parse
    itself did what it was supposed to -- a guard around a call that raised
    immediately would otherwise pass for the wrong reason.
    """
    assert not _singleton_violations(), (
        f"the interpreter was already corrupt before {label} ran; an earlier "
        f"test in this process mutated a shared singleton"
    )
    result = call()
    violations = _singleton_violations()
    assert not violations, (
        f"{label} corrupted interpreter-global state: {'; '.join(violations[:8])}"
        + (f" (and {len(violations) - 8} more)" if len(violations) > 8 else "")
        + ". A native parser wrote through an object the interpreter shares "
        "process-wide. Build the result with PyBytes_FromStringAndSize(NULL, n) "
        "and fill it, rather than copying the source in and rewriting it in place."
    )
    return result


@native
def test_a_one_letter_multipart_header_name_does_not_corrupt_bytes() -> None:
    headers = b"".join(bytes([c]) + b": v\r\n" for c in range(ord("A"), ord("Z") + 1))
    body = b"--b\r\n" + headers + b"\r\nx\r\n--b--\r\n"

    parts = assert_no_interpreter_mutation(
        "multipart_parse", lambda: _core.multipart_parse(body, b"b")
    )

    assert bytes([ord("Q")]) == b"Q"
    # UP012 is waived on the next line: `str.encode()` is the assertion, not
    # an oversight. It goes through the cache; the `b"A"` literal ruff would
    # substitute does not, and the check would then compare a literal to
    # itself and never fail again.
    assert "A".encode() == b"A"  # noqa: UP012
    # And the parse still did its job: names lowercased, body intact.
    assert len(parts) == 1
    assert parts[0][1] == b"x"
    assert (b"a", b"v") in parts[0][0]


@native
@pytest.mark.asyncio
async def test_an_ordinary_upload_route_cannot_corrupt_the_interpreter() -> None:
    from wreath import Wreath
    from wreath.testing import TestClient

    app = Wreath()

    @app.post("/upload")
    # `request` is deliberately unannotated: it matches the app's own style.
    async def upload(request) -> dict:
        form = await request.form()
        return {"title": form["title"]}

    letters = b"".join(bytes([c]) + b": v\r\n" for c in range(ord("A"), ord("Z") + 1))
    body = (
        b"--boundary123\r\n" + letters + b'Content-Disposition: form-data; name="title"\r\n\r\n'
        b"hello\r\n"
        b"--boundary123--\r\n"
    )

    client = TestClient(app)
    response = await client.post(
        "/upload",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=boundary123"},
    )

    assert response.status == 200
    assert bytes([ord("A")]) == b"A"
    # See the note above: the `.encode()` call is what reads the cache.
    assert "Z".encode() == b"Z"  # noqa: UP012
    assert not _singleton_violations()


@native
def test_multipart_still_lowercases_the_names_it_no_longer_mutates() -> None:
    body = b'--b\r\nContent-Disposition: form-data; name="f"\r\nX-A: 1\r\n\r\nv\r\n--b--\r\n'
    names = [name for name, _ in _core.multipart_parse(body, b"b")[0][0]]
    assert names == [b"content-disposition", b"x-a"]


# Ordered by how directly the input is attacker-controlled. Size is deliberately
# not the ordering: the 227-line multipart parser held the S1 while the 655-line
# HPACK decoder was read in full and found clean.

_ONE_LETTER = [bytes([c]) for c in range(ord("A"), ord("Z") + 1)]
_HOSTILE_HEADERS = b"".join(letter + b": v\r\n" for letter in _ONE_LETTER)


@native
@pytest.mark.parametrize(
    ("label", "call"),
    [
        (
            "http_parse_request",
            lambda: _core.http_parse_request(b"GET / HTTP/1.1\r\n" + _HOSTILE_HEADERS + b"\r\n"),
        ),
        (
            "http_parse_response",
            lambda: _core.http_parse_response(b"HTTP/1.1 200 OK\r\n" + _HOSTILE_HEADERS + b"\r\n"),
        ),
        (
            "build_header_map",
            lambda: _core.build_header_map([(letter, b"v") for letter in _ONE_LETTER]),
        ),
        (
            "find_header",
            lambda: _core.find_header([(b"A", b"v")], b"A"),
        ),
        (
            "parse_cookies",
            lambda: _core.parse_cookies(b"; ".join(x + b"=" + x for x in _ONE_LETTER)),
        ),
        (
            "parse_qs",
            lambda: _core.parse_qs(b"&".join(x + b"=" + x for x in _ONE_LETTER)),
        ),
        (
            "percent_decode",
            lambda: _core.percent_decode(b"%41%42%43/%61"),
        ),
        (
            "json_loads",
            lambda: _core.json_loads(stdlib_json.dumps({chr(c): chr(c) for c in range(65, 91)})),
        ),
        (
            "json_dumps",
            lambda: _core.json_dumps({chr(c): chr(c) for c in range(65, 91)}),
        ),
        (
            "msgpack_dumps",
            lambda: _core.msgpack_dumps({chr(c): chr(c) for c in range(65, 91)}),
        ),
        (
            "ws_mask",
            lambda: _core.ws_mask(b"A", b"\x00\x00\x00\x00"),
        ),
        (
            "ws_parse_frame",
            lambda: _core.ws_parse_frame(bytes([0x81, 0x81]) + b"\x00\x00\x00\x00" + b"A"),
        ),
        (
            "ws_build_frame",
            lambda: _core.ws_build_frame(1, b"A", None),
        ),
        (
            "sse_frame",
            lambda: _core.sse_frame(b"A", None, None, None),
        ),
        (
            "jose_parse",
            lambda: _core.jose_parse(b"QQ.QQ.QQ"),
        ),
        (
            "jose_b64url_decode",
            lambda: _core.jose_b64url_decode(b"QUJD"),
        ),
        (
            "parse_dotenv",
            lambda: _core.parse_dotenv("\n".join(f"{chr(c)}={chr(c)}" for c in range(65, 91))),
        ),
        (
            "append_vary",
            lambda: _core.append_vary([(b"vary", b"A")], b"origin"),
        ),
        (
            "replace_response_header",
            lambda: _core.replace_response_header([], b"x-id", b"A"),
        ),
        (
            "replace_cookie",
            lambda: _core.replace_cookie([], b"csrf=", b"csrf=A"),
        ),
        (
            "replace_server_timing",
            lambda: _core.replace_server_timing([], b"total", b"total;dur=1"),
        ),
        (
            "select_content_encoding",
            lambda: _core.select_content_encoding(b"gzip, br", 0),
        ),
        (
            "origin_matches",
            lambda: _core.origin_matches(b"https://a.example", [b"https://a.example"]),
        ),
    ],
)
def test_no_native_parser_mutates_interpreter_state(label, call) -> None:

    def invoke() -> object:
        try:
            return call()
        except (ValueError, TypeError) as exc:  # a refusal is an acceptable outcome
            return exc

    assert_no_interpreter_mutation(label, invoke)


@native
def test_the_guard_can_see_a_real_corruption() -> None:
    import ctypes

    target = ord("A")
    singleton = bytes([target])
    assert not _singleton_violations(), "must start from a clean interpreter"

    # Offset of the character data within a PyBytesObject, derived rather than
    # hard-coded: everything before ob_sval is the header plus ob_shash.
    offset = bytes.__basicsize__ - 1
    buffer = (ctypes.c_char * 1).from_address(id(singleton) + offset)
    # Derived from `target`, not read back from the buffer: `buffer[0]` returns
    # the singleton itself, so saving it and writing it back after the
    # corruption restores the corrupted value. That is the same aliasing hazard
    # the helper above documents, and it bit this test too.
    original = bytes(bytearray([target]))
    try:
        buffer[0] = b"a"
        violations = _singleton_violations()
        assert any(f"bytes[{target}]" in v for v in violations), (
            f"the guard did not see a real corruption of bytes[{target}]; "
            f"it is not observing the cache. violations={violations[:4]}"
        )
        # And a call made while corrupt fails rather than passing quietly. The
        # message names the precondition, because corruption that predates the
        # call belongs to whatever ran earlier -- attribution matters when one
        # bad parser would otherwise redden every later case in the process.
        with pytest.raises(AssertionError, match="already corrupt"):
            assert_no_interpreter_mutation("forced", lambda: None)
    finally:
        buffer[0] = original
    assert not _singleton_violations(), "the interpreter was not restored"
    assert bytes([target]) == b"A"
