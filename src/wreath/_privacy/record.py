"""The record an erasure leaves behind, written by the erasure itself.

An erasure that cannot be proved is not much better than one that did not
happen. Article 5(2) makes the controller responsible for *demonstrating*
compliance, and the demonstration for a deletion is the awkward kind: the
evidence is the absence of something. Nothing in an empty table says a subject
was erased on the third of August rather than never having existed.

So the erasure writes its own receipt, and it writes it **in the transaction
that performs the last of the erasure** -- `wreath.log.PostgresLog.append`
takes the caller's connection for exactly this reason, and `wreath.audit_log`
already depends on that property for the same argument. A receipt written on
its own pooled connection commits whether or not the erasure did, which is a
record of erasures that may not have happened; and an erasure that commits
without its receipt is an erasure nobody can prove and nobody can replay.

**What the record holds, and what it deliberately does not.** The subject
identifier, the timestamp, the digest of the plan that ran, and two counts.
Nothing else. In particular no copy of any erased value: a record of what was
erased would make the evidence store a re-identification store, and the
subject's position would be worse than before they asked.

**It is personal data and it is retained anyway.** "User 4711 was erased on 3
August" is a fact about an identified person. Two reasons outrank minimisation
here, and they are written down rather than assumed:

* it is the evidence the erasure was performed; and
* a restore from a backup taken before the erasure reinstates the data, and the
  only way to re-erase it is to know the erasure happened. Without the record a
  restore silently un-erases a subject, which is the failure mode
  `wreath.privacy`'s docstring promises to answer rather than to hide.

Its own window is `Privacy(erasure_record_retain=...)` and there is **no
default**: the honest default is "as long as your oldest backup" and nothing in
here can know that. Unset is `wreath.log.KEEP_FOREVER`, and
`describe_retention` reports it as `UNBOUNDED` -- a finding a reader can act
on, unlike a silent forever.

**The audit log's erasure door is deliberately not opened from here.**
`wreath.audit_log`'s append-only trigger refuses a `DELETE` unless the
transaction has set `wreath.audit_erasure = 'on'`, and an erasure is exactly
the case that needs it -- but not *this* erasure. The audit trail is a
`wreath.log` table rather than a mapped model, so it is not in the ORM registry
and no foreign-key traversal reaches it; nothing here can generate a delete
against it, with or without the setting. `AuditTrail.erase` opens that door for
itself, in its own transaction, which is where it belongs: one transaction, one
caller, and no path by which a privacy walk could widen it.

**Exactly once, without a unique index to lean on.** Job delivery is
at-least-once and a chunk can run twice, so the write is guarded by a read of
the same table inside the same transaction, keyed on `(subject, plan digest)`.
That is sound here for a reason worth stating: the write happens inside a
`wreath.passes` chunk, and the pass ledger's compare-and-swap already makes one
pass name the property of one worker at a time -- so there is no concurrent
second writer for the read to miss. Two *different* erasures of one subject
carry different digests and both are recorded.
"""

from __future__ import annotations

from typing import Any, Final

from ..log import KEEP_FOREVER, Column, Log, PostgresLog

__all__ = ["ERASURE_TABLE", "ErasureRecord", "erasure_log"]

#: The backing table. Named for the schema it lives in rather than for the
#: module, because it is furniture in the `wreath` schema beside the pass
#: ledger, and an operator reading `\dt wreath.*` should be able to tell what
#: it is without opening the source.
ERASURE_TABLE: Final = "wreath_erasures"


def erasure_log(*, schema: str = "wreath", retain: float | None = KEEP_FOREVER) -> Log:
    """The `wreath.log.Log` declaration behind the erasure record.

    A log rather than a bespoke table, and not merely to save a `CREATE TABLE`.
    The properties this record needs are the ones `wreath.log` exists to hold:
    rows in *commit* order (a `bigserial` is allocated before commit, so a
    reader that remembers `max(seq)` skips rows a slower transaction lands
    behind it -- exactly the replay-after-restore reader this record is for),
    an append that takes the caller's connection, and retention that must be
    declared rather than defaulted.

    The stream is the subject, so "every erasure of this subject" is a range
    scan on the index the log already declares, and a restore replays one
    subject without reading the whole table.
    """
    return Log(
        table=ERASURE_TABLE,
        schema=schema,
        stream="subject",
        retain=retain,
        columns=(
            # The subject model and its identity column travel with the row
            # rather than being implied by the application that wrote it: a
            # restore may replay these against a process that no longer holds
            # the declaration that produced them.
            Column("subject_model", "text", null=False),
            Column("subject_column", "text", null=False),
            Column("plan_digest", "text", null=False),
            Column("tables_touched", "int", null=False),
            Column("rows_affected", "bigint", null=False),
        ),
        prefix="wreath_privacy",
    )


class ErasureRecord:
    """Writes one erasure's receipt on the connection the erasure is using.

    Holds no `wreath.postgres.Database`, and that is the design rather than an
    omission: every write here carries a caller's connection, because a write
    that could reach for a pooled one is a write that could commit on its own.
    A caller that wants the retention purge asks `Privacy.erasure_records`
    for a `PostgresLog` bound to a database.

    Args:
        schema: where the table lives. The same schema as the pass ledger, so
            an erasure's cursor and its receipt are recovered together.
        retain: seconds a record lives, or `KEEP_FOREVER`.
    """

    __slots__ = ("_declaration", "_exists", "_log")

    def __init__(
        self, *, schema: str = "wreath", retain: float | None = KEEP_FOREVER
    ) -> None:
        self._declaration = erasure_log(schema=schema, retain=retain)
        self._log = PostgresLog(None, self._declaration)
        self._exists = (
            f"SELECT 1 FROM {self._declaration.qualified_table} "
            f"WHERE {self._declaration.stream} = $1 AND plan_digest = $2 LIMIT 1"
        )

    @property
    def declaration(self) -> Log:
        """The `Log` this writes to. Frozen, so it is safe to share."""
        return self._declaration

    @property
    def table(self) -> str:
        """The table as it reaches SQL, schema-qualified."""
        return self._declaration.qualified_table

    def schema_sql(self) -> str:
        """DDL for the record table. Apply it as a migration."""
        return self._declaration.schema_sql()

    def bind(self, database: Any) -> PostgresLog:
        """A `PostgresLog` over the same table, for reading and for `purge`.

        The one place a database belongs: retention and inspection are ordinary
        pooled work, and neither has an erasure transaction to join.
        """
        return PostgresLog(database, self._declaration)

    async def write(
        self,
        connection: Any,
        *,
        plan: Any,
        tables_touched: int,
        rows_affected: int,
    ) -> bool:
        """Record one erasure on `connection`, unless it is already recorded.

        Args:
            connection: the transaction the erasure is running in. Not
                optional: a receipt on any other connection commits
                independently of the thing it is a receipt for.
            plan: the `ErasurePlan` that ran.
            tables_touched: how many tables the erasure wrote to.
            rows_affected: how many rows it changed or removed.

        Returns:
            Whether a row was written. `False` means this erasure was already
            recorded -- a redelivered job, a retried chunk -- which is a fact
            about the caller's world rather than an error in this one.
        """
        digest = plan.digest
        if await connection.fetchval(self._exists, plan.subject_id, digest) is not None:
            return False
        await self._log.append(
            plan.subject_id,
            connection=connection,
            subject_model=plan.subject_model,
            subject_column=plan.subject_column,
            plan_digest=digest,
            tables_touched=int(tables_touched),
            rows_affected=int(rows_affected),
        )
        return True
