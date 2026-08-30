"""One append-only log, declared once, read back from a cursor that cannot skip.

An audit trail, a change feed, a stream of a job's output, a meter of billable
events: four features that need the same small thing, which is rows in commit
order and a cursor a reader can come back with. Written four times, the hard
parts are re-derived four times -- and they are the parts that fail silently:

* **the cursor**, which is the whole of it. A `bigserial` is allocated *before*
  commit, so a reader that remembers `max(seq)` skips every row a slower
  transaction commits afterwards with a lower number. The gap is invisible,
  intermittent and load-dependent;
* the table name reaches SQL by interpolation, so it must be a plain identifier;
* the schema is *offered* (`Log.component`) and never applied, because schema
  changes belong in the migration history with the rest of the schema;
* statements are prepared lazily, because a log is built while the application
  is being described and the database is not up yet;
* retention is **declared**, never defaulted -- a log with no answer to "how
  long do these rows live" is a disk-space incident with a delay fuse -- and it
  is *executed* by `retention_pass`, a `wreath.passes` walk, so the rows it
  removes are a number in a ledger rather than a policy nobody drove;
* appends **batch**. `append_many` decomposes a batch into powers-of-two
  multi-row inserts, because a producer whose rows are small and frequent would
  otherwise pay one round trip per row;
* an unflushed batch lost to a worker's death is **counted**, never absorbed.

Declare the shape and pick what it holds:

```python
chunks = Log(
    table="stream_chunks",
    columns=(Column("body", "bytea", null=False),),
    retain=hours(24),
)
log = PostgresLog(database, chunks)

await log.append("conversation-7", body=b"hello ")
page = await log.read("conversation-7", after=Cursor.start())
```

What is *not* shared is the payload and its meaning: what the columns hold,
what a stream name identifies, and what a reader does with a row. Each caller
keeps those.

**At-least-once, de-duplicated by the reader.** A row is delivered as many times
as a reader asks for it. `Cursor` is what makes de-duplication possible, and
promising exactly-once here would be a guarantee no transport can keep.

**Not a message bus.** `wreath.messaging` is the bus, and a `NOTIFY` remains the
doorbell that says *something moved, go read the log*. A notification is not the
delta: a subscriber that was disconnected for one never learns it happened, and
treating the payload as the change is how that subscriber silently misses data.

**Not the flight recorder.** `wreath._ring_file` is a memory-mapped ring sized to
survive the process that wrote it and to be read with no network hop; its rows
are gone on the next wrap. This is durable, ordered, shared between workers, and
read with SQL. They look similar from a distance and answer different questions,
so they stay apart.

**Not event sourcing.** No projections, no aggregates, no rebuilding state from
the log. Read-side consumers only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, NamedTuple

from ._native import _core
from .store import Column, _Statements, rows_affected, sql_identifier

__all__ = [
    "ALIAS",
    "KEEP_FOREVER",
    "Batch",
    "Column",
    "Cursor",
    "Flush",
    "Log",
    "PostgresLog",
    "Record",
    "retention_pass",
]

#: The row alias every generated statement uses, so a caller writing its own SQL
#: against the same log can reference columns the same way.
ALIAS: Final = "l"

#: `retain=KEEP_FOREVER` for a log whose rows are evidence rather than delivery.
#: Spelled out rather than defaulted: an audit trail and a chunk buffer want
#: opposite answers, and a default would silently give one of them the other's.
KEEP_FOREVER: Final = None


class Cursor(NamedTuple):
    """Where a reader got to: a transaction id and a sequence number.

    **Both halves are load-bearing.** The sequence alone is not a cursor, and
    neither is the sequence gated on visibility: sequence order is *allocation*
    order, and rows do not commit in the order they allocate. Ordering by
    `(xid, seq)` and remembering both is what makes "everything after this"
    mean it. `wreath.log`'s cursor contract has the
    proof and the two wrong answers it rules out.

    Opaque to a caller: build one with `start` or take it from a `Batch`,
    round-trip it through `encode`/`decode`, and never do arithmetic on
    it.
    """

    xid: int
    seq: int

    @classmethod
    def start(cls) -> Cursor:
        """The cursor before the first row of any log."""
        return cls(0, 0)

    def encode(self) -> str:
        """A form safe to hand a client and take back, e.g. a `Last-Event-ID`."""
        return f"{self.xid}.{self.seq}"

    @classmethod
    def decode(cls, value: str | None) -> Cursor:
        """Parse `encode`'s form, refusing anything else.

        Client-supplied by design -- a `Last-Event-ID` header is whatever the
        other end sent -- so this refuses rather than repairs. A cursor that
        parsed loosely would be an index into the log built from request text.
        """
        if not value:
            return cls.start()
        # `isascii` once over the whole value rather than once per half: the two
        # spellings are equivalent (the separator is ASCII either way), and one
        # scan is both cheaper and impossible to half-remove -- a mutation run
        # found the per-half form had an operand no test distinguished.
        # `str.isdigit` rather than `int(...)` in a `try`: `int` accepts leading
        # whitespace, a leading sign, and Unicode digits from other scripts, so
        # `" +7"` and a full-width `"７"` both parse, and `int("７")` really is 7.
        # Every one of those is a cursor this never emitted, arriving from a
        # header somebody else wrote.
        if not value.isascii():
            raise ValueError(f"not a log cursor: {value!r}")
        head, _, tail = value.partition(".")
        # No separate "is there a separator" clause: `partition` puts everything
        # in `head` and leaves `tail` empty when there is none, and `"".isdigit()`
        # is False -- so the digit check already refuses it. A mutation run
        # found the extra clause changed no outcome, which is the definition of
        # a second spelling of one condition, and two spellings are how they
        # drift apart later.
        if not head.isdigit() or not tail.isdigit():
            raise ValueError(f"not a log cursor: {value!r}")
        return cls(int(head), int(tail))


@dataclass(frozen=True, slots=True)
class Record:
    """One row, with its cursor and whatever payload columns the log declared."""

    cursor: Cursor
    stream: str
    values: Mapping[str, Any]

    def __getitem__(self, column: str) -> Any:
        return self.values[column]


@dataclass(frozen=True, slots=True)
class Batch:
    """A page of records and the cursor to come back with.

    `cursor` is where to resume *even when `records` is empty*: an empty batch
    still means "nothing below the horizon that you have not seen", and throwing
    it away would make a quiet log replay its tail forever.
    """

    records: tuple[Record, ...]
    cursor: Cursor

    def __bool__(self) -> bool:
        return bool(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Any:
        return iter(self.records)


@dataclass(frozen=True, slots=True)
class Flush:
    """When a buffered append gives up and writes.

    Bytes *or* milliseconds, whichever comes first -- one alone is a bad trade in
    one direction or the other. A byte threshold on a slow producer holds the
    first token until the last one arrives; a time threshold on a fast one writes
    a row per token, which is the write amplification buffering exists to avoid.

    `capacity` bounds the buffer in items. Offering past it drops, and the drop
    is counted rather than absorbed: bounded memory is the promise a buffer
    makes, and a silent drop is how a log starts lying about completeness.
    """

    bytes: int = 8192
    every: float = 0.05
    capacity: int = 4096

    def __post_init__(self) -> None:
        if self.bytes <= 0:
            raise ValueError("Flush(bytes=...) must be positive")
        if self.every <= 0:
            raise ValueError("Flush(every=...) must be a positive number of seconds")
        if self.capacity <= 0:
            raise ValueError("Flush(capacity=...) must be positive")


@dataclass(frozen=True, slots=True)
class Log:
    """The declaration of an append-only log: what it holds and how long.

    Args:
        table: the backing table. Interpolated, so it must be a plain identifier.
        columns: the payload. Their meaning belongs to the caller.
        retain: seconds a row lives, or `KEEP_FOREVER`. **Required**, with no
            default: a chunk buffer and an audit trail want opposite answers and
            neither should inherit the other's by omission.
        stream: the column naming the partition a reader follows.
        dedup: add a nullable `dedup` column with a unique index, for a caller
            whose producer may retry -- a metered event, a webhook receipt.
        flush: buffering policy for `PostgresLog.buffered`.
        schema: the schema the table lives in. Wreath's own furniture goes in
            `wreath`; a caller may name another.
        prefix: prepended to prepared-statement names.
    """

    table: str
    retain: float | None
    columns: tuple[Column, ...] = ()
    stream: str = "stream"
    dedup: bool = False
    flush: Flush = field(default_factory=Flush)
    schema: str = "wreath"
    prefix: str = "wreath_log"

    def __post_init__(self) -> None:
        sql_identifier(self.table)
        sql_identifier(self.stream, what="stream")
        sql_identifier(self.prefix, what="prefix")
        if self.schema:
            sql_identifier(self.schema, what="schema")
        for column in self.columns:
            sql_identifier(column.name, what="column")
        reserved = {"seq", "xid", "at", self.stream} | ({"dedup"} if self.dedup else set())
        for column in self.columns:
            if column.name in reserved:
                raise ValueError(
                    f"column {column.name!r} collides with a column the log owns "
                    f"({', '.join(sorted(reserved))}); rename the payload column"
                )
        if self.retain is not None and self.retain <= 0:
            raise ValueError(
                "retain must be a positive number of seconds, or KEEP_FOREVER for a "
                "log whose rows are evidence"
            )

    @property
    def qualified_table(self) -> str:
        """The table as it reaches SQL: schema-qualified unless the schema is `""`."""
        if not self.schema:
            return self.table
        return f'"{self.schema}".{self.table}'

    def statements(self) -> tuple[str, ...]:
        """DDL for the backing table, one statement per element.

        `xid8` rather than `xid`: the 32-bit type wraps, and a cursor built from
        a wrapping counter compares wrongly exactly once every four billion
        transactions, which is both rare enough to survive review and certain
        enough to happen.
        """
        table = self.qualified_table
        lines = [
            "    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
            "    xid xid8 NOT NULL DEFAULT pg_current_xact_id()",
            f"    {self.stream} text NOT NULL",
        ]
        lines += [
            f"    {column.name} {column.sql_type}{'' if column.null else ' NOT NULL'}"
            for column in self.columns
        ]
        if self.dedup:
            lines.append("    dedup text")
        lines.append("    at timestamptz NOT NULL DEFAULT clock_timestamp()")
        body = ",\n".join(lines)
        parts = [
            f"CREATE TABLE IF NOT EXISTS {table} (\n{body}\n)",
            # The global feed: every reader that follows the whole log.
            f"CREATE INDEX IF NOT EXISTS {self.table}_cursor_idx\n    ON {table} (xid, seq)",
            # One partition's feed. Leading with the stream is what turns
            # "everything after C for stream K" into a range scan rather than a
            # filter over the whole log.
            f"CREATE INDEX IF NOT EXISTS {self.table}_stream_idx\n"
            f"    ON {table} ({self.stream}, xid, seq)",
        ]
        if self.dedup:
            parts.append(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {self.table}_dedup_idx\n"
                f"    ON {table} ({self.stream}, dedup) WHERE dedup IS NOT NULL"
            )
        if self.retain is not None:
            # Retention deletes by age, so the age column needs its own index or
            # every purge is a sequential scan of a table that only ever grows.
            parts.append(f"CREATE INDEX IF NOT EXISTS {self.table}_at_idx\n    ON {table} (at)")
        return tuple(parts)

    def schema_claim(self, name: str) -> Any:
        """This declaration's claim on the wreath schema, under `name`.

        `name` is the caller's, because one `Log` shape backs several
        subsystems and each needs its own version marker and its own advisory
        lock -- sharing one would make an upgrade to the audit trail block a
        worker that only streams.

        Named apart from the zero-argument `component()` protocol for the
        reason `wreath.store.Keyed.schema_claim` gives: a declaration needs the
        argument, and the walk cannot pass one.
        """
        from .schema import Component, Step

        return Component(
            name=name,
            schema=self.schema,
            relations=(self.table,),
            steps=(Step(version=1, statements=self.statements()),),
        )

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined.

        A derivation of `statements()`; `wreath schema sql` is the supported
        spelling for a caller applying it by hand.
        """
        return ";\n".join(self.statements())


