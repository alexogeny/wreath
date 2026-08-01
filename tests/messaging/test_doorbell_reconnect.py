"""The doorbell survives losing its LISTEN connection.

A bus holds one connection for the life of the process to carry ephemeral
fan-out and the durable wake-ups. Every reason a long-lived Postgres connection
ends -- a failover, an idle timeout, a `pg_terminate_backend`, a network blip --
happens to that connection eventually, and the driver's `notifications()`
iterator *ends* when it does rather than raising. An unsupervised loop therefore
returns with nothing to notice: ephemeral delivery stops for the lifetime of the
process, durable consumers silently degrade to the poll interval, and the system
keeps working, slower and lossier, with no signal at all.

These tests pin the reconnect, the counter that makes the outage countable, and
the two things a reconnect loop is most likely to break: a clean shutdown, and
telling a dropped connection apart from a bug inside a subscriber's callback.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

from wreath.messaging import Message, MessageBus, _doorbell_delay


@dataclass(frozen=True, slots=True)
class FakeNotification:
    channel: str
    payload: str


class FakeListenConnection:
    """A connection whose notification stream ends when it is dropped.

    Mirrors `wreath._pure.postgres.Connection.notifications`, whose iterator
    returns (rather than raising) once the connection closes -- the detail that
    made this failure invisible.
    """

    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.listening: list[str] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self.released = False
        self._queue: deque[FakeNotification] = deque()
        self._event = asyncio.Event()

    async def listen(self, wire: str) -> None:
        if not self.listening:
            # Only the doorbell listens; the registry's connections do not, and
            # counting those would make any assertion about reconnects a lie.
            self.database.listeners.append(self)
        self.listening.append(wire)

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.calls.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """The version-2 `trace_context` column probe, and nothing else.

        Answering `True` models a database the schema component has been applied
        to. A real `SELECT true ... WHERE` returns *no rows* when the column is
        absent, which the driver reads as `None` -- so that is the shape of the
        negative answer, not `False`.
        """
        self.calls.append((sql, args))
        return True

    def deliver(self, wire: str, payload: str) -> None:
        self._queue.append(FakeNotification(channel=wire, payload=payload))
        self._event.set()

    def drop(self) -> None:
        """The connection goes away underneath a running bus."""
        self.closed = True
        self._event.set()

    async def notifications(self) -> Any:
        while True:
            while self._queue:
                yield self._queue.popleft()
            if self.closed:
                return
            self._event.clear()
            if self._queue:
                continue
            await self._event.wait()


class FakeDatabase:
    def __init__(self) -> None:
        self.connections: list[FakeListenConnection] = []
        #: The connections the doorbell issued LISTEN on, in order.
        self.listeners: list[FakeListenConnection] = []
        self.acquires = 0
        #: When set, `acquire` raises it -- the database is unreachable.
        self.acquire_error: Exception | None = None

    async def acquire(self, workload: str) -> FakeListenConnection:
        self.acquires += 1
        if self.acquire_error is not None:
            raise self.acquire_error
        connection = FakeListenConnection(self)
        self.connections.append(connection)
        return connection

    async def release(self, workload: str, connection: FakeListenConnection) -> None:
        connection.released = True

    @property
    def live(self) -> FakeListenConnection:
        """The doorbell's current connection."""
        return self.listeners[-1]


