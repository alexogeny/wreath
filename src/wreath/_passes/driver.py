"""The shift loop: one transaction per chunk, and nothing held open between them.

Two rules carry this module, and both of them are about a boundary.

**Exactly one transaction per chunk**, opened once the chunk's range is known and
committed before the next range is computed -- never one transaction for the
walk. A transaction held open for an hour holds its snapshot open for an hour,
so `VACUUM` reclaims nothing any other transaction updated in that hour, the
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

A chunk that keeps failing becomes a **hole**: a row in the dead-letter table
carrying its range, its attempt count, and the predicate that reproduces it. What
happens next is declared, because no default suits both callers -- `halt` stops
the pass where it is, and `skip` moves past and bars the terminal gate until
the hole is cleared. Skipping buys throughput; it never buys the irreversible
step.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from dataclasses import dataclass
from time import monotonic
from typing import Any

from .. import _nplusone
from .. import telemetry as _telemetry
from .._jobcore import compute_backoff
from . import keyset
from .ledger import APPLYING, BLOCKED, DONE, UNVERIFIED, VERIFIED, VERIFYING, WALKING

#: Backoff between attempts at one chunk, doubling and capped. Short, because
#: these retries happen inside a shift whose whole budget is seconds.
RETRY_BASE_SECONDS = 0.05
RETRY_CAP_SECONDS = 2.0


def _driving_trace() -> str | None:
    """The traceparent of whatever is driving this shift, or None.

    The traceparent only, for the reason `wreath.jobs.JobRunner.enqueue` gives
    and which applies with more force to a pass: `tracestate` is vendor routing
    for the *next hop of a live call*, and a backfill's second day is not a hop
    of anything. It would age in the ledger exactly as it would age in the
    queue.
    """
    bound = _telemetry.outbound_context.get()
    return bound[0] if bound else None


class Binds:
    """Accumulates bind values so every fragment lands on the right `$n`.

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
        """Bind *values* into a fragment whose placeholders are written `?`.

        A fragment written by a caller cannot know which `$n` it will end up
        at, so it writes `?` and this renumbers -- the same reason a query
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

    The range is `(cursor_from, cursor_to]` -- open at the low end so the row
    the last chunk finished on is not seen twice, closed at the high end so the
    cursor is always a key that really exists. Row counts belong to the report,
    never to the range.
    """

    table: str
    where: str
    cursor_from: tuple[Any, ...] | None
    cursor_to: tuple[Any, ...]
    #: The model this pass walks, when it walks one, so declared work can render
    #: a model predicate. `None` for a table the ORM does not own.
    model: Any = None
    #: The name a model predicate qualifies its columns with.
    alias: str = ""


@dataclass(frozen=True, slots=True)
class ShiftResult:
    """What one shift did, and why it stopped."""

    chunks: int = 0
    rows: int = 0
    complete: bool = False
    #: `complete` | `budget` | `stopping` | `lost` | `pool` | `failed`
    #: | `blocked`
    stopped: str = "complete"
    error: str | None = None
    #: Chunks given up on during this shift and written to the dead-letter table.
    holes: int = 0

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
    """`key > $cursor AND <frontier>`, as one row comparison and the frontier.

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
    parts.append(keyset.row_comparison(keys, keyset.upto_operator(keys), binds.add_all(cursor_to)))
    if frontier:
        parts.append(frontier)
    return " AND ".join(parts)


def reproduce_predicate(
    keys: tuple[keyset.Key, ...],
    *,
    table: str,
    cursor_from: tuple[Any, ...] | None,
    cursor_to: tuple[Any, ...],
) -> str:
    """A statement an operator can paste into `psql` to see the real error.

    This is what turns a hole into a task. A dead-letter row holding a truncated
    `repr` from three weeks ago tells nobody what to do; one holding the exact
    range, with the values inlined, can be run by hand in a transaction and
    rolled back.
    """
    parts: list[str] = []
    if cursor_from is not None:
        parts.append(
            keyset.row_comparison(
                keys, keyset.after_operator(keys), [literal(value) for value in cursor_from]
            )
        )
    parts.append(
        keyset.row_comparison(
            keys, keyset.upto_operator(keys), [literal(value) for value in cursor_to]
        )
    )
    return f"SELECT * FROM {table} WHERE {' AND '.join(parts)}"


def literal(value: Any) -> str:
    """One key value as a SQL literal, for a human to run rather than to bind."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, uuid.UUID):
        return f"'{value}'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "'\\x" + bytes(value).hex() + "'::bytea"
    return "'" + str(value).replace("'", "''") + "'"


