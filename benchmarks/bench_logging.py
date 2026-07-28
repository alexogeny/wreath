"""What `wreath.logging` costs, measured -- the six numbers the plan owed.

`docs/plans/first-class-logging.md` shipped stages 1-6 with a list of
measurements under "Nothing is benchmarked", and AGENTS.md forbids any
performance claim until they exist. This is that list, one suite per item:

| Suite      | The question the plan asked                                     |
| ---------- | --------------------------------------------------------------- |
| `emit`     | `SITE(a, b)` against stdlib `logging` and structlog              |
| `disabled` | what a *disabled* `DEBUG(...)` call costs -- the load-bearing one |
| `publish`  | a LOG cell's ring publish against a COMPLETION cell's            |
| `drain`    | projector throughput with log cells mixed in                     |
| `request`  | request latency with logging on and off                          |
| `memory`   | whether `MemoryBudget.logging`'s per-entry constants resemble reality |

    uv run python -m benchmarks.bench_logging --output benchmark-results-logging/latest.json

Everything timed here follows `src/wreath/_devtools/measure.py`: arms are
interleaved, an A/A control sits at the far end of each round so the floor
includes within-round drift, and a delta below twice that floor is reported as
unresolved rather than as a number. Two traps are specific to logging and both
are guarded rather than trusted:

- **A limiter that starts dropping makes its arm fast.** The default policy
  passes the first 100 records from a site per second and then one in 100, so a
  loop emitting millions from one site measures the *drop* path. Every arm
  carries a counting sink and `--verify` checks the count against what the arm
  claimed to emit, exactly as `measure.verify_serving` checks an arm still
  answers 200. The limiter gets its own arm, labelled as the drop path.
- **A buffered record is not a published one.** The failure-triggered tier holds
  TRACE/DEBUG in a per-request buffer, so its arm binds a real request scope and
  reports held records rather than published ones.

The `emit` and `disabled` suites compare against `logging` and structlog, which
are benchmark dependencies (`uv sync --inexact --group benchmark`). structlog is
skipped, and recorded as skipped, when it is not installed; the stdlib arms
always run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging as stdlib_logging
import os
import platform
import shutil
import statistics
import sys
import tempfile
import tracemalloc
from logging.handlers import QueueHandler
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wreath import Wreath
from wreath import logging as log
from wreath._devtools import measure
from wreath._devtools.measure import Arm
from wreath._flight_schema import EventKind, LogArg, LogCell, Severity
from wreath._logscratch import (
    DEFAULT_LIMITER_CAPACITY,
    LogSamplingPolicy,
    RequestLogBuffer,
    SiteLimiter,
)
from wreath._logsink import BoundedLogQueue
from wreath._logsite import LogField, SiteRegistry
from wreath._projector import ProjectedLog, Projector
from wreath.logging import LogRuntime, install
from wreath.response import TextResponse
from wreath.telemetry import (
    _LOG_LIMITER_SLOT_BYTES,
    _LOG_QUEUED_RECORD_BYTES,
    _LOG_SCRATCH_RECORD_BYTES,
    _LOG_SITE_BYTES,
)

try:  # A benchmark competitor, never a runtime dependency.
    import structlog
except ImportError:  # pragma: no cover - exercised by the absence of the group
    structlog = None  # type: ignore[assignment]

try:
    from wreath._native import _flight
except ImportError:  # pragma: no cover - a pure build has no ring to publish to
    _flight = None  # type: ignore[assignment]


# --- shared fixtures --------------------------------------------------------


class CountingSink:
    """A sink that only counts. The floor a real sink is measured against."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, cell: LogCell) -> None:
        self.count += 1


class EncodingSink:
    """Counts, but encodes first: the packing a recorder sink pays before the ring."""

    __slots__ = ("bytes_", "count")

    def __init__(self) -> None:
        self.count = 0
        self.bytes_ = 0

    def __call__(self, cell: LogCell) -> None:
        self.count += 1
        self.bytes_ += len(cell.encode())


class DiscardStream:
    """A stream that formats into nothing, so a StreamHandler arm is not I/O."""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        return None


class DiscardQueue:
    """A queue that drops, so the QueueHandler arm measures the handler.

    `QueueHandler` is stdlib's own hand-off shape and the closest thing it has
    to wreath's deferred rendering -- except that `prepare()` formats the record
    before enqueueing it, which is the point of measuring it.
    """

    def put_nowait(self, _record: object) -> None:
        return None


