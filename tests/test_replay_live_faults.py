from __future__ import annotations

import asyncio
import datetime
import os
from typing import Any

import pytest

from wreath._replay_adapters import AdapterFault
from wreath.passes import ChunkedPass, DutyCycle, Key, Purge, Rows, Sealed, Table
from wreath.postgres import Database

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to inject faults at a real transaction seam",
)

#: The corpus names this file answers for. Cross-checked from
#: `test_replay_corpus_properties.py`, so a schedule cannot be excused from the
#: in-process property and then quietly go undriven here as well.
TRANSACTION_SCHEDULES = frozenset(
    {"adapter-begin_error", "adapter-commit_error", "adapter-statement_timeout"}
)

#: One schema per xdist worker. Six workers sharing one schema race on
#: `CREATE SCHEMA IF NOT EXISTS` and `DROP SCHEMA CASCADE`, and PostgreSQL
#: reports that as `duplicate key value violates unique constraint
#: "pg_namespace_nspname_index"` -- a catalog error nobody would read as a test
#: isolation problem. Measured: green serially, six errors under `-n 6`.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_replay_faults_{_WORKER}"
_TABLE = f'"{_SCHEMA}".rows_to_purge'


class _FaultyTransaction:
    """A real transaction scope with a modeled failure raised inside it.

    Each fault is raised at the moment the region names, and *around real SQL*:

    - `BEGIN_ERROR` raises before the real scope is entered, so no `BEGIN` is
      ever sent and no work can have run.
    - `STATEMENT_TIMEOUT` raises from inside the body, after the chunk has done
      its compare-and-swap and its work, so the real scope unwinds and
      PostgreSQL rolls both back together. This is the interesting one: the
      driver *thought* it had advanced.
    - `COMMIT_ERROR` lets the real commit land and then raises, which is the
      ambiguous case -- the write is durable and the caller cannot know it.
    """

    __slots__ = ("_scope", "_fault", "_tx", "_statements", "_owner")

    def __init__(self, scope: Any, fault: AdapterFault, owner: FaultyDatabase) -> None:
        self._scope = scope
        self._fault = fault
        self._tx: Any = None
        self._statements = 0
        self._owner = owner

    async def __aenter__(self) -> _FaultyTransaction:
        if self._fault is AdapterFault.BEGIN_ERROR:
            self._owner.fired += 1
            raise ConnectionError("could not open a transaction")
        self._tx = await self._scope.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._tx is None:
            return False
        suppressed = await self._scope.__aexit__(exc_type, exc, tb)
        if exc_type is None and self._fault is AdapterFault.COMMIT_ERROR:
            self._owner.fired += 1
            raise ConnectionError("commit failed after the work was applied")
        return bool(suppressed)

    async def execute(self, sql: object, *args: object) -> Any:
        self._statements += 1
        result = await self._tx.execute(sql, *args)
        self._maybe_timeout()
        return result

    async def fetch(self, sql: object, *args: object) -> Any:
        self._statements += 1
        result = await self._tx.fetch(sql, *args)
        self._maybe_timeout()
        return result

    async def fetchrow(self, sql: object, *args: object) -> Any:
        self._statements += 1
        result = await self._tx.fetchrow(sql, *args)
        self._maybe_timeout()
        return result

    async def fetchval(self, sql: object, *args: object) -> Any:
        self._statements += 1
        result = await self._tx.fetchval(sql, *args)
        self._maybe_timeout()
        return result

    def _maybe_timeout(self) -> None:
        # Late in the body on purpose. Firing on the first statement would kill
        # the chunk before its swap and prove only that a `BEGIN` can fail; the
        # invariant under test is what happens when the swap and the work have
        # *already run* and the transaction still does not commit.
        if self._fault is AdapterFault.STATEMENT_TIMEOUT and self._statements >= 5:
            self._owner.fired += 1
            raise ConnectionError("canceling statement due to statement timeout")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tx, name)


