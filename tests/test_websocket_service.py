from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from wreath import Wreath
from wreath._flight_markers import PH_WS_FANOUT, phase_marker
from wreath.services import Supervisor
from wreath.websocket import (
    ConnectionBackpressure,
    Heartbeat,
    WebSocket,
    WebSocketService,
)

pytestmark = pytest.mark.asyncio


class FakeSocket:
    def __init__(self, *, subprotocols: list[str] | None = None) -> None:
        self.subprotocols = subprotocols or []
        self.accepted: str | None = None
        self.sent: list[str | bytes] = []
        self.closed: list[tuple[int, str]] = []
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.ready = asyncio.Event()
        self.write_started = asyncio.Event()
        self.wrote = asyncio.Event()
        self.release_writes = asyncio.Event()
        self.release_writes.set()

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = subprotocol
        self.ready.set()

    async def send(self, frame: str | bytes) -> None:
        self.write_started.set()
        await self.release_writes.wait()
        self.sent.append(frame)
        self.wrote.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))
        self.inbox.put_nowait(None)

    def deliver(self, frame: str | bytes) -> None:
        self.inbox.put_nowait(frame)

    def finish(self) -> None:
        self.inbox.put_nowait(None)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str | bytes:
        frame = await self.inbox.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


async def _start_service(**options: Any) -> WebSocketService:
    service = WebSocketService(**options)
    await service.start(Supervisor())
    return service


async def test_service_runs_sequential_handler_and_bounded_sender() -> None:
    service = await _start_service()
    socket = FakeSocket(subprotocols=["llama-trek.v1"])

    async def handle(frame: str | bytes) -> str:
        return f"seen:{frame}"

    task = asyncio.create_task(
        service.serve(
            cast(WebSocket, socket),
            handle,
            key="guide",
            subprotocol="llama-trek.v1",
        )
    )
    await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
    socket.deliver("ridge")
    await asyncio.wait_for(socket.wrote.wait(), timeout=1)
    socket.finish()
    await asyncio.wait_for(task, timeout=1)

    assert socket.accepted == "llama-trek.v1"
    assert socket.closed[-1] == (1000, "")
    assert service.snapshot.accepted == 1
    assert service.snapshot.active == 0


async def test_reject_policy_never_grows_past_queue_capacity() -> None:
    service = await _start_service(queue_capacity=1, overflow="reject")
    socket = FakeSocket()
    socket.release_writes.clear()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="camera"))
    await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
    await service.send("camera", "first")
    await asyncio.wait_for(socket.write_started.wait(), timeout=0.5)
    await service.send("camera", "second")

    with pytest.raises(ConnectionBackpressure):
        await service.send("camera", "third")

    assert service.snapshot.queued == 1
    assert service.snapshot.queue_refusals == 1
    assert socket.closed == []
    socket.release_writes.set()
    socket.finish()
    await asyncio.wait_for(task, timeout=1)


async def test_disconnect_policy_closes_a_connection_with_a_full_queue() -> None:
    service = await _start_service(queue_capacity=1, overflow="disconnect")
    socket = FakeSocket()
    socket.release_writes.clear()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="camera"))
    try:
        await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
        await service.send("camera", "first")
        await asyncio.wait_for(socket.write_started.wait(), timeout=0.5)
        await service.send("camera", "second")

        with pytest.raises(ConnectionBackpressure):
            await service.send("camera", "third")

        assert socket.closed == [(1013, "outbound queue capacity exceeded")]
    finally:
        socket.release_writes.set()
        socket.finish()
        await asyncio.wait_for(task, timeout=1)


async def test_reject_broadcast_counts_only_connections_that_accept_the_frame() -> None:
    service = await _start_service(queue_capacity=1, overflow="reject")
    full = FakeSocket()
    open_socket = FakeSocket()
    full.release_writes.clear()
    open_socket.release_writes.clear()

    async def handle(frame: str | bytes) -> None:
        return None

    full_task = asyncio.create_task(service.serve(cast(WebSocket, full), handle, key="full"))
    open_task = asyncio.create_task(service.serve(cast(WebSocket, open_socket), handle, key="open"))
    await asyncio.gather(full.ready.wait(), open_socket.ready.wait())
    await service.send("full", "writing")
    await service.send("open", "writing")
    await asyncio.gather(full.write_started.wait(), open_socket.write_started.wait())
    await service.send("full", "queued")

    assert await service.broadcast("broadcast") == 1
    assert service.snapshot.queued == 2
    assert service.snapshot.queue_refusals == 1

    full.release_writes.set()
    open_socket.release_writes.set()
    full.finish()
    open_socket.finish()
    await asyncio.gather(full_task, open_task)


