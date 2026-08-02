"""Stage 4 of first-class logging: promotion and per-call-site sampling.

These two are where the *signal* argument lives. A logger that is faster at
emitting noise has improved nothing.

- **Promotion.** TRACE and DEBUG accumulate in a per-request buffer and are
  published only if that request failed or ran slow. Verbose instrumentation
  everywhere, near-zero steady-state output, and a full history of exactly the
  requests that went wrong. The pattern is Marick's 2000 ring-buffer logging;
  the recorder's existing error/slow promotion flags are the trigger.

- **Sampling.** A runaway call site cannot flood the pipeline and evict
  everything else. First N per tick, then every Mth, per site -- Zap's rule,
  made cheap by the site id already being a dense integer.

Both drop records on purpose, so both must account for what they dropped. A
record that vanishes without a number attached is the failure an observability
system exists to prevent.
"""

from __future__ import annotations

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._logscratch import LogSamplingPolicy, RequestLogBuffer, SiteLimiter


class FakeClock:
    """A hand-cranked monotonic clock, so a tick boundary is a decision."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- per-call-site sampling -------------------------------------------------


def test_first_n_pass_then_every_mth() -> None:
    clock = FakeClock()
    limiter = SiteLimiter(LogSamplingPolicy(first=2, thereafter=3), clock=clock)
    verdicts = [limiter.allow(site_id=1, severity=fs.Severity.INFO) for _ in range(9)]
    # 1,2 pass as the first two; then every third: the 5th and the 8th.
    assert verdicts == [True, True, False, False, True, False, False, True, False]


def test_a_new_tick_resets_the_budget() -> None:
    clock = FakeClock()
    limiter = SiteLimiter(LogSamplingPolicy(first=1, thereafter=100), clock=clock)
    assert limiter.allow(site_id=1, severity=fs.Severity.INFO)
    assert not limiter.allow(site_id=1, severity=fs.Severity.INFO)
    clock.advance(1.0)
    assert limiter.allow(site_id=1, severity=fs.Severity.INFO)


def test_sites_have_independent_budgets() -> None:
    limiter = SiteLimiter(LogSamplingPolicy(first=1, thereafter=100), clock=FakeClock())
    assert limiter.allow(site_id=1, severity=fs.Severity.INFO)
    assert limiter.allow(site_id=2, severity=fs.Severity.INFO)
    assert not limiter.allow(site_id=1, severity=fs.Severity.INFO)


@pytest.mark.parametrize("severity", [fs.Severity.WARN, fs.Severity.ERROR, fs.Severity.FATAL])
def test_warn_and_above_are_never_sampled(severity: fs.Severity) -> None:
    """An error you never see is the worst failure mode in an observability
    system, even when it is counted."""
    limiter = SiteLimiter(LogSamplingPolicy(first=1, thereafter=1000), clock=FakeClock())
    assert all(limiter.allow(site_id=1, severity=severity) for _ in range(50))


def test_drops_are_carried_on_the_next_record_that_passes() -> None:
    """`dropped_siblings` is why the cell has the field: an operator reading one
    line learns how many like it were suppressed."""
    limiter = SiteLimiter(LogSamplingPolicy(first=1, thereafter=3), clock=FakeClock())
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # passes, 0 dropped
    assert limiter.take_dropped(1) == 0
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # dropped
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # dropped
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # passes (every 3rd)
    assert limiter.take_dropped(1) == 2
    assert limiter.take_dropped(1) == 0  # reading clears it


def test_an_uninterned_site_is_never_sampled() -> None:
    """Site 0 has no slot to count against; refusing to limit it is honest,
    and the site-table overflow that produced it is already counted."""
    limiter = SiteLimiter(LogSamplingPolicy(first=1, thereafter=1000), clock=FakeClock())
    assert all(limiter.allow(site_id=0, severity=fs.Severity.INFO) for _ in range(20))


def test_a_disabled_policy_passes_everything() -> None:
    limiter = SiteLimiter(LogSamplingPolicy(enabled=False), clock=FakeClock())
    assert all(limiter.allow(site_id=1, severity=fs.Severity.INFO) for _ in range(100))


def test_the_limiter_table_is_bounded() -> None:
    limiter = SiteLimiter(
        LogSamplingPolicy(first=1, thereafter=1000), clock=FakeClock(), capacity=4
    )
    # Sites beyond the table are passed rather than silently suppressed.
    assert limiter.allow(site_id=99, severity=fs.Severity.INFO)
    assert limiter.allow(site_id=99, severity=fs.Severity.INFO)


# --- per-request buffering and promotion ------------------------------------


def _cell(severity: fs.Severity = fs.Severity.DEBUG, site_id: int = 1) -> fs.LogCell:
    return fs.LogCell(request_id=0, site_id=site_id, severity=severity)


def test_a_buffer_holds_records_until_it_is_finished() -> None:
    emitted: list[fs.LogCell] = []
    buffer = RequestLogBuffer(request_id=7, budget=8)
    buffer.add(_cell())
    buffer.add(_cell())
    assert emitted == []
    buffer.finish(promoted=False, emit=emitted.append)
    assert emitted == []


def test_a_promoted_buffer_publishes_everything_it_held() -> None:
    emitted: list[fs.LogCell] = []
    buffer = RequestLogBuffer(request_id=7, budget=8)
    buffer.add(_cell(fs.Severity.TRACE))
    buffer.add(_cell(fs.Severity.DEBUG))
    buffer.finish(promoted=True, emit=emitted.append)
    assert len(emitted) == 2
    assert all(c.flags & fs.LOG_FLAG_PROMOTED for c in emitted)


def test_buffered_records_are_stamped_with_their_request() -> None:
    emitted: list[fs.LogCell] = []
    buffer = RequestLogBuffer(request_id=4242, budget=8)
    buffer.add(_cell())
    buffer.finish(promoted=True, emit=emitted.append)
    assert emitted[0].request_id == 4242


def test_an_explicit_promote_publishes_the_buffer() -> None:
    """For an anomaly the framework cannot see: the request looked fine."""
    emitted: list[fs.LogCell] = []
    buffer = RequestLogBuffer(request_id=1, budget=8)
    buffer.add(_cell())
    buffer.promote()
    buffer.finish(promoted=False, emit=emitted.append)
    assert len(emitted) == 1


def test_exhausting_the_buffer_budget_is_counted() -> None:
    buffer = RequestLogBuffer(request_id=1, budget=2)
    for _ in range(5):
        buffer.add(_cell())
    assert buffer.dropped == 3
    emitted: list[fs.LogCell] = []
    buffer.finish(promoted=True, emit=emitted.append)
    assert len(emitted) == 2


def test_a_buffer_keeps_the_oldest_records() -> None:
    """Failure-triggered logging exists to show what led *up to* the failure, so
    the head of the request is what must survive a full buffer."""
    buffer = RequestLogBuffer(request_id=1, budget=2)
    for site_id in (1, 2, 3, 4):
        buffer.add(_cell(site_id=site_id))
    emitted: list[fs.LogCell] = []
    buffer.finish(promoted=True, emit=emitted.append)
    assert [c.site_id for c in emitted] == [1, 2]


# --- the emitter honours both ----------------------------------------------


def test_debug_inside_a_request_is_buffered_not_emitted() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        with log.request_scope(request_id=5) as scope:
            log.debug("quiet {v}", v=1)
            assert records == []
            scope.finish(promoted=False)
        assert records == []


def test_debug_inside_a_failed_request_is_published() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        with log.request_scope(request_id=5) as scope:
            log.debug("led up to it {v}", v=1)
            log.debug("and then {v}", v=2)
            scope.finish(promoted=True)
    assert len(records) == 2
    assert all(c.request_id == 5 for c in records)
    assert all(c.flags & fs.LOG_FLAG_PROMOTED for c in records)


def test_warnings_inside_a_request_are_never_buffered() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        with log.request_scope(request_id=5) as scope:
            log.warn("now {v}", v=1)
            assert len(records) == 1
            scope.finish(promoted=False)
    assert len(records) == 1


def test_records_inside_a_request_carry_its_id() -> None:
    with log.testing_runtime(level=log.INFO) as records:
        with log.request_scope(request_id=99) as scope:
            log.info("during {v}", v=1)
            scope.finish(promoted=False)
    assert records[0].request_id == 99


def test_a_single_threshold_publishes_everything_above_it() -> None:
    """`capture_level` defaults to `level`, so a caller who never asks for
    buffering gets the one threshold they expect."""
    with log.testing_runtime(level=log.TRACE) as records:
        log.debug("boot {v}", v=1)
    assert len(records) == 1
    assert records[0].request_id == 0


def test_a_verbose_record_outside_a_request_is_dropped() -> None:
    """Between the two thresholds a record needs a request to be promoted with.
    At startup there is none, and publishing it anyway would contradict the
    `level` the operator set."""
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        log.debug("boot {v}", v=1)
        log.info("booted {v}", v=1)
    assert [c.severity for c in records] == [fs.Severity.INFO]


def test_a_site_below_the_floor_is_falsey() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.INFO):
        quiet = log.event("floor.debug", "x {v}", level=log.DEBUG,
                          fields=(log.field("v", int),))
        assert not quiet


def test_a_site_between_the_thresholds_is_truthy() -> None:
    """It buffers rather than publishes, which is still doing something -- a
    guard that called it disabled would silently switch off promotion."""
    with log.testing_runtime(level=log.INFO, capture_level=log.DEBUG):
        buffered = log.event("floor.buffered", "x {v}", level=log.DEBUG,
                             fields=(log.field("v", int),))
        assert buffered


def test_leaving_a_scope_without_finishing_discards_rather_than_leaks() -> None:
    """An escaped scope must not hold records alive or emit them later."""
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        with log.request_scope(request_id=5):
            log.debug("orphaned {v}", v=1)
    assert records == []


def test_the_emitter_applies_the_site_limiter() -> None:
    policy = LogSamplingPolicy(first=2, thereafter=1000)
    with log.testing_runtime(level=log.INFO, sampling=policy) as records:
        for i in range(10):
            log.info("noisy {v}", v=i)
    assert len(records) == 2


def test_the_emitter_reports_what_the_limiter_dropped() -> None:
    policy = LogSamplingPolicy(first=1, thereafter=3)
    with log.testing_runtime(level=log.INFO, sampling=policy) as records:
        for i in range(4):
            log.info("noisy {v}", v=i)
    assert len(records) == 2
    assert records[1].dropped_siblings == 2


def test_a_registered_event_takes_dropped_siblings_from_the_site_limiter() -> None:
    policy = LogSamplingPolicy(first=1, thereafter=3)
    with log.testing_runtime(level=log.INFO, sampling=policy) as records:
        event = log.event(
            "sampled.value",
            "sampled {value}",
            fields=(log.field("value", int),),
        )
        for value in range(4):
            event(value)

    assert len(records) == 2
    assert records[1].dropped_siblings == 2


def test_the_emitter_never_samples_a_warning() -> None:
    policy = LogSamplingPolicy(first=1, thereafter=1000)
    with log.testing_runtime(level=log.INFO, sampling=policy) as records:
        for i in range(10):
            log.error("bad {v}", v=i)
    assert len(records) == 10
