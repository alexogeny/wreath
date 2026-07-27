"""The pass ledger: where a walk's position lives, and why it is not the job row.

A job row is one delivery. A pass over ten million rows is thousands of
deliveries, so the position has to outlive every one of them -- and the job row
cannot hold it. The runner rewrites that row on every claim, failure, and
completion; a dead-lettered job is terminal, so the moment you most need to know
where the walk stopped is the moment the row becomes an epitaph; and a recurring
pass needs a *new* job per cycle with a new dedup key, while all of them must
share one position.

So the position is a table of its own, ``"wreath".passes``, beside the jobs
table because it is job-adjacent -- and, crucially, **in the same database as
the data being walked**. That is what makes the whole design work: the cursor
advance and the work commit in one transaction, because the ledger row and the
table are two rows in one PostgreSQL cluster. A stack with a broker and a
separate result store cannot do that, which is why every such stack's backfill
has a window in which the position and the data disagree.

Every write here is a compare-and-swap. The cursor advance takes the ledger
row's lock as the chunk's *first* statement, so two workers on one pass
serialise rather than collide, and a worker whose lease expired while a
replacement moved on fails its own swap and knows it -- without needing the job
runner's fence to stay safe.

Beside it sits ``"wreath".pass_holes``, one row per chunk that failed often
enough to be given up on. A hole is not an error message: it carries the range,
the attempt count, and **the predicate that would reproduce it**, so an operator
can run the chunk by hand and see the real error rather than a truncated ``repr``
from three weeks ago. That is the difference between a hole and a task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from .progress import WINDOW_SECONDS

#: The pass state machine.
#:
#: ``walking`` → ``done`` is the whole of it for a pass with no gate. With one,
#: completion routes through ``verifying`` → ``verified`` → (``applying`` →)
#: ``done``, and every transition is a compare-and-swap, so a second worker that
#: independently concludes "finished" matches no rows and does nothing.
#:
#: The two stopped states are deliberately distinct. ``blocked`` is a chunk that
#: was given up on, and the fix is to retry it. ``unverified`` is a verification
#: that ran and answered no, which means the walk's logic is wrong and retrying
#: it will fail identically -- so ``wreath passes retry`` clears the first and
#: refuses the second.
WALKING = "walking"
DONE = "done"
BLOCKED = "blocked"
VERIFYING = "verifying"
VERIFIED = "verified"
APPLYING = "applying"
UNVERIFIED = "unverified"

#: Every phase that means the pass has stopped and will not move on its own.
STOPPED = (BLOCKED, UNVERIFIED)


def table_name(schema: str) -> str:
    return f'"{schema}".passes'


def holes_table_name(schema: str) -> str:
    return f'"{schema}".pass_holes'


def schema_sql(schema: str) -> str:
    """DDL for the ledger and its dead-letter table. Never auto-applied.

    A table that appears because a process started is a schema change with no
    history and no review, which is the same stance ``JobRunner.schema_sql`` and
    every :mod:`wreath.store` declaration take.
    """
    table = table_name(schema)
    holes = holes_table_name(schema)
    return (
        f'CREATE SCHEMA IF NOT EXISTS "{schema}";\n'
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        "  name text NOT NULL,\n"
        "  tenant text NOT NULL DEFAULT '',\n"
        "  phase text NOT NULL DEFAULT 'walking',\n"
        "  cursor jsonb,\n"
        "  ceiling jsonb,\n"
        "  keyspace_from jsonb,\n"
        "  pending jsonb NOT NULL DEFAULT '[]'::jsonb,\n"
        "  units_done bigint NOT NULL DEFAULT 0,\n"
        "  rows_done bigint NOT NULL DEFAULT 0,\n"
        "  denominator bigint,\n"
        "  denominator_kind text,\n"
        "  chunk_limit int NOT NULL DEFAULT 0,\n"
        "  paced_reason text,\n"
        "  window_started timestamptz,\n"
        "  window_rows bigint NOT NULL DEFAULT 0,\n"
        "  window_units bigint NOT NULL DEFAULT 0,\n"
        "  started_at timestamptz,\n"
        "  last_advance timestamptz,\n"
        "  cycle_started timestamptz,\n"
        "  driven_at timestamptz,\n"
        "  last_drive_error text,\n"
        "  verified_at timestamptz,\n"
        "  verified_fact text,\n"
        "  last_error text,\n"
        "  PRIMARY KEY (name, tenant)\n"
        ");\n"
        f"CREATE TABLE IF NOT EXISTS {holes} (\n"
        "  name text NOT NULL,\n"
        "  tenant text NOT NULL DEFAULT '',\n"
        "  cursor_from jsonb,\n"
        "  cursor_to jsonb NOT NULL,\n"
        "  attempts int NOT NULL DEFAULT 1,\n"
        "  error text NOT NULL,\n"
        "  predicate text NOT NULL,\n"
        "  failed_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
        "  PRIMARY KEY (name, tenant, cursor_to)\n"
        ");\n"
    )


_COLUMNS = (
    "name, tenant, phase, cursor, ceiling, keyspace_from, pending, units_done, "
    "rows_done, denominator, denominator_kind, chunk_limit, paced_reason, "
    "window_started, window_rows, window_units, started_at, last_advance, "
    "cycle_started, driven_at, last_drive_error, verified_at, verified_fact, "
    "last_error"
)

_HOLE_COLUMNS = "name, tenant, cursor_from, cursor_to, attempts, error, predicate, failed_at"


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One pass's durable position and phase, exactly as the table holds it."""

    name: str
    tenant: str
    phase: str
    cursor: Any
    ceiling: Any
    keyspace_from: Any
    pending: Any
    units_done: int
    rows_done: int
    denominator: int | None
    denominator_kind: str | None
    chunk_limit: int
    paced_reason: str | None
    window_started: Any
    window_rows: int
    window_units: int
    started_at: Any
    last_advance: Any
    cycle_started: Any
    driven_at: Any
    last_drive_error: str | None
    verified_at: Any
    verified_fact: str | None
    last_error: str | None
    #: The database's clock at the moment this row was read. Every "how long
    #: ago" answer is computed against it rather than against the reader's own
    #: clock, so a CLI on a laptop with a skewed clock cannot invent a stall.
    now: Any = None
    #: How many chunks of this pass were given up on and not yet cleared.
    holes_open: int = 0


