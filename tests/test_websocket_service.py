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


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_connections": 0}, "max_connections must be at least one"),
        ({"queue_capacity": 0}, "queue_capacity must be at least one"),
        ({"overflow": "drop"}, "overflow must be"),
        ({"enqueue_timeout": 0}, "enqueue_timeout must be positive"),
    ],
)
async def test_service_refuses_invalid_capacity_policies(options: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        WebSocketService(**options)


async def test_service_refuses_connections_before_start() -> None:
    service = WebSocketService()
    socket = FakeSocket()
    socket.finish()

    async def handle(frame: str | bytes) -> None:
        return None

    await service.serve(cast(WebSocket, socket), handle)
    assert socket.accepted is None
    assert socket.closed == [(1013, "connection capacity unavailable")]


async def test_service_uses_socket_identity_when_no_key_is_supplied() -> None:
    service = await _start_service()
    socket = FakeSocket()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle))
    await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
    await service.send(id(socket), "identified")
    await asyncio.wait_for(socket.wrote.wait(), timeout=0.5)
    socket.finish()
    await asyncio.wait_for(task, timeout=0.5)
    assert socket.sent == ["identified"]


async def test_service_refuses_a_duplicate_connection_key() -> None:
    service = await _start_service()
    first = FakeSocket()
    duplicate = FakeSocket()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, first), handle, key="same"))
    await asyncio.wait_for(first.ready.wait(), timeout=0.5)
    duplicate.finish()
    await service.serve(cast(WebSocket, duplicate), handle, key="same")
    assert duplicate.closed == [(1013, "connection capacity unavailable")]
    first.finish()
    await asyncio.wait_for(task, timeout=0.5)


async def test_service_refuses_an_unoffered_subprotocol_with_free_capacity() -> None:
    service = await _start_service(max_connections=2)
    socket = FakeSocket(subprotocols=["offered"])
    socket.finish()

    async def handle(frame: str | bytes) -> None:
        return None

    await service.serve(cast(WebSocket, socket), handle, subprotocol="missing")
    assert socket.accepted is None
    assert socket.closed == [(1002, "subprotocol was not offered")]
    assert service.snapshot.protocol_refusals == 1


async def test_connection_cleanup_preserves_a_newer_owner_of_the_same_key() -> None:
    service = await _start_service()
    socket = FakeSocket()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="shared"))
    await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
    replacement = object()
    service._connections["shared"] = replacement
    socket.finish()
    await asyncio.wait_for(task, timeout=0.5)
    assert service._connections["shared"] is replacement
    del service._connections["shared"]


async def test_send_refuses_an_unknown_connection() -> None:
    service = await _start_service()
    with pytest.raises(KeyError, match="no active WebSocket connection 'missing'"):
        await service.send("missing", "frame")


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
    service = await _start_service(
        queue_capacity=1,
        overflow="reject",
        enqueue_timeout=0.01,
    )
    socket = FakeSocket()
    socket.release_writes.clear()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="camera"))
    await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
    await service.send("camera", "first")
    await asyncio.wait_for(socket.write_started.wait(), timeout=0.5)
    await service.send("camera", "second")

    with pytest.raises(ConnectionBackpressure, match="outbound queue is full"):
        await service.send("camera", "third")

    assert service.snapshot.queued == 1
    assert service.snapshot.queue_refusals == 1
    assert socket.closed == []
    socket.release_writes.set()
    socket.finish()
    await asyncio.wait_for(task, timeout=1)


async def test_disconnect_policy_closes_a_connection_with_a_full_queue() -> None:
    service = await _start_service(
        queue_capacity=1,
        overflow="disconnect",
        enqueue_timeout=0.01,
    )
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


async def test_backpressure_policy_times_out_a_full_queue() -> None:
    service = await _start_service(
        queue_capacity=1,
        overflow="backpressure",
        enqueue_timeout=0.01,
    )
    socket = FakeSocket()
    socket.release_writes.clear()

    async def handle(frame: str | bytes) -> None:
        return None

    task = asyncio.create_task(service.serve(cast(WebSocket, socket), handle, key="slow"))
    try:
        await asyncio.wait_for(socket.ready.wait(), timeout=0.5)
        await service.send("slow", "writing")
        await asyncio.wait_for(socket.write_started.wait(), timeout=0.5)
        await service.send("slow", "queued")
        with pytest.raises(ConnectionBackpressure, match="did not free outbound capacity"):
            await service.send("slow", "overflow")
        assert service.snapshot.queue_refusals == 1
    finally:
        socket.release_writes.set()
        socket.finish()
        await asyncio.wait_for(task, timeout=0.5)


