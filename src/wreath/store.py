"""One keyed store, declared once, backed by memory or by PostgreSQL.

A rate limiter, an idempotency ledger, and a session table are three different
features that need the same small thing: values under a key, aged by a
timestamp, in one worker's memory or in a table every worker shares. Written
three times, the PostgreSQL half is re-derived three times -- and the six
re-derivations are exactly the parts that are easy to get subtly wrong:

* the table name reaches SQL by interpolation, so it must be a plain identifier;
* the schema is *offered* (`Keyed.schema_sql`) and never applied, because
  schema changes belong in the migration history with the rest of the schema;
* statements are prepared lazily, because a store is built while the application
  is being described and the database is not up yet;
* a claim is one `INSERT ... ON CONFLICT ... RETURNING`, never a read followed
  by a write;
* the clock is `clock_timestamp()` and never `now()`, which is frozen at
  transaction start;
* expired rows are dropped by an explicit `PostgresStore.purge`, never by a
  background thread.

Declare the shape once and pick a backend:

```python
declaration = Keyed(
    table="wreath_session",
    columns=(Column("data", "jsonb", null=False),),
    key="sid",
    prefix="wreath_session",
)
store = PostgresStore(database, declaration)
```

What is *not* shared is the payload and the semantics: what the columns mean,
whether the deadline is fixed per store or supplied per write, and whether the
rows are claimed at all. Each caller keeps those.
"""

from __future__ import annotations

import datetime
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Final, NamedTuple

from ._pgname import MAX_IDENTIFIER_BYTES, validate_unquoted_identifier
from .kv import KV

#: Reserved words that cannot be a bare identifier in PostgreSQL. Not the whole
#: list -- that is hundreds long and mostly words nobody names a table -- but
#: the ones people actually reach for. A name from here reaches the generated
#: DDL unquoted and fails there, a long way from the declaration that caused it.
_RESERVED = frozenset(
    {
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "authorization",
        "between",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "constraint",
        "create",
        "cross",
        "current_date",
        "current_role",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "from",
        "grant",
        "group",
        "having",
        "in",
        "initially",
        "inner",
        "intersect",
        "into",
        "is",
        "join",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "natural",
        "not",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "overlaps",
        "placing",
        "primary",
        "references",
        "returning",
        "right",
        "select",
        "session_user",
        "similar",
        "some",
        "symmetric",
        "table",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "verbose",
        "when",
        "where",
        "window",
        "with",
    }
)

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


class Sql(str):
    """A fragment the caller is asserting is safe to interpolate.

    Column *names* are checked as identifiers; the expressions beside them
    cannot be, because an expression is arbitrary SQL by definition. The type is
    the check: a plain `str` in an expression position is refused, so text that
    arrived from a request cannot reach the statement without somebody having
    written `Sql(...)` around it and meant it.

    A bind placeholder (`$1`) needs no marker -- it is the safe form, and
    requiring ceremony for it would push callers toward the unsafe one.
    """

    __slots__ = ()


_PLACEHOLDER = re.compile(r"^\$\d+(::[A-Za-z0-9_ ]+)?$")


def _expression(value: object, *, what: str) -> str:
    """Accept a placeholder or an explicitly marked fragment; refuse plain text."""
    if isinstance(value, Sql):
        return str(value)
    if isinstance(value, str) and _PLACEHOLDER.fullmatch(value.strip()):
        return value
    raise TypeError(
        f"{what} must be a bind placeholder like '$1' or an explicitly marked "
        f"Sql(...) fragment, not {value!r}. Anything built from request data "
        "belongs in a placeholder."
    )


def sql_identifier(value: str, *, what: str = "table") -> str:
    """Return `value` if it is a bare SQL identifier, else raise.

    Table and column names are interpolated into statement text (they cannot be
    bound as parameters), so the only safe policy is to refuse anything that is
    not a plain identifier rather than to quote it.
    """
    if isinstance(value, str) and value.lower() in _RESERVED:
        raise ValueError(
            f"{what} {value!r} is a reserved SQL word; it reaches the generated "
            "statement unquoted and would fail there. Pick another name."
        )
    return validate_unquoted_identifier(value, what)


