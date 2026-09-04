from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from wreath.chat import AgentEvent, AgentRequest


class AgentConfigurationError(ValueError):
    pass


class AgentBudgetExceeded(RuntimeError):
    pass


class BackplaneError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        request_id: str | None = None,
        output_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.request_id = request_id
        self.output_started = output_started


def _exceeds_utf8(value: str, limit: int) -> bool:
    characters = len(value)
    if characters > limit:
        return True
    if characters <= limit // 4:
        return False
    return len(value.encode()) > limit


def _snapshot_json(value: Any, *, label: str) -> Any:
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain only finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        snapshot: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} object keys must be strings")
            snapshot[key] = _snapshot_json(item, label=label)
        return snapshot
    if isinstance(value, list | tuple):
        return [_snapshot_json(item, label=label) for item in value]
    raise TypeError(f"{label} must contain only JSON values")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.cached_input_tokens) < 0:
            raise ValueError("model token counts must be non-negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ToolSpecification:
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool name must be non-empty")
        if not self.description:
            raise ValueError(f"tool {self.name!r} description must be non-empty")
        object.__setattr__(
            self,
            "input_schema",
            _snapshot_json(self.input_schema, label=f"tool {self.name!r} input schema"),
        )


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported model message role {self.role!r}")
        if self.role == "tool" and (not self.name or not self.call_id):
            raise ValueError("tool messages require non-empty name and call_id")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSpecification, ...] = ()
    max_output_tokens: int | None = None
    temperature: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must be non-empty")
        if not self.messages:
            raise ValueError("model request messages must be non-empty")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("model request metadata must be a mapping")
        if not isinstance(self.metadata, MappingProxyType):
            metadata = dict(self.metadata)
            for name in ("required_capabilities", "allowed_regions"):
                facts = metadata.get(name)
                if isinstance(facts, list | set | dict):
                    metadata[name] = tuple(facts)
            object.__setattr__(self, "metadata", MappingProxyType(metadata))


@dataclass(frozen=True, slots=True)
class ModelResponseEvent:
    kind: Literal["text", "tool_call", "usage", "completed"]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: Mapping[str, Any] | None = None
    usage: ModelUsage | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"text", "tool_call", "usage", "completed"}:
            raise ValueError(f"unsupported model event kind {self.kind!r}")
        if self.kind == "text" and self.text is None:
            raise ValueError("text events require text")
        if self.kind == "tool_call" and (
            not self.tool_name or not self.tool_call_id or self.arguments is None
        ):
            raise ValueError("tool_call events require tool_name, tool_call_id, and arguments")
        if self.kind == "usage" and self.usage is None:
            raise ValueError("usage events require usage")
        if self.kind == "tool_call":
            object.__setattr__(
                self,
                "arguments",
                _snapshot_json(self.arguments, label="model tool-call arguments"),
            )

    @classmethod
    def text_delta(cls, text: str, *, request_id: str | None = None) -> ModelResponseEvent:
        return cls("text", text=text, provider_request_id=request_id)

    @classmethod
    def tool_call(
        cls,
        name: str,
        call_id: str,
        arguments: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> ModelResponseEvent:
        if not name or not call_id:
            raise ValueError("model tool calls require non-empty name and call_id")
        return cls(
            "tool_call",
            tool_name=name,
            tool_call_id=call_id,
            arguments=arguments,
            provider_request_id=request_id,
        )

    @classmethod
    def usage_report(
        cls, usage: ModelUsage, *, request_id: str | None = None
    ) -> ModelResponseEvent:
        return cls("usage", usage=usage, provider_request_id=request_id)

    @classmethod
    def completed(cls, *, request_id: str | None = None) -> ModelResponseEvent:
        return cls("completed", provider_request_id=request_id)


@runtime_checkable
class ModelBackplane(Protocol):
    name: str

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponseEvent]: ...


@dataclass(frozen=True, slots=True)
class ModelTarget:
    backplane: ModelBackplane
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.backplane, ModelBackplane):
            raise TypeError("fallback backplane must implement ModelBackplane")
        if not self.model:
            raise ValueError("fallback model must be non-empty")


@dataclass(frozen=True, slots=True)
class AgentInvocationContext:
    tenant: str
    principal: Any
    conversation: str
    correlation_id: str | None = None
    delegation: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tenant:
            raise ValueError("agent tenant must be non-empty")
        if not self.conversation:
            raise ValueError("agent conversation must be non-empty")


