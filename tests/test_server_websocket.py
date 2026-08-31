from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest
from _server_ingest import feed

from wreath import Wreath
from wreath._websocket import build_frame, parse_frame
from wreath.server import ServerConfig
from wreath.websocket import WebSocket

try:
    _native_server = importlib.import_module("wreath._native._server")
    _NativeHttpProtocol = _native_server.HttpProtocol
except ImportError:
    _NativeHttpProtocol = None

IMPLS = [
    pytest.param(
        _NativeHttpProtocol,
        id="http1",
        marks=pytest.mark.skipif(_NativeHttpProtocol is None, reason="native server not built"),
    )
]

impl = pytest.mark.parametrize("protocol_cls", IMPLS)

# RFC 6455 section 1.3 known-answer pair.
RFC_KEY = b"dGhlIHNhbXBsZSBub25jZQ=="
RFC_ACCEPT = b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

MASK = b"\x01\x02\x03\x04"


def upgrade(path: bytes = b"/ws", extra: bytes = b"") -> bytes:
    return (
        b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: "
        + RFC_KEY
        + b"\r\nSec-WebSocket-Version: 13\r\n"
        + extra
        + b"\r\n"
    )


class FakeTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.closed = False
        self.aborted = False
        self.reading_paused = False

    def write(self, data: Any) -> None:
        if not self.closed:
            self.buffer += data

    def writelines(self, chunks: Any) -> None:
        for chunk in chunks:
            self.write(chunk)

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True
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
            "peername": ("127.0.0.1", 54321),
        }.get(name, default)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def start(
    protocol_cls: type,
    app: Any,
    chunks: list[bytes],
    config: ServerConfig | None = None,
) -> tuple[Any, FakeTransport]:
    loop = asyncio.get_running_loop()
    transport = FakeTransport()
    protocol = protocol_cls(app, config or ServerConfig(), loop, set())
    protocol.connection_made(transport)
    for chunk in chunks:
        feed(protocol, chunk)
        await _settle()
    await _settle()
    return protocol, transport


def split_head(wire: bytes) -> tuple[bytes, bytes]:
    head, _, rest = wire.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def frames(data: bytes) -> list[tuple[bool, int, bytes]]:
    out = []
    while data:
        parsed = parse_frame(data)
        if parsed is None:
            break
        fin, opcode, payload, consumed = parsed
        out.append((fin, opcode, payload))
        data = data[consumed:]
    return out


async def echo_app(scope: dict, receive: Any, send: Any) -> None:
    assert scope["type"] == "websocket"
    message = await receive()
    assert message["type"] == "websocket.connect"
    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return
        if "text" in message and message["text"] is not None:
            await send({"type": "websocket.send", "text": message["text"]})
        else:
            await send({"type": "websocket.send", "bytes": message["bytes"]})


@impl
@pytest.mark.asyncio
async def test_handshake_known_answer(protocol_cls: type) -> None:
    _, transport = await start(protocol_cls, echo_app, [upgrade()])
    head, rest = split_head(bytes(transport.buffer))
    assert head.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert b"sec-websocket-accept: " + RFC_ACCEPT + b"\r\n" in head
    assert b"upgrade: websocket" in head
    assert rest == b""
    assert not transport.closed


@impl
@pytest.mark.asyncio
async def test_scope_contents(protocol_cls: type) -> None:
    captured: list[dict] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        captured.append(scope)
        await receive()
        await send({"type": "websocket.close"})

    await start(
        protocol_cls,
        app,
        [upgrade(b"/room?x=1", b"Sec-WebSocket-Protocol: chat, superchat\r\n")],
    )
    scope = captured[0]
    assert scope["type"] == "websocket"
    assert scope["path"] == "/room"
    assert scope["query_string"] == b"x=1"
    assert scope["scheme"] == "ws"
    assert scope["subprotocols"] == ["chat", "superchat"]
    assert scope["http_version"] == "1.1"


