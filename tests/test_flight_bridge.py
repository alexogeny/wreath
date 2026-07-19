"""Stage 4c -- the lazy OpenTelemetry bridge in ``wreath.telemetry``.

The bridge reads only the incoming ``traceparent`` and constructs no OTel object
unless ``activate_otel`` is called AND the OTel API is installed. These check the
native view, the traceparent round-trip, and the no-op fallback when OTel is
absent.
"""

from __future__ import annotations

import pytest

from wreath.telemetry import SpanContextView, activate_otel, current_span

_VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self._headers = headers or {}

    def header(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)


def test_current_span_parses_incoming_traceparent() -> None:
    view = current_span(FakeRequest({"traceparent": _VALID}))
    assert view.is_valid
    assert view.trace_id == 0x4BF92F3577B34DA6A3CE929D0E0E4736
    assert view.span_id == 0x00F067AA0BA902B7
    assert view.sampled is True
    assert view.trace_id_hex == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert view.span_id_hex == "00f067aa0ba902b7"


def test_traceparent_round_trips() -> None:
    view = current_span(FakeRequest({"traceparent": _VALID}))
    assert view.traceparent() == _VALID


def test_unpropagated_request_is_an_empty_view() -> None:
    view = current_span(FakeRequest())
    assert not view.is_valid
    assert view.trace_id == 0 and view.span_id == 0
    assert view.traceparent() is None


def test_malformed_traceparent_is_an_empty_view() -> None:
    view = current_span(FakeRequest({"traceparent": "garbage"}))
    assert not view.is_valid


def test_current_span_tolerates_a_request_without_header_method() -> None:
    assert current_span(object()) == SpanContextView()


def test_activate_otel_without_sdk_returns_native_view() -> None:
    import importlib.util

    if importlib.util.find_spec("opentelemetry") is not None:
        pytest.skip("opentelemetry is installed; this checks the absent-SDK path")
    result = activate_otel(FakeRequest({"traceparent": _VALID}))
    assert isinstance(result, SpanContextView)
    assert result.is_valid


def test_activate_otel_unpropagated_returns_empty_view() -> None:
    result = activate_otel(FakeRequest())
    assert isinstance(result, SpanContextView)
    assert not result.is_valid