class Column(NamedTuple):
    """One payload column: its name, its SQL type, and whether it may be NULL."""

    name: str
    sql_type: str
    null: bool = True


@dataclass(frozen=True, slots=True)
class Keyed:
    """The declaration of a keyed store: one key, some payload, one timestamp.

    A last-touched store (`deadline=False`) expires nothing: the stamp is
    arithmetic for whoever reads it, and only an idle purge ages a row out.

    `claim=True` requires a deadline -- there is nothing to reclaim without one
    -- and every payload column nullable, because a claim resets the payload.

    An index on the stamp is what keeps purging a large table cheap, and the
    prefix is what keeps two stores over different tables from colliding on one
    prepared-statement name.

    Args:
        table: the backing table. Interpolated, so it must be a plain identifier.
        columns: the payload. Their meaning belongs to the caller.
        key: the primary-key column.
        stamp: the `timestamptz` column rows are aged by.
        deadline: whether `stamp` is a deadline or a last-touched mark.
        ttl: seconds a row lives. `None` means the caller supplies it per write.
        index_stamp: also declare an index on `stamp`.
        claim: build the atomic claim statement.
        prefix: prepended to prepared-statement names.
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
            raise ValueError("a claim resets the payload, so every payload column must be nullable")

    def statements(self) -> tuple[str, ...]:
        """DDL for the backing table, one statement per element.

        The table name is **unqualified**, so it lands in whatever `search_path`
        resolves to. That is where every deployment's rows already are, and
        moving them into the `wreath` schema would not be additive -- a worker on
        the previous version would look for the old name. See
        `wreath.schema.Component.qualified`.
        """
        lines = [f"    {self.key} text PRIMARY KEY"]
        lines += [
            f"    {column.name} {column.sql_type}{'' if column.null else ' NOT NULL'}"
            for column in self.columns
        ]
        lines.append(f"    {self.stamp} timestamptz NOT NULL")
        body = ",\n".join(lines)
        parts = [f"CREATE TABLE IF NOT EXISTS {self.table} (\n{body}\n)"]
        if self.index_stamp:
            parts.append(
                f"CREATE INDEX IF NOT EXISTS {self.table}_{self.stamp}_idx\n"
                f"    ON {self.table} ({self.stamp})"
            )
        return tuple(parts)

    def schema_claim(self, name: str) -> Any:
        """This declaration's claim on the wreath schema, under `name`.

        `name` is the caller's because one `Keyed` shape backs three different
        subsystems -- sessions, rate limits, idempotency -- and each needs its own
        version marker and its own advisory lock. Sharing one would make an
        upgrade to sessions block a worker that only uses rate limits.

        Not `component()`. That name belongs to the zero-argument protocol
        `Wreath.schema_components` calls, and this needs an argument, so an
        object that reached the walk holding *this* method would have raised
        `TypeError` rather than contributing a claim. Two layers, two names.
        """
        from .schema import Component, Step

        return Component(
            name=name,
            schema="",
            relations=(self.table,),
            steps=(Step(version=1, statements=self.statements()),),
        )

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined.

        A derivation of `statements()`. Wreath applies these itself during
        lifespan now; this is retained for a caller applying the DDL by hand,
        and `wreath schema sql` is the supported spelling.
        """
        return ";\n".join(self.statements())


def rows_affected(status: Any) -> int | None:
    """The row count in a PostgreSQL command tag, or None when unreadable.

    A tag is `DELETE 5`, `UPDATE 3`, or -- the one that catches people --
    `INSERT 0 5`, where the first number is a legacy OID and the *second* is the
    count. So the count is the **last** field, never the second, and four
    separate readers in this tree disagreed about that: two took the second
    field and returned `None` for every `INSERT`, which is indistinguishable
    from "the driver reported nothing" at the call site.

    `None` rather than `0` when it cannot be read, because "no rows matched" and
    "this backend does not say" are different facts and a caller recording the
    first when it meant the second is reporting a clean sweep that never
    happened. A test double or a backend that reports nothing is a supported
    caller, not a broken one.
    """
    if not isinstance(status, str):
        return None
    _, _, count = status.rpartition(" ")
    try:
        return int(count)
    except ValueError:
        return None


