from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

from _pgfidelity import check_for

from wreath.jobs import JobRunner, _doorbell_delay


@dataclass(frozen=True, slots=True)
class FakeNotification:
    channel: str
    payload: str


class FakeListenConnection:
    """A connection whose notification stream ends when it is dropped."""

    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.listening: list[str] = []
        self.closed = False
        self.released = False
        self._queue: deque[FakeNotification] = deque()
        self._event = asyncio.Event()

    async def listen(self, channel: str) -> None:
        if not self.listening:
            # Only the doorbell listens; worker/sweeper connections do not, and
            # counting those would make any assertion about reconnects a lie.
            self.database.listeners.append(self)
        self.listening.append(channel)

    async def execute(self, sql: str, *args: Any) -> str:
        check_for(self, sql, args)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        check_for(self, sql, args)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> None:
        check_for(self, sql, args)
        return None

    async def fetchval(self, sql: str, *args: Any) -> None:
        check_for(self, sql, args)
        return None

    def deliver(self) -> None:
        self._queue.append(FakeNotification(channel="c", payload=""))
        self._event.set()

    def drop(self) -> None:
        """The connection goes away underneath a running runner."""
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

    async def stop(self, runner: JobRunner | None = None) -> None:
        """Signal, drain, then cancel -- the supervisor's own shutdown order."""
        self.stopping.set()
        if runner is not None:
            await runner.drain(asyncio.get_running_loop().time() + 1.0)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class CountingEvent(asyncio.Event):
    """An `asyncio.Event` that remembers how often it was set.

    Each worker clears its own waiter every time round its loop, so the
    instantaneous state is a race. What the doorbell actually promises is that a
    notification *causes a wake*, which is an edge, not a level.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sets = 0

    def set(self) -> None:
        self.sets += 1
        super().set()


async def _until(predicate: Any, *, within: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


async def _started(database: FakeDatabase) -> tuple[JobRunner, RunningSupervisor]:
    # concurrency=1 keeps the worker noise down; the doorbell is what is at issue.
    runner = JobRunner(database, name="events", concurrency=1)
    supervisor = RunningSupervisor()
    await runner.start(supervisor)
    return runner, supervisor


async def test_a_dropped_listen_connection_comes_back() -> None:
    database = FakeDatabase()
    runner, supervisor = await _started(database)
    try:
        first = database.live
        assert first.listening == [runner._channel]

        first.drop()

        assert await _until(lambda: len(database.listeners) >= 2), (
            "the doorbell never reconnected after its connection dropped"
        )
        second = database.live
        assert second is not first
        assert second.listening == [runner._channel]
        assert first.released, "the dead connection was never given back"
    finally:
        await supervisor.stop(runner)


async def test_a_reconnect_wakes_workers_again() -> None:
    database = FakeDatabase()
    runner, supervisor = await _started(database)
    try:
        # Registered as one more waiter: the doorbell wakes every worker, so a
        # counting waiter in the set sees each edge.
        counter = CountingEvent()
        runner._waiters.add(counter)
        database.live.drop()
        assert await _until(lambda: len(database.listeners) >= 2)

        before = counter.sets
        database.live.deliver()

        assert await _until(lambda: counter.sets > before), (
            "a notification on the reconnected connection did not wake the workers"
        )
    finally:
        await supervisor.stop(runner)


async def test_the_outage_is_countable() -> None:
    database = FakeDatabase()
    runner, supervisor = await _started(database)
    try:
        assert runner.doorbell_reconnects == 0
        database.live.drop()
        assert await _until(lambda: runner.doorbell_reconnects >= 1)
    finally:
        await supervisor.stop(runner)


async def test_a_database_down_at_boot_still_gets_a_doorbell() -> None:
    database = FakeDatabase()
    database.acquire_error = ConnectionError("database is down")
    runner = JobRunner(database, name="events", concurrency=1)
    supervisor = RunningSupervisor()
    await runner.start(supervisor)
    try:
        assert await _until(lambda: runner.doorbell_reconnects >= 1), (
            "a failed startup connect was not counted as an outage"
        )
        database.acquire_error = None
        assert await _until(lambda: len(database.listeners) >= 1), (
            "the doorbell never established itself once the database came back"
        )
    finally:
        await supervisor.stop(runner)


async def test_a_clean_stop_during_backoff_does_not_hang() -> None:
    database = FakeDatabase()
    database.acquire_error = ConnectionError("database is down")
    runner = JobRunner(database, name="events", concurrency=1)
    supervisor = RunningSupervisor()
    await runner.start(supervisor)
    await _until(lambda: runner.doorbell_reconnects >= 1)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await supervisor.stop(runner)
    assert loop.time() - started < 1.0, "shutdown waited out a reconnect backoff"
    assert all(task.done() for task in supervisor.tasks)


async def test_a_clean_drain_releases_without_reconnecting() -> None:
    database = FakeDatabase()
    runner, supervisor = await _started(database)
    assert await _until(lambda: len(database.listeners) >= 1)
    before = runner.doorbell_reconnects

    await supervisor.stop(runner)

    assert runner.doorbell_reconnects == before, (
        "a clean shutdown was counted as a dropped connection"
    )
    assert len(database.listeners) == 1, "the doorbell reopened on the way out"


def test_the_reconnect_backoff_grows_and_is_bounded() -> None:
    delays = [_doorbell_delay(attempt) for attempt in range(1, 12)]
    assert delays[0] < 0.2  # a blip recovers immediately
    assert delays[3] > delays[0]  # ... and a real outage backs off
    assert max(delays) <= 6.0  # cap 5.0 + 20% jitter
    # Jittered, so a fleet of runners does not retry in lockstep.
    assert len({_doorbell_delay(9) for _ in range(20)}) > 1
