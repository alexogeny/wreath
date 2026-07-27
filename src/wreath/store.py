"""One keyed store, declared once, backed by memory or by PostgreSQL.

A rate limiter, an idempotency ledger, and a session table are three different
features that need the same small thing: values under a key, aged by a
timestamp, in one worker's memory or in a table every worker shares. Written
three times, the PostgreSQL half is re-derived three times -- and the six
re-derivations are exactly the parts that are easy to get subtly wrong:

* the table name reaches SQL by interpolation, so it must be a plain identifier;
* the schema is *offered* (:meth:`Keyed.schema_sql`) and never applied, because
  schema changes belong in the migration history with the rest of the schema;
* statements are prepared lazily, because a store is built while the application
  is being described and the database is not up yet;
* a claim is one ``INSERT ... ON CONFLICT ... RETURNING``, never a read followed
  by a write;
* the clock is ``clock_timestamp()`` and never ``now()``, which is frozen at
  transaction start;
* expired rows are dropped by an explicit :meth:`PostgresStore.purge`, never by a
  background thread.

Declare the shape once and pick a backend::

    declaration = Keyed(
        table="wreath_session",
        columns=(Column("data", "jsonb", null=False),),
        key="sid",
        prefix="wreath_session",
    )
    store = PostgresStore(database, declaration)

What is *not* shared is the payload and the semantics: what the columns mean,
whether the deadline is fixed per store or supplied per write, and whether the
rows are claimed at all. Each caller keeps those.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Final, NamedTuple

from .cache import BoundedCache

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The row alias every generated statement uses, so a caller writing its own SQL
#: against the same store can reference columns the same way.
ALIAS: Final = "s"


class _Claimed:
    """The value of a claimed key that has nothing stored under it yet."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CLAIMED"


#: A key is claimed but carries no payload -- the in-memory twin of the NULL
#: payload a PostgreSQL claim leaves behind.
CLAIMED: Final = _Claimed()


def sql_identifier(value: str, *, what: str = "table") -> str:
    """Return ``value`` if it is a bare SQL identifier, else raise.

    Table and column names are interpolated into statement text (they cannot be
    bound as parameters), so the only safe policy is to refuse anything that is
    not a plain identifier rather than to quote it.
    """
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{what} must be a plain SQL identifier")
    return value


class Column(NamedTuple):
    """One payload column: its name, its SQL type, and whether it may be NULL."""

    name: str
    sql_type: str
    null: bool = True


@dataclass(frozen=True, slots=True)
class Keyed:
    """The declaration of a keyed store: one key, some payload, one timestamp.

    Args:
        table: the backing table. Interpolated, so it must be a plain identifier.
        columns: the payload. Their meaning belongs to the caller.
        key: the primary-key column.
        stamp: the ``timestamptz`` column rows are aged by.
        deadline: whether ``stamp`` is a deadline (the row is dead once the clock
            passes it) or a last-touched mark. A last-touched store expires
            nothing -- ``updated`` is arithmetic for whoever reads it, and only
            an idle purge ages a row out.
        ttl: seconds a row lives, when the store owns that decision. ``None``
            means the caller supplies the lifetime per write.
        index_stamp: also declare an index on ``stamp``, which is what keeps
            purging a large table cheap.
        claim: build the atomic claim statement. Requires a deadline (there is
            nothing to reclaim without one) and a nullable payload (a claim
            resets it).
        prefix: prepended to prepared-statement names, so two stores over
            different tables never collide on one.
    """

    table: str
    columns: tuple[Column, ...] = ()
    key: str = "key"
    stamp: str = "expires"
    deadline: bool = True
    ttl: float | None = None
    index_stamp: bool = False
    claim: bool = False
    prefix: str = "wreath_store"

    def __post_init__(self) -> None:
        sql_identifier(self.table)
        sql_identifier(self.key, what="key")
        sql_identifier(self.stamp, what="stamp")
        sql_identifier(self.prefix, what="prefix")
        for column in self.columns:
            sql_identifier(column.name, what="column")
        if self.ttl is not None and self.ttl <= 0:
            raise ValueError("ttl must be positive")
        if not self.claim:
            return
        if not self.deadline:
            raise ValueError("a claim needs a deadline stamp: there is nothing to reclaim")
        if self.ttl is None:
            raise ValueError("a claim needs a ttl to set the deadline from")
        if any(not column.null for column in self.columns):
            raise ValueError(
                "a claim resets the payload, so every payload column must be nullable"
            )

    def schema_sql(self) -> str:
        """DDL for the backing table. Apply it as a migration.

        Nothing in Wreath runs this. A table that appears because a process
        started is a schema change with no history and no review; this way the
        change lands where the rest of the schema's changes land.
        """
        lines = [f"    {self.key} text PRIMARY KEY"]
        lines += [
            f"    {column.name} {column.sql_type}{'' if column.null else ' NOT NULL'}"
            for column in self.columns
        ]
        lines.append(f"    {self.stamp} timestamptz NOT NULL")
        body = ",\n".join(lines)
        schema = f"CREATE TABLE IF NOT EXISTS {self.table} (\n{body}\n)"
        if self.index_stamp:
            schema += (
                f";\nCREATE INDEX IF NOT EXISTS {self.table}_{self.stamp}_idx\n"
                f"    ON {self.table} ({self.stamp})"
            )
        return schema