#: "this key was not in the mapping", distinct from a key whose value is `None`.
#: A payload column may legitimately be given `None`, so `values.get(name)`
#: alone cannot tell "absent" from "explicitly null" -- and the two take
#: different branches, one of which is a refusal.
_ABSENT: Final = object()


#: Rows returned by one `read`, when the caller names no limit. Large enough that
#: a catching-up reader is not making a round trip per handful, small enough that
#: one page fits comfortably in memory.
DEFAULT_LIMIT: Final = 512

#: The largest number of rows one batched `INSERT` carries.
#:
#: A batch is decomposed into rungs that are powers of two, largest first, so any
#: batch size costs at most one statement per rung and long ones cost
#: `ceil(n / 512)` more. Two things decide the shape:
#:
#: * **The statement text has to be one of a bounded set.** The obvious
#:   alternative -- one `INSERT` shaped to the exact batch -- is a single round
#:   trip, and a brand new SQL string every flush. The driver's per-connection
#:   plan cache holds a hundred entries and evicts by recency, so a buffer
#:   flushing ragged batches down a pooled connection would push the
#:   application's own compiled statements out to hold one-shot inserts it will
#:   never run again. Ten rungs prepare once and stay.
#: * **PostgreSQL takes at most 65535 parameters per statement**, so the top rung
#:   is clamped by the log's column count as well as by this number.
MAX_BATCH_ROWS: Final = 512


