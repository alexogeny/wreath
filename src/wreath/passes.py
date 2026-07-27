"""Backfills, rollups, reindexes: a durable, resumable, paced walk over a big table.

Sooner or later a table gets big enough that you cannot change all of it at once.
Ten million rows need a new column filled in. A month of raw events needs folding
into a daily summary. Every row needs re-encrypting under a new key. The shape is
always the same, and so is the script people write for it: a ``while True`` loop
with ``OFFSET``, one transaction around the whole thing, a ``sleep`` somebody
tuned once on a laptop, a ``print`` for progress, and an ``except: continue``
that turns a failed chunk into a silent hole. It runs in a terminal on a jump
host and it is nobody's job to watch it.

A :class:`ChunkedPass` is that script, written once and correctly::

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

What that buys, and every item is a specific thing the hand-rolled loop gets
wrong:

* **Keyset ranges, never ``OFFSET``.** The walk stays the same speed at row nine
  million as at row nine. See :mod:`wreath._passes.keyset` for the arithmetic.
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
:mod:`wreath.jobs`, which already deduplicates a cron tick fleet-wide. Its whole
vocabulary is "a half-open range over one ordered domain"; row counts are
reported and never structural, which is what keeps the door open for a range
source that counts no rows at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ._passes import driver as _driver
from ._passes import keyset as _keyset
from ._passes import ledger as _ledger
from ._passes.driver import Chunk, ShiftResult
from ._passes.keyset import Key, PassDeclarationError
from ._passes.pace import DutyCycle

_DURATION = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(ms|s|m|h)?\s*$")


def _seconds(value: Any, *, what: str, allow_zero: bool = False) -> float:
    """Read ``"2s"``, ``"250ms"``, ``"5m"`` or a plain number of seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION.fullmatch(value)
        if match is None:
            raise PassDeclarationError(
                f"{what} must be a number of seconds or a duration like '2s', "
                f"'250ms', '5m'; got {value!r}"
            )
        scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2) or "s"]
        seconds = float(match.group(1)) * scale
    else:
        raise PassDeclarationError(f"{what} must be a duration; got {value!r}")
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise PassDeclarationError(f"{what} must be positive; got {value!r}")
    return seconds


