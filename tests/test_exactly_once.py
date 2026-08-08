"""Exactly-once from the client's retry all the way to the message bus.

Three separate at-least-once mechanisms, composed into one guarantee:

* the client retries a POST, and `IdempotencyPolicy` replays the response
  instead of re-running the handler;
* the handler enqueues a durable job with the same key, and a unique index
  makes the enqueue happen once however many times the handler runs;
* the job and the outbound message are written in the *same transaction* as
  the business row, so all three commit or none do.

The subtlety these tests exist to pin is that the middleware is a **fast path,
not the guarantee**. Its store is in-process, so a retry that lands on another
worker re-runs the handler -- and the effect must still happen once, because
the database is what is actually enforcing it. A guarantee that quietly depends
on sticky sessions is not a guarantee.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath.jobs import JobRunner
from wreath.messaging import MessageBus
from wreath.policy import IdempotencyPolicy
from wreath.request import Request
from wreath.response import Response

pytestmark = pytest.mark.asyncio


class FakeTransaction:
    """One transaction: records its statements and whether it committed."""

    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, tuple]] = []
        self.committed = False

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.database.next_id()

    async def __aenter__(self) -> FakeTransaction:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.committed = exc_type is None
        if self.committed:
            self.database.committed.extend(self.calls)
        return False


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self) -> FakeTransaction:
        tx = FakeTransaction(self.database)
        self.database.transactions.append(tx)
        return tx

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.database.next_id()

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []


class FakeDatabase:
    """A fake with the one behaviour that matters: a unique index on the key."""

    def __init__(self) -> None:
        self.connection = FakeConnection(self)
        self.transactions: list[FakeTransaction] = []
        self.committed: list[tuple[str, tuple]] = []
        #: dedup key -> the id the winning INSERT got.
        self.rows: dict[Any, int] = {}
        self._next = 100

    def next_id(self) -> Any:
        """Model `ON CONFLICT (queue, dedup_key) DO NOTHING RETURNING id`."""
        return self._next

    async def acquire(self, workload):
        return self.connection

    async def release(self, workload, connection):
        return None


class DedupingDatabase(FakeDatabase):
    """Enforces the unique index: a repeated dedup key inserts nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.inserts = 0
        self.pending_key: Any = None

    async def acquire(self, workload):
        return _DedupConnection(self)

    async def release(self, workload, connection):
        return None


class _DedupConnection(FakeConnection):
    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        database: DedupingDatabase = self.database  # type: ignore[assignment]
        if "INSERT INTO" in sql:
            # Index 6 in both statement forms the runner builds -- the one that
            # carries a trace context and the one that does not. Indexed from
            # the front deliberately: a new parameter is appended, and reading
            # from the end made adding one look like a dedup failure.
            key = args[6]
            if key in database.rows:
                return None                    # the unique index dropped it
            database.inserts += 1
            database._next += 1
            database.rows[key] = database._next
            return database._next
        if "SELECT id" in sql:
            return database.rows.get(args[-1])
        return None


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(key: str = "checkout-7", path: str = "/orders") -> Request:
    from wreath._auth.models import Identity

    scope = {
        "type": "http", "method": "POST", "path": path,
        "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"host", b"x"), (b"idempotency-key", key.encode())],
    }
    request = Request(scope, _receive)
    # Idempotency is scoped by the authenticated principal and skipped without
    # one, so a checkout that expects replay is a checkout by someone.
    request._set_identity(Identity(id="alice", roles=frozenset()))
    return request


def _runner(database: Any) -> JobRunner:
    runner = JobRunner(database, name="work")

    @runner.task("send_receipt")
    async def send_receipt(ctx, order_id):
        pass

    return runner


# --- link 1: the client's retry ----------------------------------------------


async def test_a_retry_on_the_same_worker_never_reaches_the_handler() -> None:
    middleware = IdempotencyPolicy()
    runs = 0

    async def handle(request: Request) -> Response:
        nonlocal runs
        runs += 1
        return Response(b'{"order":7}', status=201)

    first = _request()
    assert await middleware.action(first) is None
    await middleware.after(first, await handle(first))

    second = _request()
    replay = await middleware.action(second)

    assert runs == 1
    assert replay.status == 201 and replay.body == b'{"order":7}'
    assert (b"idempotency-replayed", b"true") in replay.headers


async def test_a_failed_write_stays_retryable() -> None:
    """Replaying a 500 would strand the client on an error that was transient."""
    middleware = IdempotencyPolicy()
    first = _request()
    await middleware.action(first)
    await middleware.after(first, Response(b"boom", status=500))
    assert await middleware.action(_request()) is None


# --- link 2: the database is the guarantee, not the middleware ----------------


