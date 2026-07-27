"""Backfills, rollups, reindexes: a durable, resumable, paced walk over a big table.

Sooner or later a table gets big enough that you cannot change all of it at once.
Ten million rows need a new column filled in. A month of raw events needs folding
into a daily summary. Every row needs re-encrypting under a new key. The shape is
always the same, and so is the script people write for it: a `while True` loop
with `OFFSET`, one transaction around the whole thing, a `sleep` somebody
tuned once on a laptop, a `print` for progress, and an `except: continue`
that turns a failed chunk into a silent hole. It runs in a terminal on a jump
host and it is nobody's job to watch it.

A `ChunkedPass` is that script, written once and correctly:

```python
purge_replays = ChunkedPass(
    "idempotency_purge",
    over=Table("wreath_idempotency"),
    units=Rows(
        key=(
            Key("expires", "timestamptz", indexed=True),
            Key("key", "text", unique=True),
        ),
        limit=1_000,
        within="2s",
    ),
    frontier=Sealed(),
    work=Purge(),
    pace=DutyCycle(0.25),
)

jobs.drive(purge_replays, cron="*/5 * * * *")
```

What that buys, and every item is a specific thing the hand-rolled loop gets
wrong:

* **Keyset ranges, never `OFFSET`.** The walk stays the same speed at row nine
  million as at row nine. See `wreath._passes.keyset` for the arithmetic.
* **One transaction per chunk, with the cursor advanced inside it.** The position
  and the data are two rows in one database, so they commit together; a chunk is
  either wholly applied or wholly not, and a crash resumes where it stopped.
* **A compare-and-swap as each chunk's first statement**, which makes a chunk
  exactly-once with respect to everything inside the database, with no
  cooperation from the caller and no lock to hold.
* **Bounded shifts.** Work is done in stretches shorter than the job lease, so
  the runner never reclaims a handler that is still running, and a redeploy costs
  at most one chunk.
* **Pacing, from the first release.** A walk that goes as fast as it can is the
  one that takes the site down while its own dashboard stays green.
* **Refusals where you declared it**, not at three in the morning: a key that
  cannot be proven unique, a leading key column with no index, a fixed ceiling
  over a key that is not assigned in order, a shift longer than the lease.

The guide is [Chunked passes](../guides/chunked-passes.md).

**What a pass deliberately does not know.** It has no opinion on what a chunk
*means*, and no clock of its own -- scheduling belongs to
`wreath.jobs`, which already deduplicates a cron tick fleet-wide. Its whole
vocabulary is "a half-open range over one ordered domain"; row counts are
reported and never structural, which is what keeps the door open for a range
source that counts no rows at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ._passes import driver as _driver
from ._passes import duration as _duration
from ._passes import gate as _gate
from ._passes import keyset as _keyset
from ._passes import ledger as _ledger
from ._passes import progress as _progress
from ._passes.buckets import Buckets
from ._passes.driver import Chunk, ShiftResult
from ._passes.gate import Constraint, Gate, NoRowsMatch, Reconcile, Verification
from ._passes.keyset import Key, PassDeclarationError
from ._passes.ledger import Hole, PendingFact, PublishedFact
from ._passes.pace import DutyCycle
from ._passes.progress import Denominator, Estimated, Exact, Keyspace, Progress


def _seconds(value: Any, *, what: str, allow_zero: bool = False) -> float:
    """Read `"2s"`, `"250ms"`, `"5m"` or a plain number of seconds."""
    return _duration.seconds(value, what=what, allow_zero=allow_zero)


# --- what to walk ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Table:
    """A table to walk that the ORM does not own.

    Wreath's own store tables are the first callers here -- the idempotency
    ledger, the rate-limit buckets, the session rows, the webhook inbox and
    outbox -- and none of them is a model. Naming the table directly is honest
    about that, and it means a legacy table nobody has mapped is still walkable.

    The facts the refusals need travel with the `Key`, not with the table,
    because that is where a reader is looking when they ask "is this key unique?".
    """

    name: str
    schema: str | None = None

    def __post_init__(self) -> None:
        for part in (self.name, *(() if self.schema is None else (self.schema,))):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
                raise PassDeclarationError(
                    f"table {part!r} must be a plain SQL identifier"
                )

    @property
    def sql(self) -> str:
        """This table as it is written in a statement, quoted when qualified.

        Both parts are checked as plain identifiers at construction, because the
        name is interpolated into statement text rather than bound -- a table
        name cannot be a parameter.
        """
        return self.name if self.schema is None else f'"{self.schema}"."{self.name}"'


@dataclass(frozen=True, slots=True)
class Rows:
    """The next chunk is the next *limit* rows after the cursor, by key.

    All key columns must be ordered the same way: a row comparison has no
    mixed-direction form, and expanding one into ORs costs the single index scan
    that makes a keyset walk cheap.

    The chunk transaction sets its own `statement_timeout` from *within*, so a
    chunk that hits a lock wait dies as a chunk failure rather than as a
    transaction nobody notices.

    Args:
        key: one column or an ordered tuple. Model columns, or `Key` declarations.
        limit: how many rows one chunk covers.
        within: the chunk's time budget, as a duration string or seconds.
    """

    key: Any
    limit: int = 1000
    within: Any = "5s"
    keys: tuple[Key, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 1:
            raise PassDeclarationError(f"Rows limit must be a positive int; got {self.limit!r}")
        object.__setattr__(self, "keys", _keyset.normalise(self.key))
        object.__setattr__(self, "within", _seconds(self.within, what="Rows within"))

    # -- the range source protocol -------------------------------------------
    #
    # A ChunkedPass calls these; a caller declares Rows(...) and never does.

    def refuse(self, *, table: str) -> None:
        """Refuse a key a keyset walk cannot follow correctly.

        Called once, when the pass is declared. A keyset walk needs a key that
        is unique, indexed on its leading column, and ordered one way
        throughout; each of those is a silent wrong answer rather than an error
        if it is missing. `wreath._passes.keyset` carries the arithmetic
        for why.
        """
        _keyset.refuse_unsound_key(self.keys, table=table)

    def chunk_where(
        self,
        binds: Any,
        *,
        cursor_from: tuple[Any, ...] | None,
        cursor_to: tuple[Any, ...],
        frontier: str | None,
    ) -> str:
        """The `WHERE` clause selecting exactly one chunk's rows.

        Open at the bottom and closed at the top, so consecutive chunks neither
        overlap nor leave a gap, and emitted as a single row comparison rather
        than the equivalent chain of ORs -- only the row comparison is reliably
        one index scan.

        Args:
            binds: the statement's bind collector. Values are appended, not interpolated.
            cursor_from: the exclusive lower key, or `None` for the first chunk.
            cursor_to: the inclusive upper key.
            frontier: the frontier predicate to AND in, when there is one.
        """
        return _driver.chunk_predicate(
            self.keys, binds, cursor_from=cursor_from, cursor_to=cursor_to, frontier=frontier
        )

    def reproduce(
        self,
        *,
        table: str,
        cursor_from: tuple[Any, ...] | None,
        cursor_to: tuple[Any, ...],
    ) -> str:
        """The statement an operator pastes into `psql` to see the real error.

        Recorded on a hole, with the keys as literals rather than binds, so
        diagnosing a dead-lettered chunk needs nothing but the ledger row.
        """
        return _driver.reproduce_predicate(
            self.keys, table=table, cursor_from=cursor_from, cursor_to=cursor_to
        )

    async def next_range(
        self,
        executor: Any,
        *,
        walk: Any,
        cursor: tuple[Any, ...] | None,
        ceiling: Any,
        frontier_sql: Any,
    ) -> tuple[tuple[Any, ...] | None, tuple[Any, ...]] | None:
        """`(start, end)` for the next chunk, or `None` when the walk is done.

        Two probes, and the second one is the point. The first asks for the
        *limit*-th key past the cursor; when fewer than a full chunk remain it
        answers nothing -- which is **not** the end of the walk, because a chunk
        is short whenever rows in its range were deleted or filtered out and the
        next key can sit well beyond it. So the second probe walks the same index
        from the far end. Completion is a probe, never an inference from a count.

        The start is the cursor itself: a keyset chunk is open at the bottom, so
        the row the last chunk finished on is not seen twice.
        """
        end = await _driver.fetch_key(
            executor, table=walk.table, keys=self.keys, cursor=cursor,
            frontier_sql=frontier_sql, offset=self.limit - 1, reverse=False,
        )
        if end is None:
            end = await _driver.fetch_key(
                executor, table=walk.table, keys=self.keys, cursor=cursor,
                frontier_sql=frontier_sql, offset=None, reverse=True,
            )
        return None if end is None else (cursor, end)


# --- how far to walk ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ceiling:
    """A fixed frontier, captured once, so "runs to completion" means something.

    Without a ceiling, a table written to faster than the walk moves never
    terminates: the pass reports ninety-six percent forever while doing real
    work. Rows written past the ceiling are not this pass's problem, and the
    reason they are not is the precondition every pass is declared under:

    > **A pass converts the past. The application writes the future in the
    > shape the pass is converting to.**

    That is the primitive's rule rather than the caller's choice, which is why
    it is written here and not in one caller's guide.
    """

    monotone: str | None = None

    #: A fixed ceiling is captured once, so the walk it bounds can finish.
    recurring = False

    @classmethod
    def at_launch(cls, *, monotone: str | None = None) -> Ceiling:
        """Capture the largest key in the table when the walk starts.

        Sound only when a row inserted afterwards cannot land beneath the
        ceiling. An identity primary key or a `now()` default gives that;
        `gen_random_uuid()` does not, and a row that lands behind the cursor is
        one the pass will never see. So this is refused on a key the declaration
        cannot prove is assigned in order, and *monotone* is the way past it --
        a sentence a reviewer reads, not a flag, because ULIDs and UUIDv7 really
        are monotone and nothing in a column declaration can see that when the
        application assigns the value.
        """
        if monotone is not None and not str(monotone).strip():
            raise PassDeclarationError(
                "Ceiling.at_launch(monotone=...) needs a reason, not an empty string"
            )
        return cls(monotone=monotone)

    def refuse(self, keys: tuple[Key, ...], *, table: str) -> None:
        """Refuse a fixed ceiling over a key not assigned in increasing order.

        Called once, when the pass is declared. A row that can land beneath a
        captured ceiling is a row behind the cursor, which this pass will never
        see; `monotone=` is the written escape for a key the declaration
        cannot prove but the caller knows.
        """
        _keyset.refuse_unmonotone_key(keys, table=table, reason=self.monotone)

    async def derive(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> Any:
        """Capture the ceiling: the largest key in the table, encoded for the ledger.

        One index descent -- `ORDER BY key DESC LIMIT 1` against the same index
        the walk uses. Runs once, when the walk starts, and the value is durable
        from then on. `None` when the table is empty, which `predicate`
        turns into a walk with nothing to do.
        """
        order = _keyset.order_clause(keys, reverse=True)
        projection = ", ".join(item.name for item in keys)
        record = await executor.fetchrow(
            f"SELECT {projection} FROM {table} ORDER BY {order} LIMIT 1"
        )
        if record is None:
            return None
        values = tuple(
            _driver._field(record, item.name, index) for index, item in enumerate(keys)
        )
        return _keyset.encode_cursor(keys, values)

    def predicate(self, keys: tuple[Key, ...], ceiling: Any, binds: Any) -> str:
        """The `WHERE` fragment meaning "not past the ceiling".

        A row comparison against the captured key, in whichever direction the
        key is ordered. `FALSE` when no ceiling was captured, so an empty
        table's walk completes immediately.
        """
        if ceiling is None:
            # The table was empty when the ceiling was captured, so the walk has
            # nothing to do and says so rather than scanning to find out.
            return "FALSE"
        decoded = _keyset.decode_cursor(keys, ceiling)
        assert decoded is not None
        return _keyset.row_comparison(
            keys, _keyset.upto_operator(keys), binds.add_all(decoded)
        )


@dataclass(frozen=True, slots=True)
class Sealed:
    """A frontier re-derived every cycle: everything the clock has already passed.

    A recurring pass has no completion -- a *cycle* completes, the frontier moves,
    and the next cycle starts from the beginning of the domain. That rewind is
    what makes this sound where a fixed ceiling would need the key to be assigned
    in order: a row that landed behind the cursor while a cycle ran is found by
    the next one.

    *after* holds the frontier back from the present, for a caller that must not
    touch a row until it has settled. The default of zero -- everything already
    past -- is what an expiry purge wants.

    The leading key column must be a timestamp, because that is the domain the
    database clock is measured in. Stage four of the design extends this same
    object to bucketed range sources; the arithmetic does not change.
    """

    after: Any = 0.0

    #: A re-derived frontier means a cycle completes and the pass does not.
    recurring = True

    def __post_init__(self) -> None:
        after = 0.0 if self.after is None else self.after
        object.__setattr__(
            self, "after", _seconds(after, what="Sealed after", allow_zero=True)
        )

    def refuse(self, keys: tuple[Key, ...], *, table: str) -> None:
        """Refuse a clock-derived frontier over a key that is not a timestamp.

        Called once, when the pass is declared. `clock_timestamp()` produces a
        timestamp, so the leading key column has to be measured in the same
        domain for the comparison to mean anything.
        """
        _keyset.refuse_unclocked_key(keys, table=table)

    async def derive(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> Any:
        """Read the frontier for this cycle: `clock_timestamp() - after`.

        The database's clock, not the caller's, so workers on disagreeing wall
        clocks agree on where a cycle stops. Read once per cycle and bound for
        the whole of it -- see the comment below for why it cannot be inline.
        """
        # The frontier is read once per cycle and bound for the whole of it. An
        # inline clock_timestamp() would move the finish line as the walk ran,
        # so a busy table's cycle could never end.
        value = await executor.fetchval(
            "SELECT clock_timestamp() - make_interval(secs => $1::float8)", float(self.after)
        )
        return _keyset.encode_cursor(keys[:1], (value,))

    def predicate(self, keys: tuple[Key, ...], ceiling: Any, binds: Any) -> str:
        """The `WHERE` fragment meaning "the clock has already passed this row".

        A comparison on the leading key column alone, not a row comparison:
        the frontier is a point on the clock, and later key columns exist to
        break ties within it rather than to bound it.
        """
        if ceiling is None:  # pragma: no cover - the clock always answers
            return "FALSE"
        decoded = _keyset.decode_cursor(keys[:1], ceiling)
        assert decoded is not None
        operator = ">" if keys[0].descending else "<"
        return f"{keys[0].name} {operator} {binds.add(decoded[0])}"


# --- what the work is --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sql:
    """A SQL fragment and its bind values, for a table the ORM does not own.

    Placeholders are written `?` and renumbered when the fragment is spliced
    into a statement, because a fragment cannot know which `$n` it will land
    on:

    ```python
    Purge(where=Sql("state = ANY(?)", [["delivered", "failed"]]))
    ```

    """

    text: str
    values: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))


def _render_filter(where: Any, binds: Any, *, model: Any, alias: str) -> str | None:
    if where is None:
        return None
    if isinstance(where, Sql):
        return binds.splice(where.text, where.values)
    if isinstance(where, str):
        return where
    from .orm.compiler import SqlBuilder, check_predicate_columns, render_predicate

    if model is None:
        raise PassDeclarationError(
            "a model predicate needs over=<Model>; for a table the ORM does not "
            "own, write the fragment as Sql('...', [...])"
        )
    check_predicate_columns(model, where)
    builder = SqlBuilder()
    # Seed the builder with the binds already placed so its placeholders continue
    # the same numbering rather than restarting at $1.
    builder.values.extend(binds.values)
    render_predicate(where, builder, alias, {})
    binds.values.extend(builder.values[len(binds.values) :])
    return builder.sql()


def _column_name(item: Any) -> str:
    column = getattr(item, "column", None)
    if column is not None and hasattr(column, "database_name"):
        return str(column.database_name)
    if isinstance(item, str):
        return item
    raise PassDeclarationError(f"expected a column or a column name; got {item!r}")


class _Work:
    """What the pass does to one chunk. Subclasses are the declared shapes.

    Every shape here re-runs as a no-op, which is what lets the pass promise
    exactly-once *inside the database* without asking the caller for anything.
    `Apply` is the exception, and it asks in writing.
    """

    __slots__ = ()

    @property
    def writes(self) -> tuple[str, ...]:
        """Columns this work assigns. A pass refuses to walk by one of them."""
        return ()

    async def apply(self, tx: Any, chunk: Chunk, binds: Any) -> int:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Purge(_Work):
    """Delete every row in the chunk. Idempotent by nature: a delete re-run is a no-op."""

    where: Any = None

    async def apply(self, tx: Any, chunk: Chunk, binds: Any) -> int:
        """Delete one chunk's rows inside the chunk transaction.

        Args:
            tx: the chunk's transaction, which the cursor also advances in.
            chunk: the range being walked.
            binds: the statement's bind collector.

        Returns:
            Rows deleted, read from the command tag, for the report.
        """
        extra = _render_filter(self.where, binds, model=chunk.model, alias=chunk.alias)
        clause = chunk.where if extra is None else f"{chunk.where} AND ({extra})"
        tag = await tx.execute(f"DELETE FROM {chunk.table} WHERE {clause}", *binds.args)
        return _rows_in(tag)


@dataclass(frozen=True, slots=True)
class Rewrite(_Work):
    """Update every row in the chunk that still needs it.

    Re-running the chunk is a no-op because *where* excludes the rows already
    converted -- the second run matches nothing. That same predicate is what a
    later stage's verification is deliberately *not* allowed to reuse.

    Args:
        set_: `column -> SQL expression`. Model columns or plain names, either way.
        where: which rows still need converting.
    """

    set_: Any
    where: Any = None

    def __post_init__(self) -> None:
        if not self.set_:
            raise PassDeclarationError("Rewrite needs at least one column to set")

    @property
    def writes(self) -> tuple[str, ...]:
        """The columns `set_` assigns.

        A pass refuses to walk by one of these: a key the work itself changes
        moves rows past the cursor, so they are processed twice or never.
        """
        return tuple(_column_name(column) for column in self.set_)

    async def apply(self, tx: Any, chunk: Chunk, binds: Any) -> int:
        """Update one chunk's rows inside the chunk transaction.

        On a re-run the count is zero, because *where* no longer matches the rows
        already converted -- which is the property that makes the shape safe to
        repeat.

        Returns:
            Rows actually updated, read from the command tag.
        """
        assignments = []
        for column, expression in self.set_.items():
            rendered = _render_filter(expression, binds, model=chunk.model, alias=chunk.alias)
            assignments.append(f"{_column_name(column)} = {rendered}")
        extra = _render_filter(self.where, binds, model=chunk.model, alias=chunk.alias)
        clause = chunk.where if extra is None else f"{chunk.where} AND ({extra})"
        tag = await tx.execute(
            f"UPDATE {chunk.table} SET {', '.join(assignments)} WHERE {clause}", *binds.args
        )
        return _rows_in(tag)


@dataclass(frozen=True, slots=True)
class Declared:
    """A written reason that a callback is safe to run twice.

    There is no default and no `strict=False`. Job delivery is at-least-once,
    so the question cannot be avoided -- it can only be answered, and being wrong
    on purpose should at least be legible to whoever reviews it:

    ```python
    Apply(reencrypt, idempotent=Declared(
        "re-wrapping a key is idempotent: the row records the wrapping key id "
        "and rows already carrying the new id are excluded by `where`"
    ))
    ```

    """

    reason: str

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise PassDeclarationError("Declared(...) needs a reason, not an empty string")


@dataclass(frozen=True, slots=True)
class Apply(_Work):
    """Hand each chunk to a callback, for work no declared shape covers.

    The callback is awaited as `callback(tx, chunk, binds)` *inside* the chunk
    transaction, so anything it writes commits with the cursor. It returns the
    number of rows it touched, for the report.

    `idempotent=` is required: see `Declared`.
    """

    callback: Any
    idempotent: Declared | None = None

    def __post_init__(self) -> None:
        if not callable(self.callback):
            raise PassDeclarationError("Apply needs an async callable")
        if not isinstance(self.idempotent, Declared):
            raise PassDeclarationError(
                "Apply(...) needs idempotent=Declared('why re-running this chunk "
                "is safe'). Job delivery is at-least-once, so a chunk can run "
                "twice; the pass cannot check an arbitrary callback, so it asks."
            )

    async def apply(self, tx: Any, chunk: Chunk, binds: Any) -> int:
        """Await the callback inside the chunk transaction.

        `None` and `False` count as zero, so a callback with nothing useful to
        report may return nothing. The number reaches the report only; it does
        not affect whether the chunk committed.

        Returns:
            Whatever the callback returned, as an int.
        """
        affected = await self.callback(tx, chunk, binds)
        return int(affected or 0)


def _rows_in(tag: Any) -> int:
    """The row count out of a command tag such as `DELETE 412`."""
    if isinstance(tag, int):
        return tag
    if not isinstance(tag, str):
        return 0
    parts = tag.rsplit(" ", 1)
    return int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0


# --- the pass ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PassStatus:
    """One pass's durable status, read straight out of the ledger.

    The ledger row is the durable status; a progress registry is live
    commentary. This is what the CLI, and anything else that has to *decide*
    something, reads -- an in-process registry that is bounded and TTL'd cannot
    answer a question about a pass that has been running for two hours.

    `progress` carries the percentage *with* its provenance, the trailing rate,
    and either an ETA or the reason there is not one. A pass emits no phase
    records and never appears in the Flight Recorder -- attribution is bound per
    request through a `ContextVar` and a supervised background task has no
    request -- so this row is the whole picture, deliberately.
    """

    name: str
    tenant: str
    phase: str
    cursor: Any
    ceiling: Any
    units_done: int
    rows_done: int
    chunk_limit: int
    paced_reason: str | None
    started_at: Any
    last_advance: Any
    cycle_started: Any
    driven_at: Any
    last_drive_error: str | None
    #: When the gate's verification last passed, and what it established. The
    #: durable half of the terminal gate: a later migration reads this rather
    #: than trusting a percentage.
    verified_at: Any
    verified_fact: str | None
    last_error: str | None
    progress: Progress
    #: Chunks given up on and not yet cleared. While this is non-zero the pass
    #: has walked past work it did not do.
    holes_open: int
    #: Units requeued to be walked out of order and not yet taken.
    pending: int
    #: The fact this pass's gate will publish, recorded when the row is
    #: seeded. Present *before* verification, which is the point: a migration
    #: has to tell an unguarded column from one still being converted.
    guards: str | None = None

    @property
    def state(self) -> str:
        """`walking` | `slow` | `stalled` | `blocked` | `done`."""
        return self.progress.state

    @property
    def gate_barred(self) -> bool:
        """Whether an irreversible terminal step may run for this pass.

        Skipping a chunk buys throughput and never the irreversible step, so a
        pass with an open hole is barred until the hole is cleared -- which is
        what `wreath passes retry` does. The terminal gate itself is a later
        stage; this is the fact it will read, and it is true as soon as there is
        a hole to read it from.
        """
        return self.holes_open > 0

    def as_dict(self) -> dict[str, Any]:
        """This status as JSON-safe primitives, for an API or the CLI.

        Timestamps become strings and the `Progress`
        record is flattened in alongside the rest, so the result is one flat
        object rather than a nested one. `gate_barred` and the progress fields
        are derived here, so a consumer of this dictionary never has to
        recompute what `gate_barred` means.
        """
        return {
            "name": self.name,
            "tenant": self.tenant,
            "phase": self.phase,
            "cursor": self.cursor,
            "ceiling": self.ceiling,
            "units_done": self.units_done,
            "rows_done": self.rows_done,
            "chunk_limit": self.chunk_limit,
            "paced_reason": self.paced_reason,
            "started_at": None if self.started_at is None else str(self.started_at),
            "last_advance": None if self.last_advance is None else str(self.last_advance),
            "cycle_started": None if self.cycle_started is None else str(self.cycle_started),
            "driven_at": None if self.driven_at is None else str(self.driven_at),
            "last_drive_error": self.last_drive_error,
            "guards": self.guards,
            "verified_at": None if self.verified_at is None else str(self.verified_at),
            "verified_fact": self.verified_fact,
            "last_error": self.last_error,
            "holes_open": self.holes_open,
            "pending": self.pending,
            "gate_barred": self.gate_barred,
            **self.progress.as_dict(),
        }


def _status_from(row: Any, keys: tuple[Key, ...] = ()) -> PassStatus:
    return PassStatus(
        name=row.name, tenant=row.tenant, phase=row.phase, cursor=row.cursor,
        ceiling=row.ceiling, units_done=row.units_done, rows_done=row.rows_done,
        chunk_limit=row.chunk_limit, paced_reason=row.paced_reason,
        started_at=row.started_at, last_advance=row.last_advance,
        cycle_started=row.cycle_started, driven_at=row.driven_at,
        last_drive_error=row.last_drive_error, verified_at=row.verified_at,
        verified_fact=row.verified_fact, last_error=row.last_error,
        progress=_progress.describe(row, keys, now=row.now),
        holes_open=row.holes_open,
        pending=len(row.pending or ()),
        guards=row.guards,
    )


class ChunkedPass:
    """A durable, resumable, paced walk over one table.

    Declared once, validated at declaration time, and driven by the job runner
    with `wreath.jobs.JobRunner.drive`. See the module docstring for what
    the machinery buys and the guide for how to reach for it.

    Args:
        name: this pass's identity in the ledger, and how the CLI names it.
        over: a model class, or a `Table` for a table the ORM does not own.
        units: where the next range comes from. `Rows` today.
        frontier: `Ceiling.at_launch()` for a pass that finishes, `Sealed` to recur.
        work: what to do to a chunk.
        gate: the terminal verification, and the fact it publishes when it passes.
        pace: how much of the machine the pass may be. There is no "off".
        progress: what the percentage is measured against. `Estimated()` by default.
        on_chunk_failure: `"halt"` to stop at a hole, `"skip"` to walk past it.
        chunk_retries: attempts a chunk gets before it is dead-lettered. At least 1.
        shift: how long one stretch of work runs. Shorter than the lease; `drive` checks.
        tenant: the ledger row's tenant, for a fleet that keeps them apart.
        schema: where the ledger table lives; match the job runner's.
        workload: must be `"write"`; a read pool opens read-only and a pass writes.
        rewrites: the column this pass overwrites in place, so a downgrade can refuse.
    """

    __slots__ = (
        "_alias", "_chunk_retries", "_frontier", "_gate", "_ledger", "_model",
        "_name", "_on_chunk_failure", "_pace", "_progress", "_rewrites",
        "_schema", "_shift", "_table", "_tenant", "_units", "_work", "_workload",
    )

    def __init__(
        self,
        name: str,
        *,
        over: Any,
        units: Rows | Buckets,
        frontier: Ceiling | Sealed,
        work: _Work,
        gate: Gate | None = None,
        pace: DutyCycle | None = None,
        progress: Denominator | None = None,
        on_chunk_failure: str = "halt",
        chunk_retries: int = 3,
        shift: Any = "10s",
        tenant: str = "",
        schema: str = "wreath",
        workload: str = "write",
        rewrites: str | None = None,
    ) -> None:
        if not name or len(name) > 200:
            raise PassDeclarationError("a pass name must be 1..200 characters")
        if not isinstance(units, (Rows, Buckets)):
            raise PassDeclarationError(
                f"units= must be a range source -- Rows(...) or Buckets(...); "
                f"got {units!r}"
            )
        if not isinstance(work, _Work):
            raise PassDeclarationError(
                f"work= must be Purge(...), Rewrite(...) or Apply(...); got {work!r}"
            )
        if workload != "write":
            # The read pools open with default_transaction_read_only, so a pass
            # on one fails loudly rather than half-working.
            raise PassDeclarationError(
                f"a pass writes, so it must use the write workload; got {workload!r}"
            )
        if on_chunk_failure not in ("halt", "skip"):
            raise PassDeclarationError(
                f"on_chunk_failure must be 'halt' or 'skip'; got {on_chunk_failure!r}. "
                "'halt' stops at the hole, so nothing after it runs and no "
                "terminal step can follow a walk that did not finish. 'skip' "
                "moves past it for throughput and bars the terminal gate until "
                "the hole is cleared."
            )
        if not isinstance(chunk_retries, int) or isinstance(chunk_retries, bool):
            raise PassDeclarationError(f"chunk_retries must be an int; got {chunk_retries!r}")
        if chunk_retries < 1:
            raise PassDeclarationError(
                f"chunk_retries must be at least 1; got {chunk_retries}. Zero "
                "attempts would dead-letter every chunk without running it."
            )
        if gate is not None and not isinstance(gate, Gate):
            raise PassDeclarationError(
                f"gate= must be a Gate(...); got {gate!r}"
            )
        self._name = name
        self._tenant = tenant
        self._schema = schema
        self._units = units
        self._frontier = frontier
        self._work = work
        self._gate = gate
        self._rewrites = rewrites
        self._pace = pace if pace is not None else DutyCycle()
        self._progress = progress if progress is not None else Estimated()
        self._on_chunk_failure = on_chunk_failure
        self._chunk_retries = chunk_retries
        self._workload = workload
        self._shift = _seconds(shift, what="shift")
        self._model, self._table, self._alias = _resolve_source(over)

        # Each range source owns its own refusals. A keyset walk needs a unique,
        # indexed, single-direction key because its boundary is a row that
        # exists; a bucketed one needs a timestamp and an index and *not*
        # uniqueness, because its boundary is a value the calendar produced.
        # Asking one set of questions for both would be a rule nobody could
        # explain -- and it is the assumption stage one was written to avoid.
        units.refuse(table=self._table)
        if not isinstance(self._progress, Denominator):
            raise PassDeclarationError(
                "progress= must be Estimated(), Exact() or Keyspace(); "
                f"got {progress!r}"
            )
        self._progress.refuse(units.keys, table=self._table)
        refuse = getattr(frontier, "refuse", None)
        if refuse is None:
            raise PassDeclarationError(
                f"frontier= must be Ceiling.at_launch() or Sealed(...); got {frontier!r}"
            )
        refuse(units.keys, table=self._table)
        if units.within >= self._shift:
            raise PassDeclarationError(
                f"a chunk's budget must fit inside a shift: within={units.within:g}s "
                f"is not shorter than shift={self._shift:g}s. The chain is "
                "statement_timeout < within < shift < lease < command_timeout."
            )
        shared = sorted(set(work.writes) & {item.name for item in units.keys})
        if shared:
            # A key the work itself changes moves rows behind the cursor (seen
            # twice) or ahead of it (seen never). The general rule cannot be
            # checked, but the case that actually happens can be.
            raise PassDeclarationError(
                f"pass {name!r} walks by ({', '.join(item.name for item in units.keys)}) "
                f"and its work writes {', '.join(shared)}. A key the work changes "
                "moves rows past the cursor, so they are processed twice or never."
            )
        if gate is not None:
            _gate.refuse_reused_predicate(gate, work)
            if gate.scope == "pass" and frontier.recurring:
                raise PassDeclarationError(
                    f"pass {name!r} recurs, so it has no completion for a "
                    "whole-pass gate to fire at -- a cycle completes and the "
                    "frontier moves on. Use Gate(scope='unit') to verify each "
                    "range as the walk passes it, or a fixed Ceiling.at_launch()."
                )
        self._ledger = _ledger.Ledger(schema=schema, name=name, tenant=tenant)

    # -- what a driver reads -------------------------------------------------
    #
    # A declaration is read-only once built. Every refusal has already run, so
    # anything reading these -- the driver, the CLI, a test -- is looking at a
    # shape that was validated at declaration time and cannot drift afterwards.

    @property
    def name(self) -> str:
        """This pass's identity: its ledger row's key, and how the CLI names it."""
        return self._name

    @property
    def tenant(self) -> str:
        """The ledger row's tenant. `""` for a fleet that does not separate them."""
        return self._tenant

    @property
    def schema(self) -> str:
        """The schema holding the ledger table. Must match the job runner's."""
        return self._schema

    @property
    def table(self) -> str:
        """The table being walked, as it is written in a statement.

        Already quoted and qualified where that was resolvable. A model on a
        logical (central or tenant) schema resolves to a bare quoted name and
        reaches its schema through `search_path`, because the schema mode is
        not known when a pass is declared.
        """
        return self._table

    @property
    def alias(self) -> str:
        """The bare table name, used to qualify columns in a model predicate."""
        return self._alias

    @property
    def model(self) -> Any:
        """The model class walked, or `None` when `over=` named a `Table`.

        `None` is what makes a model predicate impossible for that pass: there
        is no class to check the columns against, so the filter has to be an
        explicit `Sql` fragment.
        """
        return self._model

    @property
    def units(self) -> Rows | Buckets:
        """Where the next range comes from."""
        return self._units

    @property
    def frontier(self) -> Ceiling | Sealed:
        """How far the walk goes: a fixed ceiling, or one re-derived each cycle."""
        return self._frontier

    @property
    def work(self) -> _Work:
        """What is done to each chunk."""
        return self._work

    @property
    def gate(self) -> Gate | None:
        """The terminal verification, or `None` when this pass publishes no fact."""
        return self._gate

    @property
    def guards(self) -> str | None:
        """The fact this pass claims, or `None` if its gate publishes none.

        Seeded into the ledger so a migration can ask what is still in flight.
        """
        return None if self._gate is None else self._gate.publishes

    @property
    def rewrites(self) -> str | None:
        """The column whose values this pass overwrites in place, if any.

        Seeded into the ledger so a *downgrade* can refuse forever after. A pass
        that only reads, or that fills a column it added, leaves this `None`.
        """
        return self._rewrites

    @property
    def pace(self) -> DutyCycle:
        """How much of the machine this pass may be. Never absent -- there is no "off"."""
        return self._pace

    @property
    def progress(self) -> Denominator:
        """What the reported percentage is measured against.

        `Estimated` unless another was declared,
        because a denominator that costs a count of the whole table is not a
        sensible default for a walk that exists because the table is large.
        """
        return self._progress

    @property
    def on_chunk_failure(self) -> str:
        """`"halt"` or `"skip"`.

        `halt` parks the cursor before the failing chunk and blocks the pass,
        so nothing after the hole runs. `skip` walks past it and bars the
        terminal gate until the hole is cleared. Both are recoverable only
        through `retry`.
        """
        return self._on_chunk_failure

    @property
    def chunk_retries(self) -> int:
        """Attempts a chunk gets before it becomes a hole. At least 1."""
        return self._chunk_retries

    @property
    def shift(self) -> float:
        """Seconds of work per `run_shift`, as a float.

        Declared as a duration string or a number and normalised here. Shorter
        than the job runner's lease, which is what keeps the runner from
        reclaiming a handler that is still working.
        """
        return self._shift

    @property
    def workload(self) -> str:
        """Always `"write"`. A read pool opens read-only, so a pass on one fails."""
        return self._workload

    @property
    def ledger(self) -> Any:
        """The durable state this pass reads and advances. Owned by the driver."""
        return self._ledger

    @property
    def recurring(self) -> bool:
        """Whether cycles repeat rather than the pass completing.

        True for `Sealed`, whose frontier moves each cycle, and false for
        a fixed `Ceiling`. A whole-pass gate needs a completion to fire
        at, so it is refused on a recurring pass.
        """
        return bool(self._frontier.recurring)

    def __repr__(self) -> str:
        return f"<ChunkedPass {self._name!r} over {self._table}>"

    # -- schema ---------------------------------------------------------------

    def schema_sql(self) -> str:
        """DDL for the shared ledger table. Apply it as a migration.

        Nothing in Wreath runs this, for the same reason nothing runs
        `wreath.jobs.JobRunner.schema_sql`: a table that appears because a
        process started is a schema change with no history and no review.
        """
        return self._ledger.schema_sql()

    # -- running --------------------------------------------------------------

    async def run_shift(
        self,
        database: Any,
        *,
        stopping: Any = None,
        budget: float | None = None,
        sleep: Any = None,
    ) -> ShiftResult:
        """Run chunks until the shift budget, a stop signal, or the end of the walk."""
        return await _driver.run_shift(
            self, database,
            stopping=stopping,
            budget=self._shift if budget is None else budget,
            sleep=sleep,
        )

    async def run(self, database: Any, *, stopping: Any = None, sleep: Any = None) -> ShiftResult:
        """Drive this pass to the end of its current cycle, in this process.

        The chunking, the per-chunk transaction, and the pacing all still apply,
        so this is not the long transaction a pass exists to avoid -- but it does
        occupy the caller for as long as the walk takes. For anything that might
        run for minutes, hand it to `wreath.jobs.JobRunner.drive` instead
        and let it run in bounded shifts that survive a redeploy.
        """
        chunks = 0
        rows = 0
        holes = 0
        while True:
            result = await self.run_shift(
                database, stopping=stopping, budget=None, sleep=sleep
            )
            chunks += result.chunks
            rows += result.rows
            holes += result.holes
            if result.stopped != "budget":
                return ShiftResult(
                    chunks, rows, complete=result.complete,
                    stopped=result.stopped, error=result.error, holes=holes,
                )

    async def status(self, database: Any) -> PassStatus | None:
        """This pass's durable status, or None when it has never run."""
        connection = await database.acquire(self._workload)
        try:
            row = await self._ledger.read(connection)
        finally:
            await database.release(self._workload, connection)
        return None if row is None else _status_from(row, self._units.keys)

    async def holes(self, database: Any) -> list[Hole]:
        """Chunks this pass gave up on, each with the statement that reproduces it."""
        connection = await database.acquire(self._workload)
        try:
            return await self._ledger.holes(connection)
        finally:
            await database.release(self._workload, connection)

    async def requeue(self, database: Any, unit: Any, *, after: Any = None) -> bool:
        """Walk one range again, out of order. The cursor never rewinds.

        Two callers reach for this from opposite directions and get the same
        mechanism: a rollup folding a late correction into a bucket its cursor is
        already past, and an operator clearing a dead-lettered chunk. Rewinding
        the cursor instead would redo months of correct work to redo one range.

        *unit* is the range's inclusive upper key and *after* its exclusive lower
        one, in the same shape `key=` was declared -- one value, or a tuple.
        Returns `False` when the unit is already queued, which makes calling it
        twice harmless.
        """
        keys = self._units.keys
        upper = _keyset.encode_cursor(keys, _as_tuple(unit, keys))
        lower = None if after is None else _keyset.encode_cursor(keys, _as_tuple(after, keys))
        connection = await database.acquire(self._workload)
        try:
            # A unit can be queued before the pass has ever run -- a correction
            # that arrives during a deploy, say -- so the row has to exist for
            # the queue to be appended to.
            await self._ledger.seed(
                connection,
                chunk_limit=self._units.limit,
                guards=self.guards,
                rewrites=self._rewrites,
            )
            return await self._ledger.requeue(
                connection, cursor_from=lower, cursor_to=upper
            )
        finally:
            await database.release(self._workload, connection)

    async def retry(self, database: Any) -> int:
        """Schedule every hole to be walked again. Returns how many.

        Clearing the holes is the only thing that un-bars a terminal gate, and
        the clearing happens when the chunk *succeeds* rather than when it is
        queued -- so this schedules the work, it does not declare the problem
        solved.

        It also lifts a `blocked` phase, and that half is not optional.
        `halt` parks the cursor *before* its hole and stops the pass; every
        later shift then sees a phase that is not `walking` and declines to
        run, so without lifting it the chunk is never re-attempted, the hole is
        never cleared, and the gate it bars is unreachable. `halt` would be a
        trap rather than a policy. `skip` never blocks, so there the queueing
        is the whole of it.

        A halted pass therefore walks its parked chunk twice -- once as the
        queued unit and once as the range the cursor is still pointing at.
        That is deliberate rather than unnoticed: every declared work shape
        re-runs as a no-op, which is the property the primitive is built on, and
        paying for it once per operator retry is cheaper than teaching this
        method to compare encoded cursors.

        A pass stopped at a *failed verification* is deliberately not restarted.
        That is not a transient failure to retry past; it means the walk's logic
        is wrong and the check will answer no again at the same row.
        """
        connection = await database.acquire(self._workload)
        try:
            queued = 0
            for hole in await self._ledger.holes(connection):
                if await self._ledger.requeue(
                    connection, cursor_from=hole.cursor_from, cursor_to=hole.cursor_to
                ):
                    queued += 1
            await self._ledger.unblock(connection)
            return queued
        finally:
            await database.release(self._workload, connection)


