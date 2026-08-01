"""Sealing: the point at which a bucket stops being a question and becomes an answer.

A bucket `[start, end)` is **sealed** once `now() >= end + lateness`, where
the lateness is declared by `.seal(after=...)`. Before that it is **open** and
every read recomputes it, because it can still change. After it, the value is
final, and recomputing it is not a cheap safety net -- it is doing the same
arithmetic over the same rows to reach the same number, once per reader,
forever.

**A settled bucket is not a cache**, and the vocabulary here keeps that
straight. A cache may be evicted, must be recomputable, and manages staleness
with a TTL. A settled row has no TTL, is never evicted, and is not recomputed on
a hunch. The words are `seal`, `sealed`, `open` and `settled`; the
response cache is a genuine cache and keeps its own name.

**Nothing here deletes anything.** Settling stores a value; it does not touch the
rows it was computed from. Retention, archival and destruction are later stages
and each is opt-in.

The late write
--------------

The hard case is a row that lands behind the watermark: a trek recorded late, a
backfill, an import that ran on Monday for Friday's work. Three answers, and the
design settles on the third:

* **Refuse the write.** Wrong, decisively. `Trek` is a business table, and a
  chart's watermark must never be able to fail a business write -- the same rule
  that says a broken cache subscriber cannot fail a committed write.
* **Re-open the bucket.** Right in spirit, but only sound while the raw rows are
  still there to recompute from. That is a shrinking window once retention
  lands, and a rule that works for three days and then quietly stops is worse
  than one that never worked. Available as `on_late="reopen"`, never the
  default.
* **Record a correction.** The settled value stays immutable and the difference
  is stored beside it, folded in when the series is read. A delta is small, it
  survives the raw rows being archived, and it is *observable*: the envelope
  reports which buckets carry one, so late data arriving looks like late data
  arriving rather than like a discrepancy someone finds in a spreadsheet.

Who notices a late write, and why it is not the write path
----------------------------------------------------------

Nothing here hooks the ORM's write events, and that is deliberate rather than an
omission. `_orm_events` is **model-grained on purpose** -- it publishes the set
of model *names* a session touched, with a written argument against carrying
rows -- so it cannot say which bucket a late row belongs to or what it
contributes. Making it row-grained to serve this would put per-row bookkeeping
on every write in the application to save work on a chart.

So corrections are found by `reconcile`, an operation the application runs
deliberately: it recomputes sealed buckets, compares them to what was stored,
and records the difference. Stage 8's rollup job is its intended caller, and the
design already notes that a chunked backfill and a rollup want the same
machinery. Until something calls it, a late write is not *silently* absorbed --
`SealState.settled_through` and the envelope's `corrections` say exactly
how far the settled data has been reconciled, so the gap is visible rather than
assumed away.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from typing import Any

from ..temporal import Bucket, wall_clock, zone

#: These tables are wreath's own, so they live in the `wreath` schema and wreath
#: creates them itself during lifespan -- the same rule the job ledger and the
#: pass ledger follow. They are deliberately *not* in the application's
#: migration artifact: nobody declared a settled-bucket store, and the artifact
#: describes what the author declared.
SCHEMA = "wreath"
BUCKET_TABLE = "series_buckets"
CORRECTION_TABLE = "series_corrections"


def statements(*, schema: str = SCHEMA) -> tuple[str, ...]:
    """DDL for the settled-bucket and correction tables, one per element.

    The primary key is `(view, params, bucket)`: one settled value per
    declaration, per set of bound parameters, per bucket. `params` is present
    because a view carrying `Param("herd")` computes a different number for
    each herd, and storing them under one key would serve one herd's activity to
    another.

    `measures` is JSONB rather than a column per measure because the measure
    names are the caller's, declared in Python and changeable without a
    migration -- the table has to hold whatever they chose.
    """
    return (
        f'CREATE TABLE IF NOT EXISTS "{schema}"."{BUCKET_TABLE}" (\n'
        "  view text NOT NULL,\n"
        "  params text NOT NULL,\n"
        "  bucket timestamptz NOT NULL,\n"
        "  measures jsonb NOT NULL,\n"
        "  settled_at timestamptz NOT NULL DEFAULT now(),\n"
        "  PRIMARY KEY (view, params, bucket)\n"
        ")",
        f'CREATE TABLE IF NOT EXISTS "{schema}"."{CORRECTION_TABLE}" (\n'
        "  view text NOT NULL,\n"
        "  params text NOT NULL,\n"
        "  bucket timestamptz NOT NULL,\n"
        "  delta jsonb NOT NULL,\n"
        "  noticed_at timestamptz NOT NULL DEFAULT now(),\n"
        "  PRIMARY KEY (view, params, bucket)\n"
        ")",
    )


def component(*, schema: str = SCHEMA) -> Any:
    """The settled-bucket store's claim on the wreath schema.

    Sealing writes here, so without these tables a sealed series cannot store a
    bucket at all -- which is exactly how it stood while nothing applied the
    DDL: the statements existed and no code path ran them.
    """
    from ..schema import Component, Step

    return Component(
        name="series",
        schema=schema,
        relations=(BUCKET_TABLE, CORRECTION_TABLE),
        steps=(Step(version=1, statements=statements(schema=schema)),),
    )


def schema_sql(*, schema: str = SCHEMA) -> str:
    """The settled-bucket DDL, semicolon-joined. A derivation of `statements`."""
    return component(schema=schema).sql()


@dataclass(frozen=True, slots=True)
class SettledStore:
    """What a sealed `Series` stores, as something an application can hold.

    A `Series` is a declaration, not a registered subsystem: it is built where
    it is used and the application never sees it, so `app.schema_components()`
    had nothing to ask and the settled-bucket tables were created by nothing.
    An application declaring a sealed view had to reach into
    `wreath._series.settle` past a leading underscore and run the DDL itself,
    or discover the absence at the first `settle()`.

    `app.series(database=...)` registers one of these instead. It holds no
    state and does no work; it exists so the claim has an owner in the same
    place every other wreath-owned table's claim has one, and so the lifespan
    that creates the job ledger creates these too.
    """

    schema: str = SCHEMA

    def component(self) -> Any:
        """This store's claim on the wreath schema."""
        return component(schema=self.schema)

    def schema_sql(self) -> str:
        """The DDL, semicolon-joined. A derivation of `component()`."""
        return self.component().sql()