async def test_a_retry_on_another_worker_still_enqueues_once() -> None:
    """The hole a sticky-session assumption hides, and why the key goes to SQL.

    Worker B has never seen this key, so its middleware lets the handler run
    again. The effect happens once anyway -- the unique index on the jobs table
    is what is enforcing it, and that is shared by construction.
    """
    database = DedupingDatabase()
    runner = _runner(database)

    async def handler() -> str:
        handle = await runner.launch("send_receipt", 7, key="checkout-7")
        return handle.task_id

    worker_a, worker_b = IdempotencyPolicy(), IdempotencyPolicy()

    request_a = _request()
    assert await worker_a.action(request_a) is None
    first_id = await handler()
    await worker_a.after(request_a, Response(first_id.encode(), status=201))

    request_b = _request()
    assert await worker_b.action(request_b) is None      # B has no memory of it
    second_id = await handler()                          # ... so it runs again

    assert database.inserts == 1                         # but the job exists once
    assert second_id == first_id                         # and the answer matches


async def test_the_response_is_the_same_across_workers() -> None:
    """Not just "no duplicate" -- the client must not see two different orders."""
    database = DedupingDatabase()
    runner = _runner(database)

    async def handler() -> Response:
        handle = await runner.launch("send_receipt", 7, key="checkout-7")
        return Response(handle.task_id.encode(), status=201)

    first = await handler()
    second = await handler()
    assert first.body == second.body


# --- link 3: the outbox -------------------------------------------------------


async def test_the_write_the_job_and_the_message_share_one_transaction() -> None:
    """Either the order exists with its receipt job and its event, or none do."""
    database = FakeDatabase()
    runner = _runner(database)
    bus = MessageBus(database, name="events")

    @bus.subscribe("order_placed", group="billing", durable=True)
    async def to_billing(message):
        pass

    connection = await database.acquire("write")
    async with connection.transaction() as tx:
        await tx.execute("INSERT INTO orders (id) VALUES ($1)", 7)
        await runner.enqueue("send_receipt", 7, tx=tx, key="checkout-7")
        await bus.publish("order_placed", {"id": 7}, tx=tx, durable=True)

    committed = [sql for sql, _ in database.committed]
    assert any("orders" in sql for sql in committed)
    assert any(".jobs" in sql for sql in committed)
    assert any(".messages" in sql for sql in committed)
    # And nothing went out on its own connection behind the transaction's back.
    assert database.connection.calls == []


async def test_a_rolled_back_write_leaves_no_job_and_no_message() -> None:
    database = FakeDatabase()
    runner = _runner(database)
    bus = MessageBus(database, name="events")

    @bus.subscribe("order_placed", group="billing", durable=True)
    async def to_billing(message):
        pass

    connection = await database.acquire("write")
    with pytest.raises(RuntimeError, match="deliberate"):
        async with connection.transaction() as tx:
            await tx.execute("INSERT INTO orders (id) VALUES ($1)", 7)
            await runner.enqueue("send_receipt", 7, tx=tx, key="checkout-7")
            await bus.publish("order_placed", {"id": 7}, tx=tx, durable=True)
            raise RuntimeError("deliberate")

    assert database.committed == []
    assert database.transactions[0].committed is False


# --- link 4: the job itself runs at least once --------------------------------


async def test_a_job_handler_can_guard_its_own_side_effect_with_the_key() -> None:
    """Enqueue is exactly-once; *execution* is at-least-once. The key spans both.

    A lease that expires mid-handler means the job is claimed again, so the
    side effect has to be guarded by something durable. `ctx.key` is the same
    string the client sent, which is what makes the guarantee end to end rather
    than three separate ones that happen to line up.
    """
    from wreath.jobs import _Claimed

    database = FakeDatabase()
    runner = _runner(database)
    charged: set[str] = set()

    @runner.task("charge")
    async def charge(ctx, amount):
        if ctx.key in charged:
            return                       # already done on the previous attempt
        charged.add(ctx.key)

    claim = _Claimed(
        id=1, task="charge", args=[100], tenant="", attempts=0,
        max_attempts=3, fence=1, key="checkout-7",
    )
    await runner._run(claim)
    await runner._run(claim)             # a redelivery after a lease expiry

    assert charged == {"checkout-7"}


async def test_the_clients_key_determines_the_job_row() -> None:
    """The header carries straight through to what the unique index sees.

    Not verbatim -- `dedup_key` namespaces it by queue and bounds its length,
    so one client's key cannot collide with another queue's. What matters is
    that the mapping is deterministic: the same header always lands on the same
    row, and a different header never does.
    """
    database = DedupingDatabase()
    runner = _runner(database)

    await runner.launch(
        "send_receipt", 7, key=_request(key="checkout-7").header("idempotency-key")
    )
    await runner.launch(
        "send_receipt", 7, key=_request(key="checkout-7").header("idempotency-key")
    )
    await runner.launch(
        "send_receipt", 8, key=_request(key="checkout-8").header("idempotency-key")
    )

    assert database.inserts == 2          # two distinct keys, not three requests


async def test_two_queues_do_not_collide_on_one_clients_key() -> None:
    """A key is scoped to its queue, so `checkout-7` means one thing per queue."""
    from wreath._jobcore import dedup_key

    assert dedup_key("receipts", "checkout-7") != dedup_key("audit", "checkout-7")
    assert dedup_key("receipts", "checkout-7") == dedup_key("receipts", "checkout-7")