@dataclass(frozen=True, slots=True)
class Hole:
    """One chunk that failed often enough to be given up on."""

    name: str
    tenant: str
    cursor_from: Any
    cursor_to: Any
    attempts: int
    error: str
    predicate: str
    failed_at: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tenant": self.tenant,
            "cursor_from": self.cursor_from,
            "cursor_to": self.cursor_to,
            "attempts": self.attempts,
            "error": self.error,
            "predicate": self.predicate,
            "failed_at": None if self.failed_at is None else str(self.failed_at),
        }


def _json(value: Any) -> Any:
    """Decode a ``jsonb`` column whichever way the driver handed it back."""
    if isinstance(value, (str, bytes, bytearray)):
        return _json_loads(value)
    return value


def _encode(value: Any) -> str | None:
    if value is None:
        return None
    encoded = _json_dumps(value)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


def _reader(record: Any) -> Any:
    def field(name: str, index: int, default: Any = None) -> Any:
        try:
            return record[name]
        except (KeyError, TypeError, IndexError):
            try:
                return record[index]
            except (KeyError, TypeError, IndexError):
                return default

    return field


def row_from_record(record: Any) -> LedgerRow:
    field = _reader(record)
    return LedgerRow(
        name=str(field("name", 0)),
        tenant=str(field("tenant", 1)),
        phase=str(field("phase", 2)),
        cursor=_json(field("cursor", 3)),
        ceiling=_json(field("ceiling", 4)),
        keyspace_from=_json(field("keyspace_from", 5)),
        pending=_json(field("pending", 6)) or [],
        units_done=int(field("units_done", 7) or 0),
        rows_done=int(field("rows_done", 8) or 0),
        denominator=field("denominator", 9),
        denominator_kind=field("denominator_kind", 10),
        chunk_limit=int(field("chunk_limit", 11) or 0),
        paced_reason=field("paced_reason", 12),
        window_started=field("window_started", 13),
        window_rows=int(field("window_rows", 14) or 0),
        window_units=int(field("window_units", 15) or 0),
        started_at=field("started_at", 16),
        last_advance=field("last_advance", 17),
        cycle_started=field("cycle_started", 18),
        driven_at=field("driven_at", 19),
        last_drive_error=field("last_drive_error", 20),
        verified_at=field("verified_at", 21),
        verified_fact=field("verified_fact", 22),
        last_error=field("last_error", 23),
        now=field("now", 24),
        holes_open=int(field("holes_open", 25) or 0),
    )


