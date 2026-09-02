from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .identity import principal_id


class UnknownModelOutcome(RuntimeError):
    pass


class StaleAgentFence(RuntimeError):
    def __init__(self, turn_id: str, fence: int) -> None:
        super().__init__(f"turn {turn_id!r} rejected stale fence {fence}")
        self.turn_id = turn_id
        self.fence = fence


def _stable_id(domain: str, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode())
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def stable_turn_id(tenant: str, principal_id: str, conversation: str, message_id: str) -> str:
    if not all((tenant, principal_id, conversation, message_id)):
        raise ValueError("stable turn IDs require tenant, principal, conversation, and message ID")
    return _stable_id("wreath.agent.turn.v1", tenant, principal_id, conversation, message_id)


def stable_tool_call_id(turn_id: str, index: int, name: str, arguments: Mapping[str, Any]) -> str:
    if not turn_id or not name:
        raise ValueError("stable tool-call IDs require a turn ID and tool name")
    if index < 0:
        raise ValueError("tool-call index must be non-negative")
    encoded = json.dumps(arguments, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return _stable_id("wreath.agent.tool.v1", turn_id, str(index), name, encoded)


@dataclass(frozen=True, slots=True)
class DurableTurn:
    turn_id: str
    tenant: str
    principal_id: str
    conversation: str
    prompt: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.turn_id, self.tenant, self.principal_id, self.conversation, self.prompt)):
            raise ValueError(
                "durable turns require IDs, tenant, principal, conversation, and prompt"
            )

    @classmethod
    def from_invocation(cls, invocation: Any, *, prompt: str, message_id: str) -> DurableTurn:
        resolved_principal = principal_id(invocation.principal)
        return cls(
            turn_id=stable_turn_id(
                invocation.tenant, resolved_principal, invocation.conversation, message_id
            ),
            tenant=invocation.tenant,
            principal_id=resolved_principal,
            conversation=invocation.conversation,
            prompt=prompt,
            correlation_id=invocation.correlation_id,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> DurableTurn:
        expected = {
            "turn_id",
            "tenant",
            "principal_id",
            "conversation",
            "prompt",
            "correlation_id",
        }
        if set(payload) != expected:
            raise ValueError(f"durable turn payload must contain exactly {sorted(expected)!r}")
        values = {name: payload[name] for name in expected}
        for name in expected - {"correlation_id"}:
            if not isinstance(values[name], str):
                raise ValueError(f"durable turn {name} must be a string")
        correlation = values["correlation_id"]
        if correlation is not None and not isinstance(correlation, str):
            raise ValueError("durable turn correlation_id must be a string or None")
        return cls(**values)

    def as_payload(self) -> dict[str, str | None]:
        return {
            "turn_id": self.turn_id,
            "tenant": self.tenant,
            "principal_id": self.principal_id,
            "conversation": self.conversation,
            "prompt": self.prompt,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool call name must be non-empty")
        encoded = json.dumps(
            self.arguments,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        snapshot = json.loads(encoded)
        if not isinstance(snapshot, dict):
            raise TypeError("tool call arguments must be a JSON object")
        object.__setattr__(self, "arguments", snapshot)


@dataclass(frozen=True, slots=True)
class ModelResult:
    output: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class EffectCheckpoint:
    turn_id: str
    call_id: str
    tenant: str
    principal_id: str
    fence: int


@dataclass(frozen=True, slots=True)
class DurableEffectContext:
    turn_id: str
    tenant: str
    principal_id: str
    correlation_id: str | None
    job_context: Any

    @property
    def fence(self) -> int:
        return self.job_context.fence


@runtime_checkable
class DurableModelBackend(Protocol):
    async def complete(self, turn: DurableTurn) -> ModelResult: ...


@runtime_checkable
class DurableToolExecutor(Protocol):
    async def execute(self, call: ToolCall, *, context: DurableEffectContext) -> Any: ...


@runtime_checkable
class EffectCheckpointStore(Protocol):
    async def completed(
        self, turn_id: str, call_id: str, *, tenant: str, principal_id: str
    ) -> EffectCheckpoint | None: ...

    async def validate_fence(
        self, turn_id: str, *, tenant: str, principal_id: str, fence: int
    ) -> None: ...

    async def complete(self, checkpoint: EffectCheckpoint) -> bool: ...


class DurableAgent:
    __slots__ = (
        "_backend",
        "_checkpoints",
        "_jobs",
        "_principal_id",
        "_task",
        "_tools",
    )

    def __init__(
        self,
        *,
        name: str,
        jobs: Any,
        backend: DurableModelBackend,
        tools: DurableToolExecutor,
        checkpoints: EffectCheckpointStore,
        principal_id: str | None = None,
    ) -> None:
        if not name:
            raise ValueError("durable agent name must be non-empty")
        if principal_id == "":
            raise ValueError("durable agent principal_id must be non-empty when supplied")
        self._jobs = jobs
        self._backend = backend
        self._tools = tools
        self._checkpoints = checkpoints
        self._principal_id = principal_id
        self._task = f"agent_{name}"
        jobs.task(self._task, retries=0)(self._run)

    async def enqueue(self, invocation: Any, *, prompt: str, message_id: str) -> int | None:
        turn = DurableTurn.from_invocation(invocation, prompt=prompt, message_id=message_id)
        return await self._jobs.enqueue(
            self._task,
            turn.as_payload(),
            key=turn.turn_id,
            tenant=turn.tenant,
        )

    async def _run(self, job_context: Any, payload: Mapping[str, Any]) -> None:
        turn = DurableTurn.from_payload(payload)
        attempt = getattr(job_context, "attempt", None)
        if type(attempt) is not int or attempt != 1:
            raise UnknownModelOutcome(
                f"durable turn {turn.turn_id!r} refuses recovery attempt {attempt!r} "
                "until effect claims are atomic"
            )
        if job_context.tenant != turn.tenant:
            raise ValueError(
                f"durable turn tenant {turn.tenant!r} does not match job tenant "
                f"{job_context.tenant!r}"
            )
        if self._principal_id is not None and turn.principal_id != self._principal_id:
            raise ValueError(
                f"durable turn principal {turn.principal_id!r} does not match configured "
                f"principal {self._principal_id!r}"
            )
        if job_context.key != turn.turn_id:
            raise ValueError(
                f"durable turn {turn.turn_id!r} does not match job key {job_context.key!r}"
            )
        await self._checkpoints.validate_fence(
            turn.turn_id,
            tenant=turn.tenant,
            principal_id=turn.principal_id,
            fence=job_context.fence,
        )
        result = await self._backend.complete(turn)
        calls: list[ToolCall] = []
        call_ids: set[str] = set()
        for index, proposed in enumerate(result.tool_calls):
            call_id = proposed.call_id or stable_tool_call_id(
                turn.turn_id, index, proposed.name, proposed.arguments
            )
            if call_id in call_ids:
                raise ValueError(f"model returned duplicate tool call ID {call_id!r}")
            call_ids.add(call_id)
            calls.append(ToolCall(proposed.name, proposed.arguments, call_id))
        effect_context = DurableEffectContext(
            turn.turn_id,
            turn.tenant,
            turn.principal_id,
            turn.correlation_id,
            job_context,
        )
        for call in calls:
            call_id = call.call_id
            if call_id is None:
                raise RuntimeError("compiled durable tool call has no ID")
            completed = await self._checkpoints.completed(
                turn.turn_id,
                call_id,
                tenant=turn.tenant,
                principal_id=turn.principal_id,
            )
            if completed is not None:
                if (
                    completed.turn_id != turn.turn_id
                    or completed.call_id != call_id
                    or completed.tenant != turn.tenant
                    or completed.principal_id != turn.principal_id
                ):
                    raise ValueError(
                        f"effect store returned a misbound checkpoint for call {call_id!r}"
                    )
                continue
            await self._checkpoints.validate_fence(
                turn.turn_id,
                tenant=turn.tenant,
                principal_id=turn.principal_id,
                fence=job_context.fence,
            )
            await self._tools.execute(call, context=effect_context)
            checkpoint = EffectCheckpoint(
                turn.turn_id,
                call_id,
                turn.tenant,
                turn.principal_id,
                job_context.fence,
            )
            stored = await self._checkpoints.complete(checkpoint)
            if not stored:
                raise StaleAgentFence(turn.turn_id, job_context.fence)


__all__ = [
    "DurableAgent",
    "DurableEffectContext",
    "DurableModelBackend",
    "DurableToolExecutor",
    "DurableTurn",
    "EffectCheckpoint",
    "EffectCheckpointStore",
    "ModelResult",
    "StaleAgentFence",
    "ToolCall",
    "UnknownModelOutcome",
    "stable_tool_call_id",
    "stable_turn_id",
]