def _as_tuple(value: Any, keys: tuple[Key, ...]) -> tuple[Any, ...]:
    values = tuple(value) if isinstance(value, (tuple, list)) else (value,)
    if len(values) != len(keys):
        raise PassDeclarationError(
            f"this pass walks by {len(keys)} key column(s), so a unit needs "
            f"{len(keys)} value(s); got {value!r}"
        )
    return values


def _resolve_source(over: Any) -> tuple[Any, str, str]:
    """(model, table SQL, alias) for whatever `over=` was given."""
    if isinstance(over, Table):
        return None, over.sql, over.name
    table = getattr(over, "__wreath_table__", None)
    if isinstance(over, type) and table:
        schema = getattr(over, "__wreath_schema__", None)
        name = getattr(schema, "name", None)
        kind = getattr(schema, "kind", None)
        # A logical (central/tenant) schema is only resolvable once a registry
        # has compiled with its schema mode, which has not happened when a pass
        # is declared. Those models reach the database through search_path, and
        # a caller who needs an explicit schema names it with Table(...).
        qualified = f'"{name}"."{table}"' if kind == "fixed" and name else f'"{table}"'
        return over, qualified, table
    raise PassDeclarationError(
        f"over= must be a model class or a Table('name'); got {over!r}"
    )


async def read_status(
    database: Any, *, schema: str = "wreath", name: str | None = None, workload: str = "write"
) -> list[PassStatus]:
    """Every pass in one ledger, or one of them by name.

    Reads the ledger without holding any declaration, which is what the CLI has:
    a database and a schema. Everything a reader needs is in the row.
    """
    connection = await database.acquire(workload)
    try:
        rows = await _ledger.read_all(connection, schema=schema, name=name)
    finally:
        await database.release(workload, connection)
    return [_status_from(row) for row in rows]


