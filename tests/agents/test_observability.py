from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import cast

import pytest

from wreath._agents.observability import (
    AgentCapturePolicy,
    AgentObservability,
    AgentObservation,
    AgentOutcome,
    AgentUsage,
    _truncate_utf8,
)
from wreath.recording import BodyCapture, RedactionPolicy


class Observer:
    def __init__(self) -> None:
        self.events: list[AgentObservation] = []

    async def record(self, event: AgentObservation) -> None:
        self.events.append(event)


class FailedObserver:
    async def record(self, event: AgentObservation) -> None:
        del event
        raise LookupError("recorder unavailable")


def context() -> SimpleNamespace:
    return SimpleNamespace(
        tenant="tenant-a",
        principal=SimpleNamespace(id="user-7"),
        conversation="conversation-2",
        correlation_id="trace-9",
    )


def test_agent_usage_rejects_each_negative_counter() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        AgentUsage(input_tokens=-1)
    with pytest.raises(ValueError, match="non-negative"):
        AgentUsage(output_tokens=-1)
    with pytest.raises(ValueError, match="non-negative"):
        AgentUsage(cached_input_tokens=-1)


def test_capture_policy_rejects_each_invalid_bound() -> None:
    with pytest.raises(ValueError, match="at least one field"):
        AgentCapturePolicy(
            RedactionPolicy(
                dependency=BodyCapture.METADATA,
                max_body_bytes=1,
                max_fields=1,
            ),
            fields=frozenset(),
        )
    with pytest.raises(ValueError, match="exceed redaction max_fields"):
        AgentCapturePolicy(
            RedactionPolicy(
                dependency=BodyCapture.METADATA,
                max_body_bytes=1,
                max_fields=1,
            ),
            fields=frozenset({"arguments", "result"}),
        )
    with pytest.raises(ValueError, match="positive max_body_bytes"):
        AgentCapturePolicy(
            RedactionPolicy(
                dependency=BodyCapture.METADATA,
                max_body_bytes=0,
                max_fields=1,
            ),
            fields=frozenset({"arguments"}),
        )


def test_capture_requires_an_observer() -> None:
    capture = AgentCapturePolicy(
        RedactionPolicy(
            dependency=BodyCapture.METADATA,
            max_body_bytes=1,
            max_fields=1,
        ),
        fields=frozenset({"arguments"}),
    )

    with pytest.raises(ValueError, match="requires an observer"):
        AgentObservability(capture=capture)


def test_utf8_truncation_distinguishes_exact_and_short_bounds() -> None:
    assert _truncate_utf8("é", 2) == ("é", False)
    assert _truncate_utf8("é", 1) == ("", True)


def test_payload_short_circuits_for_each_absent_input() -> None:
    assert AgentObservability()._payload({"arguments": "secret"}) == ()

    capture = AgentCapturePolicy(
        RedactionPolicy(
            dependency=BodyCapture.METADATA,
            max_body_bytes=1,
            max_fields=1,
        ),
        fields=frozenset({"arguments"}),
    )
    assert AgentObservability(observer=Observer(), capture=capture)._payload(None) == ()


@pytest.mark.asyncio
async def test_observation_rejects_invalid_duration_and_outcome() -> None:
    telemetry = AgentObservability(observer=Observer())

    with pytest.raises(ValueError, match="duration must be non-negative"):
        await telemetry.tool(
            context(),
            tool="lookup",
            call_id="call-negative",
            duration=-0.01,
            outcome="failed",
        )
    with pytest.raises(ValueError, match="unsupported agent observation outcome"):
        await telemetry.tool(
            context(),
            tool="lookup",
            call_id="call-outcome",
            duration=0,
            outcome=cast("AgentOutcome", "other"),
        )


@pytest.mark.asyncio
async def test_disabled_observer_does_not_inspect_payloads() -> None:
    class Bomb:
        def __str__(self) -> str:
            raise AssertionError("disabled observation inspected content")

        def __len__(self) -> int:
            raise AssertionError("disabled observation measured content")

    telemetry = AgentObservability()

    await telemetry.model(
        context(),
        provider="anthropic",
        model="claude",
        request_id="request-1",
        duration=0.25,
        outcome="succeeded",
        payloads={"prompt": Bomb()},
    )


@pytest.mark.asyncio
async def test_observer_infrastructure_failure_is_counted_without_escaping() -> None:
    telemetry = AgentObservability(observer=FailedObserver())

    await telemetry.tool(
        context(),
        tool="release",
        call_id="call-1",
        duration=0.1,
        outcome="succeeded",
    )

    assert telemetry.recording_errors == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redact",
    [
        lambda _field, _value: (_ for _ in ()).throw(LookupError("redactor unavailable")),
        lambda _field, _value: object(),
    ],
)
async def test_payload_redactor_failure_is_counted_without_escaping(redact: object) -> None:
    observer = Observer()
    capture = AgentCapturePolicy(
        RedactionPolicy(
            dependency=BodyCapture.STRUCTURED,
            max_body_bytes=16,
            max_fields=1,
        ),
        fields=frozenset({"result"}),
        redact=redact,
    )
    telemetry = AgentObservability(observer=observer, capture=capture)

    await telemetry.tool(
        context(),
        tool="release",
        call_id="call-1",
        duration=0.1,
        outcome="succeeded",
        payloads={"result": "completed effect"},
    )

    assert observer.events == []
    assert telemetry.recording_errors == 1


