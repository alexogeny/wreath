from __future__ import annotations

import pytest

from . import support
from .conftest import requires_h2, scope_capture_app

pytestmark = [requires_h2, pytest.mark.asyncio]


async def _expect_connection_error(make_driver, block: bytes, code: int):
    app, captured = scope_capture_app()
    d = make_driver(app)
    await d.preface()
    await d.feed_and_settle(
        support.encode_frame(
            support.HEADERS, support.FLAG_END_HEADERS | support.FLAG_END_STREAM, 1, block
        )
    )
    goaways = [f for f in d.frames() if f.type == support.GOAWAY]
    assert goaways, "malformed HPACK must be a connection error (GOAWAY)"
    _, got, _ = support.parse_goaway(goaways[-1].payload)
    assert got == code
    assert not captured, "malformed HPACK must not reach the application"
    return d


async def test_index_zero_is_compression_error(make_driver):
    # An indexed header field representation with index 0 is invalid.
    await _expect_connection_error(make_driver, bytes([0x80]), support.COMPRESSION_ERROR)


async def test_index_out_of_range_is_compression_error(make_driver):
    block = support.encode_integer(9999, 7, 0x80)
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)


async def test_truncated_integer_is_compression_error(make_driver):
    # Prefix says continuation but the stream ends.
    block = bytes([0xFF, 0x80, 0x80])  # never terminates the varint
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)


async def test_truncated_string_is_compression_error(make_driver):
    # Literal, new name, string length 10 but only 3 bytes present.
    block = bytes([0x00]) + support.encode_integer(10, 7, 0x00) + b"abc"
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)


async def test_integer_overflow_is_compression_error(make_driver):
    # A varint that decodes to an absurd value (RFC 7541 s5.1 overflow guard).
    block = bytes([0xFF]) + bytes([0xFF] * 10) + bytes([0x7F])
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)


async def test_invalid_huffman_padding_is_compression_error(make_driver):
    # Huffman string whose padding is not all-ones (RFC 7541 s5.2).
    bad = support.encode_integer(1, 7, 0x80) + bytes([0x00])
    block = bytes([0x00]) + support.encode_string(b":method-x") + bad
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)


async def test_huffman_eos_symbol_is_compression_error(make_driver):
    # A Huffman string containing the EOS symbol is invalid (RFC 7541 s5.2).
    eos_bytes = bytes([0xFF, 0xFF, 0xFF, 0xFF])  # 30 one-bits => EOS prefix
    bad = support.encode_integer(len(eos_bytes), 7, 0x80) + eos_bytes
    block = bytes([0x00]) + support.encode_string(b"x-name") + bad
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)


async def test_dynamic_table_size_update_over_max_is_error(make_driver):
    # A size update larger than SETTINGS_HEADER_TABLE_SIZE is a compression error.
    block = support.encode_integer(1 << 20, 5, 0x20)
    await _expect_connection_error(make_driver, block, support.COMPRESSION_ERROR)