def hole_from_record(record: Any) -> Hole:
    field = _reader(record)
    return Hole(
        name=str(field("name", 0)),
        tenant=str(field("tenant", 1)),
        cursor_from=_json(field("cursor_from", 2)),
        cursor_to=_json(field("cursor_to", 3)),
        attempts=int(field("attempts", 4) or 0),
        error=str(field("error", 5) or ""),
        predicate=str(field("predicate", 6) or ""),
        failed_at=field("failed_at", 7),
    )


class Ledger:
    """The statements one pass issues against its own ledger row."""

    __slots__ = ("_holes", "_name", "_schema", "_table", "_tenant")

    def __init__(self, *, schema: str, name: str, tenant: str = "") -> None:
        self._schema = schema
        self._name = name
        self._tenant = tenant
        self._table = table_name(schema)
        self._holes = holes_table_name(schema)

    @property
    def table(self) -> str:
        return self._table

    @property
    def holes_table(self) -> str:
        return self._holes

    def schema_sql(self) -> str:
        return schema_sql(self._schema)

    async def seed(self, executor: Any, *, chunk_limit: int) -> None:
        """Create this pass's row if it is not already there. Idempotent."""
        await executor.execute(
            f"INSERT INTO {self._table} (name, tenant, phase, chunk_limit, started_at) "
            "VALUES ($1, $2, 'walking', $3, clock_timestamp()) "
            "ON CONFLICT (name, tenant) DO NOTHING",
            self._name,
            self._tenant,
            int(chunk_limit),
        )

    async def read(self, executor: Any) -> LedgerRow | None:
        record = await executor.fetchrow(
            f"SELECT {_COLUMNS}, clock_timestamp() AS now, "
            f"(SELECT count(*) FROM {self._holes} h WHERE h.name = p.name "
            "AND h.tenant = p.tenant) AS holes_open "
            f"FROM {self._table} p WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
        )
        return None if record is None else row_from_record(record)

    async def advance(self, executor: Any, *, expected: Any, cursor: Any) -> bool:
        """The compare-and-swap, issued as the chunk transaction's first statement.

        Two things fall out of it being first, and together they are the
        load-bearing property of a chunked pass. It takes the ledger row's lock
        for the rest of the transaction, so two workers on one pass serialise:
        the loser blocks, then sees its own swap match no rows, rolls back, and
        cannot have done anything observable because it never committed. And a
        worker whose lease expired while a replacement advanced the cursor fails
        the swap and detects its own staleness from the position itself.

        Row counts are deliberately *not* part of this statement. They are
        reported, never structural -- the moment a count is load-bearing inside
        the primitive, a range source that counts no rows stops fitting.
        """
        tag = await executor.execute(
            f"UPDATE {self._table} SET cursor = $3::jsonb, units_done = units_done + 1, "
            "last_advance = clock_timestamp(), last_error = NULL "
            "WHERE name = $1 AND tenant = $2 AND cursor IS NOT DISTINCT FROM $4::jsonb",
            self._name,
            self._tenant,
            _encode(cursor),
            _encode(expected),
        )
        return _affected(tag) == 1

    async def skip_to(self, executor: Any, *, expected: Any, cursor: Any) -> bool:
        """Move the cursor past a hole without counting the chunk as done.

        ``units_done`` deliberately does not move: a skipped chunk is not a unit
        of work completed, and letting it count would make the percentage claim
        progress the pass did not make.
        """
        tag = await executor.execute(
            f"UPDATE {self._table} SET cursor = $3::jsonb, last_advance = clock_timestamp() "
            "WHERE name = $1 AND tenant = $2 AND cursor IS NOT DISTINCT FROM $4::jsonb",
            self._name,
            self._tenant,
            _encode(cursor),
            _encode(expected),
        )
        return _affected(tag) == 1

    async def count_rows(self, executor: Any, rows: int, *, window: float = WINDOW_SECONDS) -> None:
        """Add this chunk's rows, and fold it into the trailing rate window.

        The window is a periodically reset accumulator rather than a list of
        samples: one statement, no array to trim, and it answers the only
        question anyone asks of it -- how fast has this pass been going
        *lately*. The chunk that opens a window contributes no rows to it, so the
        count and the interval describe the same stretch of time instead of
        overlapping by one chunk.

        ``now()`` rather than ``clock_timestamp()`` on purpose: it is stable for
        the whole transaction, so the rollover test cannot disagree with itself
        between the ``CASE`` arms.
        """
        roll = (
            "window_started IS NULL "
            "OR now() - window_started > make_interval(secs => $4::float8)"
        )
        await executor.execute(
            f"UPDATE {self._table} SET rows_done = rows_done + $3, "
            f"window_started = CASE WHEN {roll} THEN now() ELSE window_started END, "
            f"window_units = CASE WHEN {roll} THEN 0 ELSE window_units + 1 END, "
            f"window_rows = CASE WHEN {roll} THEN 0 ELSE window_rows + $3 END "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            int(rows),
            float(window),
        )

    async def set_ceiling(self, executor: Any, *, ceiling: Any, cycle: bool) -> None:
        """Record the frontier this cycle is walking towards."""
        started = ", cycle_started = clock_timestamp()" if cycle else ""
        await executor.execute(
            f"UPDATE {self._table} SET ceiling = $3::jsonb{started} "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            _encode(ceiling),
        )

    async def set_denominator(self, executor: Any, *, total: Any, kind: str) -> None:
        """Record what the percentage is a percentage *of*, and where it came from.

        The kind is written in the same statement as the number, so there is no
        window in which the ledger holds a denominator nobody can source.
        """
        await executor.execute(
            f"UPDATE {self._table} SET denominator = $3, denominator_kind = $4 "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            None if total is None else int(total),
            kind,
        )

    async def set_keyspace_floor(self, executor: Any, *, floor: Any) -> None:
        """Record the smallest key in range, for a keyspace percentage."""
        await executor.execute(
            f"UPDATE {self._table} SET keyspace_from = $3::jsonb "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            _encode(floor),
        )

    async def set_pacing(self, executor: Any, *, chunk_limit: int, reason: str) -> None:
        """Record how the pass is currently paced.

        A paced pass that does not say it is paced is indistinguishable from a
        broken one, which is why this is written rather than inferred.
        """
        await executor.execute(
            f"UPDATE {self._table} SET chunk_limit = $3, paced_reason = $4 "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            int(chunk_limit),
            reason,
        )

    async def mark_driven(self, executor: Any, *, error: str | None = None) -> None:
        """Stamp that something tried to drive this pass, and how it went.

        Without this the ledger cannot tell a pass with no work to do apart from
        a pass nothing is driving -- and the second one silently never finishes,
        which is the failure the whole status surface exists to name.
        """
        await executor.execute(
            f"UPDATE {self._table} SET driven_at = clock_timestamp(), last_drive_error = $3 "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            None if error is None else error[:2000],
        )

    async def set_phase(self, executor: Any, *, expected: str, phase: str) -> bool:
        """A phase transition, compare-and-swapped like everything else here.

        A second worker that independently concludes the walk is finished matches
        no rows and does nothing, so completion happens exactly once even if
        three shifts arrive together.
        """
        tag = await executor.execute(
            f"UPDATE {self._table} SET phase = $4 "
            "WHERE name = $1 AND tenant = $2 AND phase = $3",
            self._name,
            self._tenant,
            expected,
            phase,
        )
        return _affected(tag) == 1

    async def block(self, executor: Any, *, error: str, phase: str = BLOCKED) -> None:
        """Stop the pass, and say why in the row itself.

        *phase* distinguishes the two ways a pass stops: ``blocked`` at a chunk
        it gave up on, which an operator clears by retrying it, and
        ``unverified`` at a verification that answered no, which is not
        retryable at all.
        """
        await executor.execute(
            f"UPDATE {self._table} SET phase = $3, last_error = $4 "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            phase,
            error[:2000],
        )

    async def unblock(self, executor: Any) -> bool:
        """Return a pass stopped at a hole to walking. Refuses an unverified one.

        Without this a halted pass is stopped forever: ``halt`` parks the cursor
        *before* the hole and sets ``blocked``, and every later shift sees a
        phase that is not ``walking`` and declines to run -- so nothing ever
        re-attempts the chunk, the hole is never cleared, and the terminal gate
        it bars can never be reached. Clearing a hole has to be able to restart
        the pass, or ``halt`` is not a policy but a trap.

        ``unverified`` is deliberately not matched: a verification that answered
        no will answer no again, and retrying it burns a maintenance window to
        fail at the same row.
        """
        tag = await executor.execute(
            f"UPDATE {self._table} SET phase = 'walking', last_error = NULL "
            "WHERE name = $1 AND tenant = $2 AND phase = $3",
            self._name,
            self._tenant,
            BLOCKED,
        )
        return _affected(tag) == 1

    async def publish(self, executor: Any, *, fact: str | None, detail: str) -> None:
        """Record that verification passed, and what it established.

        Written whether or not an irreversible step follows, because the fact is
        the durable half: a deferred migration's terminal step is a later
        migration someone else runs, and this row is what that migration reads
        before it agrees to narrow the column.
        """
        await executor.execute(
            f"UPDATE {self._table} SET verified_at = clock_timestamp(), "
            "verified_fact = $3, last_error = NULL WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            (fact if fact is not None else detail)[:2000],
        )

    async def begin_cycle(self, executor: Any) -> bool:
        """Rewind a recurring pass to the start of a fresh cycle.

        A recurring pass has no completion; a *cycle* completes, the cursor
        returns to the beginning of the domain, and the frontier is re-derived.
        Rows that expired behind the cursor while the last cycle ran are found by
        this one, which is the property that makes a re-derived frontier sound
        where a fixed ceiling would need the key to be monotone.
        """
        tag = await executor.execute(
            f"UPDATE {self._table} SET cursor = NULL, phase = 'walking', "
            "cycle_started = clock_timestamp() "
            "WHERE name = $1 AND tenant = $2 "
            "AND phase IN ('walking', 'done', 'verified')",
            self._name,
            self._tenant,
        )
        return _affected(tag) == 1

    async def record_error(self, executor: Any, error: str) -> None:
        await executor.execute(
            f"UPDATE {self._table} SET last_error = $3 WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            error[:2000],
        )

    # -- pending units --------------------------------------------------------

    async def requeue(self, executor: Any, *, cursor_from: Any, cursor_to: Any) -> bool:
        """Append one unit to be walked out of order. The cursor never rewinds.

        Both callers of this arrived at it from opposite directions: a rollup
        folding a late correction into a bucket its cursor is already past, and
        an operator clearing a dead-lettered chunk. One mechanism.
        """
        unit = _encode({"from": cursor_from, "to": cursor_to})
        # Deliberately allowed on a finished pass. Clearing a hole is the only
        # thing that un-bars a skipped pass's terminal gate, and a pass that
        # skipped something reaches `done` with the hole still in it -- refusing
        # here would make the gate permanently unbarrable.
        tag = await executor.execute(
            f"UPDATE {self._table} SET pending = pending || $3::jsonb "
            "WHERE name = $1 AND tenant = $2 AND NOT (pending @> $3::jsonb)",
            self._name,
            self._tenant,
            f"[{unit}]",
        )
        return _affected(tag) == 1

    async def claim_pending(self, executor: Any) -> Any:
        """Take the oldest pending unit, as the chunk transaction's first statement.

        Same exclusivity as the cursor swap and for the same reason: the read is
        ``FOR UPDATE`` so two workers serialise, and the removal commits with the
        work, so a unit whose chunk rolls back is still pending afterwards.
        """
        record = await executor.fetchrow(
            f"WITH held AS (SELECT pending FROM {self._table} "
            "WHERE name = $1 AND tenant = $2 FOR UPDATE) "
            f"UPDATE {self._table} SET pending = pending - 0 "
            "WHERE name = $1 AND tenant = $2 "
            "AND jsonb_array_length(pending) > 0 "
            "RETURNING (SELECT pending -> 0 FROM held) AS unit",
            self._name,
            self._tenant,
        )
        if record is None:
            return None
        field = _reader(record)
        return _json(field("unit", 0))

    async def drop_pending(self, executor: Any, *, cursor_from: Any, cursor_to: Any) -> None:
        """Remove one unit by value, for a unit that has been given up on."""
        unit = _encode({"from": cursor_from, "to": cursor_to})
        await executor.execute(
            f"UPDATE {self._table} SET pending = COALESCE("
            "(SELECT jsonb_agg(e) FROM jsonb_array_elements(pending) e "
            "WHERE e <> $3::jsonb), '[]'::jsonb) "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            unit,
        )

    # -- holes ----------------------------------------------------------------

    async def record_hole(
        self,
        executor: Any,
        *,
        cursor_from: Any,
        cursor_to: Any,
        attempts: int,
        error: str,
        predicate: str,
    ) -> None:
        """Write a hole, or add to the attempts of one already there."""
        await executor.execute(
            f"INSERT INTO {self._holes} "
            "(name, tenant, cursor_from, cursor_to, attempts, error, predicate) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7) "
            "ON CONFLICT (name, tenant, cursor_to) DO UPDATE SET "
            f"attempts = {self._holes}.attempts + EXCLUDED.attempts, "
            "error = EXCLUDED.error, failed_at = clock_timestamp()",
            self._name,
            self._tenant,
            _encode(cursor_from),
            _encode(cursor_to),
            int(attempts),
            error[:4000],
            predicate[:4000],
        )

    async def clear_hole(self, executor: Any, *, cursor_to: Any) -> None:
        """Delete a hole, which is the only thing that un-bars the terminal gate.

        Issued for a chunk that succeeded, never for one that was merely queued:
        the gate is un-barred by the work being done, not by somebody intending
        to do it.
        """
        await executor.execute(
            f"DELETE FROM {self._holes} WHERE name = $1 AND tenant = $2 "
            "AND cursor_to = $3::jsonb",
            self._name,
            self._tenant,
            _encode(cursor_to),
        )

    async def holes(self, executor: Any) -> list[Hole]:
        records = await executor.fetch(
            f"SELECT {_HOLE_COLUMNS} FROM {self._holes} "
            "WHERE name = $1 AND tenant = $2 ORDER BY failed_at",
            self._name,
            self._tenant,
        )
        return [hole_from_record(record) for record in records or ()]

    async def open_holes(self, executor: Any) -> int:
        total = await executor.fetchval(
            f"SELECT count(*) FROM {self._holes} WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
        )
        return int(total or 0)


