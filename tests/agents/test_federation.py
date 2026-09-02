from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import pytest

from wreath._agents.core import ToolSpecification
from wreath._agents.federation import FederatedToolCatalog


@dataclass
class Invocation:
    name: str
    arguments: Mapping[str, Any]
    call_id: str
    context: Any


class Selected:
    def __init__(
        self,
        names: tuple[str, ...],
        *,
        specifications: tuple[ToolSpecification, ...] | None = None,
    ) -> None:
        self.specifications = (
            tuple(ToolSpecification(name, f"Run {name}.", {"type": "object"}) for name in names)
            if specifications is None
            else specifications
        )
        self.invocations: list[Invocation] = []

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        context: Any,
    ) -> dict[str, Any]:
        self.invocations.append(Invocation(name, arguments, call_id, context))
        return {"owner": name, "call_id": call_id}


class Catalog:
    def __init__(self, specifications: tuple[ToolSpecification, ...] | None = None) -> None:
        self.selections: list[tuple[str, ...]] = []
        self.selected: list[Selected] = []
        self.specifications = specifications

    def select(self, names: tuple[str, ...]) -> Selected:
        self.selections.append(names)
        selected = Selected(names, specifications=self.specifications)
        self.selected.append(selected)
        return selected


def specification(name: str) -> ToolSpecification:
    return ToolSpecification(name, f"Run {name}.", {"type": "object"})


def test_selection_compiles_each_participating_catalog_once_and_qualifies_specs() -> None:
    local = Catalog()
    github = Catalog()
    unused = Catalog()
    catalog = FederatedToolCatalog(
        {"local": local, "github": github, "unused": unused}, max_tools=4
    )

    selected = catalog.select(("github__lookup", "local__inspect", "github__create"))

    assert github.selections == [("lookup", "create")]
    assert local.selections == [("inspect",)]
    assert unused.selections == []
    assert [item.name for item in selected.specifications] == [
        "github__lookup",
        "local__inspect",
        "github__create",
    ]
    assert selected.specifications[0].description == "Run lookup."
    assert selected.specifications[0].input_schema == {"type": "object"}


async def test_invoke_routes_directly_and_preserves_call_context_and_arguments() -> None:
    local = Catalog()
    github = Catalog()
    selected = FederatedToolCatalog({"local": local, "github": github}).select(
        ("local__lookup", "github__lookup")
    )
    arguments = {"key": "value"}
    context = object()

    result = await selected.invoke("github__lookup", arguments, call_id="call-7", context=context)

    assert result == {"owner": "lookup", "call_id": "call-7"}
    assert local.selected[0].invocations == []
    routed = github.selected[0].invocations
    assert len(routed) == 1
    assert routed[0].name == "lookup"
    assert routed[0].arguments is arguments
    assert routed[0].call_id == "call-7"
    assert routed[0].context is context


@pytest.mark.parametrize(
    ("catalogs", "separator", "max_tools", "match"),
    [
        ({"": Catalog()}, "__", 4, "namespace"),
        ({"bad__namespace": Catalog()}, "__", 4, "separator"),
        ({"local": object()}, "__", 4, "select"),
        ({"local": Catalog()}, "", 4, "separator"),
        ({"local": Catalog()}, "__", 0, "max_tools"),
    ],
)
def test_construction_refuses_ambiguous_or_unbounded_configuration(
    catalogs: Mapping[str, Any], separator: str, max_tools: int, match: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        FederatedToolCatalog(catalogs, separator=separator, max_tools=max_tools)


@pytest.mark.parametrize(
    ("names", "match"),
    [
        (("local",), "qualified"),
        (("__lookup",), "qualified"),
        (("local__",), "qualified"),
        (("local__bad__name",), "ambiguous"),
        ((cast(Any, []),), "qualified"),
        (("unknown__lookup",), "unknown namespace"),
        (("local__lookup", "local__lookup"), "duplicates"),
        (("local__one", "local__two", "local__three"), "tool ceiling"),
    ],
)
def test_selection_refuses_invalid_names_before_compiling_any_child(
    names: tuple[str, ...], match: str
) -> None:
    child = Catalog()
    catalog = FederatedToolCatalog({"local": child}, max_tools=2)

    with pytest.raises((LookupError, ValueError), match=match):
        catalog.select(names)

    assert child.selections == []


@pytest.mark.parametrize(
    ("specifications", "match"),
    [
        ((specification("other"),), "selection drift"),
        ((specification("lookup"), specification("lookup")), "collision"),
        (
            (ToolSpecification(cast(Any, 7), "Invalid.", {"type": "object"}),),
            "selection drift",
        ),
        ((), "selection drift"),
    ],
)
def test_selection_refuses_child_drift_and_collisions(
    specifications: tuple[ToolSpecification, ...], match: str
) -> None:
    child = Catalog(specifications)
    catalog = FederatedToolCatalog({"local": child})

    with pytest.raises(ValueError, match=match):
        catalog.select(("local__lookup",))

    assert child.selections == [("lookup",)]


async def test_selected_catalog_refuses_unknown_or_malformed_invocation() -> None:
    selected = FederatedToolCatalog({"local": Catalog()}).select(("local__lookup",))

    for name in ("lookup", "other__lookup", "local__other", "local__bad__name"):
        with pytest.raises(LookupError, match="selected federated tool"):
            await selected.invoke(name, {}, call_id="call", context=object())