def _interval(seconds: float | str) -> str:
    # A float is rendered as a literal (the store owns the lifetime); a string is
    # a placeholder, for when the caller supplies it per write.
    bound = seconds if isinstance(seconds, str) else repr(float(seconds))
    return f"make_interval(secs => {bound}::float8)"


@dataclass(slots=True)
class _Defined:
    sql: str
    workload: str
    statement: Any = field(default=None)


class PostgresStore:
    """A keyed store in a table every worker shares.

    Holds the six disciplines named in this module's docstring, and generates
    the statements a keyed store always wants -- :meth:`claim`, :meth:`read`,
    :meth:`delete`, :meth:`purge` -- from the declaration. Anything shaped by
    the caller's payload is written by the caller and registered with
    :meth:`define`, which gets it the same lazy preparation and the same
    statement naming.

    PostgreSQL owns the clock. Every generated statement reads
    ``clock_timestamp()``, so workers on disagreeing wall clocks cannot disagree
    about when a row expires, and ``clock_timestamp`` rather than ``now()``
    because ``now()`` is fixed at transaction start -- inside a transaction it
    would freeze time for as long as the transaction runs.

    Args:
        database: a :class:`~wreath.postgres.Database`.
        declaration: what the store holds.
        read_workload: the pool generated reads go to. ``"write"`` by default,
            because a store that claims must read back from the primary that
            accepted its claim; a store that only reads may pass ``"read"``.
    """

    __slots__ = ("_database", "_declaration", "_defined")

    def __init__(
        self, database: Any, declaration: Keyed, *, read_workload: str = "write"
    ) -> None:
        self._database = database
        self._declaration = declaration
        self._defined: dict[str, _Defined] = {}
        table, key, stamp = declaration.table, declaration.key, declaration.stamp
        # The SQL is built here; the prepared statements are not. A store is
        # constructed while the application is being described, which is before
        # any pool exists -- so the text is ready and the round trip that
        # registers it waits for the first call.
        if declaration.claim:
            window = self.window()
            self.define(
                "claim",
                self.upsert(
                    values={key: "$1", stamp: window},
                    # The payload is reset as well as the deadline: a reclaimed
                    # row must look exactly like a fresh one, or the next reader
                    # replays the previous holder's answer.
                    update={stamp: window} | {c.name: "NULL" for c in declaration.columns},
                    where=self.expired,
                    # Presence is the whole answer, so return the cheapest proof
                    # of it. See `claim`.
                    returning=f"{ALIAS}.{key}",
                ),
            )
        if declaration.columns:
            payload = ", ".join(column.name for column in declaration.columns)
            read = f"SELECT {payload} FROM {table} AS {ALIAS} WHERE {ALIAS}.{key} = $1"
            self.define("read", read, workload=read_workload)
            self.define("read_live", f"{read} AND {self.live}", workload=read_workload)
        self.define("delete", f"DELETE FROM {table} WHERE {key} = $1")
        if declaration.deadline:
            self.define("purge", f"DELETE FROM {table} AS {ALIAS} WHERE {self.expired}")
        self.define(
            "purge_idle",
            f"DELETE FROM {table} AS {ALIAS} "
            f"WHERE {ALIAS}.{stamp} < clock_timestamp() - {_interval('$1')}",
        )

    @property
    def declaration(self) -> Keyed:
        return self._declaration

    @property
    def table(self) -> str:
        return self._declaration.table

    @property
    def expired(self) -> str:
        """The predicate for a row the store no longer honours."""
        return f"{ALIAS}.{self._declaration.stamp} < clock_timestamp()"

    @property
    def live(self) -> str:
        """The predicate for a row that is still good.

        The exact complement of :attr:`expired`, so a purge can never drop a row
        a read would still have honoured.
        """
        return f"{ALIAS}.{self._declaration.stamp} >= clock_timestamp()"

    def window(self, seconds: float | str | None = None) -> str:
        """A deadline ``seconds`` from the database's clock, not the caller's."""
        if seconds is None:
            seconds = self._declaration.ttl
            if seconds is None:
                raise ValueError("this store has no ttl; pass the lifetime explicitly")
        return f"clock_timestamp() + {_interval(seconds)}"

    def upsert(
        self,
        *,
        values: Mapping[str, str],
        update: Mapping[str, str],
        where: str | None = None,
        returning: str | None = None,
    ) -> str:
        """One ``INSERT ... ON CONFLICT DO UPDATE`` over this store's key.

        Both mappings are ``column -> SQL expression``; the columns are
        interpolated, so each is checked as an identifier. The result is a
        single statement by construction -- two statements would be two chances
        for a concurrent worker to interleave.
        """
        for column in (*values, *update):
            sql_identifier(column, what="column")
        assignments = ",\n".join(f"    {column} = {expr}" for column, expr in update.items())
        sql = (
            f"INSERT INTO {self.table} AS {ALIAS} ({', '.join(values)})\n"
            f"VALUES ({', '.join(values.values())})\n"
            f"ON CONFLICT ({self._declaration.key}) DO UPDATE SET\n{assignments}"
        )
        if where is not None:
            sql += f"\nWHERE {where}"
        if returning is not None:
            sql += f"\nRETURNING {returning}"
        return sql

    def define(self, name: str, sql: str, *, workload: str = "write") -> None:
        """Register ``sql`` under ``name`` for lazy preparation."""
        if name in self._defined:
            raise ValueError(f"{name!r} is already defined on this store")
        self._defined[name] = _Defined(sql, workload)

    def sql(self, name: str) -> str:
        """The text registered under ``name``. Useful when explaining a plan."""
        return self._entry(name).sql

    def workload(self, name: str) -> str:
        return self._entry(name).workload

    def statement(self, name: str) -> Any:
        """The prepared statement for ``name``, preparing it on first use."""
        entry = self._entry(name)
        if entry.statement is None:
            entry.statement = self._database.statement(
                f"{self._declaration.prefix}_{name}_{self.table}",
                entry.sql,
                workload=entry.workload,
            )
        return entry.statement

    def _entry(self, name: str) -> _Defined:
        entry = self._defined.get(name)
        if entry is None:
            raise ValueError(f"no SQL named {name!r} on this store")
        return entry

    def schema_sql(self) -> str:
        """DDL for the backing table. Apply it as a migration."""
        return self._declaration.schema_sql()

    async def claim(self, key: str) -> bool:
        """Take ownership of ``key``, or report that someone else has it.

        One ``INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expired
        RETURNING``. A row comes back only when the insert succeeded or an
        expired row was reclaimed, so **"a row came back" is the claim** -- no
        owner column, no second round trip, and no window in which two workers
        both proceed. A read followed by a write would let both of them conclude
        they were first, which is the one thing a claim exists to prevent.
        """
        return await self.statement("claim").fetchrow(key) is not None

    async def read(self, key: str, *, live: bool = False) -> Any:
        """The payload columns for ``key``, or None.

        ``live=True`` refuses a row whose deadline has passed, for a store that
        purges lazily -- which is every store, since nothing purges in the
        background.
        """
        return await self.statement("read_live" if live else "read").fetchrow(key)

    async def delete(self, key: str) -> str:
        """Drop ``key``. Not an error when it is already gone."""
        return await self.statement("delete").execute(key)

    async def purge(self, idle_seconds: float | None = None) -> str:
        """Drop rows the store no longer honours.

        Nothing calls this for you: a background thread would duplicate across
        workers and swallow its own failures. Run it from a durable job.

        With ``idle_seconds``, drops rows untouched for that long instead --
        which is the only way to age out a last-touched store, whose stamp is
        never a deadline.
        """
        if idle_seconds is not None:
            return await self.statement("purge_idle").execute(float(idle_seconds))
        if not self._declaration.deadline:
            raise ValueError(
                f"{self._declaration.stamp} is a last-touched stamp, not a deadline; "
                "pass idle_seconds to purge by age"
            )
        return await self.statement("purge").execute()


