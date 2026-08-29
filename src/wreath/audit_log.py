"""Who changed what, recorded by the thing that does the changing.

Every hand-rolled audit trail develops holes, and always the same way: it
depends on somebody remembering to call `record()`. The call is added to the
handler that changes a row and not to the background job that changes the same
row at 03:00, and the gap is invisible until an auditor asks a question the log
cannot answer.

Wreath does not have to work that way. The ORM knows what it wrote and which
fields changed, and it knows it *inside the transaction that wrote them*. So an
audit record here is **complete by construction**: it is not a thing the
application remembers to do, it is a thing the write does.

Declare which models are audited on the models themselves:

```python
class Photo(Model, table="photos"):
    caption: Mapped[str] = column(Text)
    exif_gps: Mapped[str | None] = column(Text, null=True)

    _audit = audited(redact={"exif_gps"})
```

and bind an actor to whatever is doing the writing:

```python
async with actor(f"user:{request.user.id}"):
    session.add(photo)
    await session.commit()
```

Three properties, each of which is the whole point rather than a refinement:

* **Atomic with the write.** The record is appended in the same transaction, so
  a crash between the row changing and the record landing cannot happen. A
  trail assembled after the fact is a trail with a window in it.
* **Attribution fails loudly.** A write to an audited model with no actor bound
  raises. A background job is an actor, a migration is an actor, a test is an
  actor -- what is not acceptable is a record that is complete except for the
  one field that makes it evidence.
* **Append-only in the database.** `wreath.audit_log` emits a `REVOKE` and a
  trigger, so the application's own role cannot rewrite history. An audit table
  the application can `UPDATE` is not an audit log to anybody who asks for one.

Retention is deliberately `KEEP_FOREVER`. An audit trail's lifetime is a
compliance decision rather than a disk-space one, and it is frequently the
record that must *survive* an erasure the underlying row does not --
`PostgresLog.drop_stream` is the deliberate, subject-naming exception.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

from .log import KEEP_FOREVER, Column, Cursor, Log, PostgresLog
from .orm.table import Facet

__all__ = [
    "REDACTED",
    "AuditTrail",
    "AuditLog",
    "Audited",
    "Change",
    "actor",
    "append_only_statements",
    "audited",
    "current_actor",
    "declaration",
]

#: What a redacted value is replaced by. A marker rather than an omission: "this
#: field changed and you may not see to what" and "this field did not change"
#: are different facts, and collapsing them loses the one an auditor wants.
REDACTED: Final = "[redacted]"

#: The transaction-scoped setting that lets a `DELETE` past the append-only
#: trigger. Set with `SET LOCAL`, so it lasts exactly one transaction and cannot
#: leak into the next user of a pooled connection.
ERASURE_SETTING: Final = "wreath.audit_erasure"


class Audited(Facet):
    """The `audit` facet: this model is audited, and these columns are redacted.

    Built by `audited`. The redacted column names are the facet's
    `columns`, so the ORM metaclass validates them when the class is
    created -- a redaction naming a column that was renamed two migrations ago
    is a `DeclarationError` at import rather than a redaction that quietly
    stopped covering anything.
    """

    __slots__ = ()

    namespace = "audit"

    @property
    def redact(self) -> frozenset[str]:
        """Columns whose values are replaced by `REDACTED`."""
        return frozenset(self.columns)


def audited(*, redact: Iterable[str] = ()) -> Audited:
    """Declare that a model's writes are recorded, redacting `redact`.

    ```python
    class Photo(Model, table="photos"):
        _audit = audited(redact={"exif_gps"})
    ```
    """
    names = tuple(sorted(redact))
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValueError(f"audited(redact=...) takes column names, got {name!r}")
    return Audited(names)


#: The actor for writes on this task. A `ContextVar` rather than a session
#: attribute because the actor belongs to the *caller*, not to the connection:
#: one request may open several sessions and one session may be shared, and in
#: both cases the answer to "who is doing this" is the same and comes from
#: further up.
_actor: ContextVar[str | None] = ContextVar("wreath_audit_actor", default=None)


@contextmanager
def actor(name: str) -> Any:
    """Bind `name` as the actor for writes inside this block.

    ```python
    with actor(f"user:{identity.sub}"):
        await session.commit()
    ```

    Nests: an inner block wins for its own duration and the outer one resumes
    afterwards, which is what makes "this request, except this bit, which is the
    system" expressible.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            "an actor needs a non-empty name; use something resolvable later, "
            "like 'user:41' or 'job:nightly-rollup', not 'system'"
        )
    token = _actor.set(name)
    try:
        yield
    finally:
        _actor.reset(token)


def current_actor() -> str | None:
    """The actor bound to this task, or `None`."""
    return _actor.get()