def _interval(seconds: float | str | datetime.timedelta) -> Sql:
    # A float is rendered as a literal (the store owns the lifetime); a string is
    # a placeholder, for when the caller supplies it per write.
    # A `timedelta` -- which is what `wreath.temporal.Duration` is -- is read
    # first and reduced to seconds. It deliberately does *not* go through
    # `Duration.of`, because a `str` here is a bind placeholder rather than an
    # ISO-8601 duration, and reading one as the other would compile a
    # placeholder name into a literal interval.
    if isinstance(seconds, datetime.timedelta):
        seconds = seconds.total_seconds()
    if isinstance(seconds, str):
        return Sql(f"make_interval(secs => {_expression(seconds, what='an interval')}::float8)")
    value = float(seconds)
    # The literal goes into statement text, so a non-finite or negative lifetime
    # would be discovered by PostgreSQL at prepare time (or, for a negative one,
    # not at all -- it would just make every row born expired).
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("a store lifetime must be a finite number of seconds")
    if value <= 0:
        raise ValueError("a store lifetime must be positive")
    return Sql(f"make_interval(secs => {value!r}::float8)")


@dataclass(slots=True)
class _Defined:
    sql: str
    workload: str
    statement: Any = field(default=None)


class _Statements:
    """Lazy PostgreSQL statements shared by stores and append-only logs."""

    _database: Any
    _defined: dict[str, _Defined]
    _prepare_lock: threading.Lock
    _statement_owner: str

    def _init_statements(self) -> None:
        self._defined = {}
        # The call into Database.statement releases the GIL. Two first callers
        # can therefore both observe an empty entry unless preparation is
        # serialised, and PostgreSQL rejects the loser's duplicate name.
        self._prepare_lock = threading.Lock()

    def define(self, name: str, sql: str, *, workload: str = "write") -> None:
        """Register `sql` under `name` for lazy preparation."""
        if name in self._defined:
            raise ValueError(f"{name!r} is already defined on this {self._statement_owner}")
        # PostgreSQL truncates rather than refuses an over-long prepared name.
        # Refuse while the application is being described, before two distinct
        # declarations can silently acquire the same server-side name.
        statement_name = self._statement_name(name)
        name_bytes = len(statement_name.encode("utf-8"))
        if name_bytes > MAX_IDENTIFIER_BYTES:
            raise ValueError(
                f"prepared-statement name {statement_name!r} is {name_bytes} bytes; "
                f"PostgreSQL truncates at {MAX_IDENTIFIER_BYTES}, which would "
                f"collide with another {self._statement_owner}. Shorten the table "
                "name or the prefix."
            )
        self._defined[name] = _Defined(sql, workload)

    def _statement_name(self, name: str) -> str:
        raise NotImplementedError

    def sql(self, name: str) -> str:
        """The text registered under `name`. Useful when explaining a plan."""
        return self._entry(name).sql

    def workload(self, name: str) -> str:
        """The pool the statement registered under `name` runs against."""
        return self._entry(name).workload

    def statement(self, name: str) -> Any:
        """The prepared statement for `name`, preparing it on first use."""
        entry = self._entry(name)
        if entry.statement is None:
            with self._prepare_lock:
                if entry.statement is None:
                    entry.statement = self._database.statement(
                        self._statement_name(name),
                        entry.sql,
                        workload=entry.workload,
                    )
        return entry.statement

    def _entry(self, name: str) -> _Defined:
        entry = self._defined.get(name)
        if entry is None:
            raise ValueError(f"no SQL named {name!r} on this {self._statement_owner}")
        return entry