async def fetch_key(
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
    except KeyError, TypeError:
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
        with _nplusone.scope_for(
            _nplusone.Origin(kind="shift", label=walk.name),
            budget=getattr(walk, "query_budget", None),
        ):
            return await _shift(
                walk,
                connection,
                stopping=stopping,
                deadline=deadline,
                sleeper=sleeper,
                clock=clock,
            )
    finally:
        await database.release(walk.workload, connection)


async def _measure(walk: Any, connection: Any, row: Any, keys: Any) -> None:
    """Give the ledger a denominator and its provenance, once per cycle.

    Measured here rather than at declaration time because it needs a database,
    and re-measured for a recurring pass because the table it is a fraction of
    has moved on since the last cycle.
    """
    ledger = walk.ledger
    if row.denominator_kind != walk.progress.kind or row.denominator is None:
        total = await walk.progress.measure(connection, table=walk.table, keys=keys)
        await ledger.set_denominator(connection, total=total, kind=walk.progress.kind)
    if walk.progress.kind == "keyspace" and row.keyspace_from is None:
        order = keyset.order_clause(keys[:1])
        record = await connection.fetchrow(
            f"SELECT {keys[0].name} FROM {walk.table} ORDER BY {order} LIMIT 1"
        )
        floor = (
            None
            if record is None
            else keyset.encode_cursor(keys[:1], (_field(record, keys[0].name, 0),))
        )
        await ledger.set_keyspace_floor(connection, floor=floor)


async def _shift(
    walk: Any,
    connection: Any,
    *,
    stopping: asyncio.Event | None,
    deadline: float | None,
    sleeper: Any,
    clock: Any,
) -> ShiftResult:
    """Record and adopt the pass's trace, then walk under it.

    The binding wraps the whole of `_shift_bound`, so the chunk work, the
    dead-letter writes, the terminal gate and its irreversible step all run
    under one context: a hole written on day three of a backfill names the
    drive that started it, and anything a chunk calls out to joins the same
    trace with the caller doing nothing.

    The ledger's context wins over the driving shift's, and a pass that has
    never been driven with a trace binds `None` rather than leaving the ambient
    value in place. Not binding is the defect here, not the cost: a worker
    running one shift of pass A and then one of pass B would otherwise hand B
    A's trace, and a trace naming the wrong cause is worse than one naming
    none. `_driving_trace` is read *before* the bind for the same reason -- once
    the ledger's value is bound it is what the ambient read returns, so a cycle
    rollover would re-capture the value it was about to replace.
    """
    ledger = walk.ledger
    driving = _driving_trace()
    await ledger.seed(
        connection,
        chunk_limit=walk.units.limit,
        guards=walk.guards,
        rewrites=walk.rewrites,
        trace=driving,
    )
    await ledger.set_pacing(connection, chunk_limit=walk.units.limit, reason=walk.pace.reason)
    row = await ledger.read(connection)
    if row is None:  # pragma: no cover - seeded immediately above
        return ShiftResult(stopped="failed", error="the ledger row could not be seeded")
    # A finished pass that has had work requeued into it runs that work and
    # nothing else. This is how a hole gets cleared after the walk is over --
    # and a skipped chunk always reaches `done` with its hole still barring the
    # terminal gate, so without this the gate could never be un-barred.
    finished_but_requeued = row.phase == DONE and not walk.frontier.recurring and bool(row.pending)
    if row.phase == DONE and not finished_but_requeued:
        if not walk.frontier.recurring:
            return ShiftResult(complete=True)
        # Resolved *before* the bind rather than inside it: a rollover replaces
        # the row's traceparent, so binding first would run the new cycle under
        # the old one's trace -- which is the very thing the cycle boundary
        # exists to prevent. Nothing here runs the caller's code, so there is no
        # work that would be missing its context.
        if not await ledger.begin_cycle(connection, trace=driving):
            return ShiftResult(stopped="lost")
        row = await ledger.read(connection)
        if row is None:  # pragma: no cover - the row cannot vanish under us
            return ShiftResult(stopped="failed", error="the ledger row vanished")
    parent = row.trace_context
    token = _telemetry.outbound_context.set((parent, "") if parent else None)
    try:
        return await _shift_bound(
            walk,
            connection,
            row=row,
            finished_but_requeued=finished_but_requeued,
            stopping=stopping,
            deadline=deadline,
            sleeper=sleeper,
            clock=clock,
        )
    finally:
        _telemetry.outbound_context.reset(token)


async def _shift_bound(
    walk: Any,
    connection: Any,
    *,
    row: Any,
    finished_but_requeued: bool,
    stopping: asyncio.Event | None,
    deadline: float | None,
    sleeper: Any,
    clock: Any,
) -> ShiftResult:
    ledger = walk.ledger
    keys = walk.units.keys
    if row.phase in (VERIFYING, VERIFIED, APPLYING):
        # The walk finished and the gate is mid-sequence. Re-entering it is
        # safe: verification is idempotent, and every transition is a CAS.
        return await _run_gate(walk, connection, row=row)
    if row.phase != WALKING and not finished_but_requeued:  # pragma: no cover
        return ShiftResult(stopped="blocked", error=f"pass is {row.phase}")

    cursor = keyset.decode_cursor(keys, row.cursor)
    ceiling = row.ceiling
    if ceiling is None or (walk.frontier.recurring and cursor is None):
        ceiling = await walk.frontier.derive(connection, table=walk.table, keys=keys)
        await ledger.set_ceiling(connection, ceiling=ceiling, cycle=cursor is None)
        row = await ledger.read(connection) or row
    await _measure(walk, connection, row, keys)

    def frontier_sql(binds: Binds) -> str:
        return walk.frontier.predicate(keys, ceiling, binds)

    chunks = 0
    rows = 0
    holes = 0
    while True:
        if stopping is not None and stopping.is_set():
            return ShiftResult(chunks, rows, stopped="stopping", holes=holes)
        if deadline is not None and clock() >= deadline:
            return ShiftResult(chunks, rows, stopped="budget", holes=holes)

        started = clock()

        # A requeued unit comes first: it is work the walk has already gone past,
        # so leaving it behind the cursor would mean never doing it.
        unit = await ledger.claim_pending(connection)
        if unit is not None:
            outcome = await _attempt(
                walk,
                connection,
                keys=keys,
                cursor_from=keyset.decode_cursor(keys, unit.get("from")),
                cursor_to=keyset.decode_cursor(keys, unit.get("to")),
                expected=None,
                holes_open=True,
                frontier_sql=frontier_sql,
                sleeper=sleeper,
                pending=unit,
            )
            if outcome.failed:
                holes += 1
            else:
                chunks += 1
                rows += outcome.rows
            rest = walk.pace.rest_after(clock() - started)
            if rest > 0:
                await sleeper(rest)
            continue

        if finished_but_requeued:
            # The requeued work is done and the walk itself already was. Do not
            # fall through and re-derive a range: the pass finished, and this
            # shift existed only to clear what was queued into it.
            return ShiftResult(chunks, rows, complete=True, stopped="complete", holes=holes)

        # Where the next range comes from is the one structural difference
        # between range sources, and it is the only thing the loop asks them.
        # `Rows` probes the table; `Buckets` does calendar arithmetic and never
        # asks it at all.
        span = await walk.units.next_range(
            connection,
            walk=walk,
            cursor=cursor,
            ceiling=ceiling,
            frontier_sql=frontier_sql,
        )
        if span is None:
            return await _finish(walk, connection, chunks=chunks, rows=rows, holes=holes, row=row)
        range_from, cursor_to = span

        outcome = await _attempt(
            walk,
            connection,
            keys=keys,
            cursor_from=range_from,
            cursor_to=cursor_to,
            expected=cursor,
            holes_open=bool(row.holes_open),
            frontier_sql=frontier_sql,
            sleeper=sleeper,
            pending=None,
        )
        if outcome.lost:
            # Another worker advanced this pass while we were computing a range.
            # Its transaction rolled back ours; nothing observable happened.
            return ShiftResult(chunks, rows, stopped="lost", holes=holes)
        if outcome.blocked:
            return ShiftResult(
                chunks, rows, stopped="blocked", error=outcome.error, holes=holes + 1
            )
        if outcome.failed:
            holes += 1
        else:
            chunks += 1
            rows += outcome.rows
            if walk.gate is not None and walk.gate.scope == "unit":
                # A per-unit gate verifies the range the walk has just passed,
                # so one bad bucket cannot freeze the ladder behind it. Its
                # exclusivity is the chunk's own compare-and-swap: only one
                # worker owned this range.
                verdict = await _gate_unit(
                    walk, connection, cursor_from=range_from, cursor_to=cursor_to
                )
                if verdict is not None:
                    return ShiftResult(chunks, rows, stopped="blocked", error=verdict, holes=holes)
        cursor = cursor_to
        rest = walk.pace.rest_after(clock() - started)
        if rest > 0:
            await sleeper(rest)


@dataclass(frozen=True, slots=True)
class _Outcome:
    rows: int = 0
    lost: bool = False
    failed: bool = False
    blocked: bool = False
    error: str | None = None


async def _attempt(
    walk: Any,
    connection: Any,
    *,
    keys: tuple[keyset.Key, ...],
    cursor_from: tuple[Any, ...] | None,
    cursor_to: Any,
    expected: tuple[Any, ...] | None,
    holes_open: bool,
    frontier_sql: Any,
    sleeper: Any,
    pending: Any,
) -> _Outcome:
    """One chunk, with its retries, and whatever the failure policy says next.

    *cursor_from* is the range's lower bound as the predicate states it, and
    *expected* is what the ledger's compare-and-swap must find. They are the
    same value for a keyset walk and different for a bucketed one, whose first
    chunk starts at an anchor the ledger has never held.
    """
    ledger = walk.ledger
    attempts = 0
    last_error = ""
    while attempts < walk.chunk_retries:
        attempts += 1
        try:
            moved, affected = await _run_chunk(
                walk,
                connection,
                keys=keys,
                cursor_from=cursor_from,
                cursor_to=cursor_to,
                expected=expected,
                holes_open=holes_open,
                frontier_sql=frontier_sql,
                pending=pending,
            )
        except Exception as error:  # noqa: BLE001 - a chunk failure is data, not a crash
            last_error = repr(error)
            if attempts < walk.chunk_retries:
                await sleeper(
                    compute_backoff(
                        attempts,
                        base=RETRY_BASE_SECONDS,
                        cap=RETRY_CAP_SECONDS,
                    )
                )
            continue
        if not moved:
            return _Outcome(lost=True)
        return _Outcome(rows=affected)

    # Out of attempts. Record where it stopped and what would reproduce it,
    # then do what the declaration said to do about it.
    predicate = walk.units.reproduce(table=walk.table, cursor_from=cursor_from, cursor_to=cursor_to)
    # These three ledger writes are not best-effort bookkeeping: `gate_barred`
    # is `holes_open > 0`, so the hole *is* the fact the terminal gate reads.
    # Suppressing a failure here left no hole, an unbarred gate, and a `skip`
    # that had bought exactly the irreversible step §11.2 says it never can.
    # Letting it propagate is safe -- the cursor has not moved, and every write
    # below is idempotent, so the runner's retry re-runs them cleanly.
    await ledger.record_hole(
        connection,
        cursor_from=None if cursor_from is None else keyset.encode_cursor(keys, cursor_from),
        cursor_to=keyset.encode_cursor(keys, cursor_to),
        attempts=attempts,
        error=last_error,
        predicate=predicate,
    )
    if pending is not None:
        # A requeued unit that still fails goes back to being a hole and stops
        # being pending, or the shift would take it again immediately forever.
        await ledger.drop_pending(
            connection, cursor_from=pending.get("from"), cursor_to=pending.get("to")
        )
        return _Outcome(failed=True, error=last_error)

    if walk.on_chunk_failure == "skip":
        # Throughput, and nothing else. The gate stays barred while the hole is
        # in the table, so skipping can never buy the irreversible step.
        moved = await ledger.skip_to(
            connection,
            expected=None if expected is None else keyset.encode_cursor(keys, expected),
            cursor=keyset.encode_cursor(keys, cursor_to),
        )
        if not moved:
            return _Outcome(lost=True)
        return _Outcome(failed=True, error=last_error)

    await ledger.block(connection, error=last_error)
    return _Outcome(blocked=True, error=last_error)


async def _run_chunk(
    walk: Any,
    connection: Any,
    *,
    keys: tuple[keyset.Key, ...],
    cursor_from: tuple[Any, ...] | None,
    cursor_to: tuple[Any, ...],
    expected: tuple[Any, ...] | None,
    holes_open: bool,
    frontier_sql: Any,
    pending: Any = None,
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
            if pending is None:
                moved = await walk.ledger.advance(
                    tx,
                    expected=(None if expected is None else keyset.encode_cursor(keys, expected)),
                    cursor=keyset.encode_cursor(keys, cursor_to),
                )
            else:
                # A requeued unit does not move the cursor -- it is behind it --
                # so its exclusivity comes from the claim instead, which locks
                # the same row for the same reason and is likewise first.
                moved = True
            if not moved:
                # Roll the whole chunk back, work included. The loser of a swap
                # must not have done anything observable, and the only way to
                # guarantee that is to never commit.
                raise _Rollback
            binds = Binds()
            frontier = frontier_sql(binds)
            where = walk.units.chunk_where(
                binds, cursor_from=cursor_from, cursor_to=cursor_to, frontier=frontier
            )
            chunk = Chunk(
                table=walk.table,
                where=where,
                cursor_from=cursor_from,
                cursor_to=cursor_to,
                model=walk.model,
                alias=walk.alias,
            )
            affected = await walk.work.apply(tx, chunk, binds)
            await walk.ledger.count_rows(tx, affected)
            if pending is not None or holes_open:
                # Clearing the hole is the only thing that un-bars the terminal
                # gate, and it commits with the work that cleared it -- so the
                # gate is un-barred by the chunk succeeding, never by somebody
                # queueing it. Issued for an ordinary chunk too, because `halt`
                # parks the cursor *before* its hole and simply re-walks it;
                # guarded on the ledger already reporting a hole so a pass that
                # has never failed pays nothing for the possibility.
                await walk.ledger.clear_hole(tx, cursor_to=keyset.encode_cursor(keys, cursor_to))
    except _Rollback:
        return False, 0
    return True, affected


class _Rollback(Exception):
    """Abandon the chunk transaction after a lost compare-and-swap."""


async def _finish(
    walk: Any, connection: Any, *, chunks: int, rows: int, holes: int, row: Any
) -> ShiftResult:
    """The walk has no more ranges. Complete it, or hand it to the gate."""
    ledger = walk.ledger
    if walk.gate is None or walk.gate.scope == "unit":
        complete = await ledger.set_phase(connection, expected=WALKING, phase=DONE)
        return ShiftResult(chunks, rows, complete=complete, stopped="complete", holes=holes)

    open_holes = await ledger.open_holes(connection)
    if open_holes:
        # Skipping buys throughput and never the irreversible step. The pass is
        # stopped rather than merely reported, so it is visible to the health
        # check and clearable by `wreath passes retry`.
        error = (
            f"{open_holes} chunk(s) were given up on, so the gate is barred. "
            "Clear them with `wreath passes retry`."
        )
        await ledger.block(connection, error=error, phase=BLOCKED)
        return ShiftResult(chunks, rows, stopped="blocked", error=error, holes=holes)

    if not await ledger.set_phase(connection, expected=WALKING, phase=VERIFYING):
        # Another worker reached the gate first. It owns the sequence.
        return ShiftResult(chunks, rows, stopped="lost", holes=holes)
    row = await ledger.read(connection) or row
    result = await _run_gate(walk, connection, row=row)
    return ShiftResult(
        chunks,
        rows,
        complete=result.complete,
        stopped=result.stopped,
        error=result.error,
        holes=holes,
    )


async def _run_gate(walk: Any, connection: Any, *, row: Any) -> ShiftResult:
    """Verify, publish, and run the irreversible step -- each exactly once.

    Re-entrant on purpose. A process that dies between `verifying` and
    `verified` re-verifies on restart rather than proceeding on trust, which
    is always the right trade: verification is idempotent and cheap relative to
    the thing it guards.
    """
    gate = walk.gate
    ledger = walk.ledger
    phase = row.phase

    if phase == VERIFYING:
        verdict = await gate.verify.check(connection, walk=walk)
        if verdict.transient:
            # Could not run, rather than ran and answered no. Leave the phase
            # alone and let the next shift try again.
            await ledger.record_error(connection, verdict.detail)
            return ShiftResult(stopped="failed", error=verdict.detail)
        if not verdict.ok:
            # A verification that answered no means the walk's logic is wrong,
            # and running it again will fail identically at the same row.
            await ledger.block(connection, error=verdict.detail, phase=UNVERIFIED)
            return ShiftResult(stopped="blocked", error=verdict.detail)
        await ledger.publish(connection, fact=gate.publishes, detail=verdict.detail)
        if not await ledger.set_phase(connection, expected=VERIFYING, phase=VERIFIED):
            return ShiftResult(stopped="lost")
        phase = VERIFIED

    if phase == VERIFIED:
        if gate.then is None:
            complete = await ledger.set_phase(connection, expected=VERIFIED, phase=DONE)
            return ShiftResult(complete=complete, stopped="complete")
        if not await ledger.set_phase(connection, expected=VERIFIED, phase=APPLYING):
            return ShiftResult(stopped="lost")
        phase = APPLYING

    # `applying` and nothing else left. A process that dies inside the
    # irreversible step leaves the pass here deliberately: stuck needs an
    # operator, and for something that cannot be undone that is the safe
    # direction to fail in.
    try:
        await gate.then(connection, walk, None)
    except Exception as error:  # noqa: BLE001 - the step is the caller's code
        detail = f"the terminal step failed: {error!r}"
        await ledger.record_error(connection, detail)
        return ShiftResult(stopped="failed", error=detail)
    complete = await ledger.set_phase(connection, expected=APPLYING, phase=DONE)
    return ShiftResult(complete=complete, stopped="complete")


async def _gate_unit(
    walk: Any,
    connection: Any,
    *,
    cursor_from: tuple[Any, ...] | None,
    cursor_to: tuple[Any, ...],
) -> str | None:
    """Verify one unit and run its terminal step. Returns an error, or `None`.

    There is no whole-pass phase to compare-and-swap on here, and there should
    not be: a recurring pass has no completion, and one bad bucket must not
    freeze the ladder behind it. Exclusivity comes from the chunk's own swap,
    which only one worker won.

    The honest gap, stated rather than hidden: this runs *after* the chunk's
    transaction commits, so a process that dies between them leaves the unit
    walked but not terminal. A recurring pass re-derives its frontier from the
    start of the domain every cycle, so the next cycle finds it again -- which
    is why this is the scope for recurring passes and not for a fixed ceiling.
    """
    binds = Binds()
    scope = walk.units.chunk_where(
        binds, cursor_from=cursor_from, cursor_to=cursor_to, frontier=None
    )
    scope = _inline(scope, binds.args)
    verdict = await walk.gate.verify.check(connection, walk=walk, scope=scope)
    if not verdict.ok:
        phase = BLOCKED if verdict.transient else UNVERIFIED
        await walk.ledger.block(connection, error=verdict.detail, phase=phase)
        return verdict.detail
    if walk.gate.then is not None:
        try:
            await walk.gate.then(connection, walk, (cursor_from, cursor_to))
        except Exception as error:  # noqa: BLE001 - the step is the caller's code
            detail = f"the terminal step failed: {error!r}"
            await walk.ledger.block(connection, error=detail, phase=BLOCKED)
            return detail
    return None


def _inline(where: str, args: tuple[Any, ...]) -> str:
    """A predicate with its binds written in, for splicing into another statement.

    The values are this pass's own cursor keys, never a request value, and they
    go through the same literal writer the dead-letter predicate uses.
    """
    for index in range(len(args), 0, -1):
        where = where.replace(f"${index}", literal(args[index - 1]))
    return where


__all__ = [
    "Binds",
    "Chunk",
    "ShiftResult",
    "chunk_predicate",
    "range_predicate",
    "reproduce_predicate",
    "run_shift",
]
