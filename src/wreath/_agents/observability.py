from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ..recording import BodyCapture, RedactionPolicy
from .identity import principal_id

AgentOutcome = Literal["succeeded", "failed", "denied", "cancelled", "unknown"]


@dataclass(frozen=True, slots=True)
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.cached_input_tokens) < 0:
            raise ValueError("agent usage counts must be non-negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class CapturedAgentPayload:
    field: str
    length: int
    value: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class AgentCapturePolicy:
    redaction: RedactionPolicy
    fields: frozenset[str]
    redact: Callable[[str, Any], str] | None = None

    def __post_init__(self) -> None:
        mode = self.redaction.dependency
        if mode is BodyCapture.NONE:
            raise ValueError("agent payload capture requires dependency capture opt-in")
        if not self.fields:
            raise ValueError("agent payload capture requires at least one field")
        if self.redaction.max_fields < len(self.fields):
            raise ValueError("agent payload fields exceed redaction max_fields")
        if self.redaction.max_body_bytes < 1:
            raise ValueError("agent payload capture requires a positive max_body_bytes")
        if mode is BodyCapture.STRUCTURED and self.redact is None:
            raise ValueError("structured agent payload capture requires a redactor")


@dataclass(frozen=True, slots=True)
class AgentObservation:
    kind: Literal["model", "tool"]
    tenant: str
    principal_id: str
    conversation: str
    correlation_id: str | None
    duration: float
    outcome: AgentOutcome
    provider: str | None = None
    model: str | None = None
    request_id: str | None = None
    tool: str | None = None
    call_id: str | None = None
    usage: AgentUsage | None = None
    fallback: bool = False
    payload: tuple[CapturedAgentPayload, ...] = ()


@runtime_checkable
class AgentObserver(Protocol):
    async def record(self, event: AgentObservation) -> None: ...


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", "ignore"), True


class AgentObservability:
    __slots__ = ("_capture", "_record", "recording_errors")

    def __init__(
        self,
        *,
        observer: AgentObserver | None = None,
        capture: AgentCapturePolicy | None = None,
    ) -> None:
        if capture is not None and observer is None:
            raise ValueError("agent payload capture requires an observer")
        self._record = None if observer is None else observer.record
        self._capture = capture
        self.recording_errors = 0

    async def _emit(self, event: AgentObservation) -> None:
        record = self._record
        if record is None:
            return
        try:
            await record(event)
        except Exception:
            # Observability runs after model/tool effects. Propagating its failure
            # would make an at-least-once caller replay a completed effect; the
            # counter keeps the degradation visible without changing execution.
            self.recording_errors += 1

    def _payload(self, payloads: Mapping[str, Any] | None) -> tuple[CapturedAgentPayload, ...]:
        policy = self._capture
        if policy is None or not payloads:
            return ()
        mode = policy.redaction.dependency
        remaining = policy.redaction.max_body_bytes
        captured: list[CapturedAgentPayload] = []
        for field in sorted(policy.fields):
            if field not in payloads:
                continue
            value = _text(payloads[field])
            length = len(value.encode())
            if mode is BodyCapture.METADATA:
                captured.append(CapturedAgentPayload(field, length))
                continue
            if mode is BodyCapture.HASHED:
                rendered = hashlib.sha256(value.encode()).hexdigest()
            else:
                redactor = policy.redact
                if redactor is None:
                    raise RuntimeError("structured capture has no redactor")
                rendered = redactor(field, value)
                if not isinstance(rendered, str):
                    raise TypeError("agent payload redactor must return a string")
            bounded, truncated = _truncate_utf8(rendered, remaining)
            used = len(bounded.encode())
            remaining -= used
            captured.append(CapturedAgentPayload(field, length, bounded, truncated))
        return tuple(captured)

    def _captured_payload(
        self, payloads: Mapping[str, Any] | None
    ) -> tuple[CapturedAgentPayload, ...] | None:
        try:
            return self._payload(payloads)
        except Exception:
            # Capture runs after the observed effect; replay would duplicate it.
            self.recording_errors += 1
            return None

    def _base(
        self,
        context: Any,
        *,
        kind: Literal["model", "tool"],
        duration: float,
        outcome: AgentOutcome,
        payload: tuple[CapturedAgentPayload, ...],
        **values: Any,
    ) -> AgentObservation:
        if duration < 0:
            raise ValueError("agent observation duration must be non-negative")
        if outcome not in {"succeeded", "failed", "denied", "cancelled", "unknown"}:
            raise ValueError(f"unsupported agent observation outcome {outcome!r}")
        return AgentObservation(
            kind=kind,
            tenant=context.tenant,
            principal_id=principal_id(context.principal, label="observed agent"),
            conversation=context.conversation,
            correlation_id=context.correlation_id,
            duration=duration,
            outcome=outcome,
            payload=payload,
            **values,
        )

    async def model(
        self,
        context: Any,
        *,
        provider: str,
        model: str,
        request_id: str | None,
        duration: float,
        outcome: AgentOutcome,
        usage: AgentUsage | None = None,
        fallback: bool = False,
        payloads: Mapping[str, Any] | None = None,
    ) -> None:
        record = self._record
        if record is None:
            return
        payload = self._captured_payload(payloads)
        if payload is None:
            return
        event = self._base(
            context,
            kind="model",
            provider=provider,
            model=model,
            request_id=request_id,
            duration=duration,
            outcome=outcome,
            usage=usage,
            fallback=fallback,
            payload=payload,
        )
        await self._emit(event)

    async def tool(
        self,
        context: Any,
        *,
        tool: str,
        call_id: str,
        duration: float,
        outcome: AgentOutcome,
        payloads: Mapping[str, Any] | None = None,
    ) -> None:
        record = self._record
        if record is None:
            return
        payload = self._captured_payload(payloads)
        if payload is None:
            return
        event = self._base(
            context,
            kind="tool",
            tool=tool,
            call_id=call_id,
            duration=duration,
            outcome=outcome,
            payload=payload,
        )
        await self._emit(event)


__all__ = [
    "AgentCapturePolicy",
    "AgentObservation",
    "AgentObservability",
    "AgentObserver",
    "AgentOutcome",
    "AgentUsage",
    "CapturedAgentPayload",
]
