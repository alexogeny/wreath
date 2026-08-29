from __future__ import annotations

import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any

import pytest
from _server_ingest import feed

from wreath.server import ServerConfig

try:
    from wreath._native import _server
except ImportError:  # pragma: no cover
    _server = None

pytestmark = pytest.mark.fuzz


class FakeTransport:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False
        self.reading_paused = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self.reading_paused = True

    def resume_reading(self) -> None:
        self.reading_paused = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {
            "sockname": ("127.0.0.1", 8000),
            "peername": ("127.0.0.1", 50000),
            "sslcontext": None,
        }.get(name, default)


async def echo_app(scope, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect" or not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def status_classes(data: bytes) -> list[int]:
    return [int(match) // 100 for match in re.findall(rb"HTTP/1\.[01] (\d{3})", data)]


def zero_length_regression() -> bytes:
    return Path("tests/fixtures/server/zero-length-data.bin").read_bytes()


async def drive(
    protocol_type: type,
    pieces: list[bytes],
    *,
    disconnect: bool,
    config: ServerConfig,
) -> list[int]:
    loop = asyncio.get_running_loop()
    registry: set[Any] = set()
    transport = FakeTransport()
    protocol = protocol_type(echo_app, config, loop, registry)
    protocol.connection_made(transport)
    for piece in pieces:
        feed(protocol, piece)
        await asyncio.sleep(0)
    if disconnect:
        protocol.eof_received()
    for _ in range(12):
        await asyncio.sleep(0)
    protocol.connection_lost(None)
    return status_classes(bytes(transport.buffer))


def corpus() -> list[bytes]:
    values = [
        b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\ndata",
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n0\r\n\r\n",
        b"GET / HTTP/9.9\r\nHost: x\r\n\r\n",
        b"GET / HTTP/1.1\nHost: x\n\n",
        b"POST / HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nabc",
        b"POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nZ\r\n",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n2\r\na!\n0\r\n\r\n",
        b"GET / HTTP/1.1\r\nX-Large: " + b"x" * 256 + b"\r\n\r\n",
        b"POST / HTTP/1.1\r\nContent-Length: 64\r\n\r\n" + b"x" * 64,
    ]
    rng = random.Random(0x4E454F)
    alphabet = b"GETPOST /HTTP1.\r\n:;,0123456789abcdefXYZ{}[]"
    for _ in range(200):
        values.append(bytes(rng.choice(alphabet) for _ in range(rng.randrange(0, 180))))
    return values


@pytest.mark.skipif(_server is None, reason="native server extension unavailable")
@pytest.mark.asyncio
async def test_the_answer_does_not_depend_on_where_the_reads_split() -> None:
    config = ServerConfig(
        max_request_line=96,
        max_header_count=12,
        max_header_bytes=192,
        max_body_bytes=32,
        read_high_water=64,
    )
    rng = random.Random(0xC0FFEE)
    for value in corpus():
        split_sets: list[list[bytes]] = []
        if len(value) <= 96:
            split_sets.extend([[value[:index], value[index:]] for index in range(1, len(value))])
        if value:
            indexes = sorted({0, len(value), *(rng.randrange(len(value) + 1) for _ in range(5))})
            split_sets.append([value[a:b] for a, b in zip(indexes, indexes[1:], strict=False)])
        for disconnect in (False, True):
            whole = await drive(_server.HttpProtocol, [value], disconnect=disconnect, config=config)
            for pieces in split_sets:
                split = await drive(
                    _server.HttpProtocol, pieces, disconnect=disconnect, config=config
                )
                assert split == whole, (
                    value.hex(),
                    [piece.hex() for piece in pieces],
                    disconnect,
                )


@pytest.mark.skipif(_server is None, reason="native server extension unavailable")
@pytest.mark.asyncio
async def test_native_zero_length_data_is_safe() -> None:
    assert _server is not None
    loop = asyncio.get_running_loop()
    protocol = _server.HttpProtocol(echo_app, ServerConfig(), loop, set())
    protocol.connection_made(FakeTransport())
    regression = zero_length_regression()
    feed(protocol, regression)
    await asyncio.sleep(0)
    protocol.connection_lost(None)


@pytest.mark.skipif(
    _server is None or os.environ.get("WREATH_SANITIZER_STRESS") != "1",
    reason="set WREATH_SANITIZER_STRESS=1 for the 6000-request sanitizer stress",
)
@pytest.mark.asyncio
async def test_native_6000_request_pipelined_stress() -> None:
    assert _server is not None
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = _server.HttpProtocol(echo_app, ServerConfig(), loop, set())
    protocol.connection_made(transport)
    get = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
    post = b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n0\r\n\r\n"
    feed(protocol, (get + post) * 3000)
    for _ in range(12000):
        if bytes(transport.buffer).count(b"HTTP/1.1 204") == 6000:
            break
        await asyncio.sleep(0)
    assert bytes(transport.buffer).count(b"HTTP/1.1 204") == 6000
    protocol.connection_lost(None)
