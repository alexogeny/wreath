from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import get_ident

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._logscratch import LogSamplingPolicy


class NativeSpy:
    def __init__(self, outcome: int = 0) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.outcome = outcome

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.outcome


class ForbiddenLookup:
    def get(self, _default: object) -> object:
        raise AssertionError("inactive logging crossed the context boundary")


@pytest.fixture(autouse=True)
def reset_logging_context() -> Iterator[None]:
    scratch_token = log._SCRATCH.set(None)
    scope_token = log._SCOPE.set(None)
    try:
        yield
    finally:
        log._SCOPE.reset(scope_token)
        log._SCRATCH.reset(scratch_token)


@contextmanager
def installed_runtime(runtime: log.LogRuntime) -> Iterator[None]:
    previous = log.install(runtime)
    try:
        yield
    finally:
        log.install(previous)


def make_site(runtime: log.LogRuntime, *, string: bool = False) -> log.LogSite:
    type_ = str if string else int
    return runtime.registry.intern_template("value {value}", log.INFO, (log.field("value", type_),))


def test_native_publish_accepts_the_writer_and_an_unbound_runtime() -> None:
    bound_native = NativeSpy()
    bound = log.LogRuntime(lambda _cell: None, native=bound_native)
    bound.bind_writer(get_ident())
    assert bound.publish(make_site(bound), 1, log.INFO, (1,))
    assert len(bound_native.calls) == 1

    unbound_native = NativeSpy()
    unbound = log.LogRuntime(lambda _cell: None, native=unbound_native)
    assert unbound.publish(make_site(unbound), 2, log.INFO, (2,))
    assert len(unbound_native.calls) == 1


def test_native_publish_takes_drops_only_for_limited_records() -> None:
    native = NativeSpy()
    runtime = log.LogRuntime(
        lambda _cell: None,
        native=native,
        sampling=LogSamplingPolicy(first=0, thereafter=100),
    )
    site = make_site(runtime)
    assert not runtime.limiter.allow(site.site_id, log.INFO)
    assert runtime.publish(site, 1, log.INFO, (1,), limited=False)
    assert runtime.publish(site, 1, log.INFO, (1,), limited=True)
    assert [call[4] for call in native.calls] == [0, 1]


def test_native_publish_counts_exactly_the_reported_mismatches() -> None:
    native = NativeSpy(outcome=4)
    runtime = log.LogRuntime(lambda _cell: None, native=native)
    site = make_site(runtime)
    assert runtime.publish(site, 1, log.INFO, (1,))
    native.outcome = 0
    assert runtime.publish(site, 1, log.INFO, (1,))
    assert runtime.counters.type_mismatch == 2


def test_replacing_a_field_at_capacity_does_not_count_as_an_overflow() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(1, field_budget=1) as scope:
            scope.set("value", 1)
            scope.set("value", 2)
            assert scope.fields_dropped == 0
            scope.finish(promoted=False)
        assert log.attributes(records[0]) == {"value": 2}


def test_field_fallback_marks_only_redacted_values() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(1) as scope:
            scope.set("number", 4)
            scope.set("secret", "text")
            scope.finish(promoted=False)
        assert len(records) == 1
        assert records[0].flags == fs.LOG_FLAG_EVENT_FIELDS | fs.LOG_FLAG_REDACTED

    with log.testing_runtime() as records:
        with log.request_scope(1) as scope:
            scope.set("number", 4)
            scope.finish(promoted=False)
        assert records[0].flags == fs.LOG_FLAG_EVENT_FIELDS


def test_field_fallback_runs_only_when_native_publish_refuses() -> None:
    native = NativeSpy()
    emitted: list[fs.LogCell] = []
    runtime = log.LogRuntime(emitted.append, native=native)
    with installed_runtime(runtime):
        with log.request_scope(9) as scope:
            scope.set("value", 1)
            scope.finish(promoted=False)
    assert len(native.calls) == 1
    assert emitted == []


