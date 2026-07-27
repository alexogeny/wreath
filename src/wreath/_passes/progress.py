"""How far along a pass is, and the three ways of being honest about it.

A percentage without a provenance is a rumour. `64%` reads as a measurement,
and the reader plans around it; `64% (estimated)` reads as what it is, and the
difference matters most in exactly the situation where the bar has been sitting
at ninety-seven for an hour. So the denominator's kind travels with the number
everywhere it goes -- the ledger column, the CLI row, the JSON body -- and there
is no code path that emits one without the other.

There are only three honest denominators, and each lies in its own way:

======== =============================== ============ ==============================
kind     how                             cost         lies when
======== =============================== ============ ==============================
exact    `SELECT count(*)` once        a full scan  never, but it can be minutes
estimated `pg_class.reltuples`         free         `ANALYZE` is stale
keyspace `(cursor - min) / (max - min)` free        the key is sparse or clumped
======== =============================== ============ ==============================

The default is `estimated`, because a full count in front of a long pass
delays the thing the operator actually asked for in order to make a progress bar
prettier.

**Rate is measured over a trailing window, never since launch.** A pass that has
been paced hard for the last ten minutes should say so rather than average it
away against a fast first hour. And when the window holds nothing, there is no
ETA -- not infinity, not zero, not "calculating..." forever. The field is absent
and `Progress` says why, because a fabricated ETA is worse than no ETA:
someone plans around it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from .keyset import Key, PassDeclarationError

#: How long a rate window runs before it rolls over. Long enough to hold several
#: chunks of a slow pass, short enough that ten minutes of pacing shows up.
WINDOW_SECONDS = 60.0

#: A pass is stalled once it has gone this many times its own mean chunk
#: interval without advancing. A multiple rather than an absolute, so a pass
#: whose chunks take two seconds is judged differently from one whose chunks
#: take two minutes.
STALL_MULTIPLE = 10.0

#: ...but never less than this, so a pass that has only just started is not
#: called stalled because its first chunk was quick.
STALL_FLOOR_SECONDS = 30.0

#: A pass nothing has tried to drive for this long is not being driven. The
#: scheduler is down, the runner never started, or the enqueue failed.
DRIVE_SILENCE_SECONDS = 300.0

WALKING = "walking"
SLOW = "slow"
STALLED = "stalled"
BLOCKED = "blocked"
DONE = "done"


# --- the denominator ---------------------------------------------------------


class Denominator:
    """Where the number under the percentage comes from."""

    __slots__ = ()

    #: What the ledger records beside the number, and what the CLI prints.
    kind = "estimated"

    #: Whether an ETA can be derived from a rows-per-second rate.
    counts_rows = True

    def refuse(self, keys: tuple[Key, ...], *, table: str) -> None:
        """Reject a key this denominator cannot measure, where it was declared."""

    async def measure(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> int | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Estimated(Denominator):
    """`pg_class.reltuples` -- free, and stale exactly as often as `ANALYZE` is.

    The default, and the right default: a pass exists because the table is big,
    and counting a big table before starting to walk it spends minutes making a
    progress bar prettier while the operator waits for the work.
    """

    kind = "estimated"
    counts_rows = True

    async def measure(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> int | None:
        # `to_regclass($1)` rather than `$1::regclass`: the cast makes PostgreSQL
        # infer the *parameter* as `regclass` (OID 2205), which no binary encoder
        # here can write. The first execution survives it and every later one
        # raises, because only the prepared statement carries the inferred type --
        # so a pass measured once and then failed forever, which is the worst
        # possible shape for a default. `to_regclass` takes `text`, and returns
        # NULL rather than raising for a table that is not there. The three other
        # regclass lookups in this codebase already spell it this way.
        estimate = await executor.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass($1)", table
        )
        # A table that has never been analysed reports -1, which is not a
        # denominator. Reporting no percentage beats reporting a negative one.
        if estimate is None or int(estimate) < 0:
            return None
        return int(estimate)


@dataclass(frozen=True, slots=True)
class Exact(Denominator):
    """`SELECT count(*)` once, at launch. Never wrong, and never free."""

    kind = "exact"
    counts_rows = True

    async def measure(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> int | None:
        total = await executor.fetchval(f"SELECT count(*) FROM {table}")
        return None if total is None else int(total)


@dataclass(frozen=True, slots=True)
class Keyspace(Denominator):
    """How far the cursor has travelled between the smallest key and the ceiling.

    Free, and the only one of the three that measures the *walk* rather than the
    table -- so it is unmoved by rows the work deletes. It lies when the key is
    sparse or unevenly distributed, which is most keys some of the time: half the
    id range is not half the rows unless the ids were handed out evenly.

    It reports no ETA, and that is not an omission. The rate window counts rows,
    and rows are not the unit this measures, so `remaining / rate` would be a
    ratio of two different things dressed up as a time. §9.2's rule is that the
    field is absent and the state says why.
    """

    kind = "keyspace"
    counts_rows = False

    def refuse(self, keys: tuple[Key, ...], *, table: str) -> None:
        if position(keys[0], _EXAMPLE.get(keys[0].type.lower())) is not None:
            return
        raise PassDeclarationError(
            f"progress=Keyspace() measures how far the cursor has moved between "
            f"two key values, so the leading key column must be a number or a "
            f"timestamp; {keys[0].name!r} on {table} is {keys[0].type}. Use "
            "progress=Estimated() (free) or progress=Exact() (a full scan once)."
        )

    async def measure(self, executor: Any, *, table: str, keys: tuple[Key, ...]) -> int | None:
        # The span is computed at read time from the floor and the ceiling, both
        # of which the ledger already holds. Nothing to count here.
        return None


#: One value per SQL type the keyspace arithmetic can place on a line, used only
#: to answer "could this column be measured?" at declaration time.
_EXAMPLE: dict[str, Any] = {
    "int2": 0, "int4": 0, "int8": 0, "smallint": 0, "integer": 0, "bigint": 0,
    "float4": 0.0, "float8": 0.0, "real": 0.0, "double precision": 0.0, "numeric": 0.0,
    "timestamptz": datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
    "timestamp": datetime.datetime(2000, 1, 1),
    "timestamp with time zone": datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
    "timestamp without time zone": datetime.datetime(2000, 1, 1),
    "date": datetime.date(2000, 1, 1),
}


def position(key: Key, value: Any) -> float | None:
    """One key value as a point on a line, or `None` if it is not on one.

    Only the leading key column is placed. A composite key's later columns
    subdivide a value the leading column already located, and at the resolution
    a percentage is read at they are noise.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        anchor = datetime.datetime(1970, 1, 1, tzinfo=value.tzinfo)
        return (value - anchor).total_seconds()
    if isinstance(value, datetime.date):
        return float((value - datetime.date(1970, 1, 1)).days)
    if isinstance(value, str):
        # A string key is not refused outright -- a timestamp read straight out
        # of jsonb arrives as one -- but only if it really parses as a date.
        try:
            parsed = datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
        return position(key, parsed)
    return None


