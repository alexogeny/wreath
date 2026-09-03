from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wreath._agents.core import _exceeds_utf8
from wreath.agents import (
    AgentBudgetExceeded,
    AgentCapturePolicy,
    AgentCatalog,
    AgentConfigurationError,
    AgentInvocationContext,
    AgentObservability,
    AgentProfile,
    AgentRuntime,
    BackplaneError,
    ModelMessage,
    ModelRequest,
    ModelResponseEvent,
    ModelTarget,
    ModelUsage,
    ToolSpecification,
)
from wreath.chat import AgentBackend, AgentRequest, ChatContext, ChatCorrelation, ChatOps
from wreath.recording import BodyCapture, RedactionPolicy


class Backplane:
    def __init__(
        self,
        *turns: tuple[ModelResponseEvent | Exception, ...] | Exception,
        name: str = "test",
    ) -> None:
        self.name = name
        self.requests: list[ModelRequest] = []
        self.turns = list(turns)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponseEvent]:
        self.requests.append(request)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for event in turn:
            if isinstance(event, Exception):
                raise event
            yield event


class Tools:
    def __init__(self) -> None:
        self.specifications: tuple[ToolSpecification, ...] = (
            ToolSpecification("lookup", "Look up a value", {"type": "object"}),
        )
        self.calls: list[tuple[str, dict[str, Any], str, AgentInvocationContext]] = []
        self.selections = 0

    def select(self, names: tuple[str, ...]) -> Tools:
        assert names == ("lookup",)
        self.selections += 1
        return self

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        context: AgentInvocationContext,
    ) -> object:
        self.calls.append((name, dict(arguments), call_id, context))
        return {"answer": 42}


def context(*, tenant: str = "acme") -> AgentInvocationContext:
    return AgentInvocationContext(
        tenant=tenant,
        principal=object(),
        conversation="conversation-1",
        correlation_id="trace-1",
    )


def test_model_contracts_are_immutable_and_validate_the_wire_shape() -> None:
    message = ModelMessage("user", "hello")
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    tool = ToolSpecification("lookup", "Look up a value", schema)
    schema["properties"]["query"]["type"] = "integer"
    request = ModelRequest("model-1", (message,), (tool,), max_output_tokens=32)

    assert request.messages == (message,)
    assert request.tools == (tool,)
    assert tool.input_schema["properties"]["query"]["type"] == "string"
    assert ModelUsage(input_tokens=4, output_tokens=2).total_tokens == 6
    with pytest.raises(ValueError, match="model must be non-empty"):
        ModelRequest("", (message,))
    with pytest.raises(ValueError, match="tool name must be non-empty"):
        ToolSpecification("", "bad", {})


def test_utf8_ceiling_refuses_character_and_encoded_byte_overflow() -> None:
    assert _exceeds_utf8("12345", 4) is True
    assert _exceeds_utf8("€€", 4) is True
    assert _exceeds_utf8("a", 4) is False


def test_model_usage_rejects_each_negative_counter() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ModelUsage(input_tokens=-1)
    with pytest.raises(ValueError, match="non-negative"):
        ModelUsage(output_tokens=-1)
    with pytest.raises(ValueError, match="non-negative"):
        ModelUsage(cached_input_tokens=-1)


def test_model_messages_refuse_invalid_roles_and_incomplete_tool_identity() -> None:
    with pytest.raises(ValueError, match="unsupported model message role"):
        ModelMessage(cast(Any, "invalid"), "content")
    with pytest.raises(ValueError, match="require non-empty name and call_id"):
        ModelMessage("tool", "content", name="lookup")
    with pytest.raises(ValueError, match="require non-empty name and call_id"):
        ModelMessage("tool", "content", call_id="call-1")


def test_model_requests_refuse_empty_messages_and_non_positive_output_limit() -> None:
    with pytest.raises(ValueError, match="messages must be non-empty"):
        ModelRequest("model", ())
    with pytest.raises(ValueError, match="max_output_tokens must be positive"):
        ModelRequest("model", (ModelMessage("user", "hello"),), max_output_tokens=0)