#: The registered site every `emit` arm uses. Two arguments, one int and one
#: string, which is the shape the guide's example uses and the shape a real
#: `auth.denied` has. RAW on the string so the arm is not measuring SipHash;
#: the hashed variant is its own arm, because the default is HASHED and the
#: difference between the two turned out to matter.
DENIED_RAW = log.event(
    "bench.denied.raw",
    "user {user} denied {resource}",
    level=log.WARN,
    fields=(log.field("user", int), log.field("resource", str, log.RAW)),
)
DENIED_HASHED = log.event(
    "bench.denied.hashed",
    "user {user} denied {resource}",
    level=log.WARN,
    fields=(log.field("user", int), log.field("resource", str, log.HASHED)),
)
#: An INFO site, for the limiter arm. It cannot be either of the WARN sites
#: above: `LogSamplingPolicy.ceiling` is INFO, so a WARN record is never
#: sampled -- "an error nobody sees is the worst outcome" -- and an arm built on
#: one would be labelled as the drop path while measuring the emit path. The
#: integrity check below is what found that.
BUSY_INFO = log.event(
    "bench.busy",
    "user {user} touched {resource}",
    level=log.INFO,
    fields=(log.field("user", int), log.field("resource", str, log.RAW)),
)
#: A DEBUG site, for the disabled and buffered arms.
STEP = log.event(
    "bench.step",
    "step {name} at {elapsed}",
    level=log.DEBUG,
    fields=(log.field("name", str, log.RAW), log.field("elapsed", int)),
)

#: The runtime every arm mutates rather than replaces, so interned site ids stay
#: stable for the whole run and no arm pays for an `install`.
RUNTIME = LogRuntime(level=log.INFO, capture_level=log.INFO)

_UNLIMITED = LogSamplingPolicy(enabled=False)


def _configure(
    sink: Any,
    *,
    level: Severity = log.INFO,
    capture_level: Severity | None = None,
    sampling: LogSamplingPolicy | None = _UNLIMITED,
    native: Any = None,
) -> None:
    """Point the installed runtime at one arm's configuration."""
    RUNTIME.sink = sink
    RUNTIME.native = native
    RUNTIME.level = level
    RUNTIME.capture_level = level if capture_level is None else capture_level
    RUNTIME.limiter = SiteLimiter(sampling)


class NativeArm:
    """A recorder sized so one arm's whole run fits in its ring, and checked.

    A full ring is a counted drop, so an arm that overflows stops measuring the
    emit path and starts measuring `RING_FULL` -- fast, and wrong. The ring is
    sized from the run's own record budget and `check` refuses the result if a
    single record was lost, rather than trusting the arithmetic.
    """

    def __init__(self, label: str, budget: int) -> None:
        self.label = label
        records = 1 << (budget.bit_length() + 1)
        self.recorder = _flight.Recorder(
            _flight.MODE_PULSE, ring_records=records, active_requests=16
        )
        self.sink = log.recorder_sink(self.recorder)
        self.native = log.recorder_emitter(self.recorder)
        if self.native is None:
            raise SystemExit(
                "bench_logging: this build's recorder has no native emitter, so "
                "the native arms would silently measure the Python packer."
            )

    def check(self) -> int:
        lost = self.recorder.loss(_flight.LOSS_RING_FULL)
        if lost:
            raise SystemExit(
                f"bench_logging: the {self.label!r} arm lost {lost} records to a "
                "full ring, so its timings are the cost of dropping. Lower "
                "--iterations or --rounds."
            )
        return lost

    def published(self) -> int:
        """Log cells the arm actually put on the ring. Drains, so call it last.

        Byte 1 of every cell is its `EventKind`; counting the LOG ones is
        cheaper than decoding, and decoding is what the drain suite measures.
        """
        cells = 0
        while True:
            drained = self.recorder.drain(4096)
            if not drained:
                return cells
            cells += sum(
                1
                for offset in range(1, len(drained), 64)
                if drained[offset] == EventKind.LOG
            )


def _stdlib_logger(name: str, handler: stdlib_logging.Handler, level: int) -> Any:
    logger = stdlib_logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(level)
    return logger