@dataclass(frozen=True, slots=True)
class Seal:
    """How long after a bucket closes it stops being able to change.

    Args:
        after: the lateness allowance, as seconds or a duration like `"2h"`.
            A bucket is sealed once this much time has passed since its *end*,
            not since its start.
        on_late: what a reconcile does when it finds a sealed bucket whose rows
            have changed. `"correct"` records the difference beside the
            settled value and leaves it immutable. `"reopen"` replaces the
            settled value outright, which is only sound while the raw rows are
            still present -- so it stays opt-in.
    """

    after: float
    on_late: str = "correct"

    def __post_init__(self) -> None:
        if self.on_late not in ("correct", "reopen"):
            raise ValueError(
                f"seal(on_late=) is 'correct' or 'reopen', got {self.on_late!r}"
            )


@dataclass(frozen=True, slots=True)
class SealState:
    """Where the watermark falls for one range, and what is known behind it."""

    #: The first bucket start that is still open. Everything strictly before
    #: this is sealed. `None` when the whole range is open.
    sealed_through: Any
    #: Bucket starts inside the range that have a stored settled value.
    settled: tuple[Any, ...] = ()
    #: Bucket starts inside the range carrying a recorded correction.
    corrections: tuple[Any, ...] = ()

    @property
    def any_sealed(self) -> bool:
        return self.sealed_through is not None


def watermark(now: Any, *, bucket: Bucket, zone_name: str, after: float) -> Any:
    """The first bucket start that is still open.

    A bucket `[start, end)` is sealed once `now >= end + after`, so the
    buckets still open are exactly those ending after `now - after`. Flooring
    that instant gives the first of them.

    `after` is a fixed number of seconds and is subtracted as an absolute
    offset, which is the right arithmetic here: "two hours after the bucket
    closed" means two hours of elapsed time, not two hours on a wall clock that
    may have jumped. The *bucket* boundary is where the calendar matters, and
    `floor` already owns that.
    """
    deadline = now - datetime.timedelta(seconds=after)
    return bucket.floor(deadline, zone_name)


def view_key(
    *,
    model: type,
    at_column: str,
    bucket: Bucket,
    zone_name: str,
    measures: tuple[tuple[str, Any], ...],
    predicate_sql: str,
    fills: dict[str, Any],
) -> str:
    """A stable identity for one declaration's *shape*.

    Content-derived rather than a name the caller supplies, so a declaration
    that changes what it computes cannot go on reading values computed under the
    old rules. Adding a measure, moving the zone, or editing a filter all mint a
    new key, and the rows under the old one are simply no longer read.

    Those rows are **not deleted** -- nothing here deletes anything. They cost
    storage, which is recoverable by changing your mind, where deleting them
    would not be.
    """
    digest = hashlib.blake2s(digest_size=16)
    for part in (
        model.__module__,
        model.__qualname__,
        at_column,
        bucket.name,
        zone_name,
        predicate_sql,
    ):
        digest.update(f"{part}\x00".encode())
    for name, measure in measures:
        digest.update(f"{name}\x01{measure.kind}\x01{measure.column or ''}\x00".encode())
    for name in sorted(fills):
        digest.update(f"{name}\x02{fills[name]!r}\x00".encode())
    return digest.hexdigest()