async def read_holes(
    database: Any, *, schema: str = "wreath", name: str | None = None, workload: str = "write"
) -> list[Hole]:
    """Every dead-lettered chunk in one ledger, or one pass's."""
    connection = await database.acquire(workload)
    try:
        return await _ledger.read_holes(connection, schema=schema, name=name)
    finally:
        await database.release(workload, connection)


def schema_sql(schema: str = "wreath") -> str:
    """DDL for the shared pass ledger in *schema*. Apply it as a migration."""
    return _ledger.schema_sql(schema)


def column_fact(schema: str, table: str, column: str) -> str:
    """The canonical name for "this column has finished converting".

    One spelling, used by both sides of a contract that would otherwise be a
    convention: a pass declares `Gate(publishes=column_fact(...))`, and
    `wreath.migrations` refuses to narrow or drop that column until the
    fact is published. A free-form string on each side would agree right up
    until someone wrote `public.treks.grade` where the other expected
    `treks.grade`, and the failure would be a migration that sails through
    instead of one that refuses.

    The column named is **the one a later migration will narrow**, not the one
    being filled. For a retype that drains `grade` into `grade_next`, the
    fact is about `grade`: that is the column whose drop has to wait.
    """
    for part, what in ((schema, "schema"), (table, "table"), (column, "column")):
        if not str(part).strip():
            raise PassDeclarationError(f"column_fact needs a {what} name")
    return f"column:{schema}.{table}.{column}"


