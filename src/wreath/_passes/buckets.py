"""The bucket range source: arithmetic on a calendar, never a query.

A keyset walk asks the table where to go next, because only the table knows
which key values exist. A bucketed walk does not have that problem: the next
range after "the day starting 2026-07-27" is "the day starting 2026-07-28"
whether or not a single row landed in either. So §5.5 of the design says this
source needs none of §5's machinery -- no offset, no uniqueness requirement, no
monotonicity question -- and it gets the complexity property for free.

What does carry over is the index requirement, because the chunk's *predicate*
is still a range scan, and the half-open rule, because a bucket that both
contains its end and starts the next one double-counts every boundary row.

The calendar arithmetic is `wreath.temporal.Bucket`'s, deliberately. A
second implementation here would be a second place for the trap the series work
found the hard way: subtracting two aware datetimes that share a `tzinfo`
gives the *naive* difference, so "a day" is 24 hours on every day but the two a
year that a zone changes offset. `Bucket.end_of` steps on the local wall clock
and converts back, which is the answer, and it is already written.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from ..temporal import Bucket
from ..temporal import bucket as _named_bucket
from ..temporal import zone as _zone
from . import keyset
from .duration import seconds as _seconds
from .keyset import Key, PassDeclarationError


@dataclass(frozen=True, slots=True)
class BucketRange:
    """One bucket's half-open span, as the driver hands it around."""

    start: datetime.datetime
    end: datetime.datetime


def resolve_bucket(step: Any) -> Bucket:
    """The declared step as a `Bucket`, or a refusal.

    Routed through `wreath.temporal.bucket` so the SQL fragments a chunk
    interpolates come from that module's table and never from a caller.
    """
    if isinstance(step, Bucket):
        return step
    try:
        return _named_bucket(step)
    except Exception as error:  # noqa: BLE001 - re-raised as a declaration refusal
        raise PassDeclarationError(
            f"Buckets(step=...) must be a wreath.temporal bucket such as Day, or "
            f"the name of one; got {step!r} ({error})"
        ) from None


def refuse_unbucketable_key(keys: tuple[Key, ...], *, table: str) -> None:
    """Refuse a bucketed walk over a column that is not a timestamp, or not indexed.

    Uniqueness is deliberately *not* asked for. A bucket boundary is a value the
    arithmetic produced rather than a row the table happened to hold, so the
    boundary cannot land "between siblings" the way a keyset cursor can -- the
    failure §5.3 refuses does not exist here. Saying so out loud matters: the
    same refusal running for both sources would be a rule nobody could explain.
    """
    if len(keys) != 1:
        names = ", ".join(item.name for item in keys)
        raise PassDeclarationError(
            f"Buckets walks one temporal column, so key=({names}) on {table} is "
            "too many. A bucket range is arithmetic on a calendar; there is no "
            "tiebreaker to append because there is no row key to tie."
        )
    if not keys[0].is_clock:
        raise PassDeclarationError(
            f"Buckets(on={keys[0].name!r}) on {table} needs a timestamp column to "
            f"bucket by; {keys[0].name!r} is {keys[0].type}."
        )
    if not keys[0].indexed:
        raise PassDeclarationError(
            f"Buckets(on={keys[0].name!r}) on {table} has no index. Each chunk is "
            "a range scan over one bucket, so without one every chunk reads the "
            "whole table. Declare an index on it."
        )