@dataclass(frozen=True, slots=True)
class Change:
    """One recorded write: what changed, on which row, by whom."""

    table: str
    key: str
    operation: str
    actor: str
    fields: Mapping[str, Any]

    @property
    def subject(self) -> str:
        """The log stream this record belongs to: one per audited row.

        A stream per row rather than per table is what makes "everything that
        ever happened to this row" a range scan, and it is also the unit an
        erasure request names.
        """
        return f"{self.table}:{self.key}"


@dataclass(frozen=True, slots=True)
class AuditLog(Log):
    """A log declaration whose schema claim includes its append-only guard."""

    def schema_claim(self, name: str) -> Any:
        from .schema import Component, Step

        return Component(
            name=name,
            schema=self.schema,
            relations=(self.table,),
            steps=(
                Step(version=1, statements=self.statements()),
                Step(
                    version=2,
                    statements=append_only_statements(self.table, schema=self.schema),
                ),
            ),
        )

    def schema_sql(self) -> str:
        return ";\n".join(self.schema_claim("audit_log").statements())


def declaration(table: str = "audit_records", *, schema: str = "wreath") -> AuditLog:
    """The `wreath.log` declaration an `AuditTrail` is built on.

    Separate from the trail so a deployment can emit the DDL (`wreath schema
    sql`) without constructing a database handle, the way every other wreath
    component does.
    """
    return AuditLog(
        table=table,
        schema=schema,
        # An audit trail's lifetime is a compliance decision, not a disk-space
        # one. Nothing ages these out; `drop_stream` removes a named subject.
        retain=KEEP_FOREVER,
        columns=(
            Column("actor", "text", null=False),
            Column("op", "text", null=False),
            Column("row_key", "text", null=False),
            Column("fields", "jsonb", null=False),
        ),
        prefix="wreath_audit",
    )


def append_only_statements(table: str, *, schema: str = "wreath") -> tuple[str, ...]:
    """DDL that stops the application rewriting its own history.

    Two mechanisms, because either alone is escapable: the `REVOKE` stops the
    ordinary role, and the trigger stops anyone whose role was granted more --
    including a superuser running a hand-written `UPDATE` at 2am, which is the
    case the trigger exists for.

    Emitted through `wreath.migrations` rather than executed here, because it is
    schema and schema belongs in the migration history with the rest of it.
    """
    qualified = f'"{schema}".{table}' if schema else table
    guard = f"{table}_append_only"
    # The function body is one line on purpose. A dollar-quoted body containing
    # `;\n` survives `Step.statements` -- which is a tuple precisely so nothing
    # has to split it -- and does *not* survive the four older call sites that
    # still split a `schema_sql()` blob on `";\n"`. Writing it flat costs
    # nothing and removes the trap rather than documenting it.
    # `UPDATE` is refused unconditionally: there is no legitimate reason to
    # change what a record says. `DELETE` is refused unless the transaction has
    # declared itself an erasure, because an audit trail holds personal data and
    # a subject may ask to be forgotten -- a trail that could not answer that
    # would force a deployment to choose between two compliance obligations.
    # The setting is transaction-scoped (`SET LOCAL`) and cannot be reached by
    # accident: an ordinary `DELETE` from a handler, a migration, or a curious
    # superuser at 2am does not have it, and gets the exception.
    return (
        f"REVOKE UPDATE, DELETE, TRUNCATE ON {qualified} FROM PUBLIC",
        f"CREATE OR REPLACE FUNCTION {qualified}_guard() RETURNS trigger AS "
        "$guard$ BEGIN "
        f"IF TG_OP = 'DELETE' AND current_setting('{ERASURE_SETTING}', true) = 'on' "
        "THEN RETURN OLD; END IF; "
        f"RAISE EXCEPTION 'the audit trail {qualified} is append-only; "
        "% is refused', TG_OP; "
        "END; $guard$ LANGUAGE plpgsql",
        f"DROP TRIGGER IF EXISTS {guard} ON {qualified}",
        f"CREATE TRIGGER {guard} BEFORE UPDATE OR DELETE ON {qualified} "
        f"FOR EACH ROW EXECUTE FUNCTION {qualified}_guard()",
        f"DROP TRIGGER IF EXISTS {guard}_truncate ON {qualified}",
        f"CREATE TRIGGER {guard}_truncate BEFORE TRUNCATE ON {qualified} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {qualified}_guard()",
    )


class Unattributed(RuntimeError):
    """A write to an audited model happened with no actor bound.

    Raised rather than recorded as `NULL`, and raised *before* the write rather
    than after it. An audit record whose actor is unknown answers none of the
    questions an audit trail is kept for -- so the correct behaviour is to refuse
    the write, which is also the behaviour that gets noticed in development
    rather than in an audit.
    """


