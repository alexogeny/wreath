from __future__ import annotations

from typing import Any, cast

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._logscratch import LogSamplingPolicy, OffLoopStage, RequestLogBuffer, SiteLimiter


class FakeClock:
    """A hand-cranked monotonic clock, so a tick boundary is a decision."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize(
    ("field", "value", "correct_form"),
    [
        ("enabled", 1, "boolean"),
        ("first", -1, "non-negative integer"),
        ("first", True, "non-negative integer"),
        ("first", 1.5, "non-negative integer"),
        ("thereafter", 0, "positive integer"),
        ("thereafter", True, "positive integer"),
        ("thereafter", 1.5, "positive integer"),
        ("interval", 0, "finite positive number"),
        ("interval", True, "finite positive number"),
        ("interval", float("nan"), "finite positive number"),
        ("interval", float("inf"), "finite positive number"),
        ("ceiling", int(fs.Severity.INFO), "Severity"),
    ],
)
def test_sampling_policy_refuses_malformed_controls(
    field: str, value: object, correct_form: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=rf"{field}.*{correct_form}"):
        LogSamplingPolicy(**cast(Any, {field: value}))


@pytest.mark.parametrize("capacity", [True, 1.5, float("nan"), float("inf"), -1, 1 << 21])
def test_site_limiter_refuses_an_invalid_or_unbounded_capacity(capacity: object) -> None:
    with pytest.raises(ValueError, match="capacity must be an integer between 0 and 1048576"):
        SiteLimiter(capacity=cast(Any, capacity))


@pytest.mark.parametrize("capacity", [True, 1.5, float("nan"), float("inf"), -1, 1 << 23])
def test_off_loop_stage_refuses_an_invalid_or_unbounded_capacity(capacity: object) -> None:
    with pytest.raises(ValueError, match="capacity must be an integer between 0 and 4194304"):
        OffLoopStage(capacity=cast(Any, capacity))


@pytest.mark.parametrize("request_id", [True, -1, 1 << 64])
def test_request_log_buffer_refuses_a_request_id_that_would_alias(request_id: object) -> None:
    with pytest.raises(ValueError, match="request_id must be an unsigned 64-bit integer"):
        RequestLogBuffer(cast(Any, request_id), budget=1)


@pytest.mark.parametrize("budget", [True, 1.5, -1, 65537])
def test_request_log_buffer_refuses_an_invalid_or_unbounded_budget(budget: object) -> None:
    with pytest.raises(ValueError, match="budget must be an integer between 0 and 65536"):
        RequestLogBuffer(1, budget=cast(Any, budget))


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
    limiter = SiteLimiter(LogSamplingPolicy(first=1, thereafter=1000), clock=FakeClock())
    assert all(limiter.allow(site_id=1, severity=severity) for _ in range(50))


def test_drops_are_carried_on_the_next_record_that_passes() -> None:
    limiter = SiteLimiter(LogSamplingPolicy(first=1, thereafter=3), clock=FakeClock())
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # passes, 0 dropped
    assert limiter.take_dropped(1) == 0
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # dropped
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # dropped
    limiter.allow(site_id=1, severity=fs.Severity.INFO)  # passes (every 3rd)
    assert limiter.take_dropped(1) == 2
    assert limiter.take_dropped(1) == 0  # reading clears it


def test_an_uninterned_site_is_never_sampled() -> None:
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
    emitted: list[fs.LogCell] = []
    buffer = RequestLogBuffer(request_id=1, budget=8)
    buffer.add(_cell())
    buffer.promote()
    buffer.finish(promoted=False, emit=emitted.append)
    assert len(emitted) == 1


def test_buffer_finish_requires_an_exact_promotion_verdict() -> None:
    buffer = RequestLogBuffer(request_id=1, budget=1)
    with pytest.raises(ValueError, match="promoted must be a boolean"):
        buffer.finish(promoted=cast(Any, 1), emit=lambda _cell: None)


def test_exhausting_the_buffer_budget_is_counted() -> None:
    buffer = RequestLogBuffer(request_id=1, budget=2)
    for _ in range(5):
        buffer.add(_cell())
    assert buffer.dropped == 3
    emitted: list[fs.LogCell] = []
    buffer.finish(promoted=True, emit=emitted.append)
    assert len(emitted) == 2


def test_a_buffer_keeps_the_oldest_records() -> None:
    buffer = RequestLogBuffer(request_id=1, budget=2)
    for site_id in (1, 2, 3, 4):
        buffer.add(_cell(site_id=site_id))
    emitted: list[fs.LogCell] = []
    buffer.finish(promoted=True, emit=emitted.append)
    assert [c.site_id for c in emitted] == [1, 2]


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


def test_begin_request_honours_an_explicit_scratch_budget() -> None:
    scratch_token = log._SCRATCH.set(None)
    scope_token = log._SCOPE.set(None)
    try:
        with log.testing_runtime(
            level=log.INFO,
            capture_level=log.TRACE,
            scratch_budget=8,
        ):
            scope = log.begin_request(5, budget=1)
            assert scope is not None
            log.debug("first {v}", v=1)
            log.debug("second {v}", v=2)
            assert scope.held == 1
            assert scope.dropped == 1
            scope.finish(promoted=False)
    finally:
        log._SCOPE.reset(scope_token)
        log._SCRATCH.reset(scratch_token)


def test_prepared_debug_record_is_held_for_request_promotion() -> None:
    fields = ((("message", str, log.RAW), "from stdlib"),)
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        with log.request_scope(request_id=5) as scope:
            log._emit_prepared(log.DEBUG, "bridge: {message}", fields)
            assert scope.held == 1
            scope.finish(promoted=True)
    assert len(records) == 1
    assert records[0].request_id == 5


def test_prepared_info_record_is_emitted_without_a_request_buffer() -> None:
    fields = ((("message", str, log.RAW), "from stdlib"),)
    with log.testing_runtime(level=log.INFO) as records:
        log._emit_prepared(log.INFO, "bridge: {message}", fields)
    assert len(records) == 1
    assert records[0].request_id == 0


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
    with log.testing_runtime(level=log.TRACE) as records:
        log.debug("boot {v}", v=1)
    assert len(records) == 1
    assert records[0].request_id == 0


def test_a_verbose_record_outside_a_request_is_dropped() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        log.debug("boot {v}", v=1)
        log.info("booted {v}", v=1)
    assert [c.severity for c in records] == [fs.Severity.INFO]


def test_a_site_below_the_floor_is_falsey() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.INFO):
        quiet = log.event("floor.debug", "x {v}", level=log.DEBUG, fields=(log.field("v", int),))
        assert not quiet


def test_a_site_between_the_thresholds_is_truthy() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.DEBUG):
        buffered = log.event(
            "floor.buffered", "x {v}", level=log.DEBUG, fields=(log.field("v", int),)
        )
        assert buffered


def test_leaving_a_scope_without_finishing_discards_rather_than_leaks() -> None:
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