@dataclass(frozen=True, slots=True)
class Buckets:
    """The next range is the next calendar bucket, computed rather than queried.

    The recurring shape: fold yesterday into a daily rollup, settle last month
    into a monthly one, expire one day of raw at a time. The walk moves one
    bucket per chunk (or *per_chunk* of them), and it never looks at the table
    to decide where to go.

    Args:
        on: the timestamp column that assigns a row to a bucket. A model column
            or a `Key` for a table the ORM does not own.
        step: the bucket width -- `wreath.temporal.Day`, or its name.
        zone: the wall clock the buckets are cut on. **Not** a runtime argument:
            a materialised Auckland day cannot be re-cut into a London day
            afterwards, so it is part of the declaration.
        since: where the first cycle starts. Omit it and the anchor is read from
            the earliest row -- one query per *cycle*, not per chunk.
        per_chunk: how many buckets one chunk covers.
        within: the chunk's time budget, as for `Rows`.
    """

    on: Any
    step: Any = "day"
    zone: str = "UTC"
    since: Any = None
    per_chunk: int = 1
    within: Any = "5s"
    keys: tuple[Key, ...] = field(init=False, repr=False)
    bucket: Bucket = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.per_chunk, int) or isinstance(self.per_chunk, bool):
            raise PassDeclarationError(f"Buckets per_chunk must be an int; got {self.per_chunk!r}")
        if self.per_chunk < 1:
            raise PassDeclarationError(
                f"Buckets per_chunk must be at least 1; got {self.per_chunk}"
            )
        object.__setattr__(self, "keys", keyset.normalise(self.on))
        object.__setattr__(self, "bucket", resolve_bucket(self.step))
        object.__setattr__(self, "within", _seconds(self.within, what="Buckets within"))
        # Resolve the zone here rather than at the first chunk: an unknown
        # zone is a typo in a declaration, and it should cost a failed start
        # rather than a walk that dies an hour in.
        _zone(self.zone)
        if self.since is not None and not isinstance(self.since, datetime.datetime):
            raise PassDeclarationError(
                f"Buckets(since=...) must be an aware datetime; got {self.since!r}"
            )
        if self.since is not None and self.since.tzinfo is None:
            raise PassDeclarationError(
                "Buckets(since=...) must carry a zone. A naive datetime here is "
                "the bug this refuses: it would be read as the server's local "
                "time on one machine and as UTC on another."
            )

    @property
    def limit(self) -> int:
        """Reported as the ledger's chunk size. Buckets, not rows."""
        return self.per_chunk

    @property
    def tz(self) -> datetime.tzinfo:
        return _zone(self.zone)

    def refuse(self, *, table: str) -> None:
        refuse_unbucketable_key(self.keys, table=table)

    def anchor_sql(self, table: str) -> str:
        """The one query this source ever makes: where the first bucket starts."""
        return f"SELECT min({self.keys[0].name}) AS anchor FROM {table}"

    def advance(self, start: datetime.datetime) -> datetime.datetime:
        """*per_chunk* buckets past a boundary, on the declared wall clock."""
        value = start
        for _ in range(self.per_chunk):
            value = self.bucket.end_of(value, self.tz)
        return value

    def floor(self, value: datetime.datetime) -> datetime.datetime:
        return self.bucket.floor(value, self.tz)

    def chunk_where(
        self,
        binds: Any,
        *,
        cursor_from: tuple[Any, ...] | None,
        cursor_to: tuple[Any, ...],
        frontier: str | None,
    ) -> str:
        """`col >= start AND col < end`, half-open, stated once.

        Closed at the bottom and open at the top -- the opposite anchoring to a
        keyset chunk, which is open at the bottom so the row it resumed from is
        not seen twice. Both are half-open ranges over one ordered domain, which
        is the whole vocabulary the primitive has; only the end that is included
        differs, and it differs because a bucket boundary is a value the
        arithmetic produced rather than a row that exists.
        """
        column = self.keys[0].name
        parts = []
        if cursor_from is not None:
            parts.append(f"{column} >= {binds.add(cursor_from[0])}")
        parts.append(f"{column} < {binds.add(cursor_to[0])}")
        if frontier:
            parts.append(frontier)
        return " AND ".join(parts)

    def reproduce(
        self,
        *,
        table: str,
        cursor_from: tuple[Any, ...] | None,
        cursor_to: tuple[Any, ...],
    ) -> str:
        """The statement an operator pastes into `psql` to see the real error."""
        from .driver import literal

        column = self.keys[0].name
        parts = []
        if cursor_from is not None:
            parts.append(f"{column} >= {literal(cursor_from[0])}")
        parts.append(f"{column} < {literal(cursor_to[0])}")
        return f"SELECT * FROM {table} WHERE {' AND '.join(parts)}"

    async def next_range(
        self,
        executor: Any,
        *,
        walk: Any,
        cursor: tuple[Any, ...] | None,
        ceiling: Any,
        frontier_sql: Any,
    ) -> tuple[tuple[Any, ...] | None, tuple[Any, ...]] | None:
        """The next bucket, or `None` when the frontier has been reached.

        Computed, never queried: given a boundary, the next one is arithmetic on
        a calendar whether or not a single row falls between them.

        The frontier test is on the range's *end*, not its start, and that is
        the sealing rule rather than an off-by-one: a bucket cannot be settled
        before the moment it stops accepting rows, and that moment is its
        exclusive end. Folding in a day that is not over yet would settle a
        number that is still moving.

        The start is the cursor itself and is *included* -- the opposite
        anchoring to a keyset chunk, because a bucket boundary is a value the
        arithmetic produced rather than a row that has already been walked.
        """
        limit = _frontier_instant(self.keys, ceiling)
        start = cursor[0] if cursor is not None else None
        if start is None:
            start = await self._anchor(executor, walk=walk)
            if start is None:
                # No rows at all, so there is no first bucket to start from.
                return None
        end = self.advance(start)
        if limit is not None and end > limit:
            return None
        return ((start,), (end,))

    async def _anchor(self, executor: Any, *, walk: Any) -> Any:
        if self.since is not None:
            return self.floor(self.since)
        record = await executor.fetchrow(self.anchor_sql(walk.table))
        value = None
        if record is not None:
            try:
                value = record["anchor"]
            except KeyError, TypeError:
                value = record[0]
        return None if value is None else self.floor(value)


def _frontier_instant(keys: tuple[Key, ...], ceiling: Any) -> Any:
    decoded = keyset.decode_cursor(keys[:1], ceiling)
    return None if decoded is None else decoded[0]


__all__ = ["BucketRange", "Buckets", "refuse_unbucketable_key", "resolve_bucket"]
