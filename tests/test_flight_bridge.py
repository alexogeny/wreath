"""Stage 4c -- the lazy OpenTelemetry bridge in ``wreath.telemetry``.

The bridge reads only the incoming ``traceparent`` and constructs no OTel object
unless ``activate_otel`` is called AND the OTel API is installed. These check the
native view, the traceparent round-trip, and the no-op fallback when OTel is
absent.
"""

from __future__ import annotations

import pytest

from wreath.telemetry import SpanContextView, activate_otel, current_span, server_span

_VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class _FakeContext:
    """A stand-in for the native request context exposing the owned server span."""

    def __init__(self, trace_hi: int, trace_lo: int, span: int) -> None:
        self._span = (trace_hi, trace_lo, span)

    def _flight_server_span(self) -> tuple[int, int, int]:
        return self._span


class FakeRequest:
    def __init__(
        self, headers: dict[str, str] | None = None, context: object | None = None
    ) -> None:
        self._headers = headers or {}
        self._context = context

    def header(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)


def test_server_span_prefers_the_owned_generated_span() -> None:
    # trace 4bf9...4736 carried on the wire; the owned server span is a different
    # (generated) span within the same trace.
    incoming = current_span(FakeRequest({"traceparent": _VALID}))
    context = _FakeContext(incoming.trace_id >> 64, incoming.trace_id & ((1 << 64) - 1),
                           0x876BED62CCBA134B)
    owned = server_span(FakeRequest({"traceparent": _VALID}, context=context))
    assert owned.trace_id == incoming.trace_id      # same trace
    assert owned.span_id == 0x876BED62CCBA134B      # the owned server span
    assert owned.span_id != incoming.span_id        # not the incoming parent


def test_server_span_falls_back_to_incoming_without_a_context() -> None:
    request = FakeRequest({"traceparent": _VALID})
    assert server_span(request) == current_span(request)


def test_server_span_falls_back_when_the_context_has_no_owned_span() -> None:
    # A recorder-less request: the accessor returns all zeros -> fall back.
    context = _FakeContext(0, 0, 0)
    request = FakeRequest({"traceparent": _VALID}, context=context)
    assert server_span(request) == current_span(request)


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
