"""Request-scoped buffering, per-call-site rate limiting, and off-loop staging.

Three policies that all drop records on purpose, and therefore all account for
what they dropped.

**Failure-triggered buffering.** TRACE and DEBUG records made during a request
accumulate in a fixed per-request buffer instead of reaching the ring. If the
request fails, runs slow, or the application promotes it explicitly, the whole
buffer is published; otherwise it is discarded and the slot recycled. Verbose
instrumentation costs a buffer append in the steady state and produces no
output, and the requests that went wrong arrive with the history that led up to
them. The technique is old -- Brian Marick wrote it up in 2000, and the Apollo
guidance computer's "Coroner" is the ancestor -- and the recorder's existing
error/slow promotion flags are already the right trigger.

**Per-call-site limiting.** Within each tick, the first N records from a site
pass, then every Mth, and the rest are dropped. This is Zap's rule. It is cheap
here because the site id is already a dense integer, so the limiter is an array
index rather than a hash of the message text. It applies to INFO and below only:
a warning or an error is never suppressed, because an error nobody sees is the
worst outcome an observability system can produce.

**Off-loop staging.** The ring has exactly one writer, so a record emitted from
a `wreath.jobs` worker or a thread-pool task cannot go straight onto it. Those
records are staged in a bounded queue and published by the loop on its next
drain, flagged `LOG_FLAG_OFF_LOOP` and one drain interval late. That is the
counted slow path the design reserved `LossReason.LOG_OFF_LOOP` for, and it is
deliberately not per-thread staging buffers: those would order records by
whichever thread flushed first, which is a permanent tax on every record to
serve the rare one.

Drops are never silent. A limiter drop is carried on the next record from that
site as `dropped_siblings`, so one line tells an operator how many like it were
suppressed; a buffer overflow is counted on the buffer, and a staging overflow
on the stage.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from ._flight_schema import LOG_FLAG_PROMOTED, LogCell, Severity

#: Records buffered per request before overflow. Provisional, like every other
#: NFR budget: enough to hold a request's narrative, small enough that a
#: pathological handler cannot grow memory.
DEFAULT_SCRATCH_BUDGET: Final = 64

#: Sites the limiter tracks. Beyond this, records pass unlimited rather than
#: being silently suppressed -- an unbounded table would be the worse trade.
DEFAULT_LIMITER_CAPACITY: Final = 4096

#: Records staged from off the loop before the stage starts dropping. Sized like
#: the writer queue: enough to absorb a burst from a job worker between drains,
#: small enough that a loop which has stopped draining is bounded.
DEFAULT_OFF_LOOP_CAPACITY: Final = 4096


@dataclass(frozen=True, slots=True)
class LogSamplingPolicy:
    """Zap's first-N-then-every-Mth rule, per call site per tick.

    Attributes:
        enabled: Whether to limit at all.
        first: Records from a site that pass unconditionally each tick.
        thereafter: After `first`, one in every `thereafter` passes.
        interval: Tick length in seconds.
        ceiling: The highest severity that may be sampled. Anything above it
            always passes; the default means WARN and above are never dropped.
    """

    enabled: bool = True
    first: int = 100
    thereafter: int = 100
    interval: float = 1.0
    ceiling: Severity = Severity.INFO

    def __post_init__(self) -> None:
        if self.first < 0:
            raise ValueError("first must not be negative")
        if self.thereafter < 1:
            raise ValueError("thereafter must be at least 1")
        if self.interval <= 0:
            raise ValueError("interval must be positive")


@dataclass(slots=True)
class _Slot:
    """One site's counters. `dropped` deliberately survives a tick roll: it is
    cleared by being *reported*, not by time passing."""

    tick: int = -1
    count: int = 0
    dropped: int = 0


class SiteLimiter:
    """Per-call-site rate limiting over a bounded, dense table."""

    __slots__ = ("_capacity", "_clock", "_policy", "_slots")

    def __init__(
        self,
        policy: LogSamplingPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = DEFAULT_LIMITER_CAPACITY,
    ) -> None:
        self._policy = policy if policy is not None else LogSamplingPolicy()
        self._clock = clock
        self._capacity = capacity
        self._slots: dict[int, _Slot] = {}

    @property
    def policy(self) -> LogSamplingPolicy:
        return self._policy

    def allow(self, site_id: int, severity: int) -> bool:
        """Whether this record passes. Call before packing its arguments."""
        policy = self._policy
        if not policy.enabled:
            return True
        if severity > policy.ceiling:
            return True
        # Site 0 is uninterned and has no stable slot to count against; the
        # site-table overflow that produced it is already counted elsewhere.
        if site_id <= 0 or site_id > self._capacity:
            return True
        slot = self._slots.get(site_id)
        if slot is None:
            slot = _Slot()
            self._slots[site_id] = slot
        tick = int(self._clock() / policy.interval)
        if slot.tick != tick:
            slot.tick = tick
            slot.count = 0
        slot.count += 1
        if slot.count <= policy.first:
            return True
        if (slot.count - policy.first) % policy.thereafter == 0:
            return True
        slot.dropped += 1
        return False

    def take_dropped(self, site_id: int) -> int:
        """Records dropped for this site since the last time it was asked.

        Reading clears the counter, because the value is about to be carried on
        a record; leaving it would double-count it on the next one.
        """
        slot = self._slots.get(site_id)
        if slot is None or slot.dropped == 0:
            return 0
        dropped, slot.dropped = slot.dropped, 0
        return dropped

    @property
    def tracked_sites(self) -> int:
        return len(self._slots)


class OffLoopStage:
    """Records emitted from a thread that must not write to the ring.

    `ring_publish` reads the head, copies a cell, and stores the head back with
    no interlock at all, because by construction exactly one thread does it. A
    job worker or a thread-pool task calling a log site is a *second* writer,
    and two of them interleaved do not lose a record -- they overwrite one and
    advance the head anyway, which is corruption of every cell after it. So an
    off-loop record is staged here and published by the loop on its next drain.

    The lock is the honest choice rather than a lock-free append: this is the
    slow path by definition, contention is only ever between off-loop threads,
    and an exact drop count matters more here than a few nanoseconds. Overflow
    keeps the *oldest*, matching `RequestLogBuffer`, because a burst from a job
    worker is most legible from its beginning.
    """

    __slots__ = ("_capacity", "_dropped", "_lock", "_records", "_staged")

    def __init__(self, capacity: int = DEFAULT_OFF_LOOP_CAPACITY) -> None:
        if capacity < 0:
            raise ValueError("capacity must not be negative")
        self._capacity = capacity
        self._records: list[LogCell] = []
        self._lock = threading.Lock()
        self._dropped = 0
        self._staged = 0

    def stage(self, cell: LogCell) -> bool:
        """Hold a record for the loop to publish. False when the stage is full."""
        with self._lock:
            if len(self._records) >= self._capacity:
                self._dropped += 1
                return False
            self._records.append(cell)
            self._staged += 1
            return True

    def drain(self) -> list[LogCell]:
        """Take everything staged. Called on the ring's writer thread, only."""
        with self._lock:
            if not self._records:
                return []
            records, self._records = self._records, []
            return records

    @property
    def dropped(self) -> int:
        """Records the stage refused. Reported as LossReason.LOG_OFF_LOOP."""
        with self._lock:
            return self._dropped

    @property
    def staged(self) -> int:
        """Records that took the slow path at all, dropped or not."""
        with self._lock:
            return self._staged

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class RequestLogBuffer:
    """One request's held TRACE/DEBUG records.

    Overflow keeps the *oldest* records. Failure-triggered logging exists to
    show what led up to a failure, so the head of the request is what must
    survive a full buffer; dropping the head to make room for the tail would
    discard the part being asked for.
    """

    __slots__ = ("_budget", "_dropped", "_promoted", "_records", "request_id")

    def __init__(self, request_id: int, budget: int = DEFAULT_SCRATCH_BUDGET) -> None:
        if budget < 0:
            raise ValueError("budget must not be negative")
        self.request_id = request_id
        self._budget = budget
        self._records: list[LogCell] = []
        self._dropped = 0
        self._promoted = False

    def add(self, cell: LogCell) -> None:
        if len(self._records) >= self._budget:
            self._dropped += 1
            return
        self._records.append(cell)

    def promote(self) -> None:
        """Mark this request's buffer for publication regardless of outcome."""
        self._promoted = True

    @property
    def promoted(self) -> bool:
        return self._promoted

    @property
    def dropped(self) -> int:
        """Records the budget refused. Reported as LOG_SCRATCH_FULL."""
        return self._dropped

    @property
    def held(self) -> int:
        return len(self._records)

    def finish(self, *, promoted: bool, emit: Callable[[LogCell], None]) -> int:
        """Publish or discard the buffer, and empty it either way.

        Returns the number published. Emptying unconditionally is what makes an
        escaped scope inert rather than a leak.
        """
        records, self._records = self._records, []
        if not (promoted or self._promoted):
            return 0
        for cell in records:
            emit(
                replace(
                    cell,
                    request_id=self.request_id,
                    flags=cell.flags | LOG_FLAG_PROMOTED,
                )
            )
        return len(records)