# --- the reported shape ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Progress:
    """How far along, how fast, how much longer -- and what is not knowable.

    `percent` and `denominator_kind` are handed out together or not at all.
    `eta_seconds` is `None` whenever it cannot be computed honestly, and
    `eta_absent` then carries the sentence explaining which input was missing.
    """

    percent: float | None
    denominator: int | None
    denominator_kind: str | None
    rows_per_second: float | None
    eta_seconds: float | None
    eta_absent: str | None
    state: str
    #: Why the pass is in that state, when the state alone is not the answer.
    state_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "denominator": self.denominator,
            "denominator_kind": self.denominator_kind,
            "rows_per_second": self.rows_per_second,
            "eta_seconds": self.eta_seconds,
            "eta_absent": self.eta_absent,
            "state": self.state,
            "state_reason": self.state_reason,
        }


def _seconds_between(later: Any, earlier: Any) -> float | None:
    if later is None or earlier is None:
        return None
    try:
        delta = later - earlier
    except TypeError:
        return None
    return delta.total_seconds() if hasattr(delta, "total_seconds") else float(delta)


def rate_of(row: Any) -> float | None:
    """Rows per second over the trailing window, or `None` when it holds nothing.

    The chunk that opens a window contributes no rows to it, so the count and the
    interval describe the same stretch of time rather than overlapping by one
    chunk. One completed chunk since then is enough to measure; none is not, and
    the answer to "how fast" is then *unknown*, never *zero*.
    """
    units = int(getattr(row, "window_units", 0) or 0)
    if units < 1:
        return None
    elapsed = _seconds_between(row.last_advance, getattr(row, "window_started", None))
    if elapsed is None or elapsed <= 0.0:
        return None
    return float(int(getattr(row, "window_rows", 0) or 0)) / elapsed


def mean_chunk_seconds(row: Any) -> float | None:
    """How long one chunk has been taking lately, for the stall threshold."""
    units = int(getattr(row, "window_units", 0) or 0)
    if units < 1:
        return None
    elapsed = _seconds_between(row.last_advance, getattr(row, "window_started", None))
    if elapsed is None or elapsed <= 0.0:
        return None
    return elapsed / units


def stall_after(row: Any) -> float:
    """How long without advancing counts as stalled, for this pass's own pace."""
    mean = mean_chunk_seconds(row)
    if mean is None:
        return STALL_FLOOR_SECONDS
    return max(STALL_FLOOR_SECONDS, mean * STALL_MULTIPLE)


def percent_of(row: Any, keys: tuple[Key, ...]) -> float | None:
    """The fraction walked, by whichever denominator this pass declared."""
    kind = row.denominator_kind
    if kind == Keyspace.kind:
        return _keyspace_percent(row, keys)
    total = row.denominator
    if not total or total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * float(row.rows_done) / float(total)))


