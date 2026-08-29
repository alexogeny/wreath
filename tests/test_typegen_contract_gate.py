from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import pytest

from wreath import Wreath
from wreath.binding import Query
from wreath.openapi import generate_openapi
from wreath.typegen.cli import TypegenOptions, check_contract, run


@dataclass
class Llama:
    id: int
    name: str


def _v1() -> Wreath:
    app = Wreath()

    @app.get("/llamas/{llama_id}")
    async def get_llama(request: Any, llama_id: int) -> Llama:
        return Llama(id=llama_id, name="Bo")

    return app


def _v2_compatible() -> Wreath:
    """Adds an operation. OpenAPI says an addition is compatible."""
    app = _v1()

    @app.get("/herds")
    async def list_herds(request: Any) -> Llama:
        return Llama(id=1, name="north")

    return app


def _v2_breaking() -> Wreath:
    """A newly required query parameter: existing callers stop working.

    No default, so it is genuinely required -- `Query[str] = None` renders as
    optional and would have made this fixture assert nothing.
    """
    app = Wreath()

    @app.get("/llamas/{llama_id}")
    async def get_llama(request: Any, llama_id: int, tenant: Annotated[str, Query()]) -> Llama:
        return Llama(id=llama_id, name="Bo")

    return app


def _generate(app: Wreath, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    assert (
        run(app, TypegenOptions(target="python", output=str(into), class_name="LlamaClient")) == 0
    )


def test_the_generated_package_retains_the_document_it_was_built_from(tmp_path) -> None:
    _generate(_v1(), tmp_path / "pkg")
    assert (tmp_path / "pkg" / "spec.json").exists()


def test_the_pinned_document_is_the_providers_document(tmp_path) -> None:
    import json

    _generate(_v1(), tmp_path / "pkg")
    pinned = json.loads((tmp_path / "pkg" / "spec.json").read_text())
    assert pinned == generate_openapi(_v1())


def test_an_unchanged_provider_reports_no_changes(tmp_path) -> None:
    _generate(_v1(), tmp_path / "pkg")
    assert check_contract(generate_openapi(_v1()), tmp_path / "pkg") == ()


def test_a_compatible_change_is_not_breaking(tmp_path) -> None:
    _generate(_v1(), tmp_path / "pkg")
    assert check_contract(generate_openapi(_v2_compatible()), tmp_path / "pkg") == ()


def test_a_breaking_change_is_reported(tmp_path) -> None:
    _generate(_v1(), tmp_path / "pkg")
    changes = check_contract(generate_openapi(_v2_breaking()), tmp_path / "pkg")
    assert changes, "a newly required parameter must be reported"
    assert any("required" in change.kind for change in changes), changes


def test_the_cli_exits_non_zero_on_a_breaking_change(tmp_path) -> None:
    _generate(_v1(), tmp_path / "pkg")
    options = TypegenOptions(
        target="python",
        output=str(tmp_path / "pkg"),
        class_name="LlamaClient",
        check_contract=True,
    )
    assert run(_v2_breaking(), options) == 1


def test_the_cli_exits_zero_on_a_compatible_change(tmp_path) -> None:
    _generate(_v1(), tmp_path / "pkg")
    options = TypegenOptions(
        target="python",
        output=str(tmp_path / "pkg"),
        class_name="LlamaClient",
        check_contract=True,
    )
    assert run(_v2_compatible(), options) == 0


def test_the_gate_refuses_when_there_is_nothing_pinned(tmp_path) -> None:
    from wreath.typegen.cli import TypegenCliError

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TypegenCliError):
        check_contract(generate_openapi(_v1()), empty)