@impl
@pytest.mark.asyncio
async def test_missing_key_is_400(protocol_cls: type) -> None:
    request = (
        b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    _, transport = await start(protocol_cls, echo_app, [request])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 400")
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_wrong_version_is_426(protocol_cls: type) -> None:
    request = (
        b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: "
        + RFC_KEY
        + b"\r\nSec-WebSocket-Version: 8\r\n\r\n"
    )
    _, transport = await start(protocol_cls, echo_app, [request])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 426")
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_connection_upgrade_requires_an_exact_token(protocol_cls: type) -> None:
    scopes: list[str] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        scopes.append(scope["type"])
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})
            return
        await receive()
        await send({"type": "http.response.start", "status": 204})
        await send({"type": "http.response.body", "body": b""})

    request = upgrade().replace(b"Connection: Upgrade", b"Connection: xupgrade")
    _, transport = await start(protocol_cls, app, [request])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 204")
    assert scopes == ["http"]


@impl
@pytest.mark.asyncio
async def test_connection_upgrade_token_can_be_in_a_repeated_field(
    protocol_cls: type,
) -> None:
    request = upgrade().replace(
        b"Connection: Upgrade",
        b"Connection: keep-alive\r\nConnection: Upgrade",
    )
    _, transport = await start(protocol_cls, echo_app, [request])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 101 Switching Protocols")


@impl
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "framing",
    [
        pytest.param(b"Content-Length: 4\r\n", id="fixed-body"),
        pytest.param(b"Transfer-Encoding: chunked\r\n", id="chunked-body"),
        pytest.param(
            b"Content-Length: 4\r\nTransfer-Encoding: chunked\r\n",
            id="ambiguous-body",
        ),
    ],
)
async def test_websocket_upgrade_rejects_http_body_framing(
    protocol_cls: type, framing: bytes
) -> None:
    called = False

    async def app(scope: dict, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    _, transport = await start(protocol_cls, app, [upgrade(extra=framing)])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 400")
    assert transport.closed
    assert called is False


@impl
@pytest.mark.asyncio
async def test_subprotocol_negotiation(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept", "subprotocol": scope["subprotocols"][0]})
        await send({"type": "websocket.close"})

    _, transport = await start(
        protocol_cls, app, [upgrade(b"/ws", b"Sec-WebSocket-Protocol: chat\r\n")]
    )
    head, _ = split_head(bytes(transport.buffer))
    assert b"sec-websocket-protocol: chat\r\n" in head


@impl
@pytest.mark.asyncio
async def test_text_and_binary_echo(protocol_cls: type) -> None:
    _, transport = await start(
        protocol_cls,
        echo_app,
        [
            upgrade(),
            build_frame(1, "héllo".encode(), True, MASK),
            build_frame(2, b"\x00\x01\x02", True, MASK),
        ],
    )
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [
        (True, 1, "héllo".encode()),
        (True, 2, b"\x00\x01\x02"),
    ]


@impl
@pytest.mark.asyncio
async def test_fragmented_message_reassembled(protocol_cls: type) -> None:
    _, transport = await start(
        protocol_cls,
        echo_app,
        [
            upgrade(),
            build_frame(1, b"one ", False, MASK),
            build_frame(0, b"two ", False, MASK),
            build_frame(0, b"three", True, MASK),
        ],
    )
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 1, b"one two three")]


@impl
@pytest.mark.asyncio
async def test_ping_answered_with_pong(protocol_cls: type) -> None:
    received: list[dict] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            received.append(message)
            if message["type"] == "websocket.disconnect":
                return

    _, transport = await start(
        protocol_cls,
        app,
        [upgrade(), build_frame(9, b"ping!", True, MASK)],
    )
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 10, b"ping!")]
    assert received == []  # control frames never reach the app


