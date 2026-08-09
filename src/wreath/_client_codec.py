"""Pure/native selection for outbound HTTP byte codecs.

Three tiers, not two, and the order is deliberate. `_client` is the dedicated
client protocol extension and wins when built. `_core` is the framework
accelerator, which happens to carry the same two functions for the inbound path;
it is the fallback because a build may have one extension and not the other.
`_pure.http_client` is the reference implementation and the parity contract --
the native tiers are asserted byte-for-byte equal to it, so a divergence is a
parity bug rather than a behaviour change.

Response framing uses the same tier as head parsing. Although its input is
already structured, it scans every framing header and transfer-coding token;
keeping that bounded wire-policy loop native avoids rebuilding Python lists on
every response while the pure implementation remains its parity contract.

`_implementation` records which tier was chosen, for tests and diagnostics that
need to know which one they measured.
"""

from __future__ import annotations

from collections.abc import Iterable

from ._native import _client, _core

if _client is not None:
    parse_response_head = _client.parse_response_head
    response_framing = _client.response_framing
    response_keeps_alive = _client.response_keeps_alive
    _implementation = "native-client"
elif _core is not None and hasattr(_core, "http_parse_response"):
    parse_response_head = _core.http_parse_response
    response_framing = _core.http_response_framing
    response_keeps_alive = _core.http_response_keeps_alive
    _implementation = "native-core"
else:
    from ._pure.http_client import (
        parse_response_head,
        response_framing,
        response_keeps_alive,
    )

    _implementation = "pure"

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

elif _core is not None and hasattr(_core, "http_serialize_request"):

    def serialize_request(
        method: str,
        target: bytes,
        host: bytes,
        *,
        headers: Iterable[tuple[bytes, bytes]] = (),
        body: bytes | bytearray | memoryview = b"",
    ) -> bytes:
        return _core.http_serialize_request(method, target, host, tuple(headers), body)

else:
    from ._pure.http_client import serialize_request


__all__ = [
    "parse_response_head",
    "response_framing",
    "response_keeps_alive",
    "serialize_request",
]