class RunningSupervisor:
    """Spawns real tasks, and stops them the way `Supervisor` does."""

    def __init__(self) -> None:
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []

    def spawn(self, name: str, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def stop(self, bus: MessageBus | None = None) -> None:
        """Signal, drain, then cancel -- the supervisor's own shutdown order."""
        self.stopping.set()
        if bus is not None:
            await bus.drain(asyncio.get_running_loop().time() + 1.0)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def _until(predicate: Any, *, within: float = 2.0) -> bool:
    """Wait for `predicate()`; the loop is event-driven, so this is quick."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def _bus(database: FakeDatabase, **kwargs: Any) -> MessageBus:
    return MessageBus(database, name="events", **kwargs)


async def _started(database: FakeDatabase, handler: Any) -> tuple[MessageBus, RunningSupervisor]:
    bus = _bus(database)
    bus.subscribe("order_placed")(handler)
    supervisor = RunningSupervisor()
    await bus.start(supervisor)
    return bus, supervisor


# --- the finding --------------------------------------------------------------


async def test_a_dropped_listen_connection_comes_back() -> None:
    """The failure the whole file exists for: drop it, and delivery resumes.

    Before the reconnect loop, `notifications()` simply ended, `_doorbell`
    returned, and nothing ever spawned it again.
    """
    seen: list[Any] = []

    async def handler(message: Message) -> None:
        seen.append(message.payload)

    database = FakeDatabase()
    bus, supervisor = await _started(database, handler)
    wire = bus._channel_wire("order_placed")
    try:
        first = database.live
        assert first.listening == [wire]

        first.drop()

        assert await _until(lambda: len(database.listeners) == 2)
        assert await _until(lambda: database.live.listening == [wire])
        # ... and the new connection actually delivers.
        database.live.deliver(wire, '{"id": 7}')
        assert await _until(lambda: seen == [{"id": 7}])
        assert first.released is True
    finally:
        await supervisor.stop(bus)


async def test_losing_the_connection_is_counted() -> None:
    """`unrouted_publishes` is the precedent: degradation stays countable."""

    async def handler(message: Message) -> None:
        pass

    database = FakeDatabase()
    bus, supervisor = await _started(database, handler)
    try:
        assert bus.doorbell_reconnects == 0
        database.live.drop()
        assert await _until(lambda: bus.doorbell_reconnects == 1)
    finally:
        await supervisor.stop(bus)


async def test_a_database_that_is_down_at_startup_still_gets_a_doorbell() -> None:
    """Startup used to swallow the failure and never spawn the loop at all."""
    seen: list[Any] = []

    async def handler(message: Message) -> None:
        seen.append(message.payload)

    database = FakeDatabase()
    database.acquire_error = RuntimeError("the database is down")
    bus = _bus(database)
    bus.subscribe("order_placed")(handler)
    supervisor = RunningSupervisor()

    await bus.start(supervisor)                    # must not raise
    try:
        database.acquire_error = None              # the database comes back
        assert await _until(lambda: database.listeners != []), (
            "the bus never opened a doorbell once the database returned"
        )
        wire = bus._channel_wire("order_placed")
        assert await _until(lambda: database.live.listening == [wire])
        database.live.deliver(wire, '{"id": 1}')
        assert await _until(lambda: seen == [{"id": 1}])
        assert bus.doorbell_reconnects >= 1         # the outage was countable
    finally:
        await supervisor.stop(bus)


# --- backoff ------------------------------------------------------------------


def test_the_reconnect_backoff_grows_and_is_bounded() -> None:
    """A database that is genuinely down must not become a reconnect storm."""
    delays = [_doorbell_delay(attempt) for attempt in range(1, 12)]
    assert delays[0] < 0.2                          # a blip recovers immediately
    assert delays[3] > delays[0]                    # ... and a real outage backs off
    assert max(delays) <= 6.0                       # cap 5.0 + 20% jitter
    # Jittered, so a fleet of buses does not retry in lockstep.
    assert len({_doorbell_delay(9) for _ in range(20)}) > 1


async def test_a_flapping_connection_backs_off_too() -> None:
    """A connection accepted and killed at once must not reset the backoff.

    Otherwise the loop retries at the base delay forever against a database in
    exactly the state that least wants the traffic.
    """

    async def handler(message: Message) -> None:
        pass

    database = FakeDatabase()
    bus = _bus(database)
    bus.subscribe("order_placed")(handler)
    supervisor = RunningSupervisor()

    original_listen = FakeListenConnection.listen

    async def listen_then_die(self: FakeListenConnection, wire: str) -> None:
        await original_listen(self, wire)
        self.drop()

    FakeListenConnection.listen = listen_then_die       # type: ignore[method-assign]
    try:
        await bus.start(supervisor)
        await asyncio.sleep(0.4)
        # Resetting on every reopen would be ~8 in that window; growing is ~4.
        assert len(database.listeners) <= 6
        assert bus.doorbell_reconnects >= 2
    finally:
        FakeListenConnection.listen = original_listen   # type: ignore[method-assign]
        await supervisor.stop(bus)


async def test_a_persistent_outage_does_not_spin() -> None:
    """Attempts are spaced by the backoff, not issued as fast as the loop runs."""

    async def handler(message: Message) -> None:
        pass

    database = FakeDatabase()
    database.acquire_error = RuntimeError("down")
    bus = _bus(database)
    bus.subscribe("order_placed")(handler)
    supervisor = RunningSupervisor()
    await bus.start(supervisor)
    try:
        await asyncio.sleep(0.35)
        # Unbounded retries would be thousands in a third of a second.
        assert database.acquires < 20
        assert bus.doorbell_reconnects >= 1
    finally:
        await supervisor.stop(bus)


# --- shutdown -----------------------------------------------------------------


async def test_stopping_during_a_reconnect_backoff_finishes_promptly() -> None:
    """The loop must not fight cancellation, nor hold the process open."""

    async def handler(message: Message) -> None:
        pass

    database = FakeDatabase()
    database.acquire_error = RuntimeError("down")
    bus = _bus(database)
    bus.subscribe("order_placed")(handler)
    supervisor = RunningSupervisor()
    await bus.start(supervisor)
    # Let it reach a backoff sleep, then stop cooperatively -- no cancellation.
    assert await _until(lambda: bus.doorbell_reconnects >= 2)
    supervisor.stopping.set()
    await bus.drain(asyncio.get_running_loop().time() + 1.0)
    done = await asyncio.wait(supervisor.tasks, timeout=1.0)
    pending = done[1]
    assert pending == set(), "a stopped bus left a task sleeping out its backoff"
    for task in supervisor.tasks:
        assert task.exception() is None


async def test_a_clean_stop_releases_the_connection_without_reconnecting() -> None:
    """Draining closes the doorbell; that is not an outage."""

    async def handler(message: Message) -> None:
        pass

    database = FakeDatabase()
    bus, supervisor = await _started(database, handler)
    connection = database.live

    supervisor.stopping.set()
    await bus.drain(asyncio.get_running_loop().time() + 1.0)
    connection.drop()                                # as a real close would
    await asyncio.wait(supervisor.tasks, timeout=1.0)

    assert connection.released is True
    assert bus._listen_conn is None
    assert bus.doorbell_reconnects == 0
    assert len(database.listeners) == 1              # it did not reopen one
    for task in supervisor.tasks:
        assert task.done() and task.exception() is None


# --- user code is not a dropped connection ------------------------------------


async def test_a_raising_subscriber_is_not_mistaken_for_a_connection_failure() -> None:
    """A bug in a handler must not read as a flapping database."""
    seen: list[Any] = []

    async def handler(message: Message) -> None:
        seen.append(message.payload)
        raise RuntimeError("a bug in user code")

    database = FakeDatabase()
    bus, supervisor = await _started(database, handler)
    wire = bus._channel_wire("order_placed")
    try:
        database.live.deliver(wire, '{"id": 1}')
        assert await _until(lambda: bus.handler_errors == 1)
        # The same connection keeps delivering...
        database.live.deliver(wire, '{"id": 2}')
        assert await _until(lambda: seen == [{"id": 1}, {"id": 2}])
        # ... and nothing was recorded as an outage.
        assert bus.doorbell_reconnects == 0
        assert len(database.listeners) == 1
    finally:
        await supervisor.stop(bus)


async def test_an_undecodable_payload_is_delivered_as_none_not_a_disconnect() -> None:
    """Only a JSON failure is suppressed there, and it does not end the loop."""
    seen: list[Any] = []

    async def handler(message: Message) -> None:
        seen.append(message.payload)

    database = FakeDatabase()
    bus, supervisor = await _started(database, handler)
    wire = bus._channel_wire("order_placed")
    try:
        database.live.deliver(wire, "not json at all")
        database.live.deliver(wire, '{"id": 2}')
        assert await _until(lambda: seen == [None, {"id": 2}])
        assert bus.doorbell_reconnects == 0
    finally:
        await supervisor.stop(bus)


async def test_a_durable_wakeup_still_wakes_consumers_after_a_reconnect() -> None:
    """The doorbell's other job: a NOTIFY on any channel sets the wake event."""

    async def handler(message: Message) -> None:
        pass

    class RecordingEvent(asyncio.Event):
        """A durable consumer clears the wake event as soon as it parks again,
        so count the wakes rather than sampling the flag."""

        sets = 0

        def set(self) -> None:
            type(self).sets += 1
            super().set()

    database = FakeDatabase()
    bus = _bus(database)
    bus.subscribe("order_placed", group="billing", durable=True)(handler)
    # Registered as one more waiter: the doorbell wakes every parked consumer,
    # so a recording waiter in the list observes each edge.
    bus._waiters.append(RecordingEvent())
    supervisor = RunningSupervisor()
    await bus.start(supervisor)
    try:
        database.live.drop()
        assert await _until(lambda: len(database.listeners) == 2)
        before = RecordingEvent.sets
        database.live.deliver(bus._channel_wire("order_placed"), "")
        assert await _until(lambda: RecordingEvent.sets > before)
    finally:
        await supervisor.stop(bus)