def test_invocation_context_refuses_each_empty_scope_part() -> None:
    with pytest.raises(ValueError, match="tenant must be non-empty"):
        AgentInvocationContext("", object(), "conversation")
    with pytest.raises(ValueError, match="conversation must be non-empty"):
        AgentInvocationContext("tenant", object(), "")


def test_catalog_compiles_tenant_profile_selection_and_refuses_unknown_names() -> None:
    plane = Backplane(())
    internal = AgentProfile(name="internal", backplane=plane, model="model-1")
    public = AgentProfile(name="public", backplane=plane, model="model-2")
    catalog = AgentCatalog(
        profiles=(internal, public),
        default="public",
        tenants={"acme": "internal"},
    )

    assert catalog.select("acme") is internal
    assert catalog.select("elsewhere") is public
    assert catalog.select("acme", requested="public") is public
    with pytest.raises(LookupError, match="unknown agent profile"):
        catalog.select("acme", requested="")
    with pytest.raises(ValueError, match="tenant .*missing.*unknown profile"):
        AgentCatalog(profiles=(internal,), tenants={"missing": "unknown"})


def test_profile_refuses_unbounded_or_ambiguous_configuration_at_construction() -> None:
    plane = Backplane(())
    with pytest.raises(ValueError, match="max_turns must be positive"):
        AgentProfile(name="bad", backplane=plane, model="model", max_turns=0)
    with pytest.raises(ValueError, match="duplicate tool name"):
        AgentProfile(
            name="bad",
            backplane=plane,
            model="model",
            tools=("lookup", "lookup"),
        )
    with pytest.raises(ValueError, match="max_total_tokens must be positive"):
        AgentProfile(
            name="bad",
            backplane=plane,
            model="model",
            max_total_tokens=0,
        )
    with pytest.raises(ValueError, match="max_prompt_bytes must be positive"):
        AgentProfile(
            name="bad",
            backplane=plane,
            model="model",
            max_prompt_bytes=0,
        )
    with pytest.raises(ValueError, match="max_tool_result_bytes must be positive"):
        AgentProfile(
            name="bad",
            backplane=plane,
            model="model",
            max_tool_result_bytes=0,
        )
    with pytest.raises(ValueError, match="fallback model must be non-empty"):
        AgentProfile(
            name="bad",
            backplane=plane,
            model="model",
            fallbacks=(ModelTarget(plane, ""),),
        )


def test_profile_refuses_each_invalid_identity_and_budget_boundary() -> None:
    plane = Backplane(())
    invalid = (
        ({"name": ""}, "profile name"),
        ({"backplane": object()}, "backplane"),
        ({"model": ""}, "model"),
        ({"max_output_tokens": True}, "max_output_tokens"),
        ({"max_total_tokens": 1.5}, "max_total_tokens"),
        ({"max_tool_calls": -1}, "max_tool_calls"),
        ({"max_output_tokens": 0}, "max_output_tokens"),
        ({"max_tool_argument_bytes": 0}, "max_tool_argument_bytes"),
        ({"timeout": True}, "timeout"),
        ({"timeout": "forever"}, "timeout"),
        ({"timeout": 0}, "timeout"),
        ({"delegation_ttl": True}, "delegation_ttl"),
        ({"delegation_ttl": "forever"}, "delegation_ttl"),
        ({"delegation_ttl": 0}, "delegation_ttl"),
        ({"tools": ("",)}, "empty tool name"),
    )
    for options, message in invalid:
        values: dict[str, Any] = {
            "name": "profile",
            "backplane": plane,
            "model": "model",
            **options,
        }
        with pytest.raises((TypeError, ValueError), match=message):
            AgentProfile(**values)

    assert AgentProfile(
        name="profile", backplane=plane, model="model", timeout=None
    ).timeout is None
    assert AgentProfile(
        name="profile", backplane=plane, model="model", max_output_tokens=1
    ).max_output_tokens == 1