@pytest.mark.asyncio
async def test_default_observation_records_boundaries_and_metrics_without_content() -> None:
    observer = Observer()
    telemetry = AgentObservability(observer=observer)

    await telemetry.model(
        context(),
        provider="openai",
        model="gpt-5",
        request_id="request-1",
        duration=0.25,
        outcome="succeeded",
        usage=AgentUsage(input_tokens=12, output_tokens=3, cached_input_tokens=4),
        fallback=True,
        payloads={"prompt": "secret prompt"},
    )
    await telemetry.tool(
        context(),
        tool="release",
        call_id="call-1",
        duration=0.1,
        outcome="denied",
        payloads={"arguments": "secret args", "result": "secret result"},
    )

    model, tool = observer.events
    assert model.kind == "model"
    assert (model.provider, model.model, model.request_id) == (
        "openai",
        "gpt-5",
        "request-1",
    )
    assert model.duration == 0.25
    assert model.outcome == "succeeded"
    assert model.usage is not None
    assert model.usage.total_tokens == 15
    assert model.fallback is True
    assert tool.kind == "tool"
    assert (tool.tool, tool.call_id, tool.outcome) == ("release", "call-1", "denied")
    rendered = repr((asdict(model), asdict(tool)))
    assert "secret prompt" not in rendered
    assert "secret args" not in rendered
    assert "secret result" not in rendered
    assert model.payload == ()
    assert tool.payload == ()


@pytest.mark.asyncio
async def test_payload_capture_requires_bounded_explicit_redaction() -> None:
    with pytest.raises(ValueError, match="dependency capture"):
        AgentCapturePolicy(RedactionPolicy.deny_by_default(), fields=frozenset({"prompt"}))
    with pytest.raises(ValueError, match="redactor"):
        AgentCapturePolicy(
            RedactionPolicy(
                dependency=BodyCapture.STRUCTURED,
                max_body_bytes=8,
                max_fields=1,
            ),
            fields=frozenset({"prompt"}),
        )

    observer = Observer()
    capture = AgentCapturePolicy(
        RedactionPolicy(
            dependency=BodyCapture.STRUCTURED,
            max_body_bytes=8,
            max_fields=1,
        ),
        fields=frozenset({"prompt"}),
        redact=lambda _field, _value: "safe-value",
    )
    telemetry = AgentObservability(observer=observer, capture=capture)
    await telemetry.model(
        context(),
        provider="local",
        model="model",
        request_id=None,
        duration=0.01,
        outcome="failed",
        payloads={"prompt": "secret", "arguments": "never selected"},
    )

    event = observer.events[0]
    assert len(event.payload) == 1
    assert event.payload[0].field == "prompt"
    assert event.payload[0].value == "safe-val"
    assert event.payload[0].truncated is True


@pytest.mark.asyncio
async def test_metadata_capture_keeps_only_lengths() -> None:
    observer = Observer()
    capture = AgentCapturePolicy(
        RedactionPolicy(
            dependency=BodyCapture.METADATA,
            max_body_bytes=16,
            max_fields=2,
        ),
        fields=frozenset({"arguments", "result"}),
    )
    telemetry = AgentObservability(observer=observer, capture=capture)

    await telemetry.tool(
        context(),
        tool="lookup",
        call_id="call-2",
        duration=0.02,
        outcome="succeeded",
        payloads={"arguments": "top-secret", "result": "classified"},
    )

    payload = observer.events[0].payload
    assert [(item.field, item.length, item.value) for item in payload] == [
        ("arguments", 10, None),
        ("result", 10, None),
    ]


@pytest.mark.asyncio
async def test_hashed_capture_records_the_digest_without_content() -> None:
    observer = Observer()
    capture = AgentCapturePolicy(
        RedactionPolicy(
            dependency=BodyCapture.HASHED,
            max_body_bytes=64,
            max_fields=1,
        ),
        fields=frozenset({"arguments"}),
    )
    telemetry = AgentObservability(observer=observer, capture=capture)

    await telemetry.tool(
        context(),
        tool="lookup",
        call_id="call-hashed",
        duration=0,
        outcome="succeeded",
        payloads={"arguments": "secret"},
    )

    assert observer.events[0].payload[0].value == (
        "2bb80d537b1da3e38bd30361aa855686bde0eacd"
        "7162fef6a25fe97bf527a25b"
    )