def _keyspace_percent(row: Any, keys: tuple[Key, ...]) -> float | None:
    """Where the cursor sits between the floor and the ceiling, as a fraction.

    Computed from the ledger's own JSON encoding rather than from decoded key
    values, because the CLI reads passes it holds no declaration for -- it has a
    ledger connection and nothing else. The encoding is lossless for every type
    this can measure (numbers stay numbers, timestamps are ISO strings), so
    working from it costs nothing and removes the need to know the key.
    """
    key = keys[0] if keys else _ANY_KEY
    low = position(key, _first(row.keyspace_from))
    high = position(key, _first(row.ceiling))
    if low is None or high is None:
        return None
    cursor = _first(row.cursor)
    if cursor is None:
        return 0.0
    here = position(key, cursor)
    if here is None:
        return None
    if key.descending:
        low, high, here = -low, -high, -here
    span = high - low
    if span <= 0.0:
        # The floor and the ceiling met: there was nothing between them to walk.
        return 100.0
    return max(0.0, min(100.0, 100.0 * (here - low) / span))


#: Stands in when a reader has a ledger row but no declaration, which is the
#: CLI's ordinary situation. Only `descending` is ever consulted.
_ANY_KEY = Key(name="_", type="text")


def _first(encoded: Any) -> Any:
    """The leading column of a stored cursor, still in its ledger encoding."""
    if encoded is None:
        return None
    if isinstance(encoded, (list, tuple)):
        return encoded[0] if encoded else None
    return encoded


def describe(row: Any, keys: tuple[Key, ...], *, now: Any = None) -> Progress:
    """Everything a reader needs about one pass, computed from its ledger row.

    Nothing here consults a progress registry. That registry is bounded and
    TTL'd, so a pass running for two hours vanishes from it *while still
    running*; it is live commentary for a client watching a stream. The ledger
    row is the durable status, and it is what anything making a decision reads.
    """
    percent = percent_of(row, keys)
    rate = rate_of(row)
    state, reason = _state(row, rate=rate, now=now)
    eta, absent = _eta(row, rate=rate, state=state)
    return Progress(
        percent=percent,
        denominator=row.denominator,
        denominator_kind=row.denominator_kind,
        rows_per_second=rate,
        eta_seconds=eta,
        eta_absent=absent,
        state=state,
        state_reason=reason,
    )


def _state(row: Any, *, rate: float | None, now: Any) -> tuple[str, str | None]:
    """Walking, slow, stalled, blocked or done -- three of which need an operator.

    The one people leave out is `blocked`: nothing is driving the pass, so it
    will silently never finish. A hand-rolled backfill has no name for that
    state, which is why the terminal window closes and the column stays half
    converted for three weeks.
    """
    if row.phase == DONE:
        return DONE, None
    if row.phase == BLOCKED:
        return BLOCKED, row.last_error or "the pass is blocked"
    drive_error = getattr(row, "last_drive_error", None)
    if drive_error:
        return BLOCKED, f"nothing is driving this pass: {drive_error}"
    silence = _seconds_between(now, getattr(row, "driven_at", None))
    if getattr(row, "driven_at", None) is None:
        return BLOCKED, "nothing has ever tried to drive this pass"
    if silence is not None and silence > DRIVE_SILENCE_SECONDS:
        return BLOCKED, (
            f"nothing has driven this pass for {silence:.0f}s: check the job "
            "runner and its schedule"
        )
    idle = _seconds_between(now, row.last_advance)
    if idle is not None and idle > stall_after(row):
        return STALLED, (
            f"the cursor has not moved for {idle:.0f}s: look for a lock wait or a "
            "pathological statement in pg_stat_activity"
        )
    if row.paced_reason:
        # A paced pass that does not report being paced is indistinguishable
        # from a broken one, which is why this is a state and not a footnote.
        return SLOW, f"paced: {row.paced_reason}"
    return WALKING, None


def _eta(row: Any, *, rate: float | None, state: str) -> tuple[float | None, str | None]:
    if state == DONE:
        return None, None
    if row.denominator_kind == Keyspace.kind:
        return None, (
            "progress is measured in keyspace, and the rate is measured in rows; "
            "dividing one by the other would not be a time"
        )
    if not row.denominator or row.denominator <= 0:
        return None, "no denominator: nothing to count the remaining work against"
    if rate is None:
        return None, "the rate window is empty: no chunk has finished recently"
    if rate <= 0.0:
        return None, "the rate window holds no rows"
    remaining = float(row.denominator) - float(row.rows_done)
    if remaining <= 0.0:
        return 0.0, None
    return remaining / rate, None


__all__ = [
    "BLOCKED",
    "DONE",
    "DRIVE_SILENCE_SECONDS",
    "Denominator",
    "Estimated",
    "Exact",
    "Keyspace",
    "Progress",
    "SLOW",
    "STALLED",
    "WALKING",
    "WINDOW_SECONDS",
    "describe",
    "mean_chunk_seconds",
    "percent_of",
    "position",
    "rate_of",
    "stall_after",
]