def test_catalog_refuses_each_invalid_declaration_boundary() -> None:
    plane = Backplane(())
    profile = AgentProfile(name="one", backplane=plane, model="model")

    with pytest.raises(ValueError, match="at least one profile"):
        AgentCatalog(())
    with pytest.raises(ValueError, match="duplicate agent profile"):
        AgentCatalog((profile, profile))
    with pytest.raises(ValueError, match="default selects unknown profile"):
        AgentCatalog((profile,), default="missing")
    with pytest.raises(ValueError, match="tenant selector must be non-empty"):
        AgentCatalog((profile,), tenants={"": "one"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_turns", True),
        ("max_tool_calls", 1.5),
        ("max_prompt_bytes", False),
        ("max_tool_argument_bytes", 1.5),
        ("max_tool_result_bytes", True),
        ("timeout", float("nan")),
        ("delegation_ttl", float("inf")),
    ],
)
def test_profile_budget_facts_refuse_boolean_fractional_and_nonfinite_values(
    field: str,
    value: Any,
) -> None:
    with pytest.raises((TypeError, ValueError), match=field):
        AgentProfile(name="invalid", backplane=Backplane(()), model="model", **{field: value})


@pytest.mark.asyncio
async def test_runtime_runs_a_tool_loop_without_protocol_loopback() -> None:
    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {"key": "meaning"}),
            ModelResponseEvent.completed(),
        ),
        (
            ModelResponseEvent.text_delta("forty-two", request_id="provider-1"),
            ModelResponseEvent.usage_report(ModelUsage(9, 2)),
            ModelResponseEvent.completed(request_id="provider-1"),
        ),
    )
    profile = AgentProfile(
        name="default",
        backplane=plane,
        model="model-1",
        system_prompt="Be precise.",
        tools=("lookup",),
    )
    tools = Tools()
    runtime = AgentRuntime(AgentCatalog((profile,), default="default"), tools=tools)

    events = [event async for event in runtime.execute("question", context=context())]

    assert [event.kind for event in events] == [
        "tool_call",
        "completed",
        "text",
        "usage",
        "completed",
    ]
    assert tools.calls[0][:3] == ("lookup", {"key": "meaning"}, "call-1")
    assert tools.selections == 1
    assert plane.requests[0].messages == (
        ModelMessage("system", "Be precise."),
        ModelMessage("user", "question"),
    )
    assert plane.requests[0].tools == tools.specifications
    assert plane.requests[1].messages[-2] == ModelMessage(
        "assistant", '{"key":"meaning"}', name="lookup", call_id="call-1"
    )
    assert plane.requests[1].messages[-1] == ModelMessage(
        "tool", '{"answer":42}', name="lookup", call_id="call-1"
    )


@pytest.mark.asyncio
async def test_runtime_refuses_empty_prompt_before_backplane_access() -> None:
    plane = Backplane(())
    profile = AgentProfile(name="profile", backplane=plane, model="model")
    runtime = AgentRuntime(AgentCatalog((profile,)))

    with pytest.raises(ValueError, match="prompt must be non-empty"):
        [event async for event in runtime.execute("", context=context())]

    assert plane.requests == []


@pytest.mark.asyncio
async def test_runtime_omits_absent_system_prompt() -> None:
    plane = Backplane((ModelResponseEvent.completed(),))
    profile = AgentProfile(name="profile", backplane=plane, model="model")

    [
        event
        async for event in AgentRuntime(AgentCatalog((profile,))).execute(
            "hello", context=context()
        )
    ]

    assert plane.requests[0].messages == (ModelMessage("user", "hello"),)


@pytest.mark.asyncio
async def test_runtime_refuses_delegation_for_a_principal_without_narrowing() -> None:
    plane = Backplane((ModelResponseEvent.completed(),))
    profile = AgentProfile(
        name="profile",
        backplane=plane,
        model="model",
        delegation_scope=frozenset({"documents:read"}),
    )

    with pytest.raises(AgentConfigurationError, match="does not support narrow"):
        [
            event
            async for event in AgentRuntime(AgentCatalog((profile,))).execute(
                "hello", context=context()
            )
        ]


@pytest.mark.asyncio
async def test_runtime_refuses_model_tool_call_without_catalog() -> None:
    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(name="profile", backplane=plane, model="model")

    with pytest.raises(AgentConfigurationError, match="without a tool catalog"):
        [
            event
            async for event in AgentRuntime(AgentCatalog((profile,))).execute(
                "hello", context=context()
            )
        ]


