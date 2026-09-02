from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .._mcp.executor import ToolExecutionResult


class _SelectedMCPTools:
    __slots__ = ("_executor", "specifications")

    def __init__(self, executor: Any) -> None:
        from .core import ToolSpecification

        self._executor = executor
        self.specifications = tuple(
            ToolSpecification(
                name=specification.name,
                description=specification.description,
                input_schema=specification.input_schema,
            )
            for specification in executor.specifications
        )

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        context: Any,
    ) -> dict[str, Any]:
        result: ToolExecutionResult = await self._executor.invoke(
            name,
            arguments,
            tenant=context.tenant,
            principal=context.principal,
            delegation=context.delegation,
            call_id=call_id,
            conversation=context.conversation,
            correlation_id=context.correlation_id,
            metadata=context.metadata,
        )
        normalized: dict[str, Any] = {
            "content": [dict(block) for block in result.content],
            "isError": result.is_error,
            "effectId": result.effect_id,
        }
        if result.structured_content is not None:
            normalized["structuredContent"] = result.structured_content
        return normalized


class MCPToolCatalog:
    __slots__ = ("_max_tools", "_mcp")

    def __init__(self, mcp: Any, *, max_tools: int = 32) -> None:
        if max_tools < 1:
            raise ValueError("max_tools must be at least 1")
        self._mcp = mcp
        self._max_tools = max_tools

    def select(self, names: tuple[str, ...]) -> _SelectedMCPTools:
        return _SelectedMCPTools(self._mcp.executor(*names, max_tools=self._max_tools))


__all__ = ["MCPToolCatalog"]
