from types import SimpleNamespace

import pytest

from wreath import telemetry
from wreath.telemetry import (
    HistogramConfig,
    Mode,
    OTLPConfig,
    PerRoutePolicy,
    TelemetryConfig,
    TelemetryConfigError,
)


def test_histogram_policy_string_is_normalized_before_use() -> None:
    config = HistogramConfig(per_route="selected", max_route_histograms=3)

    assert config.per_route is PerRoutePolicy.SELECTED
    assert config.histogram_count(5) == 4


def test_an_empty_unmapped_ring_is_valid_when_telemetry_is_off() -> None:
    config = TelemetryConfig(mode=Mode.OFF, ring_records=0, active_requests=0)

    assert config.ring_path is None


def test_a_mapped_ring_requires_records_and_accepts_them() -> None:
    with pytest.raises(TelemetryConfigError, match="needs a non-empty ring"):
        TelemetryConfig(mode=Mode.OFF, ring_records=0, ring_path="flight.ring")

    config = TelemetryConfig(mode=Mode.OFF, ring_records=8, ring_path="flight.ring")
    assert config.ring_records == 8


def test_off_mode_returns_before_live_recorder_requirements() -> None:
    config = TelemetryConfig(
        mode=Mode.OFF,
        ring_records=0,
        active_requests=0,
        completion_summaries=True,
    )

    assert config.mode is Mode.OFF


def test_pulse_without_summaries_can_use_an_empty_ring() -> None:
    config = TelemetryConfig(
        mode=Mode.PULSE,
        ring_records=0,
        active_requests=1,
        completion_summaries=False,
    )

    assert config.ring_records == 0


def test_memory_budget_refuses_a_negative_route_count() -> None:
    with pytest.raises(TelemetryConfigError, match="route_count must be >= 0"):
        TelemetryConfig().memory_budget(route_count=-1)


def test_memory_budget_components_follow_their_enabling_modes() -> None:
    disabled = TelemetryConfig(
        mode=Mode.OFF,
        capture_slabs=2,
        slab_bytes=4096,
        otlp=OTLPConfig(enabled=False),
    ).memory_budget()
    exporting = TelemetryConfig(
        mode=Mode.PULSE,
        otlp=OTLPConfig(enabled=True, export_queue=32, batch_size=8),
    ).memory_budget()

    assert disabled.phase_scratch == 0
    assert disabled.capture == 0
    assert disabled.export_queue == 0
    assert exporting.export_queue == 32 * telemetry.CELL_SIZE


def test_memory_budget_refuses_an_implausible_computed_total(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_ACTIVE_SLOT_BYTES", 1 << 41)

    with pytest.raises(TelemetryConfigError, match="exceeds 1 TiB"):
        TelemetryConfig(active_requests=1).memory_budget()


def test_bind_propagation_ignores_a_noncallable_header_attribute(monkeypatch) -> None:
    request = SimpleNamespace(
        header="not callable",
        _context=SimpleNamespace(_flight_server_span=lambda: (1, 2, 3)),
    )
    monkeypatch.setattr(telemetry, "PROPAGATING", True)

    token = telemetry.bind_propagation(request)
    assert token is not None
    try:
        assert telemetry.outbound_context.get() == (
            "00-00000000000000010000000000000002-0000000000000003-01",
            "",
        )
    finally:
        telemetry.outbound_context.reset(token)