@pytest.mark.asyncio
async def test_runtime_refuses_pending_tool_call_at_final_turn() -> None:
    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(
        name="profile",
        backplane=plane,
        model="model",
        tools=("lookup",),
        max_turns=1,
    )
    tools = Tools()

    with pytest.raises(AgentBudgetExceeded, match="turn budget"):
        [
            event
            async for event in AgentRuntime(
                AgentCatalog((profile,)), tools=tools
            ).execute("hello", context=context())
        ]

    assert tools.calls == []


@pytest.mark.asyncio
async def test_retryable_failure_falls_back_only_before_any_visible_event() -> None:
    failed = Backplane(BackplaneError("busy", retryable=True), name="primary")
    backup = Backplane(
        (ModelResponseEvent.text_delta("ok"), ModelResponseEvent.completed()),
        name="backup",
    )
    profile = AgentProfile(
        name="default",
        backplane=failed,
        model="model-a",
        fallbacks=(ModelTarget(backup, "model-b"),),
    )
    runtime = AgentRuntime(AgentCatalog((profile,), default="default"))

    events = [event async for event in runtime.execute("hello", context=context())]

    assert [event.text for event in events if event.kind == "text"] == ["ok"]
    assert failed.requests[0].model == "model-a"
    assert backup.requests[0].model == "model-b"

    partial = Backplane(
        (
            ModelResponseEvent.text_delta("started"),
            BackplaneError("lost", retryable=True),
        )
    )
    no_call = Backplane(())
    unsafe = AgentProfile(
        name="unsafe",
        backplane=partial,
        model="model-a",
        fallbacks=(ModelTarget(no_call, "model-b"),),
    )
    with pytest.raises(BackplaneError, match="lost"):
        async for _ in AgentRuntime(AgentCatalog((unsafe,))).execute("hello", context=context()):
            pass
    assert no_call.requests == []

    flagged = Backplane(
        BackplaneError("uncertain", retryable=True, output_started=True),
    )
    flagged_backup = Backplane(())
    uncertain = AgentProfile(
        name="uncertain",
        backplane=flagged,
        model="model-a",
        fallbacks=(ModelTarget(flagged_backup, "model-b"),),
    )
    with pytest.raises(BackplaneError, match="uncertain"):
        async for _ in AgentRuntime(AgentCatalog((uncertain,))).execute(
            "hello", context=context()
        ):
            pass
    assert flagged_backup.requests == []


@pytest.mark.asyncio
async def test_retryable_failure_without_a_fallback_still_raises() -> None:
    failed = Backplane(BackplaneError("busy", retryable=True))
    profile = AgentProfile(name="default", backplane=failed, model="model")

    with pytest.raises(BackplaneError, match="busy"):
        async for _ in AgentRuntime(AgentCatalog((profile,))).execute(
            "hello", context=context()
        ):
            pass


@pytest.mark.asyncio
async def test_incomplete_or_post_terminal_model_stream_never_reaches_tools() -> None:
    for events, message in (
        (
            (ModelResponseEvent.tool_call("lookup", "call-1", {}),),
            "ended without completed",
        ),
        (
            (
                ModelResponseEvent.completed(),
                ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ),
            "emitted an event after completed",
        ),
    ):
        plane = Backplane(events)
        profile = AgentProfile(
            name="strict",
            backplane=plane,
            model="model",
            tools=("lookup",),
        )
        tools = Tools()

        with pytest.raises(BackplaneError, match=message):
            async for _ in AgentRuntime(AgentCatalog((profile,)), tools=tools).execute(
                "hello", context=context()
            ):
                pass

        assert tools.calls == []


@pytest.mark.asyncio
async def test_duplicate_tool_call_id_refuses_before_any_effect() -> None:
    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {"value": 1}),
            ModelResponseEvent.tool_call("lookup", "call-1", {"value": 2}),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(
        name="strict",
        backplane=plane,
        model="model",
        tools=("lookup",),
    )
    tools = Tools()

    with pytest.raises(BackplaneError, match="duplicate tool call ID 'call-1'"):
        async for _ in AgentRuntime(AgentCatalog((profile,)), tools=tools).execute(
            "hello", context=context()
        ):
            pass

    assert tools.calls == []