@impl
@pytest.mark.asyncio
async def test_client_close_echoed_and_delivered(protocol_cls: type) -> None:
    codes: list[int] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        message = await receive()
        assert message["type"] == "websocket.disconnect"
        codes.append(message["code"])

    _, transport = await start(
        protocol_cls,
        app,
        [upgrade(), build_frame(8, (1001).to_bytes(2) + b"bye", True, MASK)],
    )
    _, rest = split_head(bytes(transport.buffer))
    parsed = frames(rest)
    assert parsed[0][1] == 8  # close echo
    assert parsed[0][2][:2] == (1001).to_bytes(2)
    assert codes == [1001]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_app_close_sends_close_frame(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4000, "reason": "done"})

    _, transport = await start(protocol_cls, app, [upgrade()])
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 8, (4000).to_bytes(2) + b"done")]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_app_return_closes_1000(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})

    _, transport = await start(protocol_cls, app, [upgrade()])
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 8, (1000).to_bytes(2))]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_close_before_accept_is_403(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.close"})

    _, transport = await start(protocol_cls, app, [upgrade()])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 403")
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_app_return_without_accept_is_403(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()

    _, transport = await start(protocol_cls, app, [upgrade()])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 403")
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_app_exception_before_accept_is_500(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        raise RuntimeError("boom")

    _, transport = await start(protocol_cls, app, [upgrade()])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 500")
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_app_exception_after_accept_is_1011(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        message = await receive()
        assert message["type"] == "websocket.receive"
        raise RuntimeError("boom")

    _, transport = await start(protocol_cls, app, [upgrade(), build_frame(1, b"x", True, MASK)])
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 8, (1011).to_bytes(2))]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_unmasked_client_frame_fails_1002(protocol_cls: type) -> None:
    _, transport = await start(protocol_cls, echo_app, [upgrade(), build_frame(1, b"nope")])
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 8, (1002).to_bytes(2))]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_invalid_utf8_text_fails_1007(protocol_cls: type) -> None:
    _, transport = await start(
        protocol_cls, echo_app, [upgrade(), build_frame(1, b"\xff\xfe", True, MASK)]
    )
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 8, (1007).to_bytes(2))]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_oversized_control_frame_fails_1002(protocol_cls: type) -> None:
    _, transport = await start(
        protocol_cls, echo_app, [upgrade(), build_frame(9, b"p" * 126, True, MASK)]
    )
    _, rest = split_head(bytes(transport.buffer))
    assert frames(rest) == [(True, 8, (1002).to_bytes(2))]
    assert transport.closed


@impl
@pytest.mark.asyncio
async def test_transport_loss_delivers_1006(protocol_cls: type) -> None:
    codes: list[int] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        message = await receive()
        codes.append(message.get("code"))

    protocol, _ = await start(protocol_cls, app, [upgrade()])
    protocol.connection_lost(None)
    await _settle()
    assert codes == [1006]


@impl
@pytest.mark.asyncio
async def test_wreath_app_websocket_route(protocol_cls: type) -> None:
    app = Wreath()

    @app.websocket("/room/{name}")
    async def room(ws: WebSocket) -> None:
        await ws.accept()
        greeting = await ws.receive_text()
        await ws.send_text(f"{greeting} {ws.path_params['name']}")
        await ws.close()

    _, transport = await start(
        protocol_cls,
        app,
        [upgrade(b"/room/lobby"), build_frame(1, b"hey", True, MASK)],
    )
    _, rest = split_head(bytes(transport.buffer))
    parsed = frames(rest)
    assert parsed[0] == (True, 1, b"hey lobby")
    assert parsed[1][1] == 8


@impl
@pytest.mark.asyncio
async def test_wreath_app_unknown_ws_route_is_403(protocol_cls: type) -> None:
    app = Wreath()

    @app.get("/")
    async def home(request: Any) -> Any:
        from wreath.response import TextResponse

        return TextResponse("hi")

    _, transport = await start(protocol_cls, app, [upgrade(b"/nope")])
    assert bytes(transport.buffer).startswith(b"HTTP/1.1 403")


# A WebSocket message may carry zero payload bytes, so the byte watermark alone
# cannot bound the queue: an empty-message flood keeps queued_bytes at zero
# forever. Both bounds apply, and resuming needs both to fall.


def _empty_text_frame() -> bytes:
    return build_frame(1, b"", True, MASK)


@impl
@pytest.mark.asyncio
async def test_empty_messages_pause_reading_by_count(protocol_cls: type) -> None:
    gate = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await gate.wait()  # never read while the flood arrives

    config = ServerConfig(read_high_water_messages=64)
    protocol, transport = await start(protocol_cls, app, [upgrade()], config)
    for _ in range(200):
        feed(protocol, _empty_text_frame())
    await _settle()
    assert transport.reading_paused, (
        "zero-byte messages never paused reading: only a count bound can"
    )
    gate.set()
    await _settle()


@impl
@pytest.mark.asyncio
async def test_byte_watermark_still_pauses_on_large_payloads(protocol_cls: type) -> None:
    gate = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await gate.wait()

    # A generous count bound, a tiny byte bound: bytes must still pause.
    config = ServerConfig(read_high_water=1024, read_high_water_messages=100_000)
    protocol, transport = await start(protocol_cls, app, [upgrade()], config)
    for _ in range(8):
        feed(protocol, build_frame(2, b"z" * 512, True, MASK))
    await _settle()
    assert transport.reading_paused
    gate.set()
    await _settle()


