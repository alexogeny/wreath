"""Small porting boundaries whose two sides must stay observably different."""

from __future__ import annotations

import ast

import pytest

from wreath import port
from wreath._port.analyzer.background import is_celery_task
from wreath._port.analyzer.imports import _Imports
from wreath._port.analyzer.models import (
    _plain_graphql_dataclass,
    redundant_literal_validator,
)
from wreath._port.analyzer.orm import _index_tree
from wreath._port.analyzer.queries import plain_filter_mappings, query_rule
from wreath._port.analyzer.responses import response_class_rule
from wreath._port.analyzer.sessions import _function_query_names
from wreath._port.emit.queries import _QueryPlan


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    return {
        id(child): parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def test_called_and_bare_manager_attributes_have_different_session_needs() -> None:
    tree = ast.parse(
        "async def rows():\n"
        "    return await Llama.objects.get(id=1)\n"
        "async def manager():\n"
        "    return Llama.objects.get\n"
    )

    runs, defined = _function_query_names(
        tree,
        _Imports().visit(tree),
        {"Llama": {"id"}},
        {},
        {},
        {},
    )

    assert runs == {"rows"}
    assert defined == {"rows", "manager"}


def test_relative_local_pydantic_module_is_not_rewritten_as_the_dependency(
    tmp_path,
) -> None:
    source = tmp_path / "model.py"
    source.write_text(
        "from .pydantic import BaseModel\n"
        "class Reading(BaseModel):\n"
        "    value: int\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "from .pydantic import BaseModel" in emitted


def test_a_foreign_timeout_constructor_is_not_an_httpx_timeout_constant(
    tmp_path,
) -> None:
    source = tmp_path / "outbound.py"
    source.write_text(
        "import custom\n"
        "import httpx\n"
        "deadline = custom.Timeout(3)\n"
        "async def fetch():\n"
        "    async with httpx.AsyncClient(timeout=deadline) as client:\n"
        "        return await client.get('/ready')\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "deadline = custom.Timeout(3)" in emitted
    assert "ClientTimeout(total=deadline)" in emitted


def test_nested_non_config_class_survives_pydantic_model_rewrite(tmp_path) -> None:
    source = tmp_path / "model.py"
    source.write_text(
        "from pydantic import BaseModel\n"
        "class Envelope(BaseModel):\n"
        "    class Payload:\n"
        "        arbitrary_types_allowed = True\n"
        "    payload: Payload\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "class Payload:" in emitted
    assert "arbitrary_types_allowed = True" in emitted


def test_a_redundant_pydantic_config_class_is_removed(tmp_path) -> None:
    source = tmp_path / "model.py"
    source.write_text(
        "from pydantic import BaseModel\n"
        "class Envelope(BaseModel):\n"
        "    value: int\n"
        "    class Config:\n"
        "        orm_mode = True\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "redundant model config removed" in emitted
    assert "class Config:" not in emitted


class _SegmentEmitter:
    @staticmethod
    def _seg(node: ast.AST) -> str:
        return ast.unparse(node)


@pytest.mark.parametrize("source", ["delete(1)", "delete(force=True)"])
def test_query_plan_refuses_delete_arguments_directly(source) -> None:
    call = ast.parse(source).body[0].value
    assert isinstance(call, ast.Call)
    plan = _QueryPlan("Llama")

    assert plan.step(_SegmentEmitter(), "delete", call) is False
    assert plan.runner is None


@pytest.mark.parametrize(
    "tail",
    ["delete", "delete(1)", "delete(force=True)"],
)
def test_only_an_empty_called_delete_is_emitted(tmp_path, tail) -> None:
    source = tmp_path / "queries.py"
    source.write_text(
        "async def remove():\n"
        f"    return await Llama.objects.filter(id=1).{tail}\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert ".objects.filter(id=1)" in emitted
    assert "delete_where" not in emitted


def test_atomic_without_a_session_stays_visible_for_threading(tmp_path) -> None:
    source = tmp_path / "writes.py"
    source.write_text(
        "from django.db import transaction\n"
        "async def write():\n"
        "    with transaction.atomic():\n"
        "        await persist()\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "transaction.atomic()" in emitted
    assert "None.begin()" not in emitted
    assert "orm.transaction.atomic" in emitted


def test_a_nested_task_attribute_is_not_a_locally_bound_celery_runner() -> None:
    tree = ast.parse("@package.runner.task\ndef work(): pass\n")
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    decorator = function.decorator_list[0]

    assert not is_celery_task(
        decorator,
        "package.runner.task",
        frozenset({"runner"}),
        _Imports(),
    )


def test_literal_validator_using_in_is_not_deleted_as_a_not_in_guard() -> None:
    tree = ast.parse(
        "from typing import Literal\n"
        "from pydantic import field_validator\n"
        "class Reading:\n"
        "    grade: Literal['A', 'B']\n"
        "    @field_validator('grade')\n"
        "    def grade_is_known(value):\n"
        "        if value in {'A', 'B'}:\n"
        "            raise ValueError\n"
        "        return value\n"
    )
    owner = tree.body[2]
    assert isinstance(owner, ast.ClassDef)
    validator = owner.body[1]
    assert isinstance(validator, ast.FunctionDef)

    assert not redundant_literal_validator(
        validator,
        _parent_map(tree),
        _Imports().visit(tree),
    )


def test_plain_graphql_dataclass_rejects_a_non_docstring_expression() -> None:
    tree = ast.parse("class Reading:\n    value: int\n    marker\n")
    model = tree.body[0]
    assert isinstance(model, ast.ClassDef)

    assert not _plain_graphql_dataclass(_Imports(), model)


def test_orm_index_reports_a_file_it_cannot_parse(tmp_path) -> None:
    source = tmp_path / "broken.py"
    source.write_text("class Broken(:\n", encoding="utf-8")
    skipped: list[tuple[object, object]] = []

    _index_tree([source], lambda path, error: skipped.append((path, error)))

    assert len(skipped) == 1
    assert skipped[0][0] == source
    assert isinstance(skipped[0][1], SyntaxError)


def test_orm_index_ignores_a_relation_constructor_without_a_target(tmp_path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "import ormar\n"
        "class Reading(ormar.Model):\n"
        "    owner: int = ormar.ForeignKey()\n",
        encoding="utf-8",
    )

    indexed = _index_tree([source])

    assert indexed[2] == {"Reading": {}}


def test_named_mapping_argument_is_not_a_double_star_filter_mapping() -> None:
    tree = ast.parse(
        "def readings():\n"
        "    filters = {'grade': 'A'}\n"
        "    return query(filters=filters)\n"
    )
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    returned = function.body[-1]
    assert isinstance(returned, ast.Return)
    call = returned.value
    assert isinstance(call, ast.Call)

    assert plain_filter_mappings(call, _parent_map(tree)) == frozenset()


def test_get_or_create_refuses_a_field_outside_the_declared_model() -> None:
    call = ast.parse("get_or_create(unknown=1)").body[0].value
    assert isinstance(call, ast.Call)

    assert query_rule(
        "get_or_create",
        call,
        model="Reading",
        columns={"Reading": {"id", "grade"}},
    ) == "orm.query.get_or_create"


def test_get_or_create_accepts_keywords_when_model_columns_are_unknown() -> None:
    call = ast.parse("get_or_create(grade='A')").body[0].value
    assert isinstance(call, ast.Call)

    assert query_rule(
        "get_or_create", call, model="Reading"
    ) == "orm.query.get_or_create_exact"


def test_json_response_default_is_not_deleted_for_an_arbitrary_call() -> None:
    tree = ast.parse(
        "from starlette.responses import JSONResponse\n"
        "def reading():\n"
        "    return render_reading()\n"
    )
    function = tree.body[1]
    assert isinstance(function, ast.FunctionDef)
    response = ast.parse("JSONResponse").body[0].value
    assert isinstance(response, ast.expr)

    assert response_class_rule(
        _Imports().visit(tree), response, function
    ) == "route.response_class"


def test_a_foreign_type_decorator_is_not_a_strawberry_type(tmp_path) -> None:
    source = tmp_path / "schema.py"
    source.write_text(
        "import custom\n"
        "@custom.type\n"
        "class Reading:\n"
        "    value: int\n",
        encoding="utf-8",
    )

    assert not any(
        finding.rule_id.startswith("graphql.")
        for finding in port.analyze(source).findings
    )


def test_an_unknown_strawberry_decorator_is_not_a_graphql_type(tmp_path) -> None:
    source = tmp_path / "schema.py"
    source.write_text(
        "import strawberry\n"
        "@strawberry.experimental\n"
        "class Reading:\n"
        "    value: int\n",
        encoding="utf-8",
    )

    assert not any(
        finding.rule_id.startswith("graphql.")
        for finding in port.analyze(source).findings
    )


def test_an_extension_array_field_is_still_an_orm_column(tmp_path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "import custom\n"
        "import ormar\n"
        "class Reading(ormar.Model):\n"
        "    tags: list[str] = custom.ARRAY(str)\n",
        encoding="utf-8",
    )

    assert "orm.column" in {
        finding.rule_id for finding in port.analyze(source).findings
    }


def test_an_ormar_scalar_field_is_still_an_orm_column(tmp_path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "import ormar\n"
        "class Reading(ormar.Model):\n"
        "    grade: str = ormar.String(max_length=1)\n",
        encoding="utf-8",
    )

    assert "orm.column" in {
        finding.rule_id for finding in port.analyze(source).findings
    }


def test_httpx_timeout_with_more_than_one_keyword_is_not_flattened(tmp_path) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "import httpx\n"
        "timeout = httpx.Timeout(timeout=5, connect=1)\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "httpx.Timeout(timeout=5, connect=1)" in emitted
    assert "ClientTimeout" not in emitted


@pytest.mark.parametrize(
    "call",
    [
        "httpx.Timeout(5, timeout=6)",
        "httpx.Timeout(connect=1)",
    ],
)
def test_non_total_httpx_timeout_shapes_are_not_flattened(tmp_path, call) -> None:
    source = tmp_path / "client.py"
    source.write_text(f"import httpx\ntimeout = {call}\n", encoding="utf-8")

    emitted = port.emit_module(source, opinionated=True)

    assert call in emitted
    assert "ClientTimeout" not in emitted


def test_plain_httpx_timeout_is_flattened_to_one_total_deadline(tmp_path) -> None:
    source = tmp_path / "client.py"
    source.write_text(
        "import httpx\n"
        "timeout = httpx.Timeout(5)\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "ClientTimeout(total=5)" in emitted


def test_a_retained_pydantic_settings_alias_stays_imported(tmp_path) -> None:
    source = tmp_path / "settings.py"
    source.write_text(
        "from pydantic_settings import BaseSettings as SettingsBase\n"
        "runtime_base = SettingsBase\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "BaseSettings as SettingsBase" in emitted
    assert "runtime_base = SettingsBase" in emitted


def test_a_retained_unaliased_pydantic_settings_name_stays_imported(
    tmp_path,
) -> None:
    source = tmp_path / "settings.py"
    source.write_text(
        "from pydantic_settings import BaseSettings\n"
        "runtime_base = BaseSettings\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "from pydantic_settings import BaseSettings" in emitted
    assert "runtime_base = BaseSettings" in emitted


def test_a_plain_response_module_import_is_not_treated_as_from_import(
    tmp_path,
) -> None:
    source = tmp_path / "responses.py"
    source.write_text("import starlette.responses\n", encoding="utf-8")

    emitted = port.emit_module(source, opinionated=True)

    assert "import starlette.responses" in emitted


def test_a_response_class_from_import_is_rewritten(tmp_path) -> None:
    source = tmp_path / "responses.py"
    source.write_text(
        "from starlette.responses import PlainTextResponse\n"
        "response = PlainTextResponse('ok')\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "from wreath.response import TextResponse" in emitted
    assert "from starlette.responses" not in emitted
    assert "response = TextResponse('ok')" in emitted


def test_foreign_background_tasks_annotation_does_not_hide_created_work(
    tmp_path,
) -> None:
    source = tmp_path / "routes.py"
    source.write_text(
        "import asyncio\n"
        "import custom\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/')\n"
        "async def route(tasks: custom.BackgroundTasks):\n"
        "    asyncio.create_task(store())\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "_wreath_background.add_task(store)" in emitted


def test_non_call_decorator_beside_a_field_validator_is_preserved(tmp_path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "from pydantic import BaseModel, field_validator\n"
        "class Reading(BaseModel):\n"
        "    grade: str\n"
        "    @classmethod\n"
        "    @field_validator('grade')\n"
        "    def valid_grade(cls, value):\n"
        "        return value.strip()\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "def __post_init__(self)" in emitted
    assert "valid_grade" in emitted


def test_dynamic_ormar_constraint_is_reported_instead_of_crashing(tmp_path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "import ormar\n"
        "column = 'camera'\n"
        "class Reading(ormar.Model):\n"
        "    ormar_config = ormar.OrmarConfig(\n"
        "        constraints=[ormar.UniqueColumns(column)]\n"
        "    )\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "UniqueColumns on line" in emitted
    assert "column" in emitted


def test_non_string_ormar_constraint_is_reported_instead_of_emitted(tmp_path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "import ormar\n"
        "class Reading(ormar.Model):\n"
        "    ormar_config = ormar.OrmarConfig(\n"
        "        constraints=[ormar.UniqueColumns(1)]\n"
        "    )\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "UniqueColumns on line" in emitted
    assert "_unique_0 = unique(1)" not in emitted


def test_ormar_min_length_without_a_maximum_keeps_its_check(tmp_path) -> None:
    source = tmp_path / "models.py"
    source.write_text(
        "import ormar\n"
        "class Reading(ormar.Model):\n"
        "    code: str = ormar.String(min_length=2)\n",
        encoding="utf-8",
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "Length(minimum=2)" in emitted


def test_query_plan_accepts_a_filter_step_without_an_explicit_call() -> None:
    plan = _QueryPlan("Reading")

    assert plan.step(_SegmentEmitter(), "filter", None)


def test_query_plan_refuses_limit_with_an_extra_keyword() -> None:
    call = ast.parse("limit(1, extra=2)").body[0].value
    assert isinstance(call, ast.Call)
    plan = _QueryPlan("Reading")

    assert not plan.step(_SegmentEmitter(), "limit", call)
    assert plan.limit is None


@pytest.mark.parametrize("source", [None, "limit()", "limit(1, 2)"])
def test_query_plan_refuses_limit_without_exactly_one_argument(source) -> None:
    call = None
    if source is not None:
        parsed = ast.parse(source).body[0].value
        assert isinstance(parsed, ast.Call)
        call = parsed
    plan = _QueryPlan("Reading")

    assert not plan.step(_SegmentEmitter(), "limit", call)
    assert plan.limit is None