async def test_disconnect_overflow_tolerates_a_connection_without_a_send_task() -> None:
    from wreath import websocket as websocket_module

    service = await _start_service(
        queue_capacity=1,
        overflow="disconnect",
        enqueue_timeout=0.01,
    )
    socket = FakeSocket()
    connection = websocket_module._ManagedConnection(
        service, "opening", cast(WebSocket, socket)
    )
    service._connections["opening"] = connection
    connection.queue.put_nowait("full")
    with pytest.raises(ConnectionBackpressure):
        await service.send("opening", "overflow")
    assert socket.closed == [(1013, "outbound queue capacity exceeded")]


async def test_disconnect_overflow_cancels_an_active_send_task() -> None:
    from wreath import websocket as websocket_module

    service = await _start_service(
        queue_capacity=1,
        overflow="disconnect",
        enqueue_timeout=0.01,
    )
    socket = FakeSocket()
    connection = websocket_module._ManagedConnection(
        service, "blocked", cast(WebSocket, socket)
    )
    service._connections["blocked"] = connection
    connection.queue.put_nowait("full")
    send_task = asyncio.create_task(asyncio.Event().wait())
    connection.send_task = send_task
    with pytest.raises(ConnectionBackpressure):
        await service.send("blocked", "overflow")
    assert send_task.cancelled()


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


async def test_reject_broadcast_does_not_call_the_other_overflow_paths(monkeypatch) -> None:
    from wreath import websocket as websocket_module

    service = await _start_service(queue_capacity=1, overflow="reject")
    socket = FakeSocket()
    service._connections["direct"] = websocket_module._ManagedConnection(
        service, "direct", cast(WebSocket, socket)
    )

    async def unexpected_send(*_args: object) -> None:
        raise AssertionError("reject broadcast writes directly to each bounded queue")

    monkeypatch.setattr(WebSocketService, "send", unexpected_send)
    assert await service.broadcast("frame") == 1


async def test_non_reject_broadcast_uses_send_policy(monkeypatch) -> None:
    from wreath import websocket as websocket_module

    service = await _start_service(queue_capacity=1, overflow="backpressure")
    socket = FakeSocket()
    service._connections["direct"] = websocket_module._ManagedConnection(
        service, "direct", cast(WebSocket, socket)
    )
    calls: list[tuple[object, object]] = []

    async def record_send(self, key: object, frame: object) -> None:
        calls.append((key, frame))

    monkeypatch.setattr(WebSocketService, "send", record_send)
    assert await service.broadcast("frame") == 1
    assert calls == [("direct", "frame")]


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


async def test_heartbeat_acknowledgement_continues_to_the_next_probe() -> None:
    from wreath import websocket as websocket_module

    service = await _start_service(queue_capacity=2)
    socket = FakeSocket()
    connection = websocket_module._ManagedConnection(
        service, "heartbeat", cast(WebSocket, socket)
    )
    service._connections["heartbeat"] = connection
    heartbeat = Heartbeat(
        frame="ping",
        acknowledge=lambda frame: frame == "pong",
        interval=0.001,
        timeout=0.1,
    )
    task = asyncio.create_task(service._heartbeat_loop(connection, heartbeat))
    assert await asyncio.wait_for(connection.queue.get(), timeout=0.1) == "ping"
    connection.heartbeat_ack.set()
    assert await asyncio.wait_for(connection.queue.get(), timeout=0.1) == "ping"
    connection.stopping.set()
    await asyncio.wait_for(task, timeout=0.1)


async def test_heartbeat_timeout_tolerates_a_missing_send_task() -> None:
    from wreath import websocket as websocket_module

    service = await _start_service(queue_capacity=1)
    socket = FakeSocket()
    connection = websocket_module._ManagedConnection(
        service, "heartbeat", cast(WebSocket, socket)
    )
    service._connections["heartbeat"] = connection
    heartbeat = Heartbeat(
        frame="ping",
        acknowledge=lambda frame: False,
        interval=0.001,
        timeout=0.001,
    )
    await service._heartbeat_loop(connection, heartbeat)
    assert socket.closed == [(1011, "heartbeat timed out")]
