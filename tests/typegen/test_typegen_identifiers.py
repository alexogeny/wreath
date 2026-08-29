from __future__ import annotations

import re

import pytest

from wreath import Wreath
from wreath.typegen.inspect import _pascal as planner_pascal
from wreath.typegen.inspect import build_api_model
from wreath.typegen.targets.typescript import render_typescript
from wreath.typegen.typescript_renderer import _pascal as renderer_pascal

#: Ordinary names, plus the degenerate ones that split into no words at all.
NAMES = [
    "get_item",
    "getItem",
    "GetItem",
    "list_items_by_id",
    "a_b-c",
    "_leading",
    "trailing_",
    "",
    "_",
    "-",
    "__",
    "---",
    "_-_",
]


@pytest.mark.parametrize("name", NAMES)
def test_planner_and_renderer_agree_on_pascal_case(name: str) -> None:
    assert planner_pascal(name) == renderer_pascal(name)


@pytest.mark.parametrize("name", NAMES)
def test_pascal_case_never_empties_a_non_empty_name(name: str) -> None:
    if name:
        assert planner_pascal(name) != ""


@pytest.mark.parametrize("operation_id", ["_", "__", "___", "get_item"])
def test_client_only_references_interfaces_the_models_module_declares(
    operation_id: str,
) -> None:
    app = Wreath()

    @app.get("/x", operation_id=operation_id)
    async def handler(request, limit: int = 1) -> dict[str, str]:  # pragma: no cover
        ...

    files = render_typescript(build_api_model(app, allow_unknown=True))
    declared = set(re.findall(r"export interface (\S+)", files["models.ts"]))
    referenced = set(re.findall(r"parameters: (\w+)", files["client.ts"]))
    assert referenced <= declared, (
        f"client.ts references {sorted(referenced - declared)}, "
        f"which models.ts does not declare (declared: {sorted(declared)})"
    )