# --- what to walk ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Table:
    """A table to walk that the ORM does not own.

    Wreath's own store tables are the first callers here -- the idempotency
    ledger, the rate-limit buckets, the session rows, the webhook inbox and
    outbox -- and none of them is a model. Naming the table directly is honest
    about that, and it means a legacy table nobody has mapped is still walkable.

    The facts the refusals need travel with the :class:`Key`, not with the table,
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
        return self.name if self.schema is None else f'"{self.schema}"."{self.name}"'


@dataclass(frozen=True, slots=True)
class Rows:
    """The next chunk is the next *limit* rows after the cursor, by key.

    Args:
        key: one column, or a tuple of them, ordered. Model columns
            (``Trek.id``) or :class:`Key` declarations for a table the ORM does
            not own. All columns must be ordered the same way -- a row
            comparison has no mixed-direction form.
        limit: how many rows one chunk covers.
        within: the chunk's time budget. The chunk transaction sets its own
            ``statement_timeout`` from it, so a chunk that hits a lock wait dies
            as a chunk failure rather than as a transaction nobody notices.
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
        ceiling. An identity primary key or a ``now()`` default gives that;
        ``gen_random_uuid()`` does not, and a row that lands behind the cursor is
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
        _keyset.refuse_unmonotone_key(keys, table=table, reason=self.monotone)

    async def derive(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> Any:
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
        _keyset.refuse_unclocked_key(keys, table=table)

    async def derive(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> Any:
        # The frontier is read once per cycle and bound for the whole of it. An
        # inline clock_timestamp() would move the finish line as the walk ran,
        # so a busy table's cycle could never end.
        value = await executor.fetchval(
            "SELECT clock_timestamp() - make_interval(secs => $1::float8)", float(self.after)
        )
        return _keyset.encode_cursor(keys[:1], (value,))

    def predicate(self, keys: tuple[Key, ...], ceiling: Any, binds: Any) -> str:
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

    Placeholders are written ``?`` and renumbered when the fragment is spliced
    into a statement, because a fragment cannot know which ``$n`` it will land
    on::

        Purge(where=Sql("state = ANY(?)", [["delivered", "failed"]]))
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
    from .orm.compiler import _Builder, _render_predicate, check_predicate_columns

    if model is None:
        raise PassDeclarationError(
            "a model predicate needs over=<Model>; for a table the ORM does not "
            "own, write the fragment as Sql('...', [...])"
        )
    check_predicate_columns(model, where)
    builder = _Builder()
    # Seed the builder with the binds already placed so its placeholders continue
    # the same numbering rather than restarting at $1.
    builder.values.extend(binds.values)
    _render_predicate(where, builder, alias, {})
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
    :class:`Apply` is the exception, and it asks in writing.
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
        set_: ``column -> SQL expression``. A column may be a model column or a
            plain name; an expression may be raw SQL or a :class:`Sql` with binds.
        where: which rows still need converting.
    """

    set_: Any
    where: Any = None

    def __post_init__(self) -> None:
        if not self.set_:
            raise PassDeclarationError("Rewrite needs at least one column to set")

    @property
    def writes(self) -> tuple[str, ...]:
        return tuple(_column_name(column) for column in self.set_)

    async def apply(self, tx: Any, chunk: Chunk, binds: Any) -> int:
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

    There is no default and no ``strict=False``. Job delivery is at-least-once,
    so the question cannot be avoided -- it can only be answered, and being wrong
    on purpose should at least be legible to whoever reviews it::

        Apply(reencrypt, idempotent=Declared(
            "re-wrapping a key is idempotent: the row records the wrapping key id "
            "and rows already carrying the new id are excluded by `where`"
        ))
    """

    reason: str

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise PassDeclarationError("Declared(...) needs a reason, not an empty string")


@dataclass(frozen=True, slots=True)
class Apply(_Work):
    """Hand each chunk to a callback, for work no declared shape covers.

    The callback is awaited as ``callback(tx, chunk, binds)`` *inside* the chunk
    transaction, so anything it writes commits with the cursor. It returns the
    number of rows it touched, for the report.

    ``idempotent=`` is required: see :class:`Declared`.
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
        affected = await self.callback(tx, chunk, binds)
        return int(affected or 0)


def _rows_in(tag: Any) -> int:
    """The row count out of a command tag such as ``DELETE 412``."""
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
    last_error: str | None

    def as_dict(self) -> dict[str, Any]:
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
            "last_error": self.last_error,
        }


def _status_from(row: Any) -> PassStatus:
    return PassStatus(
        name=row.name, tenant=row.tenant, phase=row.phase, cursor=row.cursor,
        ceiling=row.ceiling, units_done=row.units_done, rows_done=row.rows_done,
        chunk_limit=row.chunk_limit, paced_reason=row.paced_reason,
        started_at=row.started_at, last_advance=row.last_advance,
        cycle_started=row.cycle_started, last_error=row.last_error,
    )


class ChunkedPass:
    """A durable, resumable, paced walk over one table.

    Declared once, validated at declaration time, and driven by the job runner
    with :meth:`wreath.jobs.JobRunner.drive`. See the module docstring for what
    the machinery buys and the guide for how to reach for it.

    Args:
        name: this pass's identity in the ledger, and how the CLI names it.
        over: a model class, or a :class:`Table` for a table the ORM does not own.
        units: where the next range comes from. :class:`Rows` today.
        frontier: how far the walk goes -- :meth:`Ceiling.at_launch` for a pass
            that finishes, :class:`Sealed` for one that recurs.
        work: what to do to a chunk.
        pace: how much of the machine the pass may be. There is no "off".
        shift: how long one stretch of work runs before it hands back to the job
            runner. Must be shorter than the runner's lease, which
            :meth:`~wreath.jobs.JobRunner.drive` checks.
        tenant: the ledger row's tenant, for a fleet that keeps them apart.
        schema: where the ledger table lives; match the job runner's.
    """

    __slots__ = (
        "_alias", "_frontier", "_ledger", "_model", "_name", "_pace", "_schema",
        "_shift", "_table", "_tenant", "_units", "_work", "_workload",
    )

    def __init__(
        self,
        name: str,
        *,
        over: Any,
        units: Rows,
        frontier: Ceiling | Sealed,
        work: _Work,
        pace: DutyCycle | None = None,
        shift: Any = "10s",
        tenant: str = "",
        schema: str = "wreath",
        workload: str = "write",
    ) -> None:
        if not name or len(name) > 200:
            raise PassDeclarationError("a pass name must be 1..200 characters")
        if not isinstance(units, Rows):
            raise PassDeclarationError(
                f"units= must be a range source such as Rows(...); got {units!r}"
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
        self._name = name
        self._tenant = tenant
        self._schema = schema
        self._units = units
        self._frontier = frontier
        self._work = work
        self._pace = pace if pace is not None else DutyCycle()
        self._workload = workload
        self._shift = _seconds(shift, what="shift")
        self._model, self._table, self._alias = _resolve_source(over)

        _keyset.refuse_unsound_key(units.keys, table=self._table)
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
        self._ledger = _ledger.Ledger(schema=schema, name=name, tenant=tenant)

    # -- what a driver reads -------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def tenant(self) -> str:
        return self._tenant

    @property
    def schema(self) -> str:
        return self._schema

    @property
    def table(self) -> str:
        return self._table

    @property
    def alias(self) -> str:
        return self._alias

    @property
    def model(self) -> Any:
        return self._model

    @property
    def units(self) -> Rows:
        return self._units

    @property
    def frontier(self) -> Ceiling | Sealed:
        return self._frontier

    @property
    def work(self) -> _Work:
        return self._work

    @property
    def pace(self) -> DutyCycle:
        return self._pace

    @property
    def shift(self) -> float:
        return self._shift

    @property
    def workload(self) -> str:
        return self._workload

    @property
    def ledger(self) -> Any:
        return self._ledger

    @property
    def recurring(self) -> bool:
        return bool(self._frontier.recurring)

    def __repr__(self) -> str:
        return f"<ChunkedPass {self._name!r} over {self._table}>"

    # -- schema ---------------------------------------------------------------

    def schema_sql(self) -> str:
        """DDL for the shared ledger table. Apply it as a migration.

        Nothing in Wreath runs this, for the same reason nothing runs
        :meth:`wreath.jobs.JobRunner.schema_sql`: a table that appears because a
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
        run for minutes, hand it to :meth:`wreath.jobs.JobRunner.drive` instead
        and let it run in bounded shifts that survive a redeploy.
        """
        chunks = 0
        rows = 0
        while True:
            result = await self.run_shift(
                database, stopping=stopping, budget=None, sleep=sleep
            )
            chunks += result.chunks
            rows += result.rows
            if result.stopped != "budget":
                return ShiftResult(
                    chunks, rows, complete=result.complete,
                    stopped=result.stopped, error=result.error,
                )

    async def status(self, database: Any) -> PassStatus | None:
        """This pass's durable status, or None when it has never run."""
        connection = await database.acquire(self._workload)
        try:
            row = await self._ledger.read(connection)
        finally:
            await database.release(self._workload, connection)
        return None if row is None else _status_from(row)


def _resolve_source(over: Any) -> tuple[Any, str, str]:
    """(model, table SQL, alias) for whatever ``over=`` was given."""
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
    """Every pass in one ledger, or one of them by name."""
    connection = await database.acquire(workload)
    try:
        rows = await _ledger.read_all(connection, schema=schema, name=name)
    finally:
        await database.release(workload, connection)
    return [_status_from(row) for row in rows]


def schema_sql(schema: str = "wreath") -> str:
    """DDL for the shared pass ledger in *schema*. Apply it as a migration."""
    return _ledger.schema_sql(schema)


__all__ = [
    "Apply",
    "Ceiling",
    "Chunk",
    "ChunkedPass",
    "Declared",
    "DutyCycle",
    "Key",
    "PassDeclarationError",
    "PassStatus",
    "Purge",
    "Rewrite",
    "Rows",
    "Sealed",
    "ShiftResult",
    "Sql",
    "Table",
    "read_status",
    "schema_sql",
]
