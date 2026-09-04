from __future__ import annotations

import hmac
import math
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ..recording import BodyCapture, RedactionPolicy
from .identity import principal_id

AgentOutcome = Literal["succeeded", "failed", "denied", "cancelled", "unknown"]
_UTF8_CHUNK_CHARACTERS = 4096


@dataclass(frozen=True, slots=True)
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int
            for value in (self.input_tokens, self.output_tokens, self.cached_input_tokens)
        ):
            raise TypeError("agent usage counts must be integers")
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
        fields = frozenset(self.fields)
        if any(not isinstance(field, str) or not field for field in fields):
            raise ValueError("agent payload fields must be non-empty strings")
        object.__setattr__(self, "fields", fields)
        mode = self.redaction.dependency
        if mode is BodyCapture.NONE:
            raise ValueError("agent payload capture requires dependency capture opt-in")
        if not self.fields:
            raise ValueError("agent payload capture requires at least one field")
        if type(self.redaction.max_fields) is not int:
            raise TypeError("agent payload max_fields must be an integer")
        if type(self.redaction.max_body_bytes) is not int:
            raise TypeError("agent payload max_body_bytes must be an integer")
        if self.redaction.max_fields < len(fields):
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


def _utf8_chunks(value: str) -> Iterator[bytes]:
    if len(value) <= _UTF8_CHUNK_CHARACTERS:
        yield value.encode()
        return
    for offset in range(0, len(value), _UTF8_CHUNK_CHARACTERS):
        yield value[offset : offset + _UTF8_CHUNK_CHARACTERS].encode()


def _utf8_length(value: str) -> int:
    return sum(len(chunk) for chunk in _utf8_chunks(value))


def _keyed_fingerprint(value: str, key: bytes) -> tuple[int, str]:
    digest = hmac.new(key, digestmod="sha256")
    length = 0
    for chunk in _utf8_chunks(value):
        length += len(chunk)
        digest.update(chunk)
    return length, digest.hexdigest()


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    characters = len(value)
    candidate = value[:limit]
    encoded = candidate.encode()
    if len(encoded) <= limit:
        return candidate, characters > limit
    return encoded[:limit].decode("utf-8", "ignore"), True


class AgentObservability:
    __slots__ = ("_capture", "_hash_key", "_record", "recording_errors")

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
        self._hash_key = (
            secrets.token_bytes(32)
            if capture is not None and capture.redaction.dependency is BodyCapture.HASHED
            else None
        )
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
            if mode is BodyCapture.METADATA:
                length = _utf8_length(value)
                captured.append(CapturedAgentPayload(field, length))
                continue
            if mode is BodyCapture.HASHED:
                hash_key = self._hash_key
                if hash_key is None:
                    raise RuntimeError("hashed capture has no fingerprint key")
                length, rendered = _keyed_fingerprint(value, hash_key)
            else:
                length = _utf8_length(value)
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
        if (
            type(duration) not in {int, float}
            or not math.isfinite(duration)
        ):
            raise ValueError("agent observation duration must be a finite number")
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