def test_current_scope_respects_runtime_activity_and_binding() -> None:
    buffer_token = log._SCRATCH.set(log.RequestLogBuffer(8, 1))
    bound = log.RequestScope(log._SCRATCH.get(), 1)
    scope_token = log._SCOPE.set(bound)
    try:
        with installed_runtime(log.LogRuntime()):
            assert log.current_scope() is log.INERT_SCOPE
        with installed_runtime(log.LogRuntime(lambda _cell: None)):
            assert log.current_scope() is bound
            log._SCOPE.set(None)
            assert log.current_scope() is log.INERT_SCOPE
    finally:
        log._SCOPE.reset(scope_token)
        log._SCRATCH.reset(buffer_token)


def test_request_scope_uses_explicit_and_runtime_scratch_budgets() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE, scratch_budget=2):
        with log.request_scope(1) as default_scope:
            log.debug("one")
            log.debug("two")
            assert default_scope.held == 2
        with log.request_scope(2, budget=1) as explicit_scope:
            log.debug("one")
            log.debug("two")
            assert explicit_scope.held == 1
            assert explicit_scope.dropped == 1


def test_seeded_request_requires_an_active_runtime_and_an_exact_int() -> None:
    with installed_runtime(log.LogRuntime()):
        assert log.begin_request_seeded({}) is None
    with log.testing_runtime():
        assert log.begin_request_seeded({"_wreath_flight": (1, 2)}) is None
        scope = log.begin_request_seeded({"_wreath_flight": 17})
        assert scope is not None
        assert scope.request_id == 17
        scope.finish(promoted=False)


def test_begin_request_requires_a_sink_and_honours_both_field_budgets() -> None:
    with installed_runtime(log.LogRuntime()):
        assert log.begin_request(1) is None
    with log.testing_runtime():
        default_scope = log.begin_request(2)
        assert default_scope is not None
        for index in range(log.DEFAULT_FIELD_BUDGET + 1):
            default_scope.set(str(index), index)
        assert default_scope.fields == log.DEFAULT_FIELD_BUDGET
        default_scope.finish(promoted=False)

        explicit_scope = log.begin_request(3, field_budget=1)
        assert explicit_scope is not None
        explicit_scope.set("one", 1)
        explicit_scope.set("two", 2)
        assert explicit_scope.fields == 1
        assert explicit_scope.fields_dropped == 1
        explicit_scope.finish(promoted=False)


def test_finish_helpers_require_activity_and_a_bound_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    scope_token = log._SCOPE.set(None)
    try:
        with installed_runtime(log.LogRuntime()):
            monkeypatch.setattr(log, "_SCOPE", ForbiddenLookup())
            assert log.finish_session(promoted=True) == 0
            assert log.finish_request_for(type("Response", (), {"status": 500})()) == 0
            monkeypatch.undo()
        with log.testing_runtime():
            assert log.finish_session(promoted=True) == 0
            assert log.finish_request_for(type("Response", (), {"status": 500})()) == 0
    finally:
        log._SCOPE.reset(scope_token)


def test_finish_session_publishes_a_bound_buffer_only_when_promoted() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        scope = log.begin_request(5)
        assert scope is not None
        log.debug("held")
        assert log.finish_session(promoted=True) == 1
    assert len(records) == 1


def test_registered_event_below_capture_level_does_nothing() -> None:
    native = NativeSpy()
    runtime = log.LogRuntime(
        lambda _cell: None, level=log.INFO, capture_level=log.INFO, native=native
    )
    with installed_runtime(runtime):
        quiet = log.event(
            "mutation.quiet", "value {value}", level=log.DEBUG, fields=(log.field("value", int),)
        )
        quiet(1)
    assert native.calls == []