@dataclass(frozen=True, slots=True)
class PublishedFact:
    """Something a pass has verified to be true, and when.

    The gate's durable output, and the reason a deferred migration does not have
    to trust a percentage: it reads this table and refuses to narrow a column
    whose pass has published nothing.
    """

    name: str
    tenant: str
    fact: str
    verified_at: Any


async def published_facts(
    executor: Any, *, schema: str, fact: str | None = None
) -> list[PublishedFact]:
    """Every verified fact in one ledger, or the passes that published one fact.

    Read with nothing but a connection and a schema, deliberately: the consumer
    is a migration, which has no pass declaration in hand and should not need
    one.
    """
    table = table_name(schema)
    clause = "" if fact is None else " AND verified_fact = $1"
    args = () if fact is None else (fact,)
    records = await executor.fetch(
        f"SELECT name, tenant, verified_fact, verified_at FROM {table} "
        f"WHERE verified_at IS NOT NULL{clause} ORDER BY name, tenant",
        *args,
    )
    out: list[PublishedFact] = []
    for record in records or ():
        field = _reader(record)
        out.append(
            PublishedFact(
                name=field("name", 0),
                tenant=field("tenant", 1, ""),
                fact=field("verified_fact", 2, ""),
                verified_at=field("verified_at", 3),
            )
        )
    return out


