from __future__ import annotations

import pytest

from wreath._native import _client
from wreath.http_client import (
    ClientResponse,
    ProtocolError,
    ResponseTimeout,
    ResponseTooLarge,
)


def _response_wire(informational: int) -> bytes:
    return (
        b"HTTP/1.1 100 Continue\r\n\r\n" * informational
        + b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n"
    )


def _feed(stream: _client.Http1ClientStream, data: bytes) -> None:
    buffer = stream.get_buffer(-1)
    buffer[: len(data)] = data
    stream.buffer_updated(len(data))


def _read_response(stream: _client.Http1ClientStream):
    return stream.read_response(
        "GET",
        32 * 1024,
        1024,
        ClientResponse,
        ProtocolError,
        ResponseTooLarge,
        ResponseTimeout,
        1.0,
        1.0,
    )


@pytest.mark.asyncio
async def test_native_reader_bounds_informational_responses() -> None:
    stream = _client.Http1ClientStream()
    _feed(stream, _response_wire(17))

    with pytest.raises(ProtocolError, match="too many informational responses"):
        _read_response(stream)


@pytest.mark.asyncio
async def test_native_reader_accepts_sixteen_informational_responses() -> None:
    stream = _client.Http1ClientStream()
    _feed(stream, _response_wire(16))

    response, reusable = _read_response(stream)

    assert response.status == 200
    assert reusable is True