class PostgresStore(_Statements):
    """A keyed store in a table every worker shares.

    Holds the six disciplines named in this module's docstring, and generates
    the statements a keyed store always wants -- `claim`, `read`,
    `delete`, `purge` -- from the declaration. Anything shaped by
    the caller's payload is written by the caller and registered with
    `define`, which gets it the same lazy preparation and the same
    statement naming.

    PostgreSQL owns the clock. Every generated statement reads
    `clock_timestamp()`, so workers on disagreeing wall clocks cannot disagree
    about when a row expires, and `clock_timestamp` rather than `now()`
    because `now()` is fixed at transaction start -- inside a transaction it
    would freeze time for as long as the transaction runs.

    Generated reads go to the write pool by default, because a store that claims
    must read back from the primary that accepted its claim. A store that only
    reads may pass `read_workload="read"`.

    Args:
        database: a `wreath.postgres.Database`.
        declaration: what the store holds.
        read_workload: the pool generated reads go to. `"write"` by default.
    """

    __slots__ = ("_database", "_declaration", "_defined", "_prepare_lock")

    _statement_owner = "store"

    def __init__(self, database: Any, declaration: Keyed, *, read_workload: str = "write") -> None:
        self._database = database
        self._declaration = declaration
        self._init_statements()
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
                    update={stamp: window}
                    | {column.name: Sql("NULL") for column in declaration.columns},
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
        """The `Keyed` this store was built from. Frozen, so it is safe to share.

        What a purge pass is handed: everything needed to walk the table --
        its name, its key, its stamp -- with no reference back to this store or
        its connection pool.
        """
        return self._declaration

    @property
    def table(self) -> str:
        """The backing table's name, already checked as a plain identifier."""
        return self._declaration.table

    @property
    def expired(self) -> Sql:
        """The predicate for a row the store no longer honours."""
        return Sql(f"{ALIAS}.{self._declaration.stamp} < clock_timestamp()")

    @property
    def live(self) -> Sql:
        """The predicate for a row that is still good.

        The exact complement of `expired`, so a purge can never drop a row
        a read would still have honoured.
        """
        return Sql(f"{ALIAS}.{self._declaration.stamp} >= clock_timestamp()")

    def window(self, seconds: float | str | datetime.timedelta | None = None) -> Sql:
        """A deadline `seconds` from the database's clock, not the caller's.

        A string lifetime must be a bind placeholder; see `Sql`.
        """
        if seconds is None:
            seconds = self._declaration.ttl
            if seconds is None:
                raise ValueError("this store has no ttl; pass the lifetime explicitly")
        return Sql(f"clock_timestamp() + {_interval(seconds)}")

    def upsert(
        self,
        *,
        values: Mapping[str, str],
        update: Mapping[str, str],
        where: str | None = None,
        returning: str | None = None,
    ) -> str:
        """One `INSERT ... ON CONFLICT DO UPDATE` over this store's key.

        Both mappings are `column -> expression`. The columns are
        interpolated, so each is checked as an identifier; the expressions are
        checked as `Sql` -- a bind placeholder, or a fragment the caller
        marked deliberately. The result is a single statement by construction --
        two statements would be two chances for a concurrent worker to
        interleave.
        """
        for column in (*values, *update):
            sql_identifier(column, what="column")
        checked_values = {
            column: _expression(expr, what=f"the value for {column!r}")
            for column, expr in values.items()
        }
        checked_update = {
            column: _expression(expr, what=f"the update for {column!r}")
            for column, expr in update.items()
        }
        values, update = checked_values, checked_update
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

    def _statement_name(self, name: str) -> str:
        return f"{self._declaration.prefix}_{name}_{self.table}"

    def schema_claim(self, name: str) -> Any:
        """This store's claim on the wreath schema, under `name`."""
        return self._declaration.schema_claim(name)

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined."""
        return self._declaration.schema_sql()

    async def claim(self, key: str) -> bool:
        """Take ownership of `key`, or report that someone else has it.

        One `INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expired RETURNING`.
        A row comes back only when the insert succeeded or an
        expired row was reclaimed, so **"a row came back" is the claim** -- no
        owner column, no second round trip, and no window in which two workers
        both proceed. A read followed by a write would let both of them conclude
        they were first, which is the one thing a claim exists to prevent.
        """
        return await self.statement("claim").fetchrow(key) is not None

    async def read(self, key: str, *, live: bool = False) -> Any:
        """The payload columns for `key`, or None.

        `live=True` refuses a row whose deadline has passed, for a store that
        purges lazily -- which is every store, since nothing purges in the
        background.
        """
        return await self.statement("read_live" if live else "read").fetchrow(key)

    async def delete(self, key: str) -> str:
        """Drop `key`. Not an error when it is already gone."""
        return await self.statement("delete").execute(key)

    async def purge_count(self, idle_seconds: float | None = None) -> int | None:
        """`purge`, reporting how many rows went.

        `None` when the driver's status cannot be read -- a test double, a
        backend that reports nothing. Splitting this from `purge` keeps
        the status string available to callers that want it while giving the
        scheduled job that runs this something it can actually record: "the
        purge ran" and "the purge removed 40 000 rows" are different facts.
        """
        return rows_affected(await self.purge(idle_seconds))

    async def purge(self, idle_seconds: float | None = None) -> str:
        """Drop rows the store no longer honours.

        Nothing calls this for you: a background thread would duplicate across
        workers and swallow its own failures. Run it from a durable job.

        With `idle_seconds`, drops rows untouched for that long instead --
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


