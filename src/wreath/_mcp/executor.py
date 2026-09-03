from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .._auth.models import Identity
from ..binding import ValidationError
from ..request import Request
from . import record as _record
from .outbound import ClientRequestError
from .registry import Tool, ToolRegistry
from .session import ToolContext


@dataclass(frozen=True, slots=True)
class _ToolSpecification:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    tenant: str
    call_id: str
    effect_id: str
    principal: Any
    delegation: Any = None
    conversation: str = ""
    correlation_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    content: tuple[Mapping[str, Any], ...]
    is_error: bool
    effect_id: str
    structured_content: Any = None

    @property
    def text(self) -> str:
        return "".join(
            str(block.get("text", "")) for block in self.content if block.get("type") == "text"
        )


class ToolAuthorizationError(PermissionError):
    __slots__ = ()


class ToolRateLimitError(RuntimeError):
    __slots__ = ("retry_after",)

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def _empty_receive() -> dict[str, Any]:
    return {"type": "http.disconnect"}


class _DirectOutbound:
    __slots__ = ()

    async def _sample(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ClientRequestError(
            "a direct agent tool call has no MCP client session to answer sampling"
        )

    async def _elicit(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ClientRequestError(
            "a direct agent tool call has no MCP client session to answer elicitation"
        )

    async def _roots(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ClientRequestError(
            "a direct agent tool call has no MCP client session to declare roots"
        )

    async def _read_file(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ClientRequestError(
            "a direct agent tool call has no MCP client session for file access"
        )


def _identity(principal: Any, delegation: Any) -> Any:
    if principal is None:
        if delegation is not None:
            raise ValueError("delegation requires a principal")
        return None
    bind = getattr(principal, "bind", None)
    identity = bind() if callable(bind) else principal
    declared_delegation = getattr(delegation, "narrowing", delegation)
    current = getattr(identity, "narrowing", None)
    if current == declared_delegation:
        return identity
    if current is not None:
        raise ValueError("principal already carries a different delegation")
    if isinstance(identity, Identity):
        return dataclasses.replace(identity, narrowing=declared_delegation)
    raise ValueError(
        "delegation requires a Wreath Identity or Principal so it can reach authorization"
    )


def _effect_id(tenant: str, principal: str, call_id: str, tool: str) -> str:
    return (
        f"mcp:{len(tenant)}:{tenant}:{len(principal)}:{principal}:{len(call_id)}:{call_id}:{tool}"
    )


def _request(
    identity: Any,
    invocation: ToolInvocation,
    tool: Tool,
    arguments: Mapping[str, Any],
    progress: Any,
    owner: Any,
) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "agent",
            "path": "/_wreath/agents/tools",
            "raw_path": b"/_wreath/agents/tools",
            "query_string": b"",
            "headers": [],
            "client": None,
            "server": None,
        },
        _empty_receive,
        app=getattr(owner, "_app", None),
    )
    request._set_identity(identity)
    request.state.tenant = invocation.tenant
    request.state.agent_tool = invocation
    request.state.mcp = ToolContext(
        session_id=f"agent:{invocation.tenant}",
        request_id=invocation.call_id,
        tool=tool.name,
        identity=identity,
        arguments=arguments,
        progress=progress,
        _server=_DirectOutbound(),
        _request=request,
    )
    return request


class ToolExecutor:
    __slots__ = ("_owner", "_tools", "specifications", "tool_names")

    def __init__(
        self,
        owner: Any,
        registry: ToolRegistry,
        names: Sequence[str],
        *,
        max_tools: int = 32,
    ) -> None:
        if max_tools < 1:
            raise ValueError("max_tools must be at least 1")
        if len(names) > max_tools:
            raise ValueError(
                f"an agent tool catalog may select at most {max_tools} tools; got {len(names)}"
            )
        selected: dict[str, Tool] = {}
        for name in names:
            if not isinstance(name, str):
                raise ValueError("selected MCP tool names must be non-empty strings")
            if not name:
                raise ValueError("selected MCP tool names must be non-empty strings")
            if name in selected:
                raise ValueError(f"MCP tool {name!r} was selected more than once")
            tool = registry.get(name)
            if tool is None:
                raise ValueError(f"unknown MCP tool {name!r}")
            if tool.route is not None:
                raise ValueError(
                    f"MCP tool {name!r} was derived from route {tool.route!r} and "
                    "cannot be selected for direct agent calls because application "
                    "middleware and MCP endpoint authentication would not run"
                )
            if tool.sampling_requirement is not None or tool.elicitation_requirement is not None:
                raise ValueError(
                    f"MCP tool {name!r} requires a client session for sampling or "
                    "elicitation and cannot be selected for direct agent calls"
                )
            selected[name] = tool
        self._owner = owner
        self._tools = MappingProxyType(selected)
        self.tool_names = frozenset(selected)
        self.specifications = tuple(
            _ToolSpecification(tool.name, tool.description, tool.input_schema)
            for tool in selected.values()
        )

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        tenant: str,
        principal: Any,
        delegation: Any,
        call_id: str,
        conversation: str = "",
        correlation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolExecutionResult:
        if not isinstance(tenant, str) or not tenant:
            raise ValueError("tenant must be a non-empty string")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call_id must be a non-empty string")
        if not isinstance(arguments, Mapping):
            raise TypeError("tool arguments must be a mapping")
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"tool {name!r} is not in this agent's selected catalog")
        identity = _identity(principal, delegation)
        if getattr(self._owner, "_auth", None) is not None and identity is None:
            raise ToolAuthorizationError(
                "this MCP server requires authentication; a direct call must "
                "supply a verified principal"
            )
        principal_id = getattr(identity, "id", None)
        principal_key = "" if principal_id is None else str(principal_id)
        effect_id = _effect_id(tenant, principal_key, call_id, name)
        invocation = ToolInvocation(
            tenant=tenant,
            call_id=call_id,
            effect_id=effect_id,
            principal=principal,
            delegation=delegation,
            conversation=conversation,
            correlation_id=correlation_id,
            metadata={} if metadata is None else dict(metadata),
        )
        task_id = f"agent-tool:{effect_id}"
        request = _request(
            identity,
            invocation,
            tool,
            arguments,
            self._owner.progress.reporter(task_id),
            self._owner,
        )
        started = time.perf_counter()
        _record.record_arguments(arguments)

        def marker(outcome: str) -> None:
            _record.record_call(
                tool=name,
                outcome=outcome,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                principal=principal_id,
                session=f"agent:{tenant}",
            )

        limiter = tool.limiter
        if limiter is not None:
            subject = (
                "anonymous"
                if principal_id is None
                else f"principal:{len(str(principal_id))}:{principal_id}"
            )
            key = f"agent:{len(tenant)}:{tenant}:{subject}"
            retry_after = limiter.try_acquire(key, 1.0, time.monotonic())
            if retry_after > 0.0:
                self._owner.throttled += 1
                marker(_record.OUTCOME_THROTTLED)
                raise ToolRateLimitError(
                    f"tool {name!r} is rate limited for this tenant and principal",
                    retry_after,
                )
        try:
            kwargs = tool.bind(arguments)
        except ValidationError:
            self._owner.schema_rejections += 1
            marker(_record.OUTCOME_REJECTED)
            raise
        denial = await self._owner._authorize(request, tool)
        if denial is not None:
            self._owner.unauthorized_calls += 1
            marker(_record.OUTCOME_DENIED)
            raise ToolAuthorizationError(denial)
        rendered, outcome = await self._owner._invoke(tool, request, kwargs)
        marker(outcome)
        raw_content = rendered.get("content", ())
        content = tuple(raw_content)
        return ToolExecutionResult(
            content=content,
            is_error=bool(rendered.get("isError", False)),
            effect_id=effect_id,
            structured_content=rendered.get("structuredContent"),
        )


__all__ = [
    "ToolAuthorizationError",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolInvocation",
    "ToolRateLimitError",
]