async def test_capacity_and_subprotocol_are_refused_before_accept() -> None:
    service = await _start_service(max_connections=1)
    first = FakeSocket()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, first), handle, key="first"))
    await asyncio.wait_for(first.ready.wait(), timeout=0.5)
    second = FakeSocket(subprotocols=["camera-trap.v1"])
    await service.serve(
        cast(WebSocket, second),
        handle,
        key="second",
        subprotocol="not-offered",
    )

    assert second.accepted is None
    assert second.closed == [(1013, "connection capacity unavailable")]
    assert service.snapshot.capacity_refusals == 1
    first.finish()
    await asyncio.wait_for(task, timeout=1)


async def test_drain_closes_and_waits_for_active_connections() -> None:
    service = await _start_service()
    socket = FakeSocket()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="trap"))
    await asyncio.wait_for(socket.ready.wait(), timeout=0.5)

    await service.drain(asyncio.get_running_loop().time() + 1)
    await asyncio.wait_for(task, timeout=1)

    assert socket.closed == [(1001, "service shutting down")]
    assert service.snapshot.active == 0
    assert service.snapshot.drain_timeouts == 0


async def test_outbound_write_uses_existing_flight_recorder_phase() -> None:
    service = await _start_service()
    socket = FakeSocket()
    phases: list[tuple[int, int, int, int]] = []
    token = phase_marker.set(lambda *values: phases.append(values))

    async def handle(frame: str | bytes) -> None:
        return None

    try:
        task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="flight"))
        await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
        await service.send("flight", b"frame")
        await asyncio.wait_for(socket.wrote.wait(), timeout=1)
        socket.finish()
        await asyncio.wait_for(task, timeout=1)
    finally:
        phase_marker.reset(token)

    assert phases[0][0] == PH_WS_FANOUT
    assert phases[0][3] >= 0


async def test_application_supervises_generic_services() -> None:
    events: list[str] = []

    class Service:
        async def start(self, supervisor: Supervisor) -> None:
            events.append("start")

        async def drain(self, deadline: float) -> None:
            events.append("drain")

    app = Wreath(hardening="off")
    registered = Service()
    assert app.service("connections", registered) is registered
    messages = iter(({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}))
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert events == ["start", "drain"]
    assert [message["type"] for message in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


async def test_websocket_json_helpers_share_wreath_codec() -> None:
    incoming = iter(
        (
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": '{"trail":"ridge"}'},
        )
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(incoming)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    websocket = WebSocket({"type": "websocket", "path": "/"}, receive, send)
    await websocket.accept()
    assert await websocket.receive_json() == {"trail": "ridge"}
    await websocket.send_json({"seen": True})

    assert sent[-1] == {"type": "websocket.send", "text": '{"seen":true}'}


async def test_protocol_supplied_heartbeat_is_bounded_and_consumes_ack() -> None:
    service = await _start_service(
        heartbeat=Heartbeat(
            frame="ping",
            acknowledge=lambda frame: frame == "pong",
            interval=0.01,
            timeout=0.1,
        )
    )
    socket = FakeSocket()
    handled: list[str | bytes] = []

    async def handle(frame: str | bytes) -> None:
        handled.append(frame)

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="heartbeat"))
    await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
    await asyncio.wait_for(socket.wrote.wait(), timeout=1)
    socket.deliver("pong")
    socket.finish()
    await asyncio.wait_for(task, timeout=1)

    assert socket.sent[0] == "ping"
    assert handled == []
    assert service.snapshot.heartbeat_timeouts == 0


async def test_heartbeat_timeout_joins_its_acknowledgement_waits(monkeypatch) -> None:
    service = await _start_service(
        heartbeat=Heartbeat(
            frame="ping",
            acknowledge=lambda frame: frame == "pong",
            interval=0.01,
            timeout=0.01,
        )
    )
    socket = FakeSocket()
    children: list[asyncio.Task[object]] = []
    create_task = asyncio.create_task

    def track(coro):
        task = create_task(coro)
        children.append(task)
        return task

    monkeypatch.setattr("wreath.websocket.asyncio.create_task", track)

    async def handle(frame: str | bytes) -> None:
        return None

    serving = create_task(service.serve(cast(WebSocket, socket), handle, key="heartbeat"))
    await asyncio.wait_for(serving, timeout=1)

    assert service.snapshot.heartbeat_timeouts == 1
    assert children
    assert all(task.done() for task in children)