def test_buffered_registered_event_never_calls_the_native_publisher() -> None:
    native = NativeSpy()
    runtime = log.LogRuntime(
        lambda _cell: None, level=log.INFO, capture_level=log.TRACE, native=native
    )
    with installed_runtime(runtime):
        event = log.event(
            "mutation.buffered", "value {value}", level=log.DEBUG, fields=(log.field("value", int),)
        )
        with log.request_scope(7) as scope:
            event(1)
            assert scope.held == 1
            scope.finish(promoted=False)
    assert native.calls == []


def test_buffered_event_without_a_request_is_dropped_before_buffer_access() -> None:
    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        event = log.event(
            "mutation.unbound-buffer",
            "value {value}",
            level=log.DEBUG,
            fields=(log.field("value", int),),
        )
        event(1)

    assert records == []


def test_kwargs_emitter_below_capture_level_avoids_the_context_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = log.LogRuntime(lambda _cell: None, level=log.INFO, capture_level=log.INFO)
    with installed_runtime(runtime):
        monkeypatch.setattr(log, "_SCRATCH", ForbiddenLookup())
        log._emit_values(log.DEBUG, "quiet {value}", (log.field("value", int),), (1,))
        monkeypatch.undo()
    assert runtime.registry.get(1) is None


def test_buffered_kwargs_bypass_limiter_and_native_publish() -> None:
    native = NativeSpy()
    runtime = log.LogRuntime(
        lambda _cell: None,
        level=log.INFO,
        capture_level=log.TRACE,
        native=native,
        sampling=LogSamplingPolicy(first=0, thereafter=100),
    )
    with installed_runtime(runtime):
        with log.request_scope(13) as scope:
            log._emit_values(log.DEBUG, "held {value}", (log.field("value", int),), (1,))
            assert scope.held == 1
            scope.finish(promoted=False)
        site = runtime.registry.get(1)
        assert site is not None
        assert runtime.limiter.take_dropped(site.site_id) == 0
    assert native.calls == []


def test_native_kwargs_publish_carries_the_bound_request_id() -> None:
    native = NativeSpy()
    runtime = log.LogRuntime(lambda _cell: None, native=native)
    with installed_runtime(runtime):
        with log.request_scope(29) as scope:
            log._emit_values(log.INFO, "value {value}", (log.field("value", int),), (1,))
            scope.finish(promoted=False)
    assert native.calls[0][2] == 29


def test_kwargs_fallback_counts_mismatches_and_marks_redaction() -> None:
    with log.testing_runtime() as records:
        log._emit_values(
            log.INFO,
            "number {number} secret {secret}",
            (log.field("number", int), log.field("secret", str)),
            ("wrong", "text"),
        )
        assert log.type_mismatch_count() == 1
        assert len(records) == 1
        assert records[0].flags & fs.LOG_FLAG_REDACTED

    with log.testing_runtime() as records:
        log._emit_values(log.INFO, "number {number}", (log.field("number", int),), (1,))
        assert log.type_mismatch_count() == 0
        assert records[0].flags == 0


def test_buffered_kwargs_do_not_take_a_sites_pending_drops() -> None:
    runtime = log.LogRuntime(
        lambda _cell: None,
        level=log.INFO,
        capture_level=log.TRACE,
        sampling=LogSamplingPolicy(first=0, thereafter=100),
    )
    with installed_runtime(runtime):
        site = runtime.registry.intern_template(
            "value {value}", log.DEBUG, (log.field("value", int),)
        )
        assert not runtime.limiter.allow(site.site_id, log.INFO)
        with log.request_scope(31) as scope:
            log._emit_values(log.DEBUG, "value {value}", (log.field("value", int),), (1,))
            scope.finish(promoted=True)
        assert runtime.limiter.take_dropped(site.site_id) == 1


def test_off_loop_counts_are_zero_without_a_stage() -> None:
    with installed_runtime(log.LogRuntime(lambda _cell: None)):
        assert log.off_loop_counts() == {"staged": 0, "dropped": 0, "held": 0}