@pytest.mark.asyncio
async def test_non_retryable_failure_never_crosses_to_a_fallback() -> None:
    failed = Backplane(BackplaneError("invalid", status=400), name="primary")
    no_call = Backplane((), name="backup")
    profile = AgentProfile(
        name="default",
        backplane=failed,
        model="model-a",
        fallbacks=(ModelTarget(no_call, "model-b"),),
    )

    with pytest.raises(BackplaneError, match="invalid"):
        async for _ in AgentRuntime(AgentCatalog((profile,))).execute("hello", context=context()):
            pass
    assert no_call.requests == []


def test_runtime_refuses_a_tool_profile_without_a_catalog_at_startup() -> None:
    plane = Backplane(())
    profile = AgentProfile(
        name="tools",
        backplane=plane,
        model="model",
        tools=("lookup",),
    )

    with pytest.raises(ValueError, match="declares tools but no tool catalog"):
        AgentRuntime(AgentCatalog((profile,)))


@pytest.mark.asyncio
async def test_timeout_cancels_the_active_model_stream() -> None:
    class SlowBackplane:
        name = "slow"

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponseEvent]:
            del request
            await asyncio.sleep(60)
            yield ModelResponseEvent.completed()

    profile = AgentProfile(
        name="bounded",
        backplane=SlowBackplane(),
        model="model",
        timeout=0.001,
    )

    with pytest.raises(TimeoutError):
        async for _ in AgentRuntime(AgentCatalog((profile,))).execute("hello", context=context()):
            pass


@pytest.mark.asyncio
async def test_turn_and_tool_budgets_stop_before_the_over_budget_effect() -> None:
    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.tool_call("lookup", "call-2", {}),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(
        name="bounded",
        backplane=plane,
        model="model",
        tools=("lookup",),
        max_tool_calls=1,
    )
    tools = Tools()
    runtime = AgentRuntime(AgentCatalog((profile,)), tools=tools)
    kinds: list[str] = []

    with pytest.raises(AgentBudgetExceeded, match="tool-call budget"):
        async for event in runtime.execute("hello", context=context()):
            kinds.append(event.kind)
    assert kinds == ["tool_call"]
    assert tools.calls == []


@pytest.mark.asyncio
async def test_total_token_budget_stops_before_an_over_budget_tool_effect() -> None:
    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.usage_report(ModelUsage(8, 5)),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(
        name="bounded",
        backplane=plane,
        model="model",
        tools=("lookup",),
        max_total_tokens=12,
    )
    tools = Tools()
    runtime = AgentRuntime(AgentCatalog((profile,)), tools=tools)
    kinds: list[str] = []

    with pytest.raises(AgentBudgetExceeded, match="total-token budget"):
        async for event in runtime.execute("hello", context=context()):
            kinds.append(event.kind)

    assert kinds == ["tool_call", "usage", "completed"]
    assert tools.calls == []


@pytest.mark.asyncio
async def test_total_token_budget_accumulates_across_model_turns() -> None:
    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.usage_report(ModelUsage(3, 2)),
            ModelResponseEvent.completed(),
        ),
        (
            ModelResponseEvent.usage_report(ModelUsage(4, 2)),
            ModelResponseEvent.completed(),
        ),
    )
    profile = AgentProfile(
        name="bounded",
        backplane=plane,
        model="model",
        tools=("lookup",),
        max_total_tokens=10,
    )
    tools = Tools()
    runtime = AgentRuntime(AgentCatalog((profile,)), tools=tools)

    with pytest.raises(AgentBudgetExceeded, match="total-token budget"):
        async for _ in runtime.execute("hello", context=context()):
            pass

    assert [call[2] for call in tools.calls] == ["call-1"]


