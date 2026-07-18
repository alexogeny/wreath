"""Pure/native selection for outbound HTTP byte codecs."""

from __future__ import annotations

from collections.abc import Iterable

from ._native import _client, _core
from ._pure.http_client import response_framing

if _client is not None:
    parse_response_head = _client.parse_response_head
    _implementation = "native-client"
elif _core is not None and hasattr(_core, "http_parse_response"):
    parse_response_head = _core.http_parse_response
    _implementation = "native-core"
else:
    from ._pure.http_client import parse_response_head

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


__all__ = ["parse_response_head", "response_framing", "serialize_request"]