class MemoryStore:
    """The same keyed store in one worker's memory: bounded, TTL'd, synchronous.

    Enough on its own for a single-worker deployment or a sticky load balancer.
    Behind anything else it is a fast path in front of a shared store rather
    than a substitute for one, because a second worker's memory knows none of
    this.

    Being synchronous is the feature, not an implementation detail: it is what
    makes :meth:`claim` atomic. There is no await between the read and the
    write, so no other task on this loop can interleave -- the in-process
    counterpart of the single statement :meth:`PostgresStore.claim` uses.

    Nothing needs purging: entries expire lazily when read and ``max_entries``
    bounds whatever is never read again.

    **The window opens when a key is first written, and writing again does not
    move it** -- the same lifetime :class:`PostgresStore` gives a claimed key,
    whose generated ``DO UPDATE`` leaves the stamp alone on purpose. A store
    that restarted the clock on every write would let a slow holder extend its
    own key indefinitely, and would mean the two backends honoured a key for
    different lengths of time behind one caller.
    """

    __slots__ = ("_cache", "_clock", "_ttl")

    def __init__(
        self,
        *,
        ttl: float | None = None,
        max_entries: int = 4096,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        # Entries are `(value, deadline)`. The deadline is the store's and it is
        # the one honoured; the cache's own TTL is a backstop that can never
        # fall earlier, because it is recomputed on each write while the
        # deadline is carried forward. Keeping the deadline in the value rather
        # than in a second dictionary is what bounds it: an LRU eviction at
        # `max_entries` drops the value and its deadline together.
        self._cache: BoundedCache = BoundedCache(
            max_entries=max_entries, ttl=ttl, clock=clock
        )
        self._ttl = ttl
        self._clock = clock

    def claim(self, key: str) -> bool:
        """Take ownership of ``key``, or report that it is already held."""
        if self.read(key) is not None:
            return False
        self._write(key, CLAIMED, deadline=None)
        return True

    def read(self, key: str) -> Any:
        """The stored value, :data:`CLAIMED` when claimed but unwritten, or None."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, deadline = entry
        if deadline is not None and self._clock() >= deadline:
            self._cache.delete(key)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``, keeping the deadline the key already has.

        A key that is absent or already past its deadline starts a fresh window;
        one that is live keeps the deadline it was claimed or first written with.
        """
        entry = self._cache.get(key)
        deadline = None
        if entry is not None:
            deadline = entry[1]
            if deadline is not None and self._clock() >= deadline:
                deadline = None
        self._write(key, value, deadline=deadline)

    def _write(self, key: str, value: Any, *, deadline: float | None) -> None:
        if deadline is None and self._ttl is not None:
            deadline = self._clock() + self._ttl
        self._cache.set(key, (value, deadline))

    def delete(self, key: str) -> bool:
        """Drop ``key``; report whether it was there."""
        return self._cache.delete(key)

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


__all__ = [
    "ALIAS",
    "CLAIMED",
    "Column",
    "Keyed",
    "MemoryStore",
    "PostgresStore",
    "sql_identifier",
]