class _FaultyConnection:
    """A real connection whose Nth transaction scope carries a fault.

    The coordinate counts on the *database*, not on this lease: a shift takes
    one connection but the ledger's own reads and writes run on it too, and a
    fault addressed to "the Nth transaction this pass opens" must survive the
    walk taking a fresh lease on its next shift.
    """

    __slots__ = ("_inner", "_owner")

    def __init__(self, inner: Any, owner: FaultyDatabase) -> None:
        self._inner = inner
        self._owner = owner

    def transaction(self) -> Any:
        owner = self._owner
        index = owner.transactions
        owner.transactions += 1
        scope = self._inner.transaction()
        if owner.fault is None or index != owner.at:
            return scope
        return _FaultyTransaction(scope, owner.fault, owner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class FaultyDatabase:
    """A real `Database` with the transaction seam faulted, and nothing else.

    Everything reaches PostgreSQL: the ledger is real, the keyset is real, the
    rows are real. Only the moment of failure is injected. A double could not
    stand in here -- the property under test is what the *server* did with a
    rolled-back chunk, which is precisely what a double has no opinion about.
    """

    def __init__(self, inner: Database, *, fault: AdapterFault | None, at: int = 0) -> None:
        self._inner = inner
        self.fault = fault
        self.at = at
        self.acquired = 0
        self.released = 0
        #: Transaction scopes opened through this database, the coordinate the
        #: fault is keyed to.
        self.transactions = 0
        #: Times the modeled failure was actually raised. Asserted rather than
        #: assumed: the first cut of this file keyed the coordinate per lease,
        #: the walk retried the chunk on a fresh transaction, and four of the
        #: six cases passed without the fault ever firing. An injection nobody
        #: checked fired is a test with nothing in it.
        self.fired = 0

    async def acquire(self, workload: str = "read") -> Any:
        self.acquired += 1
        return _FaultyConnection(await self._inner.acquire(workload), self)

    async def release(self, workload: str, connection: Any) -> None:
        self.released += 1
        inner = getattr(connection, "_inner", connection)
        await self._inner.release(workload, inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _apply(database: Any, sql: str) -> None:
    connection = await database.acquire("write")
    try:
        for statement in (part.strip() for part in sql.split(";\n")):
            if statement:
                await connection.execute(statement)
    finally:
        await database.release("write", connection)


@pytest.fixture
async def database() -> Any:
    from wreath.passes import schema_sql

    db = Database(
        "main",
        os.environ["WREATH_TEST_POSTGRES_DSN"],
        pools={"write": {"min_size": 1, "max_size": 4}},
    )
    await db.start()
    await _apply(db, f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"')
    await _apply(db, schema_sql(_SCHEMA))
    await _apply(
        db,
        f"CREATE TABLE IF NOT EXISTS {_TABLE} (\n"
        "  key text PRIMARY KEY,\n"
        "  expires timestamptz NOT NULL\n"
        ");\n"
        f"CREATE INDEX IF NOT EXISTS purge_expires_key_idx ON {_TABLE} (expires, key)",
    )
    await _apply(db, f"TRUNCATE {_TABLE}")
    await _apply(db, f'TRUNCATE "{_SCHEMA}".passes')
    try:
        yield db
    finally:
        await _apply(db, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await db.stop()


async def _seed(database: Any, expired: int, live: int) -> None:
    """Rows the walk must delete, and rows it must not touch.

    The live rows are the control. Without them a pass that deleted the whole
    table would satisfy every count below, and "no row was skipped" would be
    provable by a `TRUNCATE`.
    """
    now = datetime.datetime.now(datetime.UTC)
    connection = await database.acquire("write")
    try:
        for index in range(expired):
            await connection.execute(
                f"INSERT INTO {_TABLE} (key, expires) VALUES ($1, $2)",
                f"e{index:05d}",
                now - datetime.timedelta(seconds=expired - index + 60),
            )
        for index in range(live):
            await connection.execute(
                f"INSERT INTO {_TABLE} (key, expires) VALUES ($1, $2)",
                f"l{index:05d}",
                now + datetime.timedelta(days=7),
            )
    finally:
        await database.release("write", connection)


def _walk() -> ChunkedPass:
    return ChunkedPass(
        "purge_expired",
        over=Table("rows_to_purge", schema=_SCHEMA),
        units=Rows(
            key=(
                Key("expires", "timestamptz", indexed=True),
                Key("key", "text", unique=True),
            ),
            limit=10,
            within="2s",
        ),
        frontier=Sealed(),
        work=Purge(),
        pace=DutyCycle(1.0),
        schema=_SCHEMA,
    )


async def _counts(database: Any) -> tuple[int, int]:
    connection = await database.acquire("write")
    try:
        expired = await connection.fetchval(f"SELECT count(*) FROM {_TABLE} WHERE key LIKE 'e%'")
        live = await connection.fetchval(f"SELECT count(*) FROM {_TABLE} WHERE key LIKE 'l%'")
    finally:
        await database.release("write", connection)
    return int(expired), int(live)


async def _ledger(database: Any) -> tuple[int, int, str]:
    """`(units_done, rows_done, phase)` straight out of the pass's own ledger.

    Read from the table rather than from a `ShiftResult`, because the result is
    what the driver *believes* and the ledger is what committed. Under an
    ambiguous commit those are exactly the two things that can disagree.
    """
    connection = await database.acquire("write")
    try:
        row = await connection.fetchrow(
            f'SELECT units_done, rows_done, phase FROM "{_SCHEMA}".passes WHERE name = $1',
            "purge_expired",
        )
    finally:
        await database.release("write", connection)
    return int(row[0]), int(row[1]), str(row[2])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "fault"),
    [
        ("adapter-begin_error", AdapterFault.BEGIN_ERROR),
        ("adapter-statement_timeout", AdapterFault.STATEMENT_TIMEOUT),
        ("adapter-commit_error", AdapterFault.COMMIT_ERROR),
    ],
)
@pytest.mark.parametrize("at", [0, 2], ids=["first-chunk", "mid-walk"])
async def test_an_interrupted_chunk_skips_nothing_and_repeats_nothing(
    database: Any, name: str, fault: AdapterFault, at: int
) -> None:
    assert name in TRANSACTION_SCHEDULES
    await _seed(database, expired=45, live=7)
    walk = _walk()
    faulted = FaultyDatabase(database, fault=fault, at=at)

    # One faulted shift. It may raise or report; either is an owned outcome, and
    # which one is the driver's business, not this invariant's. The timeout is
    # the no-hang property applied where a hang would be most expensive: a
    # transaction left open holds locks until somebody notices.
    try:
        async with asyncio.timeout(20):
            await walk.run_shift(faulted, budget=0.2, sleep=lambda _s: asyncio.sleep(0))
    except ConnectionError:
        pass
    except TimeoutError:
        pytest.fail(f"{name} at transaction {at} hung the shift rather than failing it")

    assert faulted.fired == 1, (
        f"the {name} injection never fired ({faulted.transactions} transactions "
        "opened), so everything below this line is asserting nothing"
    )

    # The arithmetic must close *while the pass is still mid-walk*: this is the
    # window in which a bad compare-and-swap is visible, and a completed walk
    # would hide it. `rows_done` is written inside the chunk transaction, so it
    # and the table are the same commit -- if they can disagree, the swap and
    # the work are not atomic and the whole design is unfounded.
    expired_left, live_left = await _counts(database)
    deleted = 45 - expired_left
    units, rows_done, _phase = await _ledger(database)
    assert live_left == 7, "a live row was purged; the frontier moved past the seal"
    assert rows_done == deleted, (
        f"{name} at transaction {at}: the ledger counted {rows_done} rows and "
        f"{deleted} are actually gone. A chunk either committed its work "
        "without its counter or counted work it rolled back."
    )
    assert units * 10 >= deleted, "more rows are gone than units were recorded"

    # Now let it finish, unfaulted. Nothing may have been skipped on the way.
    async with asyncio.timeout(30):
        result = await walk.run(database, sleep=lambda _s: asyncio.sleep(0))
    expired_left, live_left = await _counts(database)
    _units, rows_done, _phase = await _ledger(database)
    assert expired_left == 0, (
        f"{name} at transaction {at}: the walk reported "
        f"complete={result.complete} with {expired_left} expired rows still "
        "there -- the cursor advanced past a chunk that never committed"
    )
    assert live_left == 7, "a live row was purged after the fault"
    assert rows_done == 45, (
        f"the pass reports {rows_done} rows purged and exactly 45 were; a "
        "chunk was counted twice or not at all"
    )


@pytest.mark.asyncio
async def test_the_unfaulted_control_walk_finishes_cleanly(database: Any) -> None:
    await _seed(database, expired=45, live=7)
    async with asyncio.timeout(30):
        result = await _walk().run(database, sleep=lambda _s: asyncio.sleep(0))
    assert result.complete is True
    assert result.rows == 45
    assert await _counts(database) == (0, 7)


@pytest.mark.asyncio
async def test_a_faulted_chunk_still_gives_its_connection_back(database: Any) -> None:
    await _seed(database, expired=45, live=0)
    walk = _walk()
    for round_ in range(6):
        # A fresh injector each round, so the fault fires every round rather
        # than once across all six -- six shifts against one un-fired injector
        # would prove only that an unfaulted pass returns its connection.
        faulted = FaultyDatabase(database, fault=AdapterFault.STATEMENT_TIMEOUT, at=0)
        try:
            async with asyncio.timeout(20):
                await walk.run_shift(faulted, budget=0.1, sleep=lambda _s: asyncio.sleep(0))
        except ConnectionError:
            pass
        assert faulted.acquired == faulted.released, (
            f"round {round_}: a faulted chunk kept its lease; against a "
            "max_size=4 pool the pass would be parked in acquire() by now, "
            "which looks exactly like a pass with nothing to do"
        )