@impl
@pytest.mark.asyncio
async def test_resume_requires_both_low_water_conditions(protocol_cls: type) -> None:
    drained: dict = {"count": 0}
    release = asyncio.Event()
    seen_paused = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await seen_paused.wait()
        # Drain a few: not enough to reach half the count watermark.
        for _ in range(4):
            await receive()
            drained["count"] += 1
        release.set()

    config = ServerConfig(read_high_water=1 << 20, read_high_water_messages=64)
    protocol, transport = await start(protocol_cls, app, [upgrade()], config)
    for _ in range(200):
        feed(protocol, _empty_text_frame())
    await _settle()
    assert transport.reading_paused
    seen_paused.set()
    await asyncio.wait_for(release.wait(), timeout=5)
    await _settle()
    # 196 messages still queued, far above 64 // 2: still paused.
    assert transport.reading_paused, "resumed while the message count was still high"


@impl
@pytest.mark.asyncio
async def test_queue_drains_in_order_across_compaction(protocol_cls: type) -> None:
    received: list[bytes] = []
    done = asyncio.Event()
    gate = asyncio.Event()
    total = 300

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await gate.wait()
        while len(received) < total:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                break
            received.append(message["bytes"])
        done.set()

    config = ServerConfig(read_high_water_messages=1 << 20)
    protocol, _transport = await start(protocol_cls, app, [upgrade()], config)
    expected = [f"{i:04d}".encode() for i in range(total)]
    for payload in expected:
        feed(protocol, build_frame(2, payload, True, MASK))
    await _settle()
    gate.set()
    await asyncio.wait_for(done.wait(), timeout=10)
    assert received == expected


@impl
@pytest.mark.asyncio
async def test_disconnect_is_delivered_even_when_paused(protocol_cls: type) -> None:
    saw_disconnect = asyncio.Event()
    gate = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await gate.wait()
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                saw_disconnect.set()
                return

    config = ServerConfig(read_high_water_messages=16)
    protocol, transport = await start(protocol_cls, app, [upgrade()], config)
    for _ in range(100):
        feed(protocol, _empty_text_frame())
    await _settle()
    assert transport.reading_paused
    protocol.connection_lost(None)
    gate.set()
    await asyncio.wait_for(saw_disconnect.wait(), timeout=5)


# Fragments accumulate into one bytearray rather than a list of per-fragment
# objects: an empty continuation allocates nothing, and what is retained is
# bounded by max_body_bytes rather than by frame count. The limit is checked
# before the accumulator grows.


@impl
@pytest.mark.asyncio
async def test_thousands_of_empty_fragments_complete_correctly(protocol_cls: type) -> None:
    received: list = []
    done = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        message = await receive()
        received.append(message)
        done.set()

    # Just under the default max_ws_fragments (4096): many empty fragments are
    # still a valid message, and must reassemble with nothing retained per
    # fragment. Going over the limit is covered separately.
    protocol, _transport = await start(protocol_cls, app, [upgrade()])
    feed(protocol, build_frame(1, b"start", False, MASK))
    for _ in range(4000):
        feed(protocol, build_frame(0, b"", False, MASK))
    feed(protocol, build_frame(0, b"end", True, MASK))
    await _settle()
    await asyncio.wait_for(done.wait(), timeout=10)
    assert received[0]["text"] == "startend"


@impl
@pytest.mark.asyncio
async def test_mixed_empty_and_non_empty_fragments_keep_order(protocol_cls: type) -> None:
    received: list = []
    done = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        received.append(await receive())
        done.set()

    protocol, _transport = await start(protocol_cls, app, [upgrade()])
    feed(protocol, build_frame(2, b"", False, MASK))  # empty opening fragment
    expected = b""
    for i in range(100):
        chunk = b"" if i % 2 else f"{i:03d}".encode()
        expected += chunk
        feed(protocol, build_frame(0, chunk, False, MASK))
    feed(protocol, build_frame(0, b"!", True, MASK))
    expected += b"!"
    await _settle()
    await asyncio.wait_for(done.wait(), timeout=10)
    assert received[0]["bytes"] == expected


