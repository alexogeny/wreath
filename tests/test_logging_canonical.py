from __future__ import annotations

import json

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._logsink import canonical_json, canonical_text
from wreath._projector import ProjectedTrace


def _trace(
    *,
    status: int = 200,
    terminal: fs.TerminalStatus = fs.TerminalStatus.OK,
    error_class: int = 0,
    trace_id: int = 0,
    span_id: int = 0,
    logs: tuple[fs.LogCell, ...] = (),
) -> ProjectedTrace:
    return ProjectedTrace(
        request_id=7,
        connection_id=1,
        route_id=12,
        plan_id=3,
        worker_id=0,
        duration_us=12_400,
        status=status,
        terminal=terminal,
        protocol=fs.Protocol.HTTP2,
        error_class=error_class,
        flags=0,
        bytes_in=10,
        bytes_out=20,
        trace_id=trace_id,
        span_id=span_id,
        logs=logs,
    )


def _merged(records: list[fs.LogCell]) -> dict[str, object]:
    """Fold every event-field record into one attribute mapping.

    Must be called while the runtime that interned the sites is still
    installed -- `log.attributes` resolves names through it.
    """
    attrs: dict[str, object] = {}
    for cell in records:
        if cell.flags & fs.LOG_FLAG_EVENT_FIELDS:
            attrs.update(log.attributes(cell))
    return attrs


def test_scalar_fields_reach_the_record() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            scope.set("tenant_id", 42)
            scope.set("retries", 2)
            scope.finish(promoted=False)
        assert [c for c in records if c.flags & fs.LOG_FLAG_EVENT_FIELDS]
        assert _merged(records) == {"tenant_id": 42, "retries": 2}


def test_string_fields_are_fingerprinted_unless_declared_raw() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            scope.set("token", "hunter2")
            scope.set("route_name", "orders", raw=True)
            scope.finish(promoted=False)
    blob = b"".join(c.encode() for c in records)
    assert b"hunter2" not in blob
    assert b"orders" in blob


def test_fields_are_published_even_when_the_request_succeeds() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            scope.set("tenant_id", 1)
            scope.finish(promoted=False)
    assert any(c.flags & fs.LOG_FLAG_EVENT_FIELDS for c in records)


def test_setting_the_same_key_twice_keeps_the_last_value() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            scope.set("attempt", 1)
            scope.set("attempt", 2)
            scope.finish(promoted=False)
        assert _merged(records) == {"attempt": 2}


def test_the_field_budget_is_bounded_and_overflow_is_counted() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7, field_budget=2) as scope:
            scope.set("a", 1)
            scope.set("b", 2)
            scope.set("c", 3)
            assert scope.fields_dropped == 1
            scope.finish(promoted=False)
        assert set(_merged(records)) == {"a", "b"}


def test_more_fields_than_a_cell_holds_span_several_cells() -> None:
    count = fs.LOG_MAX_ARGS + 3
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            for i in range(count):
                scope.set(f"f{i}", i)
            scope.finish(promoted=False)
        assert len([c for c in records if c.flags & fs.LOG_FLAG_EVENT_FIELDS]) >= 2
        assert len(_merged(records)) == count


def test_field_cells_carry_the_request_id() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=4242) as scope:
            scope.set("a", 1)
            scope.finish(promoted=False)
    assert all(c.request_id == 4242 for c in records)


def test_a_scope_with_no_fields_publishes_nothing_extra() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            scope.finish(promoted=False)
    assert records == []


def test_setting_a_field_outside_a_request_is_a_no_op() -> None:
    with log.testing_runtime() as records:
        log.set_field("tenant", 1)
    assert records == []


def test_set_field_reaches_the_current_scope() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            log.set_field("gateway", 3)
            scope.finish(promoted=False)
        assert _merged(records) == {"gateway": 3}


def test_canonical_json_carries_the_whole_request() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            scope.set("tenant_id", 42)
            scope.finish(promoted=False)
        trace = _trace(logs=tuple(records), trace_id=(1 << 64) | 2, span_id=3)
        payload = json.loads(canonical_json(log.installed().registry, trace))
        assert payload["request_id"] == 7
        assert payload["route_id"] == 12
        assert payload["status"] == 200
        assert payload["duration_us"] == 12_400
        assert payload["protocol"] == "HTTP2"
        assert payload["terminal"] == "OK"
        assert payload["trace_id"] == f"{(1 << 64) | 2:032x}"
        assert payload["span_id"] == f"{3:016x}"
        assert payload["attributes"] == {"tenant_id": 42}


def test_canonical_json_is_one_line() -> None:
    trace = _trace()
    assert "\n" not in canonical_json(log.installed().registry, trace)


def test_canonical_json_marks_a_failure() -> None:
    trace = _trace(status=500, terminal=fs.TerminalStatus.ERROR, error_class=3)
    payload = json.loads(canonical_json(log.installed().registry, trace))
    assert payload["failure"] is True
    assert payload["error_class"] == 3


def test_canonical_text_is_readable() -> None:
    with log.testing_runtime() as records:
        with log.request_scope(request_id=7) as scope:
            scope.set("tenant_id", 42)
            scope.finish(promoted=False)
        line = canonical_text(log.installed().registry, _trace(logs=tuple(records)))
        assert "route=12" in line
        assert "status=200" in line
        assert "12400us" in line
        assert "tenant_id=42" in line


def test_the_canonical_line_omits_correlation_when_there_is_none() -> None:
    payload = json.loads(canonical_json(log.installed().registry, _trace()))
    assert "trace_id" not in payload


def test_promoted_records_are_not_folded_into_the_attributes() -> None:
    with log.testing_runtime(level=log.TRACE) as records:
        with log.request_scope(request_id=7) as scope:
            log.debug("led up to it {v}", v=1)
            scope.set("tenant_id", 42)
            scope.finish(promoted=True)
        payload = json.loads(canonical_json(log.installed().registry, _trace(logs=tuple(records))))
        assert payload["attributes"] == {"tenant_id": 42}
        assert payload["records"] == 1


def test_request_event_is_always_safe_to_call() -> None:
    from wreath.logging import current_scope

    scope = current_scope()
    scope.set("tenant", 1)
    scope.promote()
    assert scope.request_id == 0
    assert scope.fields == 0
    assert scope.finish(promoted=True) == 0


def test_request_event_reaches_the_bound_scope() -> None:
    from wreath.logging import current_scope

    with log.testing_runtime() as records:
        with log.request_scope(request_id=3) as scope:
            current_scope().set("tenant_id", 9)
            assert current_scope().request_id == 3
            scope.finish(promoted=False)
        assert _merged(records) == {"tenant_id": 9}


def test_request_event_promote_publishes_a_healthy_request() -> None:
    from wreath.logging import current_scope

    with log.testing_runtime(level=log.INFO, capture_level=log.TRACE) as records:
        with log.request_scope(request_id=3) as scope:
            log.debug("step {v}", v=1)
            current_scope().promote()
            scope.finish(promoted=False)
    assert [c.severity for c in records] == [fs.Severity.DEBUG]
