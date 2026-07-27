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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._json import dumps as _json_dumps
from .._json import loads as _json_loads

#: The pass state machine. Stage 1 walks and completes; verification and the
#: terminal gate are a later stage, and their phases are reserved here so the
#: column never has to be re-interpreted.
WALKING = "walking"
DONE = "done"
BLOCKED = "blocked"


def table_name(schema: str) -> str:
    return f'"{schema}".passes'


def schema_sql(schema: str) -> str:
    """DDL for the ledger. Never auto-applied -- run it through migrations.

    A table that appears because a process started is a schema change with no
    history and no review, which is the same stance ``JobRunner.schema_sql`` and
    every :mod:`wreath.store` declaration take.
    """
    table = table_name(schema)
    return (
        f'CREATE SCHEMA IF NOT EXISTS "{schema}";\n'
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        "  name text NOT NULL,\n"
        "  tenant text NOT NULL DEFAULT '',\n"
        "  phase text NOT NULL DEFAULT 'walking',\n"
        "  cursor jsonb,\n"
        "  ceiling jsonb,\n"
        "  pending jsonb NOT NULL DEFAULT '[]'::jsonb,\n"
        "  units_done bigint NOT NULL DEFAULT 0,\n"
        "  rows_done bigint NOT NULL DEFAULT 0,\n"
        "  denominator bigint,\n"
        "  denominator_kind text,\n"
        "  chunk_limit int NOT NULL DEFAULT 0,\n"
        "  paced_reason text,\n"
        "  started_at timestamptz,\n"
        "  last_advance timestamptz,\n"
        "  cycle_started timestamptz,\n"
        "  verified_at timestamptz,\n"
        "  verified_fact text,\n"
        "  last_error text,\n"
        "  PRIMARY KEY (name, tenant)\n"
        ");\n"
    )


_COLUMNS = (
    "name, tenant, phase, cursor, ceiling, pending, units_done, rows_done, "
    "denominator, denominator_kind, chunk_limit, paced_reason, started_at, "
    "last_advance, cycle_started, verified_at, verified_fact, last_error"
)


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One pass's durable position and phase, exactly as the table holds it."""

    name: str
    tenant: str
    phase: str
    cursor: Any
    ceiling: Any
    pending: Any
    units_done: int
    rows_done: int
    denominator: int | None
    denominator_kind: str | None
    chunk_limit: int
    paced_reason: str | None
    started_at: Any
    last_advance: Any
    cycle_started: Any
    verified_at: Any
    verified_fact: str | None
    last_error: str | None


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


def row_from_record(record: Any) -> LedgerRow:
    def field(name: str, index: int) -> Any:
        try:
            return record[name]
        except (KeyError, TypeError):
            return record[index]

    return LedgerRow(
        name=str(field("name", 0)),
        tenant=str(field("tenant", 1)),
        phase=str(field("phase", 2)),
        cursor=_json(field("cursor", 3)),
        ceiling=_json(field("ceiling", 4)),
        pending=_json(field("pending", 5)) or [],
        units_done=int(field("units_done", 6) or 0),
        rows_done=int(field("rows_done", 7) or 0),
        denominator=field("denominator", 8),
        denominator_kind=field("denominator_kind", 9),
        chunk_limit=int(field("chunk_limit", 10) or 0),
        paced_reason=field("paced_reason", 11),
        started_at=field("started_at", 12),
        last_advance=field("last_advance", 13),
        cycle_started=field("cycle_started", 14),
        verified_at=field("verified_at", 15),
        verified_fact=field("verified_fact", 16),
        last_error=field("last_error", 17),
    )


class Ledger:
    """The statements one pass issues against its own ledger row."""

    __slots__ = ("_name", "_schema", "_table", "_tenant")

    def __init__(self, *, schema: str, name: str, tenant: str = "") -> None:
        self._schema = schema
        self._name = name
        self._tenant = tenant
        self._table = table_name(schema)

    @property
    def table(self) -> str:
        return self._table

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
            f"SELECT {_COLUMNS} FROM {self._table} WHERE name = $1 AND tenant = $2",
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

    async def count_rows(self, executor: Any, rows: int) -> None:
        """Add this chunk's row count. The row is already locked by the swap."""
        await executor.execute(
            f"UPDATE {self._table} SET rows_done = rows_done + $3 "
            "WHERE name = $1 AND tenant = $2",
            self._name,
            self._tenant,
            int(rows),
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
            "WHERE name = $1 AND tenant = $2 AND phase IN ('walking', 'done')",
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


async def read_all(executor: Any, *, schema: str, name: str | None = None) -> list[LedgerRow]:
    """Every pass in one schema's ledger, or one of them by name."""
    table = table_name(schema)
    if name is None:
        records = await executor.fetch(
            f"SELECT {_COLUMNS} FROM {table} ORDER BY name, tenant"
        )
    else:
        records = await executor.fetch(
            f"SELECT {_COLUMNS} FROM {table} WHERE name = $1 ORDER BY tenant", name
        )
    return [row_from_record(record) for record in records or ()]


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
    "Ledger",
    "LedgerRow",
    "read_all",
    "row_from_record",
    "schema_sql",
    "table_name",
]