@impl
@pytest.mark.asyncio
async def test_fragmented_message_exactly_at_limit_succeeds(protocol_cls: type) -> None:
    received: list = []
    done = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        received.append(await receive())
        done.set()

    config = ServerConfig(max_body_bytes=1024)
    protocol, _transport = await start(protocol_cls, app, [upgrade()], config)
    feed(protocol, build_frame(2, b"a" * 512, False, MASK))
    feed(protocol, build_frame(0, b"b" * 512, True, MASK))
    await _settle()
    await asyncio.wait_for(done.wait(), timeout=10)
    assert received[0]["bytes"] == b"a" * 512 + b"b" * 512


@impl
@pytest.mark.asyncio
async def test_fragmented_message_one_byte_over_limit_closes_1009(protocol_cls: type) -> None:
    protocol, transport = await start(
        protocol_cls, echo_app, [upgrade()], ServerConfig(max_body_bytes=1024)
    )
    _head, rest = split_head(bytes(transport.buffer))
    transport.buffer.clear()
    feed(protocol, build_frame(2, b"a" * 512, False, MASK))
    feed(protocol, build_frame(0, b"b" * 513, True, MASK))  # 1025 total
    await _settle()
    closes = [f for f in frames(bytes(transport.buffer)) if f[1] == 0x8]
    assert closes, "expected a close frame"
    assert int.from_bytes(closes[-1][2][:2], "big") == 1009


@impl
@pytest.mark.asyncio
async def test_invalid_utf8_spanning_fragments_closes_1007(protocol_cls: type) -> None:
    protocol, transport = await start(protocol_cls, echo_app, [upgrade()])
    transport.buffer.clear()
    # A two-byte sequence split across fragments, with an invalid continuation.
    feed(protocol, build_frame(1, b"\xc3", False, MASK))
    feed(protocol, build_frame(0, b"\x28", True, MASK))
    await _settle()
    closes = [f for f in frames(bytes(transport.buffer)) if f[1] == 0x8]
    assert closes, "expected a close frame"
    assert int.from_bytes(closes[-1][2][:2], "big") == 1007


@impl
@pytest.mark.asyncio
async def test_valid_utf8_split_across_fragments_is_accepted(protocol_cls: type) -> None:
    received: list = []
    done = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        received.append(await receive())
        done.set()

    protocol, _transport = await start(protocol_cls, app, [upgrade()])
    encoded = "héllo-✓".encode()
    feed(protocol, build_frame(1, encoded[:3], False, MASK))
    feed(protocol, build_frame(0, encoded[3:6], False, MASK))
    feed(protocol, build_frame(0, encoded[6:], True, MASK))
    await _settle()
    await asyncio.wait_for(done.wait(), timeout=10)
    assert received[0]["text"] == "héllo-✓"


@impl
@pytest.mark.asyncio
async def test_close_during_fragmentation_cleans_up(protocol_cls: type) -> None:
    saw_disconnect = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                saw_disconnect.set()
                return

    protocol, _transport = await start(protocol_cls, app, [upgrade()])
    feed(protocol, build_frame(2, b"partial", False, MASK))
    feed(protocol, build_frame(8, b"\x03\xe8", True, MASK))  # close, 1000
    await _settle()
    await asyncio.wait_for(saw_disconnect.wait(), timeout=5)


@impl
@pytest.mark.asyncio
async def test_transport_loss_during_fragmentation_cleans_up(protocol_cls: type) -> None:
    saw_disconnect = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                saw_disconnect.set()
                return

    protocol, _transport = await start(protocol_cls, app, [upgrade()])
    feed(protocol, build_frame(2, b"partial", False, MASK))
    await _settle()
    protocol.connection_lost(None)
    await asyncio.wait_for(saw_disconnect.wait(), timeout=5)


@impl
@pytest.mark.asyncio
async def test_empty_fragments_count_toward_the_fragment_limit(
    protocol_cls: type,
) -> None:
    protocol, transport = await start(
        protocol_cls, echo_app, [upgrade()], ServerConfig(max_ws_fragments=8)
    )
    transport.buffer.clear()
    feed(protocol, build_frame(1, b"a", False, MASK))
    for _ in range(16):
        feed(protocol, build_frame(0, b"", False, MASK))
    await _settle()
    closes = [f for f in frames(bytes(transport.buffer)) if f[1] == 0x8]
    assert closes, "expected a close frame"
    assert int.from_bytes(closes[-1][2][:2], "big") == 1009