@pytest.mark.asyncio
async def test_prompt_and_retained_tool_result_have_byte_ceilings() -> None:
    no_call = Backplane((ModelResponseEvent.completed(),))
    prompt_profile = AgentProfile(
        name="prompt",
        backplane=no_call,
        model="model",
        max_prompt_bytes=3,
    )
    with pytest.raises(AgentBudgetExceeded, match="prompt byte ceiling"):
        async for _ in AgentRuntime(AgentCatalog((prompt_profile,))).execute(
            "four", context=context()
        ):
            pass
    assert no_call.requests == []

    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(
        name="tool-result",
        backplane=plane,
        model="model",
        tools=("lookup",),
        max_tool_result_bytes=4,
    )
    tools = Tools()
    runtime = AgentRuntime(AgentCatalog((profile,)), tools=tools)
    with pytest.raises(AgentBudgetExceeded, match="tool-result byte ceiling"):
        async for _ in runtime.execute("ok", context=context()):
            pass
    assert len(tools.calls) == 1
    assert len(plane.requests) == 1

    argument_plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-2", {"secret": "too large"}),
            ModelResponseEvent.completed(),
        )
    )
    argument_profile = AgentProfile(
        name="tool-argument",
        backplane=argument_plane,
        model="model",
        tools=("lookup",),
        max_tool_argument_bytes=4,
    )
    argument_tools = Tools()
    with pytest.raises(AgentBudgetExceeded, match="tool-argument byte ceiling"):
        async for _ in AgentRuntime(
            AgentCatalog((argument_profile,)), tools=argument_tools
        ).execute("ok", context=context()):
            pass
    assert argument_tools.calls == []


@pytest.mark.asyncio
async def test_tool_call_arguments_are_snapshotted_before_provider_resumes() -> None:
    arguments = {"nested": {"value": "safe"}, "items": [1]}
    event = ModelResponseEvent.tool_call("lookup", "call-1", arguments)

    class MutatingBackplane:
        name = "mutating"
        calls = 0

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelResponseEvent]:
            del request
            self.calls += 1
            if self.calls > 1:
                yield ModelResponseEvent.completed()
                return
            yield event
            arguments["nested"]["value"] = "changed"
            arguments["items"].append(2)
            yield ModelResponseEvent.completed()

    profile = AgentProfile(
        name="snapshot",
        backplane=MutatingBackplane(),
        model="model",
        tools=("lookup",),
    )
    tools = Tools()

    [
        item
        async for item in AgentRuntime(AgentCatalog((profile,)), tools=tools).execute(
            "hello", context=context()
        )
    ]

    assert tools.calls[0][1] == {"nested": {"value": "safe"}, "items": [1]}


@pytest.mark.asyncio
async def test_runtime_observes_model_and_tool_boundaries_without_payloads() -> None:
    class Observer:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def record(self, event: Any) -> None:
            self.events.append(event)

    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {"secret": "hidden"}),
            ModelResponseEvent.usage_report(ModelUsage(2, 1)),
            ModelResponseEvent.completed(request_id="request-1"),
        ),
        (
            ModelResponseEvent.text_delta("done", request_id="request-2"),
            ModelResponseEvent.usage_report(ModelUsage(5, 3)),
            ModelResponseEvent.completed(),
        ),
    )
    profile = AgentProfile(
        name="observed",
        backplane=plane,
        model="model",
        tools=("lookup",),
    )
    observer = Observer()
    runtime = AgentRuntime(
        AgentCatalog((profile,)),
        tools=Tools(),
        observability=AgentObservability(observer=observer),
    )

    observed_context = AgentInvocationContext("acme", "user-1", "conversation-1")
    [event async for event in runtime.execute("hello", context=observed_context)]

    assert [event.kind for event in observer.events] == ["model", "tool", "model"]
    assert [event.outcome for event in observer.events] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert observer.events[0].usage.total_tokens == 3
    assert observer.events[2].request_id == "request-2"
    assert all(event.payload == () for event in observer.events)


