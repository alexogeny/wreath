"""The generated identifier must be spelled the same by every producer.

The planner (``typegen.targets.typescript``) *declares* the per-operation
parameter interface, and the renderer (``typegen.typescript_renderer``) *references* it. Each
derives the name from the operation id independently, so a disagreement between
their two ``_pascal`` implementations emits a client that references an
interface the models module never declared -- TypeScript that does not compile,
from a generator that reported success.

That is not hypothetical: the two spellings did disagree, on any id made
entirely of separators.
"""

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
    """One name, one spelling, on both sides of the renderer boundary.

    ``typescript_renderer`` keeps its own copy because it is the reference twin of an
    optional native renderer and must stay self-contained; this asserts the copy
    still matches rather than assuming it.
    """
    assert planner_pascal(name) == renderer_pascal(name)


@pytest.mark.parametrize("name", NAMES)
def test_pascal_case_never_empties_a_non_empty_name(name: str) -> None:
    """A non-empty id must not render as an empty identifier.

    ``export interface  {`` is a syntax error, and it is what returning ``""``
    for an all-separator name produced.
    """
    if name:
        assert planner_pascal(name) != ""


@pytest.mark.parametrize("operation_id", ["_", "__", "___", "get_item"])
def test_client_only_references_interfaces_the_models_module_declares(
    operation_id: str,
) -> None:
    """End to end: every ``parameters:`` type in client.ts exists in models.ts.

    The reachable degenerate ids are the underscore runs: ``resolve_operation_ids``
    already refuses anything ``str.isidentifier`` rejects, so ``"-"`` never gets
    this far, but ``"_"`` is a perfectly valid Python identifier and does.
    """
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
