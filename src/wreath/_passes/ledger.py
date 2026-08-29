"""The pass ledger: where a walk's position lives, and why it is not the job row.

A job row is one delivery. A pass over ten million rows is thousands of
deliveries, so the position has to outlive every one of them -- and the job row
cannot hold it. The runner rewrites that row on every claim, failure, and
completion; a dead-lettered job is terminal, so the moment you most need to know
where the walk stopped is the moment the row becomes an epitaph; and a recurring
pass needs a *new* job per cycle with a new dedup key, while all of them must
share one position.

So the position is a table of its own, `"wreath".passes`, beside the jobs
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

Beside it sits `"wreath".pass_holes`, one row per chunk that failed often
enough to be given up on. A hole is not an error message: it carries the range,
the attempt count, and **the predicate that would reproduce it**, so an operator
can run the chunk by hand and see the real error rather than a truncated `repr`
from three weeks ago. That is the difference between a hole and a task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from .._pgcatalog import column_exists
from .progress import WINDOW_SECONDS

#: The pass state machine.
#:
#: `walking` → `done` is the whole of it for a pass with no gate. With one,
#: completion routes through `verifying` → `verified` → (`applying` →)
#: `done`, and every transition is a compare-and-swap, so a second worker that
#: independently concludes "finished" matches no rows and does nothing.
#:
#: The two stopped states are deliberately distinct. `blocked` is a chunk that
#: was given up on, and the fix is to retry it. `unverified` is a verification
#: that ran and answered no, which means the walk's logic is wrong and retrying
#: it will fail identically -- so `wreath passes retry` clears the first and
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


def rewrites_table_name(schema: str) -> str:
    return f'"{schema}".pass_rewrites'


def _placeholders(values: tuple[str, ...]) -> str:
    """`$1, $2, ...` for an `IN` list, one placeholder per value.

    Not `= ANY($1)`, which is the obvious spelling and does not work: the
    driver infers a parameter's type from the Python value, and it has no case
    for `list` -- passing one raises `unsupported PostgreSQL value type`.
    Both readers here were written that way and neither had ever run against a
    real server, because their tests use fakes and fakes do not infer types.

    The list is bounded by the columns one migration touches, so an `IN` list
    is the right size of hammer; a reader proportional to the *ledger* would
    need the array, and would need a codec first.
    """
    return ", ".join(f"${index}" for index in range(1, len(values) + 1))


def statements(schema: str) -> tuple[str, ...]:
    """DDL for the ledger, its dead-letter table, and the rewrite record.

    One statement per element, which is what the driver wants: it speaks the
    extended query protocol exclusively, so it prepares each statement and
    PostgreSQL refuses `cannot insert multiple commands into a prepared
    statement`. This used to be one `;\\n`-joined blob that five call sites each
    split back apart, and the trigger function below was written on a single
    line for no reason other than to survive that split. It no longer has to be,
    though it is left as it was: reflowing SQL that guards against silent data
    loss, in the same change that moves it, would make the diff unreviewable.
    """
    table = table_name(schema)
    holes = holes_table_name(schema)
    rewrites = rewrites_table_name(schema)
    guard = f'"{schema}".pass_rewrites_is_append_only'
    return (
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
        "  guards text,\n"
        "  rewrites text,\n"
        "  verified_at timestamptz,\n"
        "  verified_fact text,\n"
        "  last_error text,\n"
        "  PRIMARY KEY (name, tenant)\n"
        ")",
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
        ")",
        # The rewrite record. Separate from the ledger row on purpose: the
        # ledger is working state that a pass rewrites on every claim, and a
        # future "tidy up finished passes" job would be entirely reasonable to
        # write against it. This table is not working state. It answers one
        # question -- "were this column's values ever overwritten in place?" --
        # whose answer can never become false, because the originals are gone
        # and no later event puts them back. So it is append-only, and the
        # guard below is what makes that a rule rather than a convention.
        f"CREATE TABLE IF NOT EXISTS {rewrites} (\n"
        "  fact text NOT NULL,\n"
        "  pass_name text NOT NULL,\n"
        "  tenant text NOT NULL DEFAULT '',\n"
        "  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
        "  PRIMARY KEY (fact, pass_name, tenant)\n"
        ")",
        f"CREATE OR REPLACE FUNCTION {guard}() RETURNS trigger AS "
        "$$ BEGIN RAISE EXCEPTION 'wreath: %.pass_rewrites is append-only; "
        "a row here records that a column''s values were overwritten in place, "
        "and a downgrade reads it to refuse restoring the old schema over the "
        "new data. Removing it does not make the downgrade safe, only silent.', "
        f"TG_TABLE_SCHEMA; END $$ LANGUAGE plpgsql",
        f"DROP TRIGGER IF EXISTS pass_rewrites_no_change ON {rewrites}",
        f"CREATE TRIGGER pass_rewrites_no_change BEFORE DELETE OR UPDATE ON {rewrites} "
        f"FOR EACH ROW EXECUTE FUNCTION {guard}()",
        # TRUNCATE does not fire row-level triggers, so it needs its own. Without
        # this the whole guard is one `TRUNCATE` away from doing nothing.
        f"DROP TRIGGER IF EXISTS pass_rewrites_no_truncate ON {rewrites}",
        f"CREATE TRIGGER pass_rewrites_no_truncate BEFORE TRUNCATE ON {rewrites} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {guard}()",
    )


def schema_claim(schema: str) -> Any:
    """The pass ledger's claim on the wreath schema.

    The ledger, its dead-letter table, and the append-only rewrite record are
    fleet machinery -- they record what a *deployment* did, not what a tenant's
    data is -- so they live in the `wreath` schema and never appear in the
    application's migration artifact.

    A module-level factory rather than a `component()` method, because the
    ledger has no holder object for the collection walk to ask; `schema_claim`
    is the name every "build a claim, given a name or a schema" callable in the
    tree carries, leaving `component()` to mean only the zero-argument protocol.
    """
    from ..schema import Component, Step

    return Component(
        name="passes",
        schema=schema,
        relations=("passes", "pass_holes", "pass_rewrites"),
        steps=(
            Step(version=1, statements=statements(schema)),
            # Version 1 is left exactly as it shipped. Rewriting its `CREATE
            # TABLE` would change what an already-bootstrapped database was told
            # it had -- `wreath.schema` records the version, not the DDL -- so a
            # cluster already at 1 would never see the column. Additive, so a
            # worker on the previous build keeps running against it.
            Step(
                version=2,
                statements=(
                    f"ALTER TABLE {table_name(schema)} ADD COLUMN IF NOT EXISTS trace_context text",
                ),
            ),
        ),
    )


async def has_trace_column(executor: Any, *, schema: str) -> bool:
    """Whether this database's ledger table has the version-2 column.

    A deployment whose role cannot `CREATE SCHEMA` applies the DDL by hand, so
    there is always a window in which this build is newer than the table it
    meets. The shape of the table is therefore a precondition callers *check*
    rather than an error they catch: a broad `except` around the seed would
    swallow a revoked grant and a driver fault alongside the one case it means
    to survive, and the seed runs inside the shift, where poisoning the
    connection would take the walk with it.
    """
    return await column_exists(executor, schema=schema, table="passes", column="trace_context")


def schema_sql(schema: str) -> str:
    """The ledger DDL, semicolon-joined. A derivation of `statements`."""
    return schema_claim(schema).sql()


_COLUMNS = (
    "name, tenant, phase, cursor, ceiling, keyspace_from, pending, units_done, "
    "rows_done, denominator, denominator_kind, chunk_limit, paced_reason, "
    "window_started, window_rows, window_units, started_at, last_advance, "
    "cycle_started, driven_at, last_drive_error, guards, rewrites, verified_at, "
    "verified_fact, last_error"
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
    #: The fact this pass *claims* it will establish, written when the row is
    #: seeded. Distinct from `verified_fact`, which is written only once
    #: verification passes: a migration asking "may I narrow this column?"
    #: has to tell "no pass guards it" apart from "a pass guards it and has
    #: not finished", and those are the same answer if the claim is recorded
    #: only at publication. Last in the field order, and defaulted, so every
    #: existing construction of this row keeps working.
    guards: str | None = None
    #: The column whose *values* this pass overwrote in place, if any.
    #:
    #: Deliberately not `guards`, and the difference is the whole point.
    #: `guards` is a claim a gate will discharge -- it is answered by
    #: `verified_at` and stops mattering once publication clears it.
    #: `rewrites` is a fact about the data that publication does **not**
    #: clear: once a re-encode has run, the old values are gone from the table
    #: and no later event puts them back. A downgrade therefore has to read it
    #: with no `verified_at` filter, because a *finished* re-encode is the
    #: dangerous case rather than the safe one.
    rewrites: str | None = None
    #: The traceparent of the drive that started this cycle, or `None` when
    #: nothing traced has ever driven it. See `Ledger.seed` for what "started"
    #: means and why nothing is minted when there is no such drive.
    trace_context: str | None = None


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
    """Decode a `jsonb` column whichever way the driver handed it back."""
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
        except KeyError, TypeError, IndexError:
            try:
                return record[index]
            except KeyError, TypeError, IndexError:
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
        guards=field("guards", 21),
        rewrites=field("rewrites", 22),
        verified_at=field("verified_at", 23),
        verified_fact=field("verified_fact", 24),
        last_error=field("last_error", 25),
        now=field("now", 26),
        holes_open=int(field("holes_open", 27) or 0),
        # Absent from the record when the column is not there, so a build newer
        # than its schema reads `None` rather than raising a KeyError from a
        # stack frame that says nothing useful.
        trace_context=field("trace_context", 28),
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

    __slots__ = (
        "_holes",
        "_name",
        "_rewrites",
        "_schema",
        "_table",
        "_tenant",
        "_trace_column",
    )

    def __init__(self, *, schema: str, name: str, tenant: str = "") -> None:
        self._schema = schema
        self._name = name
        self._tenant = tenant
        self._table = table_name(schema)
        self._holes = holes_table_name(schema)
        self._rewrites = rewrites_table_name(schema)
        #: Tri-state: None until probed, then whether this database's ledger has
        #: the version-2 `trace_context` column. Cached because a pass reads its
        #: row several times a shift and runs for days -- `wreath.jobs`'s first
        #: draft put the same lookup on the claim path and a robustness double
        #: caught it immediately.
        self._trace_column: bool | None = None

    async def carries_trace(self, executor: Any) -> bool:
        """Whether the trace column is there, asked once per ledger and cached."""
        if self._trace_column is None:
            self._trace_column = await has_trace_column(executor, schema=self._schema)
        return self._trace_column

    @property
    def table(self) -> str:
        return self._table

    @property
    def holes_table(self) -> str:
        return self._holes

    def schema_sql(self) -> str:
        return schema_sql(self._schema)

    async def seed(
        self,
        executor: Any,
        *,
        chunk_limit: int,
        guards: str | None = None,
        rewrites: str | None = None,
        trace: str | None = None,
    ) -> None:
        """Create this pass's row if it is not already there. Idempotent.

        *guards* is the fact this pass's gate will publish, recorded here rather
        than at publication because that is the only way a migration can tell
        "nothing guards this column" from "something guards it and has not
        finished". `DO UPDATE` keeps it current when a redeploy changes the
        declaration, without disturbing a walk already in progress.

        *rewrites* is the column whose values this pass overwrites in place.
        It is recorded for the opposite reason: not so a migration can wait for
        it, but so a *downgrade* can refuse forever after. `COALESCE` on the
        update rather than a plain overwrite, because a redeploy that drops the
        declaration must not erase the record that the values were already
        changed -- forgetting is the failure mode here, not staleness.

        It is also written to the append-only `pass_rewrites` table, and that
        copy is the one a downgrade actually depends on. The ledger row is
        working state and a plausible future purge job would delete it; the
        record is not, and deleting it is refused by the database. Written
        *before* the ledger row so a crash between the two leaves the safe
        residue -- a record with no pass reads as "these values were changed",
        which is true, where the reverse would read as "they were not".

        *trace* is the traceparent of the drive running this shift, and the
        rule for it is **capture, never mint**.

        `COALESCE(existing, incoming)`, so the first drive that *has* a trace
        names this cycle's trace and every later shift runs under it -- the
        instance owns the trace, exactly as a workflow instance row does.
        Later drives do not replace it, or a backfill already three days in
        would be re-attributed to whoever last poked it.

        A pass driven only by `cron` has no originating request and stores SQL
        `NULL`, not a minted id and not `''`. Two of plan 01's own non-goals
        forbid minting one: wreath propagates context rather than generating
        spans, and it carries the sampling decision rather than re-deciding it
        -- a minted traceparent has to choose a flag, and neither choice is
        defensible. `-01` forces every backend in the path to retain a trace
        that may run for days; `-00` produces an id that is stored, printed by
        `wreath passes status`, and collected by nothing, which is an answer
        that looks like an answer. The empty string is refused for the reason
        `enqueue` refuses it: `WHERE trace_context IS NOT NULL` would then match
        every untraced pass.

        The trace is re-captured at the cycle boundary rather than carried over
        -- see `begin_cycle` -- so a recurring pass's trace lives one cycle
        instead of the process's lifetime.
        """
        if rewrites is not None:
            await executor.execute(
                f"INSERT INTO {self._rewrites} (fact, pass_name, tenant) "
                "VALUES ($1, $2, $3) ON CONFLICT (fact, pass_name, tenant) DO NOTHING",
                rewrites,
                self._name,
                self._tenant,
            )
        if await self.carries_trace(executor):
            await executor.execute(
                f"INSERT INTO {self._table} "
                "(name, tenant, phase, chunk_limit, started_at, guards, rewrites, "
                "trace_context) "
                "VALUES ($1, $2, 'walking', $3, clock_timestamp(), $4, $5, $6) "
                "ON CONFLICT (name, tenant) DO UPDATE SET guards = EXCLUDED.guards, "
                "rewrites = COALESCE(EXCLUDED.rewrites, "
                f"{self._table}.rewrites), trace_context = COALESCE("
                f"{self._table}.trace_context, EXCLUDED.trace_context)",
                self._name,
                self._tenant,
                int(chunk_limit),
                guards,
                rewrites,
                trace,
            )
            return
        await executor.execute(
            f"INSERT INTO {self._table} "
            "(name, tenant, phase, chunk_limit, started_at, guards, rewrites) "
            "VALUES ($1, $2, 'walking', $3, clock_timestamp(), $4, $5) "
            "ON CONFLICT (name, tenant) DO UPDATE SET guards = EXCLUDED.guards, "
            "rewrites = COALESCE(EXCLUDED.rewrites, "
            f"{self._table}.rewrites)",
            self._name,
            self._tenant,
            int(chunk_limit),
            guards,
            rewrites,
        )

    async def read(self, executor: Any) -> LedgerRow | None:
        # Selected only where the column exists, so a build newer than its
        # schema walks untraced instead of failing on an unknown column.
        trace = ", trace_context" if await self.carries_trace(executor) else ""
        record = await executor.fetchrow(
            f"SELECT {_COLUMNS}, clock_timestamp() AS now, "
            f"(SELECT count(*) FROM {self._holes} h WHERE h.name = p.name "
            f"AND h.tenant = p.tenant) AS holes_open{trace} "
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
        return await self._move_cursor(executor, expected=expected, cursor=cursor, count=True)

    async def skip_to(self, executor: Any, *, expected: Any, cursor: Any) -> bool:
        """Move the cursor past a hole without counting the chunk as done.

        `units_done` deliberately does not move: a skipped chunk is not a unit
        of work completed, and letting it count would make the percentage claim
        progress the pass did not make.
        """
        return await self._move_cursor(executor, expected=expected, cursor=cursor, count=False)

    async def _move_cursor(self, executor: Any, *, expected: Any, cursor: Any, count: bool) -> bool:
        update = (
            "cursor = $3::jsonb, units_done = units_done + 1, "
            "last_advance = clock_timestamp(), last_error = NULL"
            if count
            else "cursor = $3::jsonb, last_advance = clock_timestamp()"
        )
        tag = await executor.execute(
            f"UPDATE {self._table} SET {update} WHERE name = $1 AND tenant = $2 "
            "AND cursor IS NOT DISTINCT FROM $4::jsonb",
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

        `now()` rather than `clock_timestamp()` on purpose: it is stable for
        the whole transaction, so the rollover test cannot disagree with itself
        between the `CASE` arms.
        """
        roll = (
            "window_started IS NULL OR now() - window_started > make_interval(secs => $4::float8)"
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
            f"UPDATE {self._table} SET keyspace_from = $3::jsonb WHERE name = $1 AND tenant = $2",
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
            f"UPDATE {self._table} SET phase = $4 WHERE name = $1 AND tenant = $2 AND phase = $3",
            self._name,
            self._tenant,
            expected,
            phase,
        )
        return _affected(tag) == 1

    async def block(self, executor: Any, *, error: str, phase: str = BLOCKED) -> None:
        """Stop the pass, and say why in the row itself.

        *phase* distinguishes the two ways a pass stops: `blocked` at a chunk
        it gave up on, which an operator clears by retrying it, and
        `unverified` at a verification that answered no, which is not
        retryable at all.
        """
        await executor.execute(
            f"UPDATE {self._table} SET phase = $3, last_error = $4 WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            phase,
            error[:2000],
        )

    async def unblock(self, executor: Any) -> bool:
        """Return a pass stopped at a hole to walking. Refuses an unverified one.

        Without this a halted pass is stopped forever: `halt` parks the cursor
        *before* the hole and sets `blocked`, and every later shift sees a
        phase that is not `walking` and declines to run -- so nothing ever
        re-attempts the chunk, the hole is never cleared, and the terminal gate
        it bars can never be reached. Clearing a hole has to be able to restart
        the pass, or `halt` is not a policy but a trap.

        `unverified` is deliberately not matched: a verification that answered
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

    async def begin_cycle(self, executor: Any, *, trace: str | None = None) -> bool:
        """Rewind a recurring pass to the start of a fresh cycle.

        A recurring pass has no completion; a *cycle* completes, the cursor
        returns to the beginning of the domain, and the frontier is re-derived.
        Rows that expired behind the cursor while the last cycle ran are found by
        this one, which is the property that makes a re-derived frontier sound
        where a fixed ceiling would need the key to be monotone.

        The trace is **replaced**, not carried over, and that is the retention
        bound on this whole feature. A recurring pass runs for the life of the
        deployment, so keeping the first cycle's traceparent would produce a
        trace that never ends -- which no backend assembles, and which a
        forensic tool would report as one causal chain covering months. The
        cycle is the instance, so the cycle boundary is where the trace
        restarts: this cycle belongs to the drive that began it, or to nothing.
        """
        if await self.carries_trace(executor):
            tag = await executor.execute(
                f"UPDATE {self._table} SET cursor = NULL, phase = 'walking', "
                "cycle_started = clock_timestamp(), trace_context = $3 "
                "WHERE name = $1 AND tenant = $2 "
                "AND phase IN ('walking', 'done', 'verified')",
                self._name,
                self._tenant,
                trace,
            )
            return _affected(tag) == 1
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
        `FOR UPDATE` so two workers serialise, and the removal commits with the
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
            f"DELETE FROM {self._holes} WHERE name = $1 AND tenant = $2 AND cursor_to = $3::jsonb",
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


@dataclass(frozen=True, slots=True)
class PendingFact:
    """A fact some pass claims and has not yet established."""

    name: str
    tenant: str
    fact: str
    phase: str
    holes_open: int


async def all_pending_facts(executor: Any, *, schema: str) -> list[PendingFact]:
    """Every fact currently claimed and unpublished, for an operator's overview.

    Separate from `pending_facts` rather than an empty-filter special case:
    that one is asked "are *these* columns safe" and an empty candidate list
    honestly means "nothing to check", so making it mean "everything" would put
    the two readings one typo apart.
    """
    table = table_name(schema)
    holes = holes_table_name(schema)
    records = await executor.fetch(
        f"SELECT name, tenant, guards, phase, "
        f"(SELECT count(*) FROM {holes} h WHERE h.name = p.name "
        f"AND h.tenant = p.tenant) AS holes_open "
        f"FROM {table} p WHERE verified_at IS NULL AND guards IS NOT NULL "
        "ORDER BY name, tenant"
    )
    return [_pending_from_record(record) for record in records or ()]


def _pending_from_record(record: Any) -> PendingFact:
    field = _reader(record)
    return PendingFact(
        name=field("name", 0),
        tenant=field("tenant", 1, ""),
        fact=field("guards", 2, ""),
        phase=field("phase", 3, ""),
        holes_open=int(field("holes_open", 4) or 0),
    )


async def pending_facts(
    executor: Any, *, schema: str, facts: tuple[str, ...] = ()
) -> list[PendingFact]:
    """Facts claimed by a pass that has not published them.

    The inverse of `published_facts`, and the half a migration actually
    needs: it is asking whether it may narrow a column, and the dangerous answer
    is not "no pass ever published this" but "a pass is *still working on it*".
    Restricting to *facts* keeps the read proportional to the migration rather
    than to the ledger.
    """
    if not facts:
        return []
    table = table_name(schema)
    holes = holes_table_name(schema)
    records = await executor.fetch(
        f"SELECT name, tenant, guards, phase, "
        f"(SELECT count(*) FROM {holes} h WHERE h.name = p.name "
        f"AND h.tenant = p.tenant) AS holes_open "
        f"FROM {table} p WHERE verified_at IS NULL AND guards IN ({_placeholders(facts)}) "
        "ORDER BY name, tenant",
        *facts,
    )
    return [_pending_from_record(record) for record in records or ()]


@dataclass(frozen=True, slots=True)
class RewrittenColumn:
    """A column whose values a pass has overwritten, or is overwriting."""

    name: str
    tenant: str
    fact: str
    phase: str
    finished: bool
    #: `False` when the append-only record survives but the ledger row that
    #: should sit beside it is gone. The refusal is the same either way -- the
    #: values were changed and that cannot become false -- but an operator
    #: seeing this needs to know their ledger has been tidied, because the next
    #: thing they will do is go looking for a pass that is not there.
    ledger_row_present: bool = True


async def rewritten_columns(
    executor: Any, *, schema: str, facts: tuple[str, ...] = ()
) -> list[RewrittenColumn]:
    """Passes that have re-encoded any of *facts*, finished or not.

    The reader a downgrade needs, and the one place in this module that asks a
    question **without** filtering on `verified_at`. Every other reader here
    is asking "is this settled yet?", where finishing is the good answer. This
    one is asking "have the values on disk already been changed?", where
    finishing is the *worse* answer: a half-converted column at least still
    holds some originals, while a completed re-encode holds none at all.

    Reads the **union** of the ledger and the append-only `pass_rewrites`
    record, and that union is the whole protection. A naive "no row, no hazard"
    cannot tell a column that was never re-encoded from one whose ledger row was
    deleted, because both are the absence of a row -- and getting that wrong in
    the safe direction refuses every downgrade forever, while getting it wrong
    in the unsafe direction is the silent data loss this exists to prevent. The
    record breaks the tie: it is written when the pass is seeded, it is never
    updated, and the database refuses to delete it.

    Restricted to *facts* for the same reason as `pending_facts` -- the
    read stays proportional to the migration rather than to the ledger -- and an
    empty tuple honestly means "nothing to check".
    """
    if not facts:
        return []
    table = table_name(schema)
    rewrites = rewrites_table_name(schema)
    marks = _placeholders(facts)
    records = await executor.fetch(
        "SELECT r.pass_name AS name, r.tenant, r.fact, "
        "COALESCE(p.phase, '') AS phase, (p.name IS NOT NULL) AS ledger_row_present "
        f"FROM {rewrites} r "
        f"LEFT JOIN {table} p ON p.name = r.pass_name AND p.tenant = r.tenant "
        f"WHERE r.fact IN ({marks}) "
        "UNION "
        # The ledger's own column as well, so a pass seeded before this table
        # existed is still found. Those rows have no record behind them and a
        # purge would take them, which is exactly the gap being closed -- but
        # refusing on a fact only the ledger knows is strictly better than not
        # refusing at all.
        "SELECT p.name, p.tenant, p.rewrites AS fact, p.phase, true "
        f"FROM {table} p WHERE p.rewrites IN ({marks}) "
        "ORDER BY name, tenant",
        *facts,
    )
    # `UNION` already collapses the case where both sources agree. This keeps
    # the richer reading if a driver ever hands back both spellings anyway:
    # a row the ledger still has beats one it does not, because the phase is
    # only knowable from the ledger.
    found: dict[tuple[str, str, str], RewrittenColumn] = {}
    for record in records or ():
        field = _reader(record)
        phase = str(field("phase", 3, "") or "")
        entry = RewrittenColumn(
            name=field("name", 0),
            tenant=field("tenant", 1, ""),
            fact=field("fact", 2, ""),
            phase=phase,
            finished=phase == "done",
            ledger_row_present=bool(field("ledger_row_present", 4, True)),
        )
        key = (entry.name, entry.tenant, entry.fact)
        if key not in found or entry.ledger_row_present:
            found[key] = entry
    return [found[key] for key in sorted(found)]


async def read_all(executor: Any, *, schema: str, name: str | None = None) -> list[LedgerRow]:
    """Every pass in one schema's ledger, or one of them by name."""
    table = table_name(schema)
    holes = holes_table_name(schema)
    # Probed per call rather than cached: this is the CLI's read, issued once
    # per invocation against a database it has just connected to, so there is no
    # steady state to keep a lookup out of.
    trace = ", trace_context" if await has_trace_column(executor, schema=schema) else ""
    extra = (
        f"clock_timestamp() AS now, (SELECT count(*) FROM {holes} h "
        f"WHERE h.name = p.name AND h.tenant = p.tenant) AS holes_open{trace}"
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


async def read_holes(executor: Any, *, schema: str, name: str | None = None) -> list[Hole]:
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
    """The row count out of a command tag such as `UPDATE 1`.

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
    "has_trace_column",
    "hole_from_record",
    "holes_table_name",
    "read_all",
    "read_holes",
    "row_from_record",
    "schema_sql",
    "table_name",
]
