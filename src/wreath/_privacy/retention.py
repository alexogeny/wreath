"""Retention windows as scheduled passes, and the record an erasure leaves.

Two things that look unrelated and are the same argument. A retention policy
nobody can count is a sentence in a document; a retention policy expressed as a
`wreath.passes.ChunkedPass` reports rows deleted per run, per table, in the
pass ledger, so "we delete support tickets after 90 days" becomes a number a
regulator can be shown. The erasure record is the same move for the other
obligation: it is how an erasure that happened stays provable, and how a
restore from backup can be made to replay it.

**The tombstone decision, written down once.** An erasure record names the
subject. That makes it personal data about an identified person -- "user 4711
was erased on 3 August" is a fact about them -- and it is retained anyway, for
two reasons that both outrank minimisation here:

* it is the evidence the erasure was performed, which is the controller's
  obligation to demonstrate; and
* a restore from a backup taken before the erasure reinstates the data, and the
  only way to re-erase it is to know that the erasure happened. Without the
  record, a restore silently un-erases a subject.

It holds the subject identifier, the timestamp, the plan digest and the counts
-- and deliberately nothing else. No copy of the erased values, which would
make the record a re-identification store, and no other personal columns.

Its own window is `Privacy(erasure_record_retain=...)`, which the operator sets
to their backup horizon. There is **no default**, because the honest default is
"as long as your oldest backup", and this module cannot know that. Unset, the
record is kept and `describe_retention` says out loud that it is unbounded --
which is a finding a reader can act on, unlike a silent forever.

The record's shape, its write path and the rest of that argument live in
`wreath._privacy.record`; this module keeps the *reporting* of it, because
"every window this application declared, and every table that has none" is one
question and it should have one answer.
"""

from __future__ import annotations

from typing import Any

from .record import erasure_log
from .registry import PrivacyRegistry, Retention

__all__ = [
    "describe_retention",
    "retention_passes",
    "schema_sql",
]


def retention_passes(
    registry: PrivacyRegistry,
    *,
    limit: int = 1000,
    within: str = "2s",
    schema: str = "wreath",
    workload: str = "write",
) -> tuple[tuple[Retention, Any], ...]:
    """One `ChunkedPass` per declared retention window.

    The window is expressed as the pass's own frontier rather than as a
    predicate: `Sealed(after=...)` re-derives "everything the database clock
    has already passed" every cycle, which is exactly what a retention window
    is.
    Written as a `WHERE now() - interval` instead, the finish line would move
    while the walk ran and a busy table's cycle could never end -- and the
    frontier is read once per cycle from the *database's* clock, so workers on
    disagreeing wall clocks agree on where a cycle stops.

    That choice fixes the walk order too: the leading key column is the
    timestamp, with the primary key beneath it to break ties, because a
    clock-derived frontier is a point on the clock and has to be compared
    against one.

    Deletions are counted by the pass ledger, which is the property that makes
    a retention policy checkable rather than merely stated.

    Drive them with `jobs.drive(pass_, cron=...)`; scheduling belongs to
    `wreath.jobs`, which already deduplicates a cron tick fleet-wide, and a
    second scheduler here would be a second answer to a solved question.
    """
    from ..passes import ChunkedPass, DutyCycle, Purge, Rows, Sealed
    from .execute import primary_key

    built: list[tuple[Retention, Any]] = []
    for model in sorted(
        registry.retentions, key=lambda item: getattr(item, "__name__", "")
    ):
        policy = registry.retentions[model]
        # No "has this a primary key?" guard: `wreath.orm.Model` refuses a
        # mapped model without one at class creation, so a second check here
        # would be a clause the guard above it already subsumes -- and two
        # spellings of one condition is how they drift apart later.
        primary = primary_key(model)
        key = (
            getattr(model, policy.on),
            *(getattr(model, item.python_name) for item in primary),
        )
        built.append(
            (
                policy,
                ChunkedPass(
                    f"privacy_retain_{getattr(model, '__wreath_table__', 'rows')}",
                    over=model,
                    units=Rows(key=key, limit=limit, within=within),
                    frontier=Sealed(after=policy.after),
                    work=Purge(),
                    pace=DutyCycle(0.25),
                    schema=schema,
                    workload=workload,
                ),
            )
        )
    return tuple(built)


def describe_retention(
    registry: PrivacyRegistry, *, erasure_record_retain: float | None
) -> tuple[str, ...]:
    """Every declared window as a line, plus the erasure record's own.

    Absence is stated. A model with personal columns and no retention window
    is listed as unbounded rather than omitted, because "we could not find a
    retention policy for this table" is exactly the finding a reader is looking
    for and a silence reads as "there isn't a problem".
    """
    lines: list[str] = []
    for model, policy in sorted(
        registry.retentions.items(), key=lambda item: getattr(item[0], "__name__", "")
    ):
        name = getattr(model, "__name__", str(model))
        reason = f" -- {policy.reason}" if policy.reason else ""
        lines.append(
            f"{name}: rows deleted {_days(policy.after)} after {policy.on}{reason}"
        )
    for model, item in sorted(
        registry.classifications.items(), key=lambda pair: getattr(pair[0], "__name__", "")
    ):
        if model in registry.retentions or not item.personal:
            continue
        name = getattr(model, "__name__", str(model))
        lines.append(
            f"{name}: UNBOUNDED -- holds personal data ({', '.join(sorted(item.personal))}) "
            "with no declared retention window"
        )
    if erasure_record_retain is None:
        lines.append(
            "erasure records: UNBOUNDED -- set Privacy(erasure_record_retain=...) to "
            "your backup horizon. They identify erased subjects and are kept so a "
            "restore can replay the erasure"
        )
    else:
        lines.append(
            f"erasure records: deleted {_days(erasure_record_retain)} after the erasure"
        )
    return tuple(lines)


def _days(seconds: float) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    return f"{seconds:g}s"


def schema_sql(schema: str = "wreath", *, retain: float | None = None) -> str:
    """DDL for the erasure record in *schema*. Apply it as a migration.

    Offered, never applied -- schema changes belong in the migration history
    with the rest of the schema, which is the rule `wreath.store` and
    `wreath.passes` already follow.

    `retain` only decides whether the age index is declared, because that index
    exists to keep the retention purge from being a sequential scan of a table
    that only ever grows. Emitting it for a `KEEP_FOREVER` record would be an
    index nothing ever reads.
    """
    return erasure_log(schema=schema, retain=retain).schema_sql()
