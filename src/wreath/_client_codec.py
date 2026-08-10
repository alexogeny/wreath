"""Which extension serves the outbound HTTP byte codecs.

Two tiers, and the order is deliberate. `_client` is the dedicated client
protocol extension and wins when built. `_core` is the framework accelerator,
which happens to carry the same functions for the inbound path; it is the
fallback because a build may have one extension and not the other. `_core` is
mandatory, so one of the two is always there.

Response framing uses the same tier as head parsing. Although its input is
already structured, it scans every framing header and transfer-coding token, and
keeping that bounded wire-policy loop native avoids rebuilding Python lists on
every response.

`_implementation` records which tier was chosen, for tests and diagnostics that
need to know which one they measured.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ._native import _client, _core

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

if _client is not None:
    parse_response_head = _client.parse_response_head
    response_framing = _client.response_framing
    response_keeps_alive = _client.response_keeps_alive
    _implementation = "native-client"
else:
    parse_response_head = _core.http_parse_response
    response_framing = _core.http_response_framing
    response_keeps_alive = _core.http_response_keeps_alive
    _implementation = "native-core"


if _client is not None:

    def serialize_request(
        method: str,
        target: bytes,
        host: bytes,
        *,
        headers: Iterable[tuple[bytes, bytes]] = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> bytes:
        return _client.serialize_request(method, target, host, tuple(headers), body)

else:

    def serialize_request(
        method: str,
        target: bytes,
        host: bytes,
        *,
        headers: Iterable[tuple[bytes, bytes]] = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> bytes:
        return _core.http_serialize_request(method, target, host, tuple(headers), body)


__all__ = [
    "parse_response_head",
    "response_framing",
    "response_keeps_alive",
    "serialize_request",
]