async def read_all(executor: Any, *, schema: str, name: str | None = None) -> list[LedgerRow]:
    """Every pass in one schema's ledger, or one of them by name."""
    table = table_name(schema)
    holes = holes_table_name(schema)
    extra = (
        f"clock_timestamp() AS now, (SELECT count(*) FROM {holes} h "
        "WHERE h.name = p.name AND h.tenant = p.tenant) AS holes_open"
    )
    if name is None:
        records = await executor.fetch(
            f"SELECT {_COLUMNS}, {extra} FROM {table} p ORDER BY name, tenant"
        )
    else:
        records = await executor.fetch(
            f"SELECT {_COLUMNS}, {extra} FROM {table} p WHERE name = $1 ORDER BY tenant",
            name,
        )
    return [row_from_record(record) for record in records or ()]


async def read_holes(
    executor: Any, *, schema: str, name: str | None = None
) -> list[Hole]:
    """Every dead-lettered chunk in one schema's ledger, or one pass's."""
    table = holes_table_name(schema)
    if name is None:
        records = await executor.fetch(
            f"SELECT {_HOLE_COLUMNS} FROM {table} ORDER BY name, tenant, failed_at"
        )
    else:
        records = await executor.fetch(
            f"SELECT {_HOLE_COLUMNS} FROM {table} WHERE name = $1 ORDER BY tenant, failed_at",
            name,
        )
    return [hole_from_record(record) for record in records or ()]


def _affected(tag: Any) -> int:
    """The row count out of a command tag such as ``UPDATE 1``.

    A driver that hands back something other than a tag (a fake in a test, a
    backend that returns None for DML) is read as "one row", because the
    alternative -- treating an unknown result as a lost compare-and-swap -- would
    turn a working pass into a silently stalled one.
    """
    if isinstance(tag, int):
        return tag
    if not isinstance(tag, str):
        return 1
    parts = tag.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return 1


__all__ = [
    "BLOCKED",
    "DONE",
    "WALKING",
    "Hole",
    "Ledger",
    "LedgerRow",
    "hole_from_record",
    "holes_table_name",
    "read_all",
    "read_holes",
    "row_from_record",
    "schema_sql",
    "table_name",
]
