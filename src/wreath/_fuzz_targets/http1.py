from __future__ import annotations

import asyncio
import re
from typing import Any

from wreath._fuzz import HTTP1_STRATEGY, FuzzTarget
from wreath._native import _core, _server
from wreath.server import ServerConfig

from ._corpus import load_versioned

_STATUS = re.compile(rb"HTTP/1\.[01] (\d{3})")
_SIMPLE_REQUEST = re.compile(
    rb"(?P<method>GET|HEAD) (?P<target>/[a-z/]*) HTTP/1\.(?P<minor>[01])\r\n"
    rb"Host: (?P<host>[a-z.]+)\r\n\r\n\Z"
)
_CONFIG = ServerConfig(
    max_request_line=2_048,
    max_header_count=64,
    max_header_bytes=4_096,
    max_body_bytes=8_192,
    read_high_water=16_384,
)


class _Transport:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    def writelines(self, parts: list[bytes]) -> None:
        self.buffer.extend(b"".join(parts))

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        return None

    def resume_reading(self) -> None:
        return None

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {
            "sockname": ("127.0.0.1", 8000),
            "peername": ("127.0.0.1", 50_000),
            "sslcontext": None,
        }.get(name, default)


async def _app(scope, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect" or not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _drive(pieces: tuple[bytes, ...]) -> tuple[tuple[int, ...], bool]:
    loop = asyncio.get_running_loop()
    transport = _Transport()
    protocol = _server.HttpProtocol(_app, _CONFIG, loop, set())
    protocol.connection_made(transport)
    for piece in pieces:
        protocol.data_received(piece)
        await asyncio.sleep(0)
    for _ in range(8):
        await asyncio.sleep(0)
    protocol.connection_lost(None)
    await asyncio.sleep(0)
    statuses = tuple(int(status) for status in _STATUS.findall(transport.buffer))
    return statuses, transport.closed


def _splits(data: bytes) -> tuple[int, ...]:
    if len(data) < 2:
        return ()
    if len(data) <= 32:
        return tuple(range(1, len(data)))
    return tuple(sorted({1, len(data) // 2, len(data) - 1}))


async def _fragmentation_result(data: bytes) -> tuple[tuple[int, ...], bool]:
    indexes = _splits(data)
    results = await asyncio.gather(
        _drive((data,)),
        *(_drive((data[:index], data[index:])) for index in indexes),
    )
    whole = results[0]
    for index, split in zip(indexes, results[1:], strict=True):
        if split != whole:
            raise AssertionError(
                f"HTTP/1 fragmentation changed the result at byte {index}: "
                f"whole={whole!r}, split={split!r}"
            )
    return whole


def run(data: bytes) -> tuple[str, ...]:
    try:
        parsed = _core.http_parse_request(data)
    except ValueError:
        parse_feature = "http:refused"
    else:
        parse_feature = "http:incomplete" if parsed is None else "http:parsed"
    statuses, closed = asyncio.run(_fragmentation_result(data))
    features = [parse_feature, f"http:transport:{'closed' if closed else 'open'}"]
    simple = _SIMPLE_REQUEST.fullmatch(data)
    if simple is not None:
        expected = (
            simple.group("method").decode(),
            simple.group("target"),
            int(simple.group("minor")),
            [(b"host", simple.group("host"))],
            len(data),
        )
        if parsed != expected or statuses != (204,):
            raise AssertionError(
                f"HTTP/1 parser disagreed with the restricted grammar: "
                f"parsed={parsed!r}, statuses={statuses!r}, expected={expected!r}"
            )
        features.append("http:differential:restricted-grammar")
    features.extend(f"http:status:{status}" for status in statuses)
    return tuple(dict.fromkeys(features))


TARGET = FuzzTarget(
    "http1-parser",
    run,
    seeds=load_versioned("http1"),
    dictionary=(
        b"GET ",
        b"POST ",
        b" HTTP/1.1\r\n",
        b"Host: ",
        b"Content-Length: ",
        b"Transfer-Encoding: chunked\r\n",
        b"Connection: ",
        b"\r\n\r\n",
        b"0\r\n\r\n",
    ),
    source_files=(
        "src/wreath/_native/http.c",
        "src/wreath/_native/server_http1.c",
    ),
    operator_names=(
        "guard.always-fires",
        "guard.never-fires",
        "guard.remove-raise",
        "predicate.always-true",
        "predicate.drop-operand",
        "value.widen-bound",
    ),
    strategy=HTTP1_STRATEGY,
)
