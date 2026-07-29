"""Regression tests for native HTTP/2 response and HPACK boundaries."""
from __future__ import annotations

import pytest

from wreath.server import ServerConfig

from . import support
from .conftest import requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


async def _expect_connection_error(make_driver, block: bytes, code: int) -> None:
    app, captured = scope_capture_app()
    driver = make_driver(app)
    await driver.preface()
    await driver.feed_and_settle(
        support.encode_frame(
            support.HEADERS,
            support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
            1,
            block,
        )
    )
    goaways = [frame for frame in driver.frames() if frame.type == support.GOAWAY]
    assert goaways
    assert int.from_bytes(goaways[-1].payload[4:8], "big") == code
    assert not captured


async def test_invalid_response_status_is_rejected(make_driver) -> None:
    errors: list[str] = []

    async def app(scope, receive, send) -> None:
        try:
            await send({"type": "http.response.start", "status": 123456, "headers": []})
        except ValueError as exc:
            errors.append(str(exc))

    driver = make_driver(app)
    await driver.preface()
    await driver.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    assert errors == ["response status must be between 100 and 999"]


async def test_duplicate_response_start_is_rejected(make_driver) -> None:
    errors: list[str] = []

    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await send({"type": "http.response.start", "status": 201, "headers": []})
        except RuntimeError as exc:
            errors.append(str(exc))
        await send({"type": "http.response.body", "body": b"ok"})

    driver = make_driver(app)
    await driver.preface()
    await driver.feed_and_settle(support.build_headers_frame(1, support.request_headers()))
    assert errors == ["response already started"]


async def test_dynamic_table_size_update_after_header_is_error(make_driver) -> None:
    block = bytes([0x82]) + support.encode_integer(0, 5, 0x20)
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)


async def test_header_block_larger_than_configured_limit_is_rejected(make_driver) -> None:
    app, captured = scope_capture_app()
    config = ServerConfig(protocols=("h2",), max_header_list_bytes=64)
    driver = make_driver(app, config)
    await driver.preface()
    block = support.HpackEncoder().encode(
        support.request_headers() + [(b"x-large", b"a" * 128)]
    )
    await driver.feed_and_settle(
        support.encode_frame(
            support.HEADERS,
            support.FLAG_END_HEADERS | support.FLAG_END_STREAM,
            1,
            block,
        )
    )
    resets = [frame for frame in driver.frames() if frame.type == support.RST_STREAM]
    assert resets
    assert int.from_bytes(resets[-1].payload, "big") == support.ENHANCE_YOUR_CALM
    assert not captured


async def test_hpack_stops_at_configured_header_count(make_driver) -> None:
    """Compressed one-byte fields must not expand into an unbounded Python list."""
    app, captured = scope_capture_app()
    config = ServerConfig(
        protocols=("h2",), max_header_count=8, max_header_list_bytes=1 << 20
    )
    driver = make_driver(app, config)
    await driver.preface()
    headers = support.request_headers(extra=[(b"accept", b"")] * 32)
    await driver.feed_and_settle(support.build_headers_frame(1, headers))

    resets = [frame for frame in driver.frames() if frame.type == support.RST_STREAM]
    assert resets, "HPACK materialized fields beyond max_header_count"
    assert int.from_bytes(resets[-1].payload, "big") == support.ENHANCE_YOUR_CALM
    assert not captured
