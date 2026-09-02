from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .core import ToolSpecification

__all__ = ["FederatedToolCatalog"]


class _FederatedToolSet:
    __slots__ = ("_routes", "specifications")

    def __init__(
        self,
        routes: Mapping[str, tuple[Any, str]],
        specifications: tuple[ToolSpecification, ...],
    ) -> None:
        self._routes = dict(routes)
        self.specifications = specifications

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        context: Any,
    ) -> object:
        try:
            selected, child_name = self._routes[name]
        except KeyError:
            raise LookupError(f"selected federated tool set does not contain {name!r}") from None
        return await selected.invoke(
            child_name,
            arguments,
            call_id=call_id,
            context=context,
        )


class FederatedToolCatalog:
    __slots__ = ("_catalogs", "_max_tools", "_separator")

    def __init__(
        self,
        namespaces: Mapping[str, Any],
        *,
        separator: str = "__",
        max_tools: int = 32,
    ) -> None:
        if not isinstance(separator, str) or not separator or separator.strip() != separator:
            raise ValueError("federated tool separator must be a non-empty trimmed string")
        if not isinstance(max_tools, int) or isinstance(max_tools, bool) or max_tools < 1:
            raise ValueError("federated tool max_tools must be a positive integer")
        catalogs: dict[str, Any] = {}
        for namespace, catalog in namespaces.items():
            _component(namespace, separator=separator, label="namespace")
            if namespace in catalogs:
                raise ValueError(f"duplicate federated tool namespace {namespace!r}")
            if not callable(getattr(catalog, "select", None)):
                raise TypeError(
                    f"federated tool namespace {namespace!r} catalog must provide select(names)"
                )
            catalogs[namespace] = catalog
        self._catalogs = catalogs
        self._separator = separator
        self._max_tools = max_tools

    def select(self, names: tuple[str, ...]) -> _FederatedToolSet:
        if len(names) > self._max_tools:
            raise ValueError(f"federated selection exceeds tool ceiling {self._max_tools}")
        parsed: list[tuple[str, str, str]] = []
        grouped: dict[str, list[str]] = {}
        seen: set[str] = set()
        for qualified in names:
            namespace, child_name = self._parse(qualified)
            if qualified in seen:
                raise ValueError("federated tool selection contains duplicates")
            seen.add(qualified)
            if namespace not in self._catalogs:
                raise LookupError(f"unknown namespace {namespace!r} in federated tool selection")
            parsed.append((qualified, namespace, child_name))
            grouped.setdefault(namespace, []).append(child_name)

        selected: dict[str, Any] = {}
        specifications: dict[str, dict[str, ToolSpecification]] = {}
        for namespace, child_names in grouped.items():
            requested = tuple(child_names)
            child_set = self._catalogs[namespace].select(requested)
            child_specs = tuple(child_set.specifications)
            by_name: dict[str, ToolSpecification] = {}
            for item in child_specs:
                child_name = getattr(item, "name", None)
                if child_name in by_name:
                    raise ValueError(
                        f"federated tool collision in namespace {namespace!r} for {child_name!r}"
                    )
                if not isinstance(child_name, str):
                    raise ValueError(f"federated tool selection drift in namespace {namespace!r}")
                by_name[child_name] = item
            if set(by_name) != set(requested):
                raise ValueError(
                    f"federated tool selection drift in namespace {namespace!r}: "
                    "child specifications must exactly match requested names"
                )
            selected[namespace] = child_set
            specifications[namespace] = by_name

        routes: dict[str, tuple[Any, str]] = {}
        qualified_specs: list[ToolSpecification] = []
        for qualified, namespace, child_name in parsed:
            routes[qualified] = (selected[namespace], child_name)
            source = specifications[namespace][child_name]
            qualified_specs.append(
                ToolSpecification(
                    qualified,
                    source.description,
                    source.input_schema,
                )
            )
        return _FederatedToolSet(routes, tuple(qualified_specs))

    def _parse(self, qualified: str) -> tuple[str, str]:
        if not isinstance(qualified, str):
            raise ValueError("federated tool names must be qualified strings")
        occurrences = qualified.count(self._separator)
        if occurrences == 0:
            raise ValueError(
                f"federated tool name {qualified!r} must be qualified with {self._separator!r}"
            )
        if occurrences > 1:
            raise ValueError(f"ambiguous federated tool name {qualified!r}")
        namespace, child_name = qualified.split(self._separator)
        if not namespace or not child_name:
            raise ValueError(
                f"federated tool name {qualified!r} must be qualified as "
                f"namespace{self._separator}tool"
            )
        _component(namespace, separator=self._separator, label="namespace")
        _component(child_name, separator=self._separator, label="tool name")
        return namespace, child_name


def _component(value: Any, *, separator: str, label: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"federated tool {label} must be a non-empty trimmed string")
    if separator in value:
        raise ValueError(f"federated tool {label} must not contain separator {separator!r}")