@dataclass(frozen=True, slots=True)
class AgentProfile:
    name: str
    backplane: ModelBackplane
    model: str
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    fallbacks: tuple[ModelTarget, ...] = ()
    max_turns: int = 8
    max_tool_calls: int = 16
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_prompt_bytes: int = 256 * 1024
    max_tool_argument_bytes: int = 1024 * 1024
    max_tool_result_bytes: int = 1024 * 1024
    timeout: float | None = 120.0
    delegation_scope: frozenset[str] = frozenset()
    delegation_ttl: float = 300.0
    _targets: tuple[ModelTarget, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("agent profile name must be non-empty")
        if not isinstance(self.backplane, ModelBackplane):
            raise TypeError("agent backplane must implement ModelBackplane")
        if not self.model:
            raise ValueError("agent model must be non-empty")
        for field_name in (
            "max_turns",
            "max_tool_calls",
            "max_prompt_bytes",
            "max_tool_argument_bytes",
            "max_tool_result_bytes",
        ):
            if type(getattr(self, field_name)) is not int:
                raise TypeError(f"{field_name} must be an integer")
        for field_name in ("max_output_tokens", "max_total_tokens"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not int:
                raise TypeError(f"{field_name} must be an integer or None")
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be non-negative")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.max_total_tokens is not None and self.max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive")
        if self.max_prompt_bytes < 1:
            raise ValueError("max_prompt_bytes must be positive")
        if self.max_tool_argument_bytes < 1:
            raise ValueError("max_tool_argument_bytes must be positive")
        if self.max_tool_result_bytes < 1:
            raise ValueError("max_tool_result_bytes must be positive")
        if self.timeout is not None and (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, int | float)
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("agent timeout must be a positive finite duration")
        if (
            isinstance(self.delegation_ttl, bool)
            or not isinstance(self.delegation_ttl, int | float)
            or not math.isfinite(self.delegation_ttl)
            or self.delegation_ttl <= 0
        ):
            raise ValueError("delegation_ttl must be a positive finite duration")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError(f"agent profile {self.name!r} has a duplicate tool name")
        if any(not tool for tool in self.tools):
            raise ValueError(f"agent profile {self.name!r} has an empty tool name")
        object.__setattr__(
            self,
            "_targets",
            (ModelTarget(self.backplane, self.model), *self.fallbacks),
        )

    @property
    def targets(self) -> tuple[ModelTarget, ...]:
        return self._targets


class AgentCatalog:
    __slots__ = ("_default", "_profiles", "_tenants")

    def __init__(
        self,
        profiles: tuple[AgentProfile, ...],
        *,
        default: str | None = None,
        tenants: Mapping[str, str] | None = None,
    ) -> None:
        if not profiles:
            raise ValueError("agent catalog requires at least one profile")
        compiled: dict[str, AgentProfile] = {}
        for profile in profiles:
            if profile.name in compiled:
                raise ValueError(f"duplicate agent profile {profile.name!r}")
            compiled[profile.name] = profile
        selected_default = profiles[0].name if default is None else default
        if selected_default not in compiled:
            raise ValueError(f"default selects unknown profile {selected_default!r}")
        compiled_tenants = dict(tenants or {})
        for tenant, profile_name in compiled_tenants.items():
            if not tenant:
                raise ValueError("agent tenant selector must be non-empty")
            if profile_name not in compiled:
                raise ValueError(f"tenant {tenant!r} selects unknown profile {profile_name!r}")
        self._profiles = compiled
        self._default = selected_default
        self._tenants = compiled_tenants

    @property
    def profiles(self) -> tuple[AgentProfile, ...]:
        return tuple(self._profiles.values())

    def select(self, tenant: str, *, requested: str | None = None) -> AgentProfile:
        name = requested if requested is not None else self._tenants.get(tenant, self._default)
        try:
            return self._profiles[name]
        except KeyError:
            raise LookupError(f"unknown agent profile {name!r}") from None


class _ToolSet(Protocol):
    specifications: tuple[ToolSpecification, ...]

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        context: AgentInvocationContext,
    ) -> object: ...


class _ToolCatalog(Protocol):
    def select(self, names: tuple[str, ...]) -> _ToolSet: ...