class MemoryStore(KV):
    """The same keyed store in one worker's memory: bounded, TTL'd, synchronous.

    Enough on its own for a single-worker deployment or a sticky load balancer.
    Behind anything else it is a fast path in front of a shared store rather
    than a substitute for one, because a second worker's memory knows none of
    this.

    Being synchronous is the feature, not an implementation detail: it is what
    makes `claim` atomic. There is no await between the read and the
    write, so no other task on this loop can interleave -- the in-process
    counterpart of the single statement `PostgresStore.claim` uses.

    Nothing needs purging: entries expire lazily when read and `max_entries`
    bounds whatever is never read again.

    **The window opens when a key is first written, and writing again does not
    move it.** A store that restarted the clock on every write would let a slow
    holder extend its own key indefinitely -- an idempotency key would outlive
    its TTL for as long as the handler kept touching it.

    Over PostgreSQL the same rule is enforced a statement at a time rather than
    by this class: a caller's own upsert omits the stamp from its `DO UPDATE`,
    the way `middleware.idempotency.PostgresIdempotencyStore`'s `store`
    statement does. The generated `PostgresStore.claim` is the deliberate
    exception -- its `DO UPDATE` *does* reset the stamp, because it only fires
    on an already-expired row and a reclaimed row must look exactly like a fresh
    one. Matching here is what lets one caller swap the backends and have a key
    honoured for the same length of time.
    """

    def __init__(
        self,
        *,
        ttl: float | None = None,
        max_entries: int = 4096,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        # Its own signature rather than `KV`'s, because this one is half of a
        # two-backend protocol: it is keyword-only and it holds more keys by
        # default than a cache would, and `PostgresStore` beside it takes the
        # same shape. `clock is monotonic` becomes `None` so the default keeps
        # the table's C clock rather than paying for a Python call per read.
        super().__init__(
            max_entries=max_entries,
            ttl=ttl,
            clock=None if clock is monotonic else clock,
        )

    def claim(self, key: str) -> bool:  # type: ignore[override]
        """Take ownership of `key`, or report that it is already held."""
        return super().claim(key, CLAIMED)

    def read(self, key: str) -> Any:
        """The stored value, `CLAIMED` when claimed but unwritten, or None."""
        return self.get(key)

    def set(self, key: str, value: Any) -> None:  # type: ignore[override]
        """Store `value` under `key`, keeping the deadline the key already has.

        A key that is absent or already past its deadline starts a fresh window;
        one that is live keeps the deadline it was claimed or first written with.
        """
        super().set(key, value, keep_deadline=True)


__all__ = [
    "ALIAS",
    "CLAIMED",
    "Sql",
    "Column",
    "Keyed",
    "MemoryStore",
    "PostgresStore",
    "rows_affected",
    "sql_identifier",
]
