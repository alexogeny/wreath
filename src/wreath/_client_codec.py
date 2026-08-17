"""Outbound HTTP byte codecs.

Response framing uses the same tier as head parsing. Although its input is
already structured, it scans every framing header and transfer-coding token, and
keeping that bounded wire-policy loop native avoids rebuilding Python lists on
every response.

"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ._native import _client

#: `parse_response_head(data)` -- a complete status line and header block to
#: `(version, status, reason, headers, consumed)`, or `None` while the head is
#: still incomplete. Annotated here because a compiled function is `Any`, and a
#: caller that learns nothing from it infers `Any` all the way down.
_ParsedHead = tuple[int, int, bytes, list[tuple[bytes, bytes]], int]
parse_response_head: Callable[[bytes], _ParsedHead | None]

#: `response_framing(method, status, headers)` -- `(mode, length)`, where mode
#: is one of `"length"`, `"chunked"`, `"close"` or `"none"`.
response_framing: Callable[[str, int, list[tuple[bytes, bytes]]], tuple[str, int]]

#: `response_keeps_alive(minor_version, headers, framed)` -- whether the
#: connection may be reused once this response is complete.
response_keeps_alive: Callable[[int, list[tuple[bytes, bytes]], bool], bool]

#: `parse_chunk_size(line)` -- validate one complete CRLF-terminated chunk-size
#: line and return its hexadecimal size. Chunk extensions are deliberately
#: ignored, as required by the response reader.
parse_chunk_size: Callable[[bytes], int]

parse_response_head = _client.parse_response_head
response_framing = _client.response_framing
response_keeps_alive = _client.response_keeps_alive
parse_chunk_size = _client.parse_chunk_size
configure_fast_path = _client._configure_fast_path
request_once = _client._request_once
request_default = _client._request_default
new_counters = _client._counters_new
counter_snapshot = _client._counters_snapshot


def serialize_request(
    method: str,
    target: bytes,
    host: bytes,
    *,
    headers: Iterable[tuple[bytes, bytes]] = (),
    body: bytes | bytearray | memoryview = b"",
) -> bytes:
    return _client.serialize_request(method, target, host, tuple(headers), body)


__all__ = [
    "parse_response_head",
    "parse_chunk_size",
    "response_framing",
    "response_keeps_alive",
    "serialize_request",
]