class AuditTrail:
    """Appends a `Change` per audited write, inside the writing transaction.

    Installed on a session by `wreath.orm.Session(audit=...)`. The session calls
    `record` from inside its flush, which is inside its transaction, so the
    record and the row it describes commit or roll back together.
    """

    __slots__ = ("_log", "_recorded", "_refused")

    def __init__(self, log: PostgresLog) -> None:
        self._log = log
        self._recorded = 0
        self._refused = 0

    @property
    def log(self) -> PostgresLog:
        """The underlying log, for reads and for retention."""
        return self._log

    @property
    def recorded(self) -> int:
        """Records appended. Never resets."""
        return self._recorded

    @property
    def refused(self) -> int:
        """Writes refused for want of an actor. Never resets."""
        return self._refused

    def attribute(self) -> str:
        """The actor for the write about to happen, or raise.

        Split out from `record` so the session can refuse *before* it
        issues the statement rather than after: a write that has already
        happened cannot be un-happened by a failed append, and an audited write
        with no attribution must not reach the database at all.
        """
        name = current_actor()
        if name is None:
            self._refused += 1
            raise Unattributed(
                "a write to an audited model needs an actor; wrap it in "
                "wreath.audit_log.actor('user:41'). A background job, a "
                "migration and a test are all actors -- what is refused here is "
                "an audit record with nobody's name on it."
            )
        return name

    async def record(self, change: Change, *, connection: Any = None) -> Cursor:
        """Append one change, on the connection that made it.

        `connection` is not optional in practice: without it the append takes
        its own pooled connection and its own transaction, and the record then
        outlives a write that rolled back. It is keyword-with-a-default only
        because a caller replaying records into a fresh trail has no
        transaction to join. The session always passes one.
        """
        cursor = await self._log.append(change.subject, connection=connection, **_row(change))
        self._recorded += 1
        return cursor

    async def record_many(self, changes: Sequence[Change], *, connection: Any = None) -> int:
        """Append a flush's worth of changes together, returning how many landed.

        What the session calls. A record per statement would put an `INSERT`
        inside the writing transaction for every audited instance in the flush,
        which is a hundred round trips for a hundred rows -- the audit trail
        paying its own tax on top of the write it describes. `append_many`
        collapses that to one statement per power-of-two rung, on the *same*
        connection, so the property that makes the record evidence is unchanged:
        the batch is in the caller's transaction and commits if and only if it
        does.

        No cursors come back, because a batched `INSERT` makes no promise about
        the order of `RETURNING` and nothing here wanted one. `record` is still
        the single-record spelling, for a caller replaying into a fresh trail.

        No empty-batch guard here: `append_many` already issues no statement for
        an empty sequence and answers zero, so a second spelling of that check
        would be one more place for the two to drift apart.
        """
        written = await self._log.append_many(
            [(change.subject, _row(change)) for change in changes],
            connection=connection,
        )
        self._recorded += written
        return written

    async def history(self, table: str, key: str, *, after: Cursor | None = None) -> Any:
        """Everything that ever happened to one row, oldest first."""
        return await self._log.read(f"{table}:{key}", after=after or Cursor.start())

    async def forget(self, table: str, key: str) -> str:
        """Drop one subject's records outright.

        The erasure path, and deliberately the only one: retention is
        `KEEP_FOREVER`, so nothing ages an audit record out by accident. Removing
        evidence is always an explicit act naming exactly whose evidence it is.

        The `SET LOCAL` is what the append-only trigger looks for. It is
        transaction-scoped, so the permission to delete exists for exactly this
        statement and cannot travel with the pooled connection into whatever
        runs next.
        """
        database = self._log.database
        connection = await database.acquire("write")
        try:
            await connection.execute("BEGIN")
            try:
                await connection.execute(f"SET LOCAL {ERASURE_SETTING} = 'on'")
                status = await self._log.drop_stream(f"{table}:{key}", connection=connection)
                await connection.execute("COMMIT")
                return status
            except BaseException:
                # Including cancellation: an open transaction on a connection
                # about to go back to the pool is how the *next* user inherits
                # somebody else's aborted state.
                await connection.execute("ROLLBACK")
                raise
        finally:
            await database.release("write", connection)


def _row(change: Change) -> dict[str, Any]:
    """One change as the log's payload columns. Spelled once, for both appends."""
    return {
        "actor": change.actor,
        "op": change.operation,
        "row_key": change.key,
        "fields": json.dumps(change.fields, default=_stringify, sort_keys=True),
    }


def _stringify(value: Any) -> str:
    """Render a value JSON does not know. Never raises.

    A `UUID`, a `datetime`, a `Decimal`, an enum: the audit trail records what
    changed rather than round-tripping it, so a readable string is the right
    answer and a serialisation failure that fails the write is not.
    """
    return str(value)


def changed_fields(instance: Any, spec: Any, facet: Audited, *, mask: int | None) -> dict[str, Any]:
    """The values this write is setting, redacted where the facet says so.

    `mask` is the session's dirty-column mask for an update, or `None` for an
    insert or delete, where every column is part of the record.
    """
    redact = facet.redact
    fields: dict[str, Any] = {}
    for position, column in enumerate(spec.columns):
        if mask is not None and not mask & (1 << position):
            continue
        name = column.python_name
        if name in redact:
            fields[name] = REDACTED
            continue
        fields[name] = getattr(instance, name, None)
    return fields
