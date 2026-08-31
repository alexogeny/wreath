from __future__ import annotations

import pytest

from . import conftest, support
from .conftest import requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


async def _send_raw_headers(make_driver, headers, *, end_stream=True):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    block = support.HpackEncoder().encode(headers)
    flags = support.FLAG_END_HEADERS | (support.FLAG_END_STREAM if end_stream else 0)
    await d.feed_and_settle(support.encode_frame(support.HEADERS, flags, 1, block))
    return d, captured


def _stream_error(d, stream_id=1):
    frames = d.frames()
    rst = [f for f in frames if f.type == support.RST_STREAM and f.stream_id == stream_id]
    goaway = [f for f in frames if f.type == support.GOAWAY]
    if rst:
        return int.from_bytes(rst[-1].payload, "big")
    if goaway:
        return support.parse_goaway(goaway[-1].payload)[1]
    return None


async def test_missing_method_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver, [(b":path", b"/"), (b":scheme", b"https"), (b":authority", b"x")]
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_missing_path_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver, [(b":method", b"GET"), (b":scheme", b"https"), (b":authority", b"x")]
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_missing_scheme_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver, [(b":method", b"GET"), (b":path", b"/"), (b":authority", b"x")]
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_duplicate_pseudo_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":method", b"POST"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_host_must_not_disagree_with_authority(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"evil.example"),
            (b"host", b"good.example"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_authority_must_not_contain_userinfo(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"good.example@evil.example"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_duplicate_host_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"good.example"),
            (b"host", b"good.example"),
            (b"host", b"good.example"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_unknown_pseudo_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x"),
            (b":bogus", b"1"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_pseudo_header_after_regular_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b"x-regular", b"1"),
            (b":authority", b"x"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_uppercase_header_name_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x"),
            (b"X-Uppercase", b"1"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_connection_specific_header_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x"),
            (b"connection", b"keep-alive"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_te_header_non_trailers_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x"),
            (b"te", b"gzip"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_te_trailers_is_allowed(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x"),
            (b"te", b"trailers"),
        ],
    )
    assert captured, "te: trailers is explicitly permitted"


async def test_empty_path_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver,
        [(b":method", b"GET"), (b":path", b""), (b":scheme", b"https"), (b":authority", b"x")],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_connect_without_authority_is_protocol_error(make_driver):
    # CONNECT uses :authority and omits :scheme/:path (RFC 9113 s8.5).
    d, captured = await _send_raw_headers(make_driver, [(b":method", b"CONNECT")])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


BASE = [(b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"), (b":authority", b"x")]


@pytest.mark.parametrize(
    ("label", "field"),
    [
        ("crlf in value", (b"x-note", b"a\r\nx-injected: 1")),
        ("lf in value", (b"x-note", b"a\nx-injected: 1")),
        ("nul in value", (b"x-note", b"a\x00b")),
        ("del in value", (b"x-note", b"a\x7fb")),
        ("leading space in value", (b"x-note", b" v")),
        ("trailing tab in value", (b"x-note", b"v\t")),
        ("crlf in name", (b"x\r\nx-injected: 1", b"v")),
        ("space in name", (b"x y", b"v")),
        ("colon in name", (b"x:y", b"v")),
        ("nul in name", (b"x\x00y", b"v")),
        ("non-ascii in name", (b"x\xffy", b"v")),
    ],
)
async def test_malformed_field_octets_are_protocol_errors(make_driver, label, field):
    d, captured = await _send_raw_headers(make_driver, [*BASE, field])
    assert _stream_error(d) == support.PROTOCOL_ERROR, f"{label} accepted"
    assert not captured, f"{label} reached the application"


async def test_crlf_in_pseudo_header_value_is_protocol_error(make_driver):
    # A pseudo-header is a field too: :authority lands in the scope as `host`.
    d, captured = await _send_raw_headers(
        make_driver,
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x\r\nx-injected: 1"),
        ],
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_ordinary_field_octets_are_still_accepted(make_driver):
    # The rule rejects control octets, not the printable range: a value may
    # carry spaces, tabs and colons inside it, just not at its edges.
    d, captured = await _send_raw_headers(
        make_driver, [*BASE, (b"x-note", b"a b\tc: d"), (b"x-token", b"!#$%&'*+-.^_`|~")]
    )
    assert captured, "a well-formed field was rejected"
    assert (b"x-note", b"a b\tc: d") in captured[0]["headers"]


@pytest.mark.parametrize(
    "value",
    [b"100abc", b"0x10", b" 5", b"5 ", b"+5", b"-1", b"", b"1e3", b"99999999999999999999999999999"],
)
async def test_malformed_content_length_is_protocol_error(make_driver, value):
    d, captured = await _send_raw_headers(make_driver, [*BASE, (b"content-length", value)])
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_conflicting_duplicate_content_length_is_protocol_error(make_driver):
    d, captured = await _send_raw_headers(
        make_driver, [*BASE, (b"content-length", b"5"), (b"content-length", b"6")]
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
    assert not captured


async def test_repeated_identical_content_length_is_accepted(make_driver):
    d, captured = await _send_raw_headers(
        make_driver, [*BASE, (b"content-length", b"0"), (b"content-length", b"0")]
    )
    assert captured, "identical repeated content-length is not a conflict"


async def test_declared_content_length_is_enforced(make_driver):
    d = make_driver(conftest.ok_app)
    await d.preface()
    block = support.HpackEncoder().encode(
        [
            (b":method", b"POST"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"x"),
            (b"content-length", b"100"),
        ]
    )
    await d.feed_and_settle(
        support.encode_frame(support.HEADERS, support.FLAG_END_HEADERS, 1, block)
    )
    await d.feed_and_settle(
        support.encode_frame(support.DATA, support.FLAG_END_STREAM, 1, b"short")
    )
    assert _stream_error(d) == support.PROTOCOL_ERROR