def _structlog_logger(level: int) -> Any:
    """structlog in its documented fast configuration.

    `ReturnLoggerFactory` renders and returns rather than writing, which is the
    fairest floor available: it excludes I/O from both sides, exactly as the
    wreath arms exclude the writer thread.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.ReturnLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()


# --- suite: emit ------------------------------------------------------------


def suite_emit(rounds: int, iterations: int, warmup: int) -> dict[str, Any]:
    """Measurement 1: an enabled record, against stdlib and structlog."""
    counting = CountingSink()
    encoding = EncodingSink()

    def noop(n: int) -> None:
        _configure(counting)
        for _ in range(n):
            pass

    def wreath_event_raw(n: int) -> None:
        _configure(counting)
        for i in range(n):
            DENIED_RAW(i, "orders")

    def wreath_event_hashed(n: int) -> None:
        _configure(counting)
        for i in range(n):
            DENIED_HASHED(i, "orders")

    def wreath_event_encoded(n: int) -> None:
        # The production shape: pack, then encode 64 bytes for the ring.
        _configure(encoding)
        for i in range(n):
            DENIED_RAW(i, "orders")

    def wreath_event_limited(n: int) -> None:
        # The default policy, from one INFO site, at benchmark rates: this is
        # the drop path, and it is here so the drop path is never mistaken for
        # the emit path in a later reading of these numbers.
        _configure(counting, sampling=LogSamplingPolicy())
        for i in range(n):
            BUSY_INFO(i, "orders")

    def wreath_kwargs(n: int) -> None:
        _configure(counting)
        for i in range(n):
            log.info("user {user} denied {resource}", user=i, resource="orders")

    # The production path: pack in C, straight into a ring cell. Each arm gets
    # its own recorder because the ring has to hold that arm's entire run.
    budget = warmup + rounds * iterations
    native_arms = (
        [
            NativeArm("native event (raw)", budget),
            NativeArm("native event (hashed)", budget),
            NativeArm("native log.info(**kwargs)", budget),
        ]
        if _flight is not None
        else []
    )

    def _native_payload(arm: NativeArm, emit: Any) -> Any:
        def payload(n: int) -> None:
            _configure(arm.sink, native=arm.native)
            for i in range(n):
                emit(i)

        return payload

    stdlib_null = _stdlib_logger(
        "bench.null", stdlib_logging.NullHandler(), stdlib_logging.WARNING
    )
    stdlib_stream = _stdlib_logger(
        "bench.stream",
        stdlib_logging.StreamHandler(DiscardStream()),
        stdlib_logging.WARNING,
    )
    stdlib_queued = _stdlib_logger(
        "bench.queued", QueueHandler(DiscardQueue()), stdlib_logging.WARNING
    )

    def _stdlib_arm(logger: Any) -> Any:
        def payload(n: int) -> None:
            for i in range(n):
                logger.warning("user %s denied %s", i, "orders")

        return payload

    arms = [
        Arm("noop", payload=noop),
        Arm("wreath event, 2 args (raw)", payload=wreath_event_raw),
        Arm("wreath event, 2 args (hashed)", payload=wreath_event_hashed),
        Arm("wreath event + cell encode", payload=wreath_event_encoded),
        Arm("wreath log.info(**kwargs)", payload=wreath_kwargs),
        Arm("wreath event (limiter dropping)", payload=wreath_event_limited),
        Arm("stdlib NullHandler", payload=_stdlib_arm(stdlib_null)),
        Arm("stdlib StreamHandler", payload=_stdlib_arm(stdlib_stream)),
        Arm("stdlib QueueHandler", payload=_stdlib_arm(stdlib_queued)),
    ]
    if native_arms:
        raw_arm, hashed_arm, kwargs_arm = native_arms
        arms.extend(
            [
                Arm(
                    "wreath event -> native emitter",
                    payload=_native_payload(
                        raw_arm, lambda i: DENIED_RAW(i, "orders")
                    ),
                ),
                Arm(
                    "wreath event hashed -> native",
                    payload=_native_payload(
                        hashed_arm, lambda i: DENIED_HASHED(i, "orders")
                    ),
                ),
                Arm(
                    "wreath log.info(**kwargs) -> native",
                    payload=_native_payload(
                        kwargs_arm,
                        lambda i: log.info(
                            "user {user} denied {resource}", user=i, resource="orders"
                        ),
                    ),
                ),
            ]
        )
    if structlog is not None:
        logger = _structlog_logger(stdlib_logging.WARNING)

        def structlog_arm(n: int) -> None:
            for i in range(n):
                logger.warning("user denied", user=i, resource="orders")

        arms.append(Arm("structlog (ReturnLogger)", payload=structlog_arm))
    arms.append(Arm("noop (A/A)", payload=noop))

    measure.measure_callables(arms, rounds, iterations, warmup)
    document = measure.report(arms, "noop", "noop (A/A)")
    document["structlog"] = "installed" if structlog is not None else "not installed"
    document["integrity"] = _verify_emit(counting)
    document["integrity"]["native_ring_losses"] = {
        arm.label: arm.check() for arm in native_arms
    }
    return document


def _verify_emit(sink: CountingSink) -> dict[str, Any]:
    """Drive each shape a fixed number of times and check what reached the sink.

    An arm whose records are being dropped is not measuring what its label says.
    This is the logging equivalent of `measure.verify_serving`.
    """
    checks: dict[str, Any] = {}

    sink.count = 0
    _configure(sink)
    for i in range(1000):
        DENIED_RAW(i, "orders")
    checks["unlimited_published"] = sink.count

    sink.count = 0
    _configure(sink, sampling=LogSamplingPolicy())
    for i in range(1000):
        BUSY_INFO(i, "orders")
    checks["limited_published"] = sink.count

    _configure(sink)
    if checks["unlimited_published"] != 1000:
        raise SystemExit(
            f"bench_logging: the unlimited emit arm published "
            f"{checks['unlimited_published']} of 1000 records. Its timings are the "
            "cost of dropping, not of emitting."
        )
    if checks["limited_published"] >= 1000:
        raise SystemExit(
            "bench_logging: the limiter arm published everything, so it is not "
            "measuring the drop path it is labelled with."
        )
    return checks


# --- suite: disabled --------------------------------------------------------


def suite_disabled(rounds: int, iterations: int, warmup: int) -> dict[str, Any]:
    """Measurement 2: the cost of a call that produces nothing.

    The load-bearing one. Failure-triggered logging assumes verbose
    instrumentation is affordable, and that assumption is exactly this number.
    Three states a DEBUG site can be in, all measured:

    - **disabled** -- below `capture_level`, so the call returns after a compare;
    - **guarded** -- `if STEP:`, which skips building the argument tuple;
    - **buffered** -- at `capture_level` but below `level`, inside a request, so
      the record is packed and held for a promotion that may never come.
    """
    counting = CountingSink()

    def noop(n: int) -> None:
        _configure(counting)
        for _ in range(n):
            pass

    def disabled_event(n: int) -> None:
        _configure(counting)  # level and capture_level both INFO: DEBUG is off
        for i in range(n):
            STEP("validate", i)

    def disabled_guarded(n: int) -> None:
        _configure(counting)
        for i in range(n):
            if STEP:
                STEP("validate", i)

    def disabled_kwargs(n: int) -> None:
        _configure(counting)
        for i in range(n):
            log.debug("step {name} at {elapsed}", name="validate", elapsed=i)

    def buffered_event(n: int) -> None:
        # capture_level DEBUG, level INFO: packed, then held. A budget of n so
        # the buffer never overflows into the drop path mid-measurement, and a
        # discarding `finish` afterwards so held records are released between
        # rounds -- retaining them across the run measures the GC, not the tier.
        _configure(counting, level=log.INFO, capture_level=log.DEBUG)
        scope = log.begin_request(1, budget=n + 1)
        for i in range(n):
            STEP("validate", i)
        held = 0 if scope is None else scope.held
        if scope is not None:
            scope.finish(promoted=False)
        if held != n:
            raise SystemExit(
                f"bench_logging: the buffered arm held {held} of {n} records; it is "
                "measuring the scratch-overflow drop path."
            )

    stdlib_off = _stdlib_logger(
        "bench.off", stdlib_logging.NullHandler(), stdlib_logging.WARNING
    )

    def stdlib_disabled(n: int) -> None:
        for i in range(n):
            stdlib_off.debug("step %s at %s", "validate", i)

    def stdlib_disabled_guarded(n: int) -> None:
        for i in range(n):
            if stdlib_off.isEnabledFor(stdlib_logging.DEBUG):
                stdlib_off.debug("step %s at %s", "validate", i)

    arms = [
        Arm("noop", payload=noop),
        Arm("wreath DEBUG site, disabled", payload=disabled_event),
        Arm("wreath DEBUG site, if-guarded", payload=disabled_guarded),
        Arm("wreath log.debug(**kwargs), disabled", payload=disabled_kwargs),
        Arm("wreath DEBUG site, buffered", payload=buffered_event),
        Arm("stdlib logger.debug, disabled", payload=stdlib_disabled),
        Arm("stdlib logger.debug, isEnabledFor", payload=stdlib_disabled_guarded),
    ]
    if structlog is not None:
        logger = _structlog_logger(stdlib_logging.WARNING)

        def structlog_disabled(n: int) -> None:
            for i in range(n):
                logger.debug("step", name="validate", elapsed=i)

        arms.append(Arm("structlog .debug(), filtered out", payload=structlog_disabled))
    arms.append(Arm("noop (A/A)", payload=noop))

    measure.measure_callables(arms, rounds, iterations, warmup)
    document = measure.report(arms, "noop", "noop (A/A)")

    # Nothing may have reached the sink: every arm here is disabled or buffered.
    counting.count = 0
    _configure(counting)
    for i in range(1000):
        STEP("validate", i)
    if counting.count:
        raise SystemExit(
            f"bench_logging: {counting.count} records escaped a disabled site; the "
            "disabled arms are measuring an emit path."
        )
    document["integrity"] = {"disabled_published": counting.count}
    return document


# --- suite: publish ---------------------------------------------------------


def _publish_samples(cycle: Any, recorder: Any, batch: int, trials: int) -> list[float]:
    """Nanoseconds per operation, draining the ring between batches, untimed.

    Draining inside a timed batch would measure the drain; not draining at all
    would fill the ring and measure `RING_FULL` accounting, which is the trap
    `bench_flight_recorder` documents. So: drain, time a ring's worth, repeat.
    """
    samples: list[float] = []
    for _ in range(trials):
        recorder.drain(batch)
        started = perf_counter_ns()
        for i in range(batch):
            cycle(i)
        samples.append((perf_counter_ns() - started) / batch)
    return samples


def suite_publish(trials: int, ring: int) -> dict[str, Any]:
    """Measurement 3: a LOG cell's publish against a COMPLETION cell's.

    The plan's expectation is that they are near-identical -- both are a bounds
    check and a 64-byte `memcpy` into the same ring -- and that a gap means the
    packing above the seam is wrong. This decomposes the gap: encode alone,
    publish alone, both together, against the C completion path.
    """
    if _flight is None:
        return {"skipped": "the native _flight extension is not built"}

    batch = min(ring, 1 << 14)
    template = LogCell(
        request_id=1,
        site_id=DENIED_RAW.site_id,
        severity=log.WARN,
        args=(LogArg.integer(17), LogArg.text("orders")),
    )
    encoded = template.encode()

    def make_recorder(ring_path: str | None = None) -> Any:
        return _flight.Recorder(
            _flight.MODE_PULSE,
            ring_records=ring,
            active_requests=2048,
            ring_path=ring_path,
        )

    def encode_only(_i: int) -> None:
        template.encode()

    def publish_only(recorder: Any) -> Any:
        def cycle(_i: int) -> None:
            recorder.publish_log(encoded)

        return cycle

    def encode_and_publish(recorder: Any) -> Any:
        def cycle(_i: int) -> None:
            recorder.publish_log(template.encode())

        return cycle

    def completion(recorder: Any) -> Any:
        def cycle(_i: int) -> None:
            request = recorder.begin(1, 1, 0)
            request.route(7, 3)
            request.finish(1_000, 200, 0, 0, 0, 12)

        return cycle

    def native(recorder: Any) -> Any:
        # `wreath_nfr_log`: pack and publish in one call, no Python object in
        # between. This is the arm the other three exist to be read against --
        # the plan predicted the publish would be near-identical to a
        # completion's and the packing above it would be where the gap was.
        specs_blob = DENIED_RAW.site.specs
        key = RUNTIME.registry.key
        values = (17, "orders")

        def cycle(_i: int) -> None:
            recorder.log(
                DENIED_RAW.site_id, int(log.WARN), 1, 0, 0, specs_blob, values,
                key[0], key[1],
            )

        return cycle

    recorders = [make_recorder() for _ in range(6)]
    # The forensic ring, in the same round as the heap one: what does it cost a
    # publish to write into a MAP_SHARED file rather than PyMem memory? The
    # question an operator actually asks before turning it on, and the only way
    # to answer it without the between-run drift a separate run would carry.
    mapped_dir = tempfile.mkdtemp(prefix="wreath-bench-ring-")
    mapped = make_recorder(os.path.join(mapped_dir, "flight.wfrr"))
    specs = [
        ("noop", lambda _i: None, recorders[0]),
        ("LogCell.encode() only", encode_only, recorders[0]),
        ("publish_log(pre-encoded)", publish_only(recorders[1]), recorders[1]),
        ("publish_log -> mapped ring file", publish_only(mapped), mapped),
        ("encode + publish_log", encode_and_publish(recorders[2]), recorders[2]),
        ("native pack + publish", native(recorders[3]), recorders[3]),
        ("completion (begin/route/finish)", completion(recorders[4]), recorders[4]),
        ("noop (A/A)", lambda _i: None, recorders[5]),
    ]

    arms = []
    for label, cycle, recorder in specs:
        for _ in range(batch):  # warm the path and the active-slot free list
            cycle(0)
        recorder.drain(batch)
        arm = Arm(label)
        arm.samples = _publish_samples(cycle, recorder, batch, trials)
        arms.append(arm)

    document = measure.report(arms, "noop", "noop (A/A)", unit="ns")
    document["batch"] = batch
    document["trials"] = trials
    document["ring_file_bytes"] = os.path.getsize(
        os.path.join(mapped_dir, "flight.wfrr")
    )
    shutil.rmtree(mapped_dir, ignore_errors=True)
    return document


# --- suite: drain -----------------------------------------------------------


def suite_drain(trials: int, requests: int, ratios: tuple[int, ...]) -> dict[str, Any]:
    """Measurement 4: projector drain throughput with log cells mixed in.

    Logs outnumber completions by one to two orders of magnitude and the
    projector was sized for completions, so the question is whether per-cell
    cost holds as the mix shifts. Filling the ring is untimed; one `poll()` --
    drain, decode, ingest, settle -- is the timed region.
    """
    if _flight is None:
        return {"skipped": "the native _flight extension is not built"}

    rows: list[dict[str, Any]] = []
    for ratio in ratios:
        cells_per_request = 1 + ratio
        total_cells = requests * cells_per_request
        # The ring must hold a whole trial: a fill that overflows would measure
        # RING_FULL accounting, and a poll capped below the fill would leave the
        # tail for the next one, so `max_cells` is sized to the fill as well.
        ring = 1 << (total_cells.bit_length() + 1)
        samples: list[float] = []
        for _ in range(trials):
            recorder = _flight.Recorder(
                _flight.MODE_PULSE, ring_records=ring, active_requests=4096
            )
            projector = Projector(
                recorder,
                on_log=lambda _record: None,
                max_cells=total_cells + 16,
                pending=requests * 2,
            )
            for _ in range(requests):
                request = recorder.begin(1, 1, 0)
                request_id = request.request_id
                request.route(7, 3)
                request.finish(1_000, 200, 0, 0, 0, 12)
                for index in range(ratio):
                    recorder.publish_log(
                        LogCell(
                            request_id=request_id,
                            site_id=DENIED_RAW.site_id,
                            severity=log.WARN,
                            args=(LogArg.integer(index), LogArg.text("orders")),
                        ).encode()
                    )
            started = perf_counter_ns()
            projector.poll()
            settled = projector.poll()  # a completion settles on a quiet cycle
            elapsed = perf_counter_ns() - started
            if settled != requests:
                raise SystemExit(
                    f"bench_logging: the drain arm settled {settled} of {requests} "
                    "traces, so its per-cell cost covers work it did not finish."
                )
            samples.append(elapsed / total_cells)
        median = statistics.median(samples)
        rows.append(
            {
                "logs_per_request": ratio,
                "cells": total_cells,
                "ns_per_cell": round(median, 1),
                "million_cells_per_second": round(1000.0 / median, 2),
                "raw": [round(value, 1) for value in samples],
            }
        )
        print(
            f"{ratio:4d} logs/request  {median:7.1f} ns/cell  "
            f"{1000.0 / median:6.2f}M cells/s"
        )
    return {"rows": rows, "requests_per_trial": requests, "trials": trials}


# --- suite: request ---------------------------------------------------------


def _bench_app(records: int, fields: int, level: Severity) -> Any:
    """One route whose handler emits `records` records and attaches `fields`."""
    app = Wreath()

    @app.get("/bench")
    async def handler(request: Any) -> Any:
        for index in range(records):
            if level is log.DEBUG:
                STEP("validate", index)
            else:
                DENIED_RAW(index, "orders")
        for index in range(fields):
            log.set_field(f"field{index}", index)
        return TextResponse("ok")

    return app


#: The runtime an "off" arm installs: no sink, so `_ACTIVE` is false and the
#: request path takes the same branches it takes in a process that never
#: configured logging.
OFF_RUNTIME = LogRuntime(level=log.INFO)


def _switching_app(app: Any, *, on: bool) -> Any:
    """Wrap an app so it puts the process into one arm's logging state.

    Interleaving is the whole method here -- arms must alternate so drift hits
    all of them -- but "logging on" and "logging off" are *process* state, not
    per-app state. So each arm asserts its state at the top of every request.
    Both branches do the same work (a compare, an `install`, one dict
    operation), so the switch cancels in the delta rather than favouring an arm.

    Seeding `_wreath_flight` is what an HTTP/2, HTTP/3 or WebSocket protocol
    does before dispatch; an in-process ASGI call has no protocol to do it, and
    without it the request arms would exercise no log scope at all.
    """

    async def wrapper(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if on:
            install(RUNTIME)
            scope["_wreath_flight"] = 1
        else:
            install(OFF_RUNTIME)
            scope.pop("_wreath_flight", None)
        await app(scope, receive, send)

    return wrapper


def suite_request(rounds: int, iterations: int, warmup: int) -> dict[str, Any]:
    """Measurement 5: what a request costs with logging on and off.

    Ablation at whole-request level, per AGENTS.md: the arms differ only in what
    logging is doing, so the delta is logging's cost on the Python request path.
    This is not a socket round trip -- `--suite e2e` is that -- and the number
    that transfers between the two is the absolute microseconds, not the
    percentage, because the base here excludes the server.
    """
    counting = CountingSink()
    # The production configuration: a recorder-backed sink and the native
    # emitter behind it. Measuring the pure packer here would report what
    # logging used to cost rather than what it costs, and the two differ by
    # more than the whole request the arms are built around.
    native_arm: NativeArm | None = None
    if _flight is not None:
        native_arm = NativeArm("request", warmup + rounds * iterations * 8)
        _configure(
            native_arm.sink,
            level=log.INFO,
            capture_level=log.DEBUG,
            native=native_arm.native,
        )
    else:
        _configure(counting, level=log.INFO, capture_level=log.DEBUG)
    template = measure.scope(path="/bench")

    arms = [
        Arm("logging off", _switching_app(_bench_app(0, 0, log.INFO), on=False)),
        Arm(
            "logging on, handler silent",
            _switching_app(_bench_app(0, 0, log.INFO), on=True),
        ),
        Arm(
            "logging on, 1 INFO record",
            _switching_app(_bench_app(1, 0, log.INFO), on=True),
        ),
        Arm(
            "logging on, 5 INFO records",
            _switching_app(_bench_app(5, 0, log.INFO), on=True),
        ),
        Arm(
            "logging on, 5 buffered DEBUG",
            _switching_app(_bench_app(5, 0, log.DEBUG), on=True),
        ),
        Arm(
            "logging on, 3 canonical fields",
            _switching_app(_bench_app(0, 3, log.INFO), on=True),
        ),
        Arm("logging off (A/A)", _switching_app(_bench_app(0, 0, log.INFO), on=False)),
    ]

    asyncio.run(measure.measure_apps(arms, template, rounds, iterations, warmup))
    document = measure.report(arms, "logging off", "logging off (A/A)")

    if native_arm is not None:
        # Two ways these arms could measure nothing: a ring that filled (which
        # would make the record-emitting arms *faster* than the silent one), and
        # a log scope that never opened (which would make every arm identical).
        document["integrity"] = {
            "native_ring_losses": native_arm.check(),
            "records_on_the_ring": native_arm.published(),
        }
        if document["integrity"]["records_on_the_ring"] == 0:
            raise SystemExit(
                "bench_logging: the request arms put no records on the ring. The "
                "log scope is not opening, so every arm measures the same thing."
            )
    elif counting.count == 0:
        raise SystemExit(
            "bench_logging: the request arms published no records at all. The log "
            "scope is not opening, so every arm is measuring the same thing."
        )
    else:
        document["integrity"] = {"records_published": counting.count}
    install(RUNTIME)
    return document


# --- suite: memory ----------------------------------------------------------


def _traced(build: Any) -> int:
    """Bytes still allocated for what `build` returns, transients excluded.

    `get_traced_memory()[0]` is *current* allocation rather than peak, so a
    builder's temporaries do not count and what remains is what the table
    actually costs to hold. The result stays referenced across the second
    reading, which is why it is bound to a name rather than discarded.
    """
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    retained = build()
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    del retained
    return after - before


def suite_memory(entries: int) -> dict[str, Any]:
    """Measurement 6: whether the per-entry constants resemble reality.

    `MemoryBudget.logging` is the one estimated component of an otherwise exact
    budget, built from four provisional constants in `telemetry.py`. Each is
    measured here against the object it claims to describe.
    """

    def build_sites() -> Any:
        registry = SiteRegistry(capacity=entries)
        for index in range(entries):
            registry.register(
                f"bench.site.{index}",
                "user {user} denied {resource}",
                Severity.INFO,
                (
                    LogField("user", int, log.RAW),
                    LogField("resource", str, log.HASHED),
                ),
            )
        return registry

    def build_limiter() -> Any:
        limiter = SiteLimiter(LogSamplingPolicy(), capacity=entries)
        for index in range(1, entries + 1):
            limiter.allow(index, Severity.INFO)
        return limiter

    def build_queue() -> Any:
        queue = BoundedLogQueue(capacity=entries)
        for index in range(entries):
            queue.offer(
                ProjectedLog(
                    cell=LogCell(
                        request_id=index,
                        site_id=1,
                        severity=Severity.INFO,
                        args=(LogArg.integer(index), LogArg.text("orders")),
                    )
                )
            )
        return queue

    def build_scratch() -> Any:
        buffer = RequestLogBuffer(1, budget=entries)
        for index in range(entries):
            buffer.add(
                LogCell(
                    request_id=1,
                    site_id=1,
                    severity=Severity.DEBUG,
                    args=(LogArg.text("validate"), LogArg.integer(index)),
                )
            )
        return buffer

    measured = {
        "site": (_traced(build_sites), _LOG_SITE_BYTES),
        "limiter_slot": (_traced(build_limiter), _LOG_LIMITER_SLOT_BYTES),
        "queued_record": (_traced(build_queue), _LOG_QUEUED_RECORD_BYTES),
        "scratch_record": (_traced(build_scratch), _LOG_SCRATCH_RECORD_BYTES),
    }

    rows = []
    print(f"{'component':18s} {'measured':>12s} {'constant':>10s} {'ratio':>8s}")
    print("-" * 52)
    for name, (total, constant) in measured.items():
        actual = total / entries
        rows.append(
            {
                "component": name,
                "bytes_per_entry_measured": round(actual, 1),
                "bytes_per_entry_constant": constant,
                "ratio": round(actual / constant, 3),
            }
        )
        print(f"{name:18s} {actual:11.1f}B {constant:9d}B {actual / constant:7.2f}x")
    return {"entries": entries, "rows": rows, "limiter_capacity": DEFAULT_LIMITER_CAPACITY}


# --- suite: e2e -------------------------------------------------------------


def suite_e2e(requests: int, concurrency: int, repeats: int) -> dict[str, Any]:
    """Measurement 5, over a socket: the number a user actually sees.

    Boots wreath's own server twice -- once with logging configured, once
    without -- and drives each with the built-in development load generator.
    That generator shares the process with the server, so these are development
    numbers and are labelled as such; the arms are A/B/A interleaved so the
    *delta* between them survives the drift that shared-process measurement
    introduces. Absolute throughput here must not be quoted against any other
    generator's rows.
    """
    from benchmarks import load
    from wreath.server import ServerConfig, serve
    from wreath.telemetry import LoggingConfig, Mode, SamplingPolicy, TelemetryConfig

    app = Wreath()

    @app.get("/bench")
    async def handler(request: Any) -> Any:
        DENIED_RAW(17, "orders")
        return TextResponse("ok")

    def config(*, logging_on: bool) -> Any:
        return ServerConfig(
            host="127.0.0.1",
            port=0,
            lifespan="off",
            telemetry=TelemetryConfig(
                mode=Mode.PULSE,
                ring_records=8192,
                active_requests=1024,
                detailed=SamplingPolicy(rate=0.0),
                logging=LoggingConfig(enabled=logging_on),
            ),
            # A discarding writer, so the arms differ in what logging costs the
            # request path and not in what a terminal costs the writer thread.
            log_writer=lambda _line: None,
        )

    async def one(logging_on: bool) -> Any:
        server = await serve(app, config(logging_on=logging_on))
        try:
            port = server.sockets[0].getsockname()[1]
            return await load.measure(
                "127.0.0.1",
                port,
                "/bench",
                duration=0.0,
                warmup=0.0,
                concurrency=concurrency,
                requests=requests,
                warmup_requests=max(100, requests // 10),
            )
        finally:
            await server.close()

    async def drive() -> list[tuple[str, Any]]:
        results: list[tuple[str, Any]] = []
        for _ in range(repeats):
            for label, on in (("off", False), ("on", True), ("off (A/A)", False)):
                results.append((label, await one(on)))
        return results

    raw = asyncio.run(drive())
    grouped: dict[str, list[Any]] = {}
    for label, result in raw:
        grouped.setdefault(label, []).append(result)

    rows = []
    for label, results in grouped.items():
        rows.append(
            {
                "arm": label,
                "requests_per_second": round(
                    statistics.median(r.requests_per_second for r in results), 1
                ),
                "latency_ms_median": round(
                    statistics.median(r.latency_ms_median for r in results), 4
                ),
                "latency_ms_p99": round(
                    statistics.median(r.latency_ms_p99 for r in results), 4
                ),
                "errors": sum(r.errors for r in results),
            }
        )
        print(
            f"{label:12s} {rows[-1]['requests_per_second']:9.1f} rps  "
            f"median {rows[-1]['latency_ms_median']:.4f}ms  "
            f"p99 {rows[-1]['latency_ms_p99']:.4f}ms"
        )
    return {
        "generator": load.LOAD_GENERATOR,
        "generator_version": load.LOAD_GENERATOR_VERSION,
        "shared_process": True,
        "requests": requests,
        "concurrency": concurrency,
        "repeats": repeats,
        "rows": rows,
    }


# --- entry point ------------------------------------------------------------


SUITES = ("emit", "disabled", "publish", "drain", "request", "memory")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        nargs="+",
        default=list(SUITES),
        choices=[*SUITES, "e2e", "all"],
        help="which measurements to run (default: everything but e2e)",
    )
    parser.add_argument("--rounds", type=int, default=measure.DEFAULT_ROUNDS)
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--warmup", type=int, default=5_000)
    parser.add_argument("--trials", type=int, default=15)
    parser.add_argument("--ring", type=int, default=1 << 16)
    parser.add_argument("--drain-requests", type=int, default=512)
    parser.add_argument("--memory-entries", type=int, default=4096)
    parser.add_argument("--e2e-requests", type=int, default=20_000)
    parser.add_argument("--e2e-concurrency", type=int, default=32)
    parser.add_argument("--e2e-repeats", type=int, default=3)
    parser.add_argument("--label", default="unlabelled")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    selected = list(SUITES) + ["e2e"] if "all" in args.suite else args.suite
    install(RUNTIME)

    document: dict[str, Any] = {
        "tool": "benchmarks.bench_logging",
        "schema_version": 1,
        "label": args.label,
        "python": sys.version,
        "implementation": sys.implementation.name,
        "platform": platform.platform(),
        "native_flight": None if _flight is None else _flight.__file__,
        "rounds": args.rounds,
        "iterations": args.iterations,
        "suites": {},
    }

    for name in selected:
        print(f"\n=== {name} " + "=" * (60 - len(name)))
        if name == "emit":
            result = suite_emit(args.rounds, args.iterations, args.warmup)
        elif name == "disabled":
            result = suite_disabled(args.rounds, args.iterations, args.warmup)
        elif name == "publish":
            result = suite_publish(args.trials, args.ring)
        elif name == "drain":
            result = suite_drain(args.trials, args.drain_requests, (0, 1, 10, 100))
        elif name == "request":
            result = suite_request(args.rounds, max(1000, args.iterations // 10),
                                   args.warmup // 2)
        elif name == "memory":
            result = suite_memory(args.memory_entries)
        else:
            result = suite_e2e(
                args.e2e_requests, args.e2e_concurrency, args.e2e_repeats
            )
        document["suites"][name] = result

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
