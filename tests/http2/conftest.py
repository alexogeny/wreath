from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from wreath.server import ServerConfig

from . import support

try:
    _native_server = importlib.import_module("wreath._native._server")
    Http2Protocol = getattr(_native_server, "Http2Protocol", None)
except ImportError:  # pragma: no cover - extension always built in CI
    Http2Protocol = None

requires_h2 = pytest.mark.skipif(
    Http2Protocol is None,
    reason="native Http2Protocol not built yet (Step 3)",
)


class FakeTransport(asyncio.Transport):
    def __init__(self, extra: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.writes: list[bytes] = []
        self.closed = False
        self.aborted = False
        self.reading_paused = False
        self.write_paused_signalled = False
        self._extra = extra or {
            "sockname": ("127.0.0.1", 8000),
            "peername": ("127.0.0.1", 54321),
        }

    def write(self, data: Any) -> None:
        if not self.closed:
            b = bytes(data)
            self.writes.append(b)
            self.buffer += b

    def writelines(self, list_of_data: Any) -> None:
        for chunk in list_of_data:
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
        return self._extra.get(name, default)


async def _settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


class H2Driver:
    """Drives one native Http2Protocol connection for tests."""

    def __init__(
        self,
        app: Any,
        config: ServerConfig | None = None,
        extra: dict[str, Any] | None = None,
        metal_scheduler: bool = False,
    ) -> None:
        assert Http2Protocol is not None
        self.loop = asyncio.get_event_loop()
        self.registry: set[Any] = set()
        self.transport = FakeTransport(extra)
        self.config = config or ServerConfig(protocols=("h2",))
        missing = object()
        previous = getattr(self.loop, "_native_loop", missing)
        if metal_scheduler:
            self.loop._native_loop = True
        try:
            self.protocol = Http2Protocol(app, self.config, self.loop, self.registry)
        finally:
            if metal_scheduler:
                if previous is missing:
                    del self.loop._native_loop
                else:
                    self.loop._native_loop = previous
        self.parser = support.FrameParser()
        self._consumed = 0

    def connection_made(self) -> None:
        self.protocol.connection_made(self.transport)

    def feed(self, data: bytes) -> None:
        self.protocol.data_received(data)

    async def feed_and_settle(self, data: bytes) -> None:
        self.protocol.data_received(data)
        await _settle()

    async def settle(self) -> None:
        await _settle()

    def raw_out(self) -> bytes:
        return bytes(self.transport.buffer)

    def frames(self) -> list[support.Frame]:
        """Return all frames the server has emitted so far."""
        data = bytes(self.transport.buffer)
        fresh = data[self._consumed :]
        self._consumed = len(data)
        self.parser.feed(fresh)
        return self.parser.frames()

    async def preface(self, client_settings: dict[int, int] | None = None) -> None:
        """Perform the client half of the connection preface."""
        self.connection_made()
        await _settle()
        self.feed(support.PREFACE)
        self.feed(support.encode_settings(client_settings or {}))
        await _settle()

    def close(self) -> None:
        self.protocol.connection_lost(None)


@pytest.fixture
def make_driver():
    drivers: list[H2Driver] = []

    def _make(
        app: Any,
        config: ServerConfig | None = None,
        extra: dict[str, Any] | None = None,
        metal_scheduler: bool = False,
    ) -> H2Driver:
        d = H2Driver(app, config, extra, metal_scheduler)
        drivers.append(d)
        return d

    yield _make
    for d in drivers:
        try:
            d.close()
        except Exception:  # noqa: BLE001 -- fixture teardown, best effort
            # Teardown for a driver a test may have already torn down itself. A
            # failure here must not mask the assertion that actually ran.
            pass


async def ok_app(scope: dict, receive: Any, send: Any) -> None:
    assert scope["type"] == "http"
    body = b""
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"hello"})


def scope_capture_app() -> tuple[Any, list[dict]]:
    captured: list[dict] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        captured.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app, captured