class PostgresLog(_Statements):
    """An append-only log in a table every worker shares.

    Holds the disciplines named in this module's docstring, and generates the
    statements a log always wants -- `append`, `read`, `tail`,
    `purge` -- from the declaration.

    **The horizon is what makes the cursor honest.** A read never returns a row
    whose transaction may still be in flight: it stops at
    `pg_snapshot_xmin(pg_current_snapshot())`, the oldest transaction id not yet
    settled. Every row below that horizon has finished, so nothing can later
    appear behind a cursor that has passed it.

    The cost is latency, and it is worth naming: a long-running transaction --
    anybody's, anywhere in the database -- pins the horizon and stalls every
    log reader until it ends. That is a property of the database, not of this
    module, and it is visible as a log that stops advancing while appends
    continue. `PostgresLog.horizon_lag` reports it.

    Args:
        database: a `wreath.postgres.Database`.
        declaration: what the log holds.
        read_workload: the pool reads go to. `"write"` by default, because a
            reader that has just appended must see its own rows.
    """

    __slots__ = (
        "_batch_columns",
        "_database",
        "_declaration",
        "_defined",
        "_dropped",
        "_prepare_lock",
        "_rung_sql",
        "_rungs",
    )

    _statement_owner = "log"

    def __init__(self, database: Any, declaration: Log, *, read_workload: str = "write") -> None:
        self._database = database
        self._declaration = declaration
        self._init_statements()
        self._dropped = 0

        table = declaration.qualified_table
        stream = declaration.stream
        payload = [column.name for column in declaration.columns]
        columns = [stream, *payload] + (["dedup"] if declaration.dedup else [])
        placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
        head = f"INSERT INTO {table} ({', '.join(columns)})\nVALUES ({placeholders})\n"
        returning = "RETURNING xid::text, seq"
        self.define("append", head + returning)
        if declaration.dedup:
            # A retried producer must not append twice. `DO NOTHING` returns no
            # row for the duplicate, which is the caller's signal that this event
            # was already recorded -- not an error, and not a second row.
            self.define("append_once", f"{head}ON CONFLICT DO NOTHING\n{returning}")

        selected = ", ".join(["xid::text", "seq", stream, *payload])
        # `(xid, seq) > ($1, $2)` as a row comparison rather than two clauses:
        # PostgreSQL can drive the composite index from it, and spelling it out
        # as `xid > $1 OR (xid = $1 AND seq > $2)` is the same predicate written
        # in a form the planner has to work harder to recognise.
        self.define(
            "read",
            f"SELECT {selected} FROM {table} AS {ALIAS}\n"
            f"WHERE {ALIAS}.{stream} = $1\n"
            f"  AND {ALIAS}.xid < pg_snapshot_xmin(pg_current_snapshot())\n"
            f"  AND ({ALIAS}.xid, {ALIAS}.seq) > ($2::text::xid8, $3)\n"
            f"ORDER BY {ALIAS}.xid, {ALIAS}.seq\n"
            "LIMIT $4",
            workload=read_workload,
        )
        self.define(
            "read_all",
            f"SELECT {selected} FROM {table} AS {ALIAS}\n"
            f"WHERE {ALIAS}.xid < pg_snapshot_xmin(pg_current_snapshot())\n"
            f"  AND ({ALIAS}.xid, {ALIAS}.seq) > ($1::text::xid8, $2)\n"
            f"ORDER BY {ALIAS}.xid, {ALIAS}.seq\n"
            "LIMIT $3",
            workload=read_workload,
        )
        self.define(
            "horizon",
            "SELECT pg_snapshot_xmin(pg_current_snapshot())::text",
            workload=read_workload,
        )
        self.define(
            "latest",
            f"SELECT coalesce(max(xid)::text, '0') FROM {table} AS {ALIAS}",
            workload=read_workload,
        )
        if declaration.retain is not None:
            self.define(
                "purge",
                f"DELETE FROM {table} AS {ALIAS}\n"
                f"WHERE {ALIAS}.at < clock_timestamp() - make_interval("
                f"secs => {float(declaration.retain)!r}::float8)",
            )
        self.define(
            "drop_stream",
            f"DELETE FROM {table} AS {ALIAS} WHERE {ALIAS}.{stream} = $1",
        )

        # The rungs a batched append is decomposed into. `dedup` is deliberately
        # absent: `append_once` is a one-row conflict check and a batch has no
        # per-row answer to give back, so a log declared with dedup batches its
        # payload columns and keeps its de-duplicating append single.
        self._batch_columns = (stream, *payload)
        top = min(MAX_BATCH_ROWS, 65535 // len(self._batch_columns))
        rungs: list[int] = []
        self._rung_sql: dict[int, str] = {}
        rung = 1
        while rung <= top:
            rungs.append(rung)
            self._rung_sql[rung] = self._batch_sql(rung)
            self.define(f"b{rung}", self._rung_sql[rung])
            rung *= 2
        # Descending, because `_rung_for` wants the largest that fits and the
        # last element is 1 -- which is what makes that search total.
        self._rungs = tuple(reversed(rungs))

    @property
    def database(self) -> Any:
        """The `wreath.postgres.Database` this log runs against.

        Exposed because two legitimate operations need a connection of their
        own rather than a pooled statement: an erasure, which carries a
        transaction-scoped setting, and a retention walk, which paces itself.
        `wreath.store.PostgresStore` has no equivalent because neither applies
        to a keyed store.
        """
        return self._database

    @property
    def declaration(self) -> Log:
        """The `Log` this was built from. Frozen, so it is safe to share."""
        return self._declaration

    @property
    def table(self) -> str:
        """The backing table, schema-qualified as it reaches SQL."""
        return self._declaration.qualified_table

    def schema_claim(self, name: str) -> Any:
        """This log's claim on the wreath schema, under `name`."""
        return self._declaration.schema_claim(name)

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined."""
        return self._declaration.schema_sql()

    def _statement_name(self, name: str) -> str:
        return f"{self._declaration.prefix}_{name}_{self._declaration.table}"

    async def append(self, stream: str, /, *, connection: Any = None, **values: Any) -> Cursor:
        """Append one row to `stream`, returning where it landed.

        The returned cursor is *this row's* position, not a resume point: a
        reader handed it would skip this row. It is the append's receipt --
        proof the row committed and where it sits in the order -- and callers
        that want to read from here pass it as `after`.

        **`connection` is what makes an append atomic with a write.** Without
        it the statement takes its own pooled connection and therefore its own
        transaction, which commits whether or not the caller's does -- so a
        change feed built that way describes writes that rolled back, and an
        audit trail built that way records them. Pass the connection the caller
        is already writing on and the row shares its fate.

        Each caller chooses. A change feed and an audit trail must pass one; a
        buffered stream of output must not, because its loss is survivable and
        its latency is not.
        """
        bound = self._bind(stream, values)
        if connection is not None:
            row = await connection.fetchrow(self.sql("append"), *bound)
        else:
            row = await self.statement("append").fetchrow(*bound)
        if row is None:
            raise RuntimeError(
                f"append to {self.table} returned no row; the statement is an "
                "INSERT ... RETURNING and cannot legitimately be empty"
            )
        return Cursor(int(row[0]), int(row[1]))

    async def append_once(self, stream: str, /, *, dedup: str, **values: Any) -> Cursor | None:
        """Append unless `dedup` is already recorded for `stream`.

        `None` means the event was already there -- a retried producer, a
        redelivered webhook -- which is a fact about the caller's world rather
        than an error in this one.

        Raises:
            ValueError: when the log was not declared with `dedup=True`.
        """
        if not self._declaration.dedup:
            raise ValueError(
                f"{self.table} was declared without dedup=True, so there is no "
                "unique index to conflict on; append_once cannot be honoured"
            )
        row = await self.statement("append_once").fetchrow(*self._bind(stream, values), dedup)
        return None if row is None else Cursor(int(row[0]), int(row[1]))

    def _batch_sql(self, rows: int) -> str:
        """A multi-row `INSERT` for exactly `rows` rows.

        No `RETURNING`: a batched append reports how many rows landed and not
        where each one landed. PostgreSQL does not promise `RETURNING` comes
        back in the order the `VALUES` list was written, so pairing cursors to
        inputs would be a guarantee this cannot keep -- and nothing wants it.
        The buffered producer wants a count, and the audit trail discards the
        cursor `record` already hands it.
        """
        width = len(self._batch_columns)
        groups = ", ".join(
            "(" + ", ".join(f"${row * width + index + 1}" for index in range(width)) + ")"
            for row in range(rows)
        )
        return f"INSERT INTO {self.table} ({', '.join(self._batch_columns)})\nVALUES {groups}"

    def _rung_for(self, remaining: int) -> int:
        """The largest batch statement that fits in `remaining` rows.

        Total for any `remaining >= 1`, because the rungs descend to 1.
        """
        return next(size for size in self._rungs if size <= remaining)

    async def append_many(
        self,
        rows: Sequence[tuple[str, Mapping[str, Any]]],
        /,
        *,
        connection: Any = None,
    ) -> int:
        """Append many rows as multi-row inserts, returning how many landed.

        The point of the whole thing. `append` is one statement per row, and a
        producer with a buffer in front of it -- a chunked stream, a metered
        event per request, an ORM flush of a hundred audited instances -- turns
        that into a round trip per row, which is the write amplification the
        buffer exists to remove. This decomposes the batch into powers of two
        and issues one prepared `INSERT` per rung, so a thousand rows cost six
        statements rather than a thousand.

        `rows` is `(stream, values)` pairs, because the audit trail's rows go to
        one stream *per audited row* and a single-stream signature could not
        carry them.

        **`connection` carries the same meaning it does on `append`**, and it is
        what keeps the batch atomic with the write it describes: every rung runs
        on the connection the caller is already writing on, inside the caller's
        transaction, so the records commit if and only if that transaction does.
        Without it each rung takes its own pooled connection and its own
        transaction, and a batch that fails halfway leaves the rungs before it
        committed -- which is what a buffered producer wants and what an audit
        trail must not have.
        """
        bound = [self._bind(stream, values) for stream, values in rows]
        written = 0
        index = 0
        remaining = len(bound)
        while remaining:
            size = self._rung_for(remaining)
            arguments: list[Any] = []
            for row in bound[index : index + size]:
                arguments.extend(row)
            if connection is not None:
                tag = await connection.execute(self._rung_sql[size], *arguments)
            else:
                tag = await self.statement(f"b{size}").execute(*arguments)
            landed = _rows_in(tag)
            if landed != size:
                raise RuntimeError(
                    f"append to {self.table} reported {landed} rows for a batch of "
                    f"{size}; the statement is a plain multi-row INSERT with no "
                    "conflict clause and cannot legitimately write fewer"
                )
            written += landed
            index += size
            remaining -= size
        return written

    def _bind(self, stream: str, values: Mapping[str, Any]) -> list[Any]:
        """Order a caller's payload the way the statement expects it.

        Guarding the precondition rather than letting the driver report a
        parameter-count mismatch: `append(stream, boyd=...)` is a typo, and the
        error should name the column, not the placeholder index.

        Reads `values` rather than consuming it, so a batched caller can hand
        over the mapping it built without copying it first -- and so nothing
        here can empty a dict its caller still owns. Undeclared columns are
        found by counting the ones that matched, which needs no second pass on
        the path that has none.
        """
        if not isinstance(stream, str) or not stream:
            raise ValueError("a log row needs a non-empty stream name")
        bound: list[Any] = [stream]
        matched = 0
        for column in self._declaration.columns:
            value = values.get(column.name, _ABSENT)
            if value is _ABSENT:
                if not column.null:
                    raise ValueError(
                        f"{self.table}.{column.name} is NOT NULL and was not supplied; "
                        "append() takes "
                        f"{', '.join(column.name for column in self._declaration.columns)}"
                    )
                bound.append(None)
                continue
            matched += 1
            bound.append(value)
        if matched != len(values):
            declared = ", ".join(column.name for column in self._declaration.columns) or "none"
            extra = set(values) - {column.name for column in self._declaration.columns}
            raise ValueError(
                f"{self.table} declares no column named "
                f"{', '.join(sorted(extra))}; it has {declared}"
            )
        return bound

    async def read(
        self, stream: str | None = None, *, after: Cursor, limit: int = DEFAULT_LIMIT
    ) -> Batch:
        """Records after `after`, stopping at the horizon.

        `stream=None` reads the whole log rather than one partition -- the shape
        a change feed wants, where every row matters to somebody.

        The returned `Batch.cursor` is where to resume. When the batch is
        empty it is `after` unchanged, so a quiet log does not rewind.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        if stream is None:
            rows = await self.statement("read_all").fetch(str(after.xid), after.seq, limit)
        else:
            rows = await self.statement("read").fetch(stream, str(after.xid), after.seq, limit)
        return self._batch(rows, after)

    def _batch(self, rows: Sequence[Any], after: Cursor) -> Batch:
        names = tuple(column.name for column in self._declaration.columns)
        return _core.log_batch(rows, names, after, Cursor, Record, Batch)

    async def horizon(self) -> int:
        """The transaction id below which every row has settled."""
        return int(await self.statement("horizon").fetchval())

    async def horizon_lag(self) -> int:
        """How far the newest appended row sits above the horizon.

        Zero on a healthy log. A number that grows and does not come back down
        is a transaction somebody left open: readers stall while writers carry
        on, which looks like the log has stopped rather than like the database
        has a held snapshot.
        """
        latest = int(await self.statement("latest").fetchval())
        return max(0, latest - await self.horizon())

    async def purge(self) -> str:
        """Drop rows past their retention.

        Nothing calls this for you, and on a log large enough to need retention
        this is the wrong shape: it is one unbounded `DELETE` over a table that
        only ever grows, which is a lock held for as long as it takes and a WAL
        spike nobody scheduled. `retention_pass` is the same deletion as a
        durable, resumable, paced walk whose rows are counted in the pass
        ledger. Reach for this on a small log, and for the pass otherwise.

        Raises:
            ValueError: on a `KEEP_FOREVER` log, where there is nothing to drop.
        """
        if self._declaration.retain is None:
            raise ValueError(
                f"{self.table} was declared retain=KEEP_FOREVER; its rows are "
                "evidence and purging them is a compliance decision, not a "
                "disk-space one. Delete a stream with drop_stream() instead."
            )
        return await self.statement("purge").execute()

    async def drop_stream(self, stream: str, *, connection: Any = None) -> str:
        """Drop one partition outright, whatever its retention.

        The erasure path: a stream is a subject, and a subject may ask to be
        forgotten. Deliberately separate from `purge` so that a
        `KEEP_FOREVER` log still has an answer, and so that dropping evidence is
        always an explicit act naming what it drops.

        `connection` runs the delete on a connection the caller already holds --
        which is what an erasure needs, because it has to carry a transaction of
        its own (see `wreath.audit_log.AuditTrail.forget`).
        """
        if connection is not None:
            return await connection.execute(self.sql("drop_stream"), stream)
        return await self.statement("drop_stream").execute(stream)

    def retention_pass(self, *, name: str, **options: Any) -> Any:
        """This log's retention walk. See `wreath.log.retention_pass`."""
        return retention_pass(self._declaration, name=name, **options)

    def buffered(self, stream: str) -> _Buffer:
        """A byte-or-millisecond buffer over `append` for one stream.

        For a producer whose rows are small and frequent -- a token at a time, a
        metered event per request -- where a row per item is write amplification
        that a flush threshold removes. Rows still in the buffer when the process
        dies are lost and **counted**; a caller who cannot afford that appends
        directly.
        """
        return _Buffer(self, stream)

    @property
    def dropped(self) -> int:
        """Buffered rows lost -- to a full buffer, or to a flush that failed.

        Never resets. A log that has dropped anything is a log whose completeness
        claim has a number attached to it, which is the only honest way to make
        one.
        """
        return self._dropped


class _Buffer:
    """One stream's pending rows, flushed on bytes or on age.

    Not thread-safe and not shared: a buffer belongs to the task producing into
    it. Two producers on one stream take two buffers, and the log orders them.
    """

    __slots__ = ("_bytes", "_log", "_queue", "_since", "_stream")

    def __init__(self, log: PostgresLog, stream: str) -> None:
        from time import monotonic

        from .queue import Queue

        self._log = log
        self._stream = stream
        self._queue = Queue(capacity=log.declaration.flush.capacity)
        self._bytes = 0
        self._since = monotonic()

    def offer(self, **values: Any) -> bool:
        """Buffer one row. `False` when the buffer was full and it was dropped."""
        if not self._queue.offer(values):
            self._log._dropped += 1
            return False
        self._bytes += _weigh(values)
        return True

    @property
    def due(self) -> bool:
        """Whether the buffer has reached either threshold."""
        from time import monotonic

        if len(self._queue) == 0:
            return False
        policy = self._log.declaration.flush
        return self._bytes >= policy.bytes or (monotonic() - self._since) >= policy.every

    @property
    def pending(self) -> int:
        """Rows buffered and not yet written."""
        return len(self._queue)

    def abandon(self) -> int:
        """Give up on everything buffered, counting it as dropped.

        What a shutdown path calls when the window cannot be flushed -- the
        database is gone, the task is being cancelled, the worker is going away.
        The rows are lost either way; the difference this makes is that
        `PostgresLog.dropped` says how many, instead of the loss being a
        buffer that was simply never looked at again.

        Nothing in a process can count what a `SIGKILL` takes with it. What it
        can do is make the loss *bounded and observable while it is still
        pending* -- `pending` before the fact, `dropped` after a shutdown that
        got as far as this method -- and say plainly that a caller who cannot
        afford the window appends directly instead of buffering.
        """
        lost = len(self._queue.drain())
        self._bytes = 0
        self._log._dropped += lost
        return lost

    async def flush(self) -> int:
        """Write everything buffered, returning how many rows landed.

        One `INSERT` per rung of `PostgresLog.append_many`, not one per row --
        the buffer exists to remove write amplification, and a flush that issued
        a statement each did not remove any of it.

        The batch is drained *before* the write and re-counted as dropped if the
        write raises. Draining afterwards would mean a failed flush leaves the
        rows queued and the next flush retries them, which sounds better and is
        worse: the same failure then blocks every later row behind it, and the
        buffer -- bounded -- starts dropping the new ones instead of the old.

        A batch spanning more than one rung is more than one transaction, so a
        failure partway through leaves the earlier rungs written and still
        counts the whole batch as dropped. Over-counting is the safe direction:
        `dropped` bounds what may be missing, and a buffered producer's rows are
        delivery rather than evidence. A caller that cannot carry that appends
        directly, on its own connection, the way the audit trail does.
        """
        from time import monotonic

        from .postgres import PostgresError

        batch = self._queue.drain()
        self._bytes = 0
        self._since = monotonic()
        if not batch:
            return 0
        try:
            return await self._log.append_many([(self._stream, values) for values in batch])
        except PostgresError, OSError, RuntimeError, ValueError:
            # Narrow on purpose, counted, and re-raised. The count is what makes
            # a log that is losing rows a number rather than a mystery; the
            # re-raise is what stops this deciding on the caller's behalf whether
            # losing them was survivable. A driver failure and a payload that
            # does not match the declaration are both losses of exactly this
            # batch, and the caller is the only one who knows which of those it
            # can carry on from.
            # `PostgresError` is named explicitly because it is **not** an
            # `OSError`: it descends straight from `Exception`, so a server-side
            # refusal -- the single most likely way a flush fails -- would
            # otherwise have escaped uncounted while a socket error was
            # counted. Imported here rather than at module scope so a
            # `PostgresLog` can still be described against a duck-typed database
            # (which is what the declaration tests do), and once per flush
            # rather than once per row.
            self._log._dropped += len(batch)
            raise


def retention_pass(
    declaration: Log,
    *,
    name: str,
    chunk: int = 1000,
    within: Any = "5s",
    shift: Any = "10s",
    pace: Any = None,
    schema: str = "wreath",
    tenant: str = "",
) -> Any:
    """A `wreath.passes.ChunkedPass` that drops this log's rows once they age out.

    `retain=` is a declaration and this is what executes it. `purge()` is the
    one statement it would otherwise take -- an unbounded `DELETE` over a table
    that only ever grows, which on a log large enough to need retention is a
    lock held for minutes and a WAL spike nobody scheduled. A pass is the same
    deletion done as a durable, resumable, paced keyset walk, and the rows it
    removes are **counted**: `ShiftResult.rows` per drive, `PassStatus.rows_done`
    cumulatively in the ledger. That is what makes "we have a retention policy"
    a number rather than a claim.

    The walk is `(at, seq)`: `at` because that is the ordered domain the
    retention window is measured in and the column `Log.statements` indexes for
    exactly this, and `seq` appended because two rows can share a
    `clock_timestamp()` and a chunk boundary that is not unique either skips its
    siblings or loops on them forever.

    Takes the declaration rather than a `PostgresLog`, because a
    `ChunkedPass` is handed its database when it is *driven* -- so a connection
    passed here would have nowhere to go. Drive it with
    `wreath.jobs.JobRunner.drive(pass_, cron=...)`.

    **Retention does not use the erasure door, deliberately.** An audit trail's
    append-only trigger refuses `DELETE` unless the transaction has set
    `wreath.audit_erasure`, and a retention walk could be taught to set it too.
    It is not, and it must not be: that setting is the *whole* of what stands
    between an audit record and a background job, and a scheduled walk holding
    it would mean the permission to delete evidence is granted every five
    minutes to a process nobody is watching. So the two doors stay separate --
    `AuditTrail.forget` names one subject and carries the setting for exactly
    one transaction, and retention is refused outright on a `KEEP_FOREVER` log,
    which is what an audit trail is. A log that both retains *and* wears the
    append-only trigger is a contradiction the trigger will report at the first
    chunk, and it is the declaration that is wrong.

    Args:
        declaration: the `Log` whose rows are being aged out.
        name: this pass's identity in the ledger.
        chunk: rows one chunk deletes.
        within: a chunk's time budget.
        shift: how long one stretch of work runs.
        pace: how much of the machine the walk may be. `DutyCycle()` by default.
        schema: where the pass ledger lives; match the job runner's.
        tenant: the ledger row's tenant.

    Raises:
        ValueError: on a `KEEP_FOREVER` log. There is nothing to age out, and a
            pass that silently walked one would be a scheduled deletion of
            evidence.
    """
    from .passes import ChunkedPass, Key, Purge, Rows, Sealed, Table

    if declaration.retain is None:
        raise ValueError(
            f"{declaration.qualified_table} was declared retain=KEEP_FOREVER, so "
            "there is nothing to age out and a retention walk over it would be a "
            "scheduled deletion of evidence. Erasure is AuditTrail.forget() or "
            "drop_stream(), which name the subject they remove."
        )
    return ChunkedPass(
        name,
        over=Table(declaration.table, schema=declaration.schema or None),
        units=Rows(
            key=(
                # Indexed because `Log.statements` emits `{table}_at_idx` for
                # exactly this walk whenever a retention is declared.
                Key("at", "timestamptz", indexed=True),
                Key("seq", "bigint", unique=True),
            ),
            limit=chunk,
            within=within,
        ),
        # The frontier *is* the retention window: everything the clock has
        # already carried past `retain` seconds ago. Re-derived per cycle, so
        # the walk recurs rather than completing once.
        frontier=Sealed(after=declaration.retain),
        work=Purge(),
        pace=pace,
        # A retention purge has no terminal step, so a skipped chunk buys no
        # irreversible thing -- and one undeletable row must not stop the table
        # from being kept small forever. The hole is still recorded and
        # `wreath passes retry` still comes back for it. Same call
        # `wreath._passes.stores.keyed_purge_pass` makes, for the same reason.
        on_chunk_failure="skip",
        shift=shift,
        schema=schema,
        tenant=tenant,
    )


def _rows_in(tag: Any) -> int:
    """The row count out of a command tag such as `INSERT 0 512`.

    The server's own answer rather than the length of the list that was sent,
    so "the batch landed" is a fact the database reported instead of an
    assumption this module made about its own SQL.
    """
    if not isinstance(tag, str):
        raise RuntimeError(f"expected a PostgreSQL command tag from a batched append, got {tag!r}")
    # Parsed by `wreath.store.rows_affected` and *raised on* here rather than
    # defaulted: everywhere else an unreadable tag means "this backend does not
    # say", which is survivable. On this path it would mean reporting a batch
    # landed without the server having said so, which is the assumption this
    # function exists to remove.
    count = rows_affected(tag)
    if count is None:
        raise RuntimeError(f"malformed command tag from a batched append: {tag!r}")
    return count


def _weigh(values: Mapping[str, Any]) -> int:
    """Roughly how many bytes a row will occupy, for the flush threshold.

    Deliberately approximate: the threshold is a policy knob, not an accounting
    boundary, and an exact answer would mean serialising the row twice.
    """
    total = 0
    for value in values.values():
        if isinstance(value, (str, bytes, bytearray, memoryview)):
            total += len(value)
        else:
            total += 16
    return total