async def published_facts(
    database: Any,
    *,
    schema: str = "wreath",
    fact: str | None = None,
    workload: str = "write",
) -> list[PublishedFact]:
    """Every fact a gate has verified in one ledger, or the passes claiming one.

    The gate's durable output, readable with nothing but a connection and a
    schema. That is the point of it: the consumer is a migration deciding
    whether it may narrow a column, and it has no pass declaration in hand.
    """
    connection = await database.acquire(workload)
    try:
        return await _ledger.published_facts(connection, schema=schema, fact=fact)
    finally:
        await database.release(workload, connection)


async def pending_facts(
    database: Any,
    *,
    facts: tuple[str, ...],
    schema: str = "wreath",
    workload: str = "write",
) -> list[PendingFact]:
    """Which of *facts* a pass claims and has not yet established.

    The half `published_facts` cannot answer. A migration about to narrow
    a column needs to tell "nothing guards this" from "something guards it and
    is still working", and an absent published fact means both. This reads the
    claim a pass records when its ledger row is seeded.
    """
    connection = await database.acquire(workload)
    try:
        return await _ledger.pending_facts(connection, schema=schema, facts=facts)
    finally:
        await database.release(workload, connection)


__all__ = [
    "Apply",
    "Buckets",
    "Ceiling",
    "Chunk",
    "ChunkedPass",
    "Constraint",
    "Declared",
    "Denominator",
    "DutyCycle",
    "Estimated",
    "Exact",
    "Gate",
    "Hole",
    "Key",
    "Keyspace",
    "NoRowsMatch",
    "PassDeclarationError",
    "PassStatus",
    "PendingFact",
    "Progress",
    "PublishedFact",
    "Purge",
    "Reconcile",
    "Rewrite",
    "Rows",
    "Sealed",
    "ShiftResult",
    "Sql",
    "Table",
    "Verification",
    "column_fact",
    "pending_facts",
    "published_facts",
    "read_holes",
    "read_status",
    "schema_sql",
]