@pytest.mark.asyncio
async def test_runtime_passes_opted_in_model_and_tool_payloads_to_observability() -> None:
    class Observer:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def record(self, event: Any) -> None:
            self.events.append(event)

    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {"secret": "argument"}),
            ModelResponseEvent.completed(),
        ),
        (ModelResponseEvent.completed(),),
    )
    profile = AgentProfile(
        name="observed",
        backplane=plane,
        model="model",
        tools=("lookup",),
    )
    observer = Observer()
    capture = AgentCapturePolicy(
        RedactionPolicy(
            dependency=BodyCapture.STRUCTURED,
            max_body_bytes=256,
            max_fields=3,
        ),
        fields=frozenset({"prompt", "arguments", "result"}),
        redact=lambda field, value: f"{field}:{value}",
    )
    runtime = AgentRuntime(
        AgentCatalog((profile,)),
        tools=Tools(),
        observability=AgentObservability(observer=observer, capture=capture),
    )

    supplied = AgentInvocationContext("acme", "user-1", "conversation-1")
    [event async for event in runtime.execute("private prompt", context=supplied)]

    assert [(item.field, item.value) for item in observer.events[0].payload] == [
        ("prompt", "prompt:private prompt")
    ]
    assert [(item.field, item.value) for item in observer.events[1].payload] == [
        ("arguments", 'arguments:{"secret":"argument"}'),
        ("result", "result:{'answer': 42}"),
    ]
    assert [(item.field, item.value) for item in observer.events[2].payload] == [
        ("prompt", "prompt:private prompt")
    ]


@pytest.mark.asyncio
async def test_observer_failure_is_counted_without_replaying_a_tool_effect() -> None:
    class FailingObserver:
        async def record(self, event: Any) -> None:
            del event
            raise OSError("collector unavailable")

    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.completed(),
        ),
        (ModelResponseEvent.text_delta("done"), ModelResponseEvent.completed()),
    )
    profile = AgentProfile(
        name="observed",
        backplane=plane,
        model="model",
        tools=("lookup",),
    )
    tools = Tools()
    observability = AgentObservability(observer=FailingObserver())
    runtime = AgentRuntime(AgentCatalog((profile,)), tools=tools, observability=observability)
    supplied = AgentInvocationContext("acme", "user-1", "conversation-1")

    events = [event async for event in runtime.execute("hello", context=supplied)]

    assert [event.text for event in events if event.kind == "text"] == ["done"]
    assert len(tools.calls) == 1
    assert observability.recording_errors == 3