def params_key(values: dict[str, Any]) -> str:
    """A stable identity for one set of bound parameters.

    `""` when a view takes none, so the common case reads as absent rather
    than as a hash of nothing.
    """
    if not values:
        return ""
    digest = hashlib.blake2s(digest_size=16)
    for name in sorted(values):
        digest.update(f"{name}\x00{values[name]!r}\x00".encode())
    return digest.hexdigest()


# -- reading and writing settled rows -----------------------------------------


def select_settled(*, schema: str = SCHEMA) -> str:
    """Settled values and any corrections for one view, params, and range."""
    return (
        "SELECT b.bucket, b.measures, c.delta "
        f'FROM "{schema}"."{BUCKET_TABLE}" AS b '
        f'LEFT JOIN "{schema}"."{CORRECTION_TABLE}" AS c '
        "ON c.view = b.view AND c.params = b.params AND c.bucket = b.bucket "
        "WHERE b.view = $1 AND b.params = $2 AND b.bucket >= $3 AND b.bucket < $4 "
        "ORDER BY b.bucket"
    )


def insert_settled(*, schema: str = SCHEMA) -> str:
    """Store one computed bucket.

    `DO NOTHING` rather than `DO UPDATE`: two readers materialising the same
    sealed bucket compute the same number from the same rows, so the loser has
    nothing to add. Overwriting would also be the one path by which an ordinary
    read could silently change a settled value, which is exactly what sealing
    promises it will not do.
    """
    return (
        f'INSERT INTO "{schema}"."{BUCKET_TABLE}" (view, params, bucket, measures) '
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (view, params, bucket) DO NOTHING"
    )


def upsert_correction(*, schema: str = SCHEMA) -> str:
    """Record what a reconcile found, replacing any earlier delta for that bucket.

    Replacing is right where storing a second delta would not be: the delta is
    the difference between the settled value and the current truth, so a later
    reconcile supersedes an earlier one rather than adding to it.
    """
    return (
        f'INSERT INTO "{schema}"."{CORRECTION_TABLE}" (view, params, bucket, delta) '
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (view, params, bucket) DO UPDATE "
        "SET delta = EXCLUDED.delta, noticed_at = now()"
    )


def replace_settled(*, schema: str = SCHEMA) -> str:
    """`on_late="reopen"`: overwrite the settled value and drop its correction.

    Two statements rather than one, because dropping a correction that is no
    longer true is part of reopening and leaving it would double-count.
    """
    return (
        f'INSERT INTO "{schema}"."{BUCKET_TABLE}" (view, params, bucket, measures) '
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (view, params, bucket) DO UPDATE "
        "SET measures = EXCLUDED.measures, settled_at = now()"
    )


def clear_correction(*, schema: str = SCHEMA) -> str:
    return (
        f'DELETE FROM "{schema}"."{CORRECTION_TABLE}" '
        "WHERE view = $1 AND params = $2 AND bucket = $3"
    )


def fold(settled: dict[str, Any], delta: dict[str, Any] | None) -> dict[str, Any]:
    """The value a reader sees: the settled measures plus any recorded delta.

    Only additive measures fold. A count and a sum are the difference plus the
    original; an average, a minimum and a maximum are not, so a correction
    against them carries the *replacement* rather than a difference and is
    applied as one. That asymmetry is why the delta is stored per measure rather
    than as one number.
    """
    if not delta:
        return dict(settled)
    out = dict(settled)
    for name, item in delta.items():
        if isinstance(item, dict) and "set" in item:
            out[name] = item["set"]
        elif item is None:
            continue
        else:
            base = out.get(name)
            out[name] = item if base is None else base + item
    return out


def difference(
    settled: dict[str, Any], current: dict[str, Any], measures: tuple[tuple[str, Any], ...]
) -> dict[str, Any] | None:
    """What to record so `fold(settled, delta) == current`.

    `None` when nothing moved, so a reconcile over a quiet range writes
    nothing at all rather than a table of zeroes.
    """
    delta: dict[str, Any] = {}
    for name, measure in measures:
        was, now = settled.get(name), current.get(name)
        if was == now:
            continue
        if measure.has_identity and isinstance(was, (int, float)) and isinstance(
            now, (int, float)
        ):
            delta[name] = now - was
        else:
            # Not additive: an average cannot be corrected by adding anything,
            # so the correction carries the answer rather than a difference.
            delta[name] = {"set": now}
    return delta or None


def naive_local(value: Any, zone_name: str) -> Any:
    """The wall clock an instant reads as in `zone_name`.

    Shared with the compiler's bucketing so a settled bucket and a freshly
    computed one are the same instant, not two readings that agree most days.
    """
    return wall_clock(value, zone(zone_name))