@impl
@pytest.mark.asyncio
async def test_a_message_at_the_exact_fragment_limit_is_delivered(
    protocol_cls: type,
) -> None:
    received: list = []
    done = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        received.append(await receive())
        done.set()

    # 8 fragments total: the opening frame plus 7 continuations.
    protocol, _transport = await start(
        protocol_cls, app, [upgrade()], ServerConfig(max_ws_fragments=8)
    )
    feed(protocol, build_frame(1, b"a", False, MASK))
    for _ in range(6):
        feed(protocol, build_frame(0, b"b", False, MASK))
    feed(protocol, build_frame(0, b"c", True, MASK))
    await _settle()
    assert done.is_set()
    assert received[0]["text"] == "a" + "b" * 6 + "c"


@impl
@pytest.mark.asyncio
async def test_one_fragment_over_the_limit_closes_1009(protocol_cls: type) -> None:
    protocol, transport = await start(
        protocol_cls, echo_app, [upgrade()], ServerConfig(max_ws_fragments=8)
    )
    transport.buffer.clear()
    feed(protocol, build_frame(1, b"a", False, MASK))
    for _ in range(7):
        feed(protocol, build_frame(0, b"b", False, MASK))
    feed(protocol, build_frame(0, b"c", True, MASK))  # the 9th fragment
    await _settle()
    closes = [f for f in frames(bytes(transport.buffer)) if f[1] == 0x8]
    assert closes, "expected a close frame"
    assert int.from_bytes(closes[-1][2][:2], "big") == 1009


@impl
@pytest.mark.asyncio
async def test_the_fragment_count_resets_between_messages(protocol_cls: type) -> None:
    received: list = []
    done = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        while len(received) < 2:
            received.append(await receive())
        done.set()

    protocol, _transport = await start(
        protocol_cls, app, [upgrade()], ServerConfig(max_ws_fragments=4)
    )
    for _ in range(2):
        feed(protocol, build_frame(1, b"x", False, MASK))
        feed(protocol, build_frame(0, b"y", False, MASK))
        feed(protocol, build_frame(0, b"z", True, MASK))
    await _settle()
    await asyncio.wait_for(done.wait(), timeout=10)
    assert [item["text"] for item in received] == ["xyz", "xyz"]


def test_a_socket_without_path_params_gets_an_empty_mapping() -> None:
    # `path_params or {}`: a route with no captures passes None, and every
    # reader expects a mapping rather than having to guard for one.
    from wreath.websocket import WebSocket

    ws = WebSocket({"type": "websocket"}, None, None, path_params=None)
    assert ws.path_params == {}


def test_websocket_scope_lists_need_no_eager_fallback() -> None:
    from wreath.websocket import WebSocket

    class Scope(dict):
        def get(self, key, default=None):
            if key in {"headers", "subprotocols"} and isinstance(default, list):
                raise AssertionError(f"{key} allocated an unused fallback list")
            return super().get(key, default)

    headers = [(b"host", b"example")]
    subprotocols = ["chat"]
    websocket = WebSocket(
        Scope(type="websocket", headers=headers, subprotocols=subprotocols), None, None
    )
    assert websocket.headers is headers
    assert websocket.subprotocols is subprotocols


@pytest.mark.parametrize("code", [1004, 1005, 1006, 1015, 999, 2999, 5000])
def test_a_close_code_an_endpoint_may_not_send_is_refused(code: int) -> None:
    import asyncio

    from wreath.websocket import WebSocket

    ws = WebSocket({"type": "websocket"}, None, None, path_params={})
    with pytest.raises(ValueError, match="invalid WebSocket close code"):
        asyncio.run(ws.close(code=code))


@pytest.mark.parametrize("code", [1000, 1001, 1011, 3000, 4999])
def test_a_sendable_close_code_is_accepted(code: int) -> None:
    from wreath.websocket import _valid_close_code

    assert _valid_close_code(code) is True


async def test_a_sendable_close_code_reaches_the_transport() -> None:
    from wreath.websocket import WebSocket

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    ws = WebSocket({"type": "websocket"}, receive, send, path_params={})
    await ws.close(code=1000)
    assert sent and sent[-1]["type"] == "websocket.close"
    assert sent[-1]["code"] == 1000