@pytest.mark.asyncio
async def test_denied_tool_observation_does_not_read_an_absent_result() -> None:
    class Observer:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def record(self, event: Any) -> None:
            self.events.append(event)

    class DeniedTools(Tools):
        async def invoke(
            self,
            name: str,
            arguments: Mapping[str, Any],
            *,
            call_id: str,
            context: AgentInvocationContext,
        ) -> object:
            del name, arguments, call_id, context
            raise PermissionError("denied")

    plane = Backplane(
        (
            ModelResponseEvent.tool_call("lookup", "call-1", {}),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(
        name="observed",
        backplane=plane,
        model="model",
        tools=("lookup",),
    )
    observer = Observer()
    runtime = AgentRuntime(
        AgentCatalog((profile,)),
        tools=DeniedTools(),
        observability=AgentObservability(observer=observer),
    )

    with pytest.raises(PermissionError, match="denied"):
        [
            event
            async for event in runtime.execute(
                "hello",
                context=AgentInvocationContext("acme", "user-1", "conversation-1"),
            )
        ]

    assert observer.events[-1].kind == "tool"
    assert observer.events[-1].outcome == "denied"


@pytest.mark.asyncio
async def test_profile_delegation_uses_the_existing_principal_narrowing_owner() -> None:
    class Principal:
        def __init__(self) -> None:
            self.arguments: tuple[Any, ...] | None = None

        def narrow(self, *, actor: str, scope: frozenset[str], ttl: float) -> object:
            self.arguments = (actor, scope, ttl)
            return "narrowed"

    principal = Principal()
    plane = Backplane((ModelResponseEvent.completed(),))
    profile = AgentProfile(
        name="delegated",
        backplane=plane,
        model="model",
        delegation_scope=frozenset({"documents:read"}),
        delegation_ttl=60,
    )
    runtime = AgentRuntime(AgentCatalog((profile,)))
    supplied = AgentInvocationContext("acme", principal, "conversation")

    [event async for event in runtime.execute("hello", context=supplied)]

    assert principal.arguments == (
        "wreath.agent/delegated",
        frozenset({"documents:read"}),
        60,
    )


@pytest.mark.asyncio
async def test_runtime_is_a_chat_backend_and_keeps_the_resolved_principal() -> None:
    principal = object()
    plane = Backplane(
        (
            ModelResponseEvent.text_delta("hello"),
            ModelResponseEvent.completed(),
        )
    )
    profile = AgentProfile(name="default", backplane=plane, model="model")
    runtime = AgentRuntime(AgentCatalog((profile,)))
    request = AgentRequest(
        tenant="acme",
        actor="user-1",
        conversation="conversation-1",
        prompt="hi",
        correlation=ChatCorrelation(interaction_id="interaction-1", trace_id="trace-1"),
        principal=principal,
    )

    events = [event async for event in runtime.run(request)]

    assert isinstance(runtime, AgentBackend)
    assert [(event.kind, event.content) for event in events] == [
        ("text", "hello"),
        ("completed", None),
    ]


@pytest.mark.asyncio
async def test_chat_runtime_uses_the_most_specific_available_correlation() -> None:
    for correlation, expected in (
        (
            ChatCorrelation(
                interaction_id="interaction-1",
                job_id="job-1",
                trace_id="trace-1",
            ),
            "trace-1",
        ),
        (ChatCorrelation(interaction_id="interaction-1", job_id="job-1"), "job-1"),
        (ChatCorrelation(interaction_id="interaction-1"), "interaction-1"),
    ):
        plane = Backplane(
            (
                ModelResponseEvent.tool_call("lookup", "call-1", {}),
                ModelResponseEvent.completed(),
            ),
            (ModelResponseEvent.completed(),),
        )
        tools = Tools()
        runtime = AgentRuntime(
            AgentCatalog(
                (
                    AgentProfile(
                        name="default",
                        backplane=plane,
                        model="model",
                        tools=("lookup",),
                    ),
                )
            ),
            tools=tools,
        )
        supplied = AgentRequest(
            tenant="acme",
            actor="user-1",
            conversation="conversation-1",
            prompt="hi",
            correlation=correlation,
            principal=object(),
        )

        [event async for event in runtime.run(supplied)]

        assert tools.calls[0][3].correlation_id == expected


@pytest.mark.asyncio
async def test_chat_runtime_does_not_render_non_text_model_events() -> None:
    plane = Backplane(
        (
            ModelResponseEvent("usage", text="not chat text", usage=ModelUsage(1, 1)),
            ModelResponseEvent.completed(),
        )
    )
    runtime = AgentRuntime(
        AgentCatalog((AgentProfile(name="default", backplane=plane, model="model"),))
    )
    supplied = AgentRequest(
        tenant="acme",
        actor="user-1",
        conversation="conversation-1",
        prompt="hi",
        correlation=ChatCorrelation(interaction_id="interaction-1"),
        principal=object(),
    )

    events = [event async for event in runtime.run(supplied)]

    assert [(event.kind, event.content) for event in events] == [("completed", None)]


def test_chatops_registers_an_agent_as_one_durable_governed_command() -> None:
    plane = Backplane((ModelResponseEvent.completed(),))
    runtime = AgentRuntime(
        AgentCatalog((AgentProfile(name="default", backplane=plane, model="model"),))
    )
    chat = ChatOps(name="support", providers=())

    handler = chat.agent(
        "ask",
        runtime,
        description="Ask the support agent",
        action="Support::ask",
        resource="conversation",
    )

    declaration = chat.commands["ask"]
    assert declaration.handler is handler
    assert declaration.execution == "durable"
    assert declaration.retries == 0
    assert declaration.action == "Support::ask"
    assert declaration.resource == "conversation"
    assert [parameter.name for parameter in declaration.parameters] == ["context", "prompt"]

    with pytest.raises(ValueError, match="action must be non-empty"):
        chat.agent("unsafe", runtime, action="")


def test_durable_chat_activation_carries_the_federated_principal_to_the_agent() -> None:
    principal = object()
    chat = ChatOps(name="support", providers=())

    async def emit(_event: object) -> None:
        return None

    context = ChatContext(
        provider="slack",
        installation="workspace-1",
        tenant="acme",
        actor="U1",
        conversation="C1",
        delivery_id="D1",
        native={},
        principal=principal,
    )

    activated = chat._durable_context(
        context,
        job_context=SimpleNamespace(job_id=7, trace_context="trace-1"),
        arguments={"prompt": "hello"},
        emit=emit,
    )

    assert activated.agent_request is not None
    assert activated.agent_request.principal is principal