class AgentRuntime:
    __slots__ = ("_observe_model", "_observe_tool", "catalog", "_toolsets")

    def __init__(
        self,
        catalog: AgentCatalog,
        *,
        tools: _ToolCatalog | None = None,
        observability: Any = None,
    ) -> None:
        self.catalog = catalog
        self._observe_model = (
            None if observability is None else getattr(observability, "model", None)
        )
        self._observe_tool = None if observability is None else getattr(observability, "tool", None)
        if observability is not None and (
            not callable(self._observe_model) or not callable(self._observe_tool)
        ):
            raise TypeError("agent observability must provide model(...) and tool(...)")
        selected: dict[str, _ToolSet | None] = {}
        for profile in catalog.profiles:
            if not profile.tools:
                selected[profile.name] = None
            elif tools is None:
                raise AgentConfigurationError(
                    f"agent profile {profile.name!r} declares tools but no tool catalog "
                    "was supplied"
                )
            else:
                selected[profile.name] = tools.select(profile.tools)
        self._toolsets = selected

    async def _model_events(
        self,
        *,
        profile: AgentProfile,
        context: AgentInvocationContext,
        messages: list[ModelMessage],
        tools: tuple[ToolSpecification, ...],
        prompt: str,
    ) -> AsyncIterator[ModelResponseEvent]:
        targets = profile.targets
        compiled_messages = tuple(messages)
        for index, target in enumerate(targets):
            request = ModelRequest(
                model=target.model,
                messages=compiled_messages,
                tools=tools,
                max_output_tokens=profile.max_output_tokens,
                metadata={"agent_profile": profile.name, "tenant": context.tenant},
            )
            emitted = False
            completed = False
            request_id = None
            usage = None
            observed = self._observe_model is not None
            started = time.perf_counter() if observed else 0.0
            try:
                async for event in target.backplane.stream(request):
                    if completed:
                        raise BackplaneError(
                            f"model backplane {target.backplane.name!r} emitted an event after "
                            "completed",
                            output_started=True,
                        )
                    emitted = True
                    completed = event.kind == "completed"
                    if observed:
                        request_id = event.provider_request_id or request_id
                        if event.usage is not None:
                            usage = event.usage
                    yield event
                if not completed:
                    raise BackplaneError(
                        f"model backplane {target.backplane.name!r} ended without completed",
                        retryable=True,
                        output_started=emitted,
                    )
                if observed:
                    await self._model_observation(
                        context,
                        target=target,
                        request_id=request_id,
                        usage=usage,
                        duration=time.perf_counter() - started,
                        outcome="succeeded",
                        fallback=index > 0,
                        prompt=prompt,
                    )
                return
            except asyncio.CancelledError:
                if observed:
                    await self._model_observation(
                        context,
                        target=target,
                        request_id=request_id,
                        usage=usage,
                        duration=time.perf_counter() - started,
                        outcome="cancelled",
                        fallback=index > 0,
                        prompt=prompt,
                    )
                raise
            except BackplaneError as error:
                if observed:
                    await self._model_observation(
                        context,
                        target=target,
                        request_id=error.request_id or request_id,
                        usage=usage,
                        duration=time.perf_counter() - started,
                        outcome="unknown" if emitted or error.output_started else "failed",
                        fallback=index > 0,
                        prompt=prompt,
                    )
                can_fallback = (
                    not emitted
                    and not error.output_started
                    and error.retryable
                    and index + 1 < len(targets)
                )
                if not can_fallback:
                    raise

    async def _model_observation(
        self,
        context: AgentInvocationContext,
        *,
        target: ModelTarget,
        request_id: str | None,
        usage: ModelUsage | None,
        duration: float,
        outcome: str,
        fallback: bool,
        prompt: str,
    ) -> None:
        observe = self._observe_model
        if observe is None:
            return
        from .observability import AgentUsage

        observed_usage = (
            None
            if usage is None
            else AgentUsage(
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_input_tokens,
            )
        )
        await observe(
            context,
            provider=target.backplane.name,
            model=target.model,
            request_id=request_id,
            duration=duration,
            outcome=outcome,
            usage=observed_usage,
            fallback=fallback,
            payloads={"prompt": prompt},
        )

    async def _tool_observation(
        self,
        context: AgentInvocationContext,
        *,
        name: str,
        call_id: str,
        started: float,
        outcome: str,
        arguments: str,
        result: Any,
    ) -> None:
        observe = self._observe_tool
        if observe is not None:
            payloads = {"arguments": arguments}
            if outcome == "succeeded":
                payloads["result"] = result
            await observe(
                context,
                tool=name,
                call_id=call_id,
                duration=time.perf_counter() - started,
                outcome=outcome,
                payloads=payloads,
            )

    def _delegate(
        self, profile: AgentProfile, context: AgentInvocationContext
    ) -> AgentInvocationContext:
        if not profile.delegation_scope:
            return context
        narrow = getattr(context.principal, "narrow", None)
        if not callable(narrow):
            raise AgentConfigurationError(
                f"agent profile {profile.name!r} declares delegation_scope but its principal "
                "does not support narrow(actor=, scope=, ttl=)"
            )
        delegated = narrow(
            actor=f"wreath.agent/{profile.name}",
            scope=profile.delegation_scope,
            ttl=profile.delegation_ttl,
        )
        return replace(
            context,
            principal=delegated,
            delegation=getattr(delegated, "narrowing", delegated),
        )

    async def execute(
        self,
        prompt: str,
        *,
        context: AgentInvocationContext,
        profile: str | None = None,
    ) -> AsyncIterator[ModelResponseEvent]:
        if not prompt:
            raise ValueError("agent prompt must be non-empty")
        selected = self.catalog.select(context.tenant, requested=profile)
        if _exceeds_utf8(prompt, selected.max_prompt_bytes):
            raise AgentBudgetExceeded(
                f"agent profile {selected.name!r} exhausted its prompt byte ceiling"
            )
        effective_context = self._delegate(selected, context)
        toolset = self._toolsets[selected.name]
        specifications = () if toolset is None else toolset.specifications
        messages = [ModelMessage("user", prompt)]
        if selected.system_prompt:
            messages.insert(0, ModelMessage("system", selected.system_prompt))

        async with asyncio.timeout(selected.timeout):
            tool_calls = 0
            total_tokens = 0
            seen_call_ids: set[str] = set()
            for turn in range(selected.max_turns):
                pending: list[ModelResponseEvent] = []
                turn_tokens = 0
                async for event in self._model_events(
                    profile=selected,
                    context=effective_context,
                    messages=messages,
                    tools=specifications,
                    prompt=prompt,
                ):
                    if event.kind == "tool_call":
                        if tool_calls + len(pending) == selected.max_tool_calls:
                            raise AgentBudgetExceeded(
                                f"agent profile {selected.name!r} exhausted its tool-call budget"
                            )
                        call_id = event.tool_call_id
                        if call_id in seen_call_ids:
                            raise BackplaneError(
                                f"model returned duplicate tool call ID {call_id!r}",
                                output_started=True,
                            )
                        if call_id is not None:
                            seen_call_ids.add(call_id)
                        pending.append(event)
                    if event.usage is not None:
                        turn_tokens += event.usage.total_tokens
                    yield event
                total_tokens += turn_tokens
                if (
                    selected.max_total_tokens is not None
                    and total_tokens > selected.max_total_tokens
                ):
                    raise AgentBudgetExceeded(
                        f"agent profile {selected.name!r} exhausted its total-token budget"
                    )
                if not pending:
                    return
                if turn + 1 == selected.max_turns:
                    raise AgentBudgetExceeded(
                        f"agent profile {selected.name!r} exhausted its turn budget"
                    )
                for call in pending:
                    if toolset is None:
                        raise AgentConfigurationError(
                            f"model requested tool {call.tool_name!r} without a tool catalog"
                        )
                    tool_calls += 1
                    name = call.tool_name
                    call_id = call.tool_call_id
                    arguments = call.arguments
                    if name is None or call_id is None or arguments is None:
                        raise RuntimeError("invalid tool call event")
                    rendered_arguments = json.dumps(
                        arguments,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    if _exceeds_utf8(rendered_arguments, selected.max_tool_argument_bytes):
                        raise AgentBudgetExceeded(
                            f"agent profile {selected.name!r} exhausted its tool-argument "
                            "byte ceiling"
                        )
                    observed = self._observe_tool is not None
                    started = time.perf_counter() if observed else 0.0
                    outcome = "failed"
                    try:
                        result = await toolset.invoke(
                            name,
                            arguments,
                            call_id=call_id,
                            context=effective_context,
                        )
                    except asyncio.CancelledError:
                        outcome = "cancelled"
                        raise
                    except PermissionError:
                        outcome = "denied"
                        raise
                    else:
                        outcome = "succeeded"
                    finally:
                        if observed:
                            await self._tool_observation(
                                effective_context,
                                name=name,
                                call_id=call_id,
                                started=started,
                                outcome=outcome,
                                arguments=rendered_arguments,
                                result=result if outcome == "succeeded" else "",
                            )
                    rendered_result = json.dumps(
                        result,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    if _exceeds_utf8(rendered_result, selected.max_tool_result_bytes):
                        raise AgentBudgetExceeded(
                            f"agent profile {selected.name!r} exhausted its tool-result "
                            "byte ceiling"
                        )
                    messages.append(
                        ModelMessage(
                            "assistant",
                            rendered_arguments,
                            name=name,
                            call_id=call_id,
                        )
                    )
                    messages.append(
                        ModelMessage(
                            "tool",
                            rendered_result,
                            name=name,
                            call_id=call_id,
                        )
                    )
            raise AgentBudgetExceeded(f"agent profile {selected.name!r} exhausted its turn budget")

    async def run(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        from wreath.chat import AgentEvent

        correlation = request.correlation
        correlation_id = correlation.trace_id or correlation.job_id or correlation.interaction_id
        context = AgentInvocationContext(
            tenant=request.tenant,
            principal=request.principal,
            conversation=request.conversation,
            correlation_id=correlation_id,
            metadata={"actor": request.actor},
        )
        async for event in self.execute(request.prompt, context=context):
            if event.kind == "text" and event.text is not None:
                yield AgentEvent.text(event.text, id=event.provider_request_id)
        yield AgentEvent.completed()
