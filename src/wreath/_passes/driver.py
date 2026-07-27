"""The shift loop: one transaction per chunk, and nothing held open between them.

Two rules carry this module, and both of them are about a boundary.

**Exactly one transaction per chunk**, opened once the chunk's range is known and
committed before the next range is computed -- never one transaction for the
walk. A transaction held open for an hour holds its snapshot open for an hour,
so ``VACUUM`` reclaims nothing any other transaction updated in that hour, the
write-ahead log grows, and a hot standby inherits the same bloat or cancels
queries instead. The application does not slow down during the backfill; it
slows down for as long as the bloat takes to work back out, which is the part
that is hard to attribute afterwards. The payoff is that a partially applied
chunk cannot exist: the transaction rolls back, the cursor does not move, and
the chunk is retried from its start.

**The cursor advances inside that transaction**, together with the work. The
alternatives are both broken and asymmetrically so -- a cursor that commits after
the work re-runs a chunk on a crash, which is recoverable, and a cursor that
commits before the work *skips* one, which is an unrecorded hole the pass
reports as success. Neither is necessary here, because the ledger and the table
are in one database.

And one rule about time. A job's lease is thirty seconds by default and there is
no heartbeat, so a handler that runs for an hour is reclaimed while it is still
running, picked up by a second worker, and re-claimed again when the first
finally finishes and its fenced completion matches nothing. So a pass runs in
bounded **shifts**: a shift ends at a chunk boundary, re-enqueues itself, and is
always shorter than the lease. That also makes shutdown cheap -- a redeploy
mid-pass costs at most one chunk -- and it is what makes yielding a check between
chunks rather than a cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from time import monotonic
from typing import Any

from . import keyset
from .ledger import DONE, WALKING


class Binds:
    """Accumulates bind values so every fragment lands on the right ``$n``.

    The cursor is always a bind and never interpolated. That is the injection
    answer, and it is also what keeps the chunk SQL textually identical from the
    first chunk to the last, so the driver's prepared-statement cache holds one
    entry for the whole walk instead of one per chunk.
    """

    __slots__ = ("values",)

    def __init__(self) -> None:
        self.values: list[Any] = []

    def add(self, value: Any) -> str:
        self.values.append(value)
        return f"${len(self.values)}"

    def add_all(self, values: Any) -> list[str]:
        return [self.add(value) for value in values]

    def splice(self, text: str, values: Any = ()) -> str:
        """Bind *values* into a fragment whose placeholders are written ``?``.

        A fragment written by a caller cannot know which ``$n`` it will end up
        at, so it writes ``?`` and this renumbers -- the same reason a query
        builder never lets a caller pick placeholder numbers.
        """
        parts = text.split("?")
        supplied = tuple(values)
        if len(parts) - 1 != len(supplied):
            raise ValueError(
                f"SQL fragment has {len(parts) - 1} '?' placeholders but "
                f"{len(supplied)} values: {text!r}"
            )
        out = [parts[0]]
        for part, value in zip(parts[1:], supplied, strict=True):
            out.append(self.add(value))
            out.append(part)
        return "".join(out)

    @property
    def args(self) -> tuple[Any, ...]:
        return tuple(self.values)


@dataclass(frozen=True, slots=True)
class Chunk:
    """One chunk of a pass: a half-open range over the pass's ordered domain.

    The range is ``(cursor_from, cursor_to]`` -- open at the low end so the row
    the last chunk finished on is not seen twice, closed at the high end so the
    cursor is always a key that really exists. Row counts belong to the report,
    never to the range.
    """

    table: str
    where: str
    cursor_from: tuple[Any, ...] | None
    cursor_to: tuple[Any, ...]
    #: The model this pass walks, when it walks one, so declared work can render
    #: a model predicate. ``None`` for a table the ORM does not own.
    model: Any = None
    #: The name a model predicate qualifies its columns with.
    alias: str = ""


@dataclass(frozen=True, slots=True)
class ShiftResult:
    """What one shift did, and why it stopped."""

    chunks: int = 0
    rows: int = 0
    complete: bool = False
    #: ``complete`` | ``budget`` | ``stopping`` | ``lost`` | ``pool`` | ``failed``
    stopped: str = "complete"
    error: str | None = None

    @property
    def should_continue(self) -> bool:
        """Whether another shift is worth enqueuing straight away."""
        return self.stopped in ("budget", "pool")


def range_predicate(
    keys: tuple[keyset.Key, ...],
    binds: Binds,
    *,
    cursor: tuple[Any, ...] | None,
    frontier: str | None,
) -> str:
    """``key > $cursor AND <frontier>``, as one row comparison and the frontier.

    The frontier is carried even where the upper key bound already implies it.
    It costs the planner nothing (it is the same index range) and it means every
    statement the pass issues says out loud which rows it is allowed to touch.
    """
    parts: list[str] = []
    if cursor is not None:
        operator = keyset.after_operator(keys)
        parts.append(keyset.row_comparison(keys, operator, binds.add_all(cursor)))
    if frontier:
        parts.append(frontier)
    return " AND ".join(parts) if parts else "TRUE"


def chunk_predicate(
    keys: tuple[keyset.Key, ...],
    binds: Binds,
    *,
    cursor_from: tuple[Any, ...] | None,
    cursor_to: tuple[Any, ...],
    frontier: str | None,
) -> str:
    parts: list[str] = []
    if cursor_from is not None:
        parts.append(
            keyset.row_comparison(keys, keyset.after_operator(keys), binds.add_all(cursor_from))
        )
    parts.append(
        keyset.row_comparison(keys, keyset.upto_operator(keys), binds.add_all(cursor_to))
    )
    if frontier:
        parts.append(frontier)
    return " AND ".join(parts)


async def _fetch_key(
    executor: Any,
    *,
    table: str,
    keys: tuple[keyset.Key, ...],
    cursor: tuple[Any, ...] | None,
    frontier_sql: Any,
    offset: int | None,
    reverse: bool,
) -> tuple[Any, ...] | None:
    binds = Binds()
    frontier = frontier_sql(binds)
    where = range_predicate(keys, binds, cursor=cursor, frontier=frontier)
    order = keyset.order_clause(keys, reverse=reverse)
    projection = ", ".join(item.name for item in keys)
    sql = f"SELECT {projection} FROM {table} WHERE {where} ORDER BY {order}"
    if offset:
        sql += f" OFFSET {binds.add(int(offset))}"
    sql += " LIMIT 1"
    record = await executor.fetchrow(sql, *binds.args)
    if record is None:
        return None
    return tuple(_field(record, item.name, index) for index, item in enumerate(keys))


def _field(record: Any, name: str, index: int) -> Any:
    try:
        return record[name]
    except (KeyError, TypeError):
        return record[index]


def _statement_timeout_ms(within: float) -> int:
    return max(1, int(within * 1000))


async def run_shift(
    walk: Any,
    database: Any,
    *,
    stopping: asyncio.Event | None = None,
    budget: float | None = None,
    sleep: Any = None,
    clock: Any = monotonic,
) -> ShiftResult:
    """Run chunks until the shift budget, a stop signal, or the end of the walk.

    Failing to acquire a connection is a *pacing signal*, not a chunk failure:
    it means request traffic is using the pool, which is the pass's cue to back
    off rather than to burn a retry. Counting it would let a traffic spike
    dead-letter a run of perfectly good chunks, which is precisely backwards --
    the pass would punish itself for behaving correctly.
    """
    sleeper = asyncio.sleep if sleep is None else sleep
    deadline = None if budget is None else clock() + budget
    try:
        connection = await database.acquire(walk.workload)
    except TimeoutError:
        return ShiftResult(stopped="pool", error="timed out acquiring a connection")
    try:
        return await _shift(
            walk, connection, stopping=stopping, deadline=deadline,
            sleeper=sleeper, clock=clock,
        )
    finally:
        await database.release(walk.workload, connection)


async def _shift(
    walk: Any,
    connection: Any,
    *,
    stopping: asyncio.Event | None,
    deadline: float | None,
    sleeper: Any,
    clock: Any,
) -> ShiftResult:
    ledger = walk.ledger
    keys = walk.units.keys
    await ledger.seed(connection, chunk_limit=walk.units.limit)
    await ledger.set_pacing(
        connection, chunk_limit=walk.units.limit, reason=walk.pace.reason
    )
    row = await ledger.read(connection)
    if row is None:  # pragma: no cover - seeded immediately above
        return ShiftResult(stopped="failed", error="the ledger row could not be seeded")
    if row.phase == DONE:
        if not walk.frontier.recurring:
            return ShiftResult(complete=True)
        if not await ledger.begin_cycle(connection):
            return ShiftResult(stopped="lost")
        row = await ledger.read(connection)
        if row is None:  # pragma: no cover - the row cannot vanish under us
            return ShiftResult(stopped="failed", error="the ledger row vanished")
    if row.phase != WALKING:
        return ShiftResult(stopped="failed", error=f"pass is {row.phase}")

    cursor = keyset.decode_cursor(keys, row.cursor)
    ceiling = row.ceiling
    if ceiling is None or (walk.frontier.recurring and cursor is None):
        ceiling = await walk.frontier.derive(connection, table=walk.table, keys=keys)
        await ledger.set_ceiling(connection, ceiling=ceiling, cycle=cursor is None)

    def frontier_sql(binds: Binds) -> str:
        return walk.frontier.predicate(keys, ceiling, binds)

    chunks = 0
    rows = 0
    while True:
        if stopping is not None and stopping.is_set():
            return ShiftResult(chunks, rows, stopped="stopping")
        if deadline is not None and clock() >= deadline:
            return ShiftResult(chunks, rows, stopped="budget")

        started = clock()
        cursor_to = await _fetch_key(
            connection, table=walk.table, keys=keys, cursor=cursor,
            frontier_sql=frontier_sql, offset=walk.units.limit - 1, reverse=False,
        )
        if cursor_to is None:
            # Fewer than a full chunk remain -- which is *not* the end of the
            # walk. A chunk is short whenever rows in its range were deleted or
            # filtered out, and the next key can sit well beyond it. The honest
            # test is one more indexed probe from the far end of the range.
            cursor_to = await _fetch_key(
                connection, table=walk.table, keys=keys, cursor=cursor,
                frontier_sql=frontier_sql, offset=None, reverse=True,
            )
        if cursor_to is None:
            complete = await ledger.set_phase(connection, expected=WALKING, phase=DONE)
            return ShiftResult(chunks, rows, complete=complete, stopped="complete")

        try:
            moved, affected = await _run_chunk(
                walk, connection, keys=keys, cursor_from=cursor,
                cursor_to=cursor_to, frontier_sql=frontier_sql,
            )
        except Exception as error:  # noqa: BLE001 - a chunk failure is data, not a crash
            with contextlib.suppress(Exception):
                await ledger.record_error(connection, repr(error))
            return ShiftResult(chunks, rows, stopped="failed", error=repr(error))
        if not moved:
            # Another worker advanced this pass while we were computing a range.
            # Its transaction rolled back ours; nothing observable happened.
            return ShiftResult(chunks, rows, stopped="lost")

        chunks += 1
        rows += affected
        cursor = cursor_to
        rest = walk.pace.rest_after(clock() - started)
        if rest > 0:
            await sleeper(rest)


async def _run_chunk(
    walk: Any,
    connection: Any,
    *,
    keys: tuple[keyset.Key, ...],
    cursor_from: tuple[Any, ...] | None,
    cursor_to: tuple[Any, ...],
    frontier_sql: Any,
) -> tuple[bool, int]:
    """One chunk, in one transaction, with the swap as its first statement."""
    timeout = _statement_timeout_ms(walk.units.within)
    affected = 0
    try:
        async with connection.transaction() as tx:
            # A chunk that hits a lock wait dies as a chunk failure rather than
            # becoming the long transaction this whole design exists to avoid.
            await tx.execute(f"SET LOCAL statement_timeout = {timeout}")
            await tx.execute(f"SET LOCAL idle_in_transaction_session_timeout = {timeout}")
            moved = await walk.ledger.advance(
                tx,
                expected=(
                    None if cursor_from is None else keyset.encode_cursor(keys, cursor_from)
                ),
                cursor=keyset.encode_cursor(keys, cursor_to),
            )
            if not moved:
                # Roll the whole chunk back, work included. The loser of a swap
                # must not have done anything observable, and the only way to
                # guarantee that is to never commit.
                raise _Rollback
            binds = Binds()
            frontier = frontier_sql(binds)
            where = chunk_predicate(
                keys, binds, cursor_from=cursor_from, cursor_to=cursor_to, frontier=frontier
            )
            chunk = Chunk(
                table=walk.table, where=where, cursor_from=cursor_from,
                cursor_to=cursor_to, model=walk.model, alias=walk.alias,
            )
            affected = await walk.work.apply(tx, chunk, binds)
            await walk.ledger.count_rows(tx, affected)
    except _Rollback:
        return False, 0
    return True, affected


class _Rollback(Exception):
    """Abandon the chunk transaction after a lost compare-and-swap."""


__all__ = [
    "Binds",
    "Chunk",
    "ShiftResult",
    "chunk_predicate",
    "range_predicate",
    "run_shift",
]
