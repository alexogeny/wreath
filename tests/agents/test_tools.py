from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from wreath._agents.core import (
    AgentCatalog,
    AgentInvocationContext,
    AgentProfile,
    AgentRuntime,
    ModelResponseEvent,
)
from wreath._agents.tools import MCPToolCatalog, _SelectedMCPTools
from wreath._auth.models import Identity
from wreath._mcp.executor import ToolExecutionResult
from wreath.mcp import MCP


@dataclass(frozen=True)
class InvocationContext:
    tenant: str
    principal: Any
    conversation: str
    correlation_id: str | None = None
    delegation: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


async def test_agent_catalog_adapts_shared_context_to_direct_mcp_executor() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Inspect invocation context.")
    async def inspect(request: Any, value: int) -> dict[str, Any]:
        invocation = request.state.agent_tool
        return {
            "value": value,
            "conversation": invocation.conversation,
            "metadata": invocation.metadata,
            "correlation": invocation.correlation_id,
        }

    catalog = MCPToolCatalog(mcp, max_tools=1)
    selected = catalog.select(("inspect",))
    context = InvocationContext(
        tenant="tenant-a",
        principal=Identity("user-1"),
        conversation="thread-9",
        correlation_id="trace-3",
        metadata={"job": "job-7"},
    )

    assert [(item.name, item.description) for item in selected.specifications] == [
        ("inspect", "Inspect invocation context.")
    ]
    result = await selected.invoke("inspect", {"value": 7}, call_id="step-2", context=context)

    assert result["structuredContent"] == {
        "value": 7,
        "conversation": "thread-9",
        "metadata": {"job": "job-7"},
        "correlation": "trace-3",
    }


async def test_selected_tools_omit_absent_structured_content() -> None:
    class Executor:
        specifications: tuple[object, ...] = ()

        async def invoke(self, *_args: Any, **_kwargs: Any) -> ToolExecutionResult:
            return ToolExecutionResult(
                content=({"type": "text", "text": "plain"},),
                is_error=False,
                effect_id="effect-1",
            )

    selected = _SelectedMCPTools(Executor())
    result = await selected.invoke(
        "plain",
        {},
        call_id="call-1",
        context=InvocationContext("tenant-a", Identity("user-1"), "thread-1"),
    )

    assert result == {
        "content": [{"type": "text", "text": "plain"}],
        "isError": False,
        "effectId": "effect-1",
    }


def test_agent_catalog_selection_is_a_snapshot() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="First.")
    async def first(_request: Any) -> str:
        return "first"

    catalog = MCPToolCatalog(mcp)
    selected = catalog.select(("first",))

    @mcp.tool(description="Added later.")
    async def later(_request: Any) -> str:
        return "later"

    assert [tool.name for tool in selected.specifications] == ["first"]


class Backplane:
    name = "probe"

    async def stream(self, request: Any) -> Any:
        del request
        if False:
            yield None


def test_agent_runtime_refuses_an_unknown_profile_tool_at_construction() -> None:
    mcp = MCP(name="agents", version="1")
    profile = AgentProfile(
        name="worker",
        backplane=Backplane(),
        model="probe",
        tools=("missing",),
    )

    with pytest.raises(ValueError, match="unknown MCP tool 'missing'"):
        AgentRuntime(AgentCatalog((profile,)), tools=MCPToolCatalog(mcp))


class CallingBackplane:
    name = "calling"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def stream(self, request: Any) -> Any:
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelResponseEvent.tool_call("double", "call-1", {"value": 6})
            yield ModelResponseEvent.completed()
            return
        tool_result = json.loads(request.messages[-1].content)
        yield ModelResponseEvent.text_delta(tool_result["structuredContent"]["result"])
        yield ModelResponseEvent.completed()


async def test_agent_runtime_invokes_selected_mcp_tool_without_protocol_loopback() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Double one number.")
    async def double(request: Any, value: int) -> dict[str, Any]:
        return {
            "result": str(value * 2),
            "effect": request.state.agent_tool.effect_id,
        }

    backplane = CallingBackplane()
    profile = AgentProfile(
        name="worker",
        backplane=backplane,
        model="probe",
        tools=("double",),
    )
    runtime = AgentRuntime(AgentCatalog((profile,)), tools=MCPToolCatalog(mcp))
    context = AgentInvocationContext(
        tenant="tenant-a",
        principal=Identity("user-1"),
        conversation="thread-1",
    )

    events = [event async for event in runtime.execute("go", context=context)]

    assert [event.text for event in events if event.kind == "text"] == ["12"]
    tool_result = json.loads(backplane.requests[1].messages[-1].content)
    assert tool_result["effectId"] == "mcp:8:tenant-a:6:user-1:6:call-1:double"
    assert tool_result["structuredContent"]["effect"] == tool_result["effectId"]
