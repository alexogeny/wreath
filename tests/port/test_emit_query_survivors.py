from __future__ import annotations

import ast

import pytest

from wreath import port
from wreath._port.analyzer import DjangoImage
from wreath._port.emit.queries import _QueryPlan


class _PlanEmitter:
    rejected_predicates: set[str] = set()
    rejected_projections: set[str] = set()
    rejected_includes: set[str] = set()

    @staticmethod
    def _seg(node: ast.AST) -> str:
        return ast.unparse(node)

    def _predicate(self, model: str, keyword: ast.keyword) -> str | None:
        if keyword.arg in self.rejected_predicates:
            return None
        return f"{model}.{keyword.arg} == {self._seg(keyword.value)}"

    def _projection_pair(self, model: str, name: str) -> tuple[str, str] | None:
        if name in self.rejected_projections:
            return None
        return name, f"{name}_column"

    def _include_expression(self, model: str, path: str) -> str | None:
        if path in self.rejected_includes:
            return None
        return f"{model}.{path}.selectin()"


def _call(source: str) -> ast.Call:
    expression = ast.parse(source, mode="eval").body
    assert isinstance(expression, ast.Call)
    return expression


def _step(
    verb: str,
    source: str | None,
    *,
    emitter: _PlanEmitter | None = None,
    prepare: tuple[str, object] | None = None,
) -> tuple[bool, _QueryPlan]:
    plan = _QueryPlan("Reading")
    if prepare is not None:
        setattr(plan, *prepare)
    return plan.step(emitter or _PlanEmitter(), verb, _call(source) if source else None), plan


def test_a_finished_query_refuses_every_later_step() -> None:
    accepted, plan = _step("filter", "filter(active=True)", prepare=("runner", "fetch"))

    assert not accepted
    assert plan.wheres == []
    assert plan.runner == "fetch"


@pytest.mark.parametrize(
    ("source", "accepted"),
    [
        (None, False),
        ("get_or_create('Ada')", False),
        ("get_or_create()", False),
        ("get_or_create(**values)", False),
        ("get_or_create(defaults=values)", False),
        ("get_or_create(_defaults=values)", False),
        ("get_or_create(name__iexact='ada')", False),
        ("get_or_create(name='Ada', active=True)", True),
    ],
)
def test_get_or_create_accepts_only_literal_plain_keyword_pairs(
    source: str | None, accepted: bool
) -> None:
    result, plan = _step("get_or_create", source)

    assert result is accepted
    if accepted:
        assert plan.create_pairs == [("name", "'Ada'"), ("active", "True")]
        assert plan.wheres == ["Reading.name == 'Ada'", "Reading.active == True"]
        assert plan.write_values == ["name='Ada'", "active=True"]
        assert plan.runner == "get_or_create"
    else:
        assert plan.runner is None


def test_get_or_create_refuses_mixed_positional_and_expanded_arguments() -> None:
    positional, positional_plan = _step("get_or_create", "get_or_create(value, name='Ada')")
    expanded, expanded_plan = _step("get_or_create", "get_or_create(name='Ada', **values)")

    assert not positional and positional_plan.runner is None
    assert not expanded and expanded_plan.runner is None


@pytest.mark.parametrize(
    ("source", "accepted"),
    [
        (None, False),
        ("select_all(1)", False),
        ("select_all(extra=True)", False),
        ("select_all()", True),
    ],
)
def test_select_all_requires_one_empty_call(source: str | None, accepted: bool) -> None:
    result, plan = _step("select_all", source)

    assert result is accepted
    assert plan.runner is None


@pytest.mark.parametrize("verb", ["values", "values_list"])
def test_projection_runners_reject_unreadable_or_unresolved_fields(verb: str) -> None:
    rejected = _PlanEmitter()
    rejected.rejected_projections = {"missing"}

    result, plan = _step(verb, f"{verb}('id', 'missing')", emitter=rejected)

    assert not result
    assert plan.projection_pairs == []
    assert plan.runner is None


@pytest.mark.parametrize("verb", ["values", "values_list"])
def test_projection_runners_accept_literal_fields_and_reuse_fields(verb: str) -> None:
    result, plan = _step(verb, f"{verb}('id', 'name')")

    assert result
    assert plan.projection_pairs == [("id", "id_column"), ("name", "name_column")]
    assert plan.runner == verb

    result, reused = _step(verb, f"{verb}()", prepare=("projection_pairs", [("id", "id_column")]))
    assert result
    assert reused.projection_pairs == [("id", "id_column")]
    assert reused.runner == verb


@pytest.mark.parametrize(
    "source",
    [None, "values()", "values(name)", "values(extra=True)"],
)
def test_values_without_literal_or_preselected_fields_is_refused(source: str | None) -> None:
    result, plan = _step("values", source)

    assert not result
    assert plan.runner is None


@pytest.mark.parametrize("verb", ["values", "values_list"])
def test_preselected_projection_still_refuses_dynamic_arguments_and_keywords(verb: str) -> None:
    selected = [("id", "id_column")]
    positional, positional_plan = _step(
        verb, f"{verb}(names)", prepare=("projection_pairs", selected.copy())
    )
    keyword, keyword_plan = _step(
        verb, f"{verb}(extra=names)", prepare=("projection_pairs", selected.copy())
    )

    assert not positional and positional_plan.runner is None
    assert not keyword and keyword_plan.runner is None


def test_fields_requires_literal_resolved_names() -> None:
    rejected = _PlanEmitter()
    rejected.rejected_projections = {"missing"}

    absent, absent_plan = _step("fields", "fields(names)")
    unresolved, unresolved_plan = _step("fields", "fields('id', 'missing')", emitter=rejected)
    accepted, accepted_plan = _step("fields", "fields(['id', 'name'])")

    assert not absent and absent_plan.projection_pairs == []
    assert not unresolved and unresolved_plan.projection_pairs == []
    assert accepted
    assert accepted_plan.projection_pairs == [("id", "id_column"), ("name", "name_column")]
    assert accepted_plan.runner is None


def test_get_primary_key_shortcut_requires_one_named_keyword_and_no_arguments() -> None:
    accepted, plan = _step("get", "get(pk=identity)")
    positional, positional_plan = _step("get", "get(identity, pk=other)")
    several, several_plan = _step("get", "get(pk=identity, active=True)")

    assert accepted
    assert plan.primary_key == "identity"
    assert plan.runner == "require"
    assert not positional
    assert positional_plan.primary_key == "other"
    assert positional_plan.runner == "require"
    assert several
    assert several_plan.primary_key is None
    assert several_plan.wheres == ["Reading.pk == identity", "Reading.active == True"]
    assert several_plan.runner == "require_one"


def test_filter_refuses_a_predicate_it_cannot_spell_and_positional_arguments() -> None:
    emitter = _PlanEmitter()
    emitter.rejected_predicates = {"relation"}

    rejected, rejected_plan = _step("filter", "filter(relation=value)", emitter=emitter)
    positional, positional_plan = _step("filter", "filter(Q(active=True))")
    implicit, implicit_plan = _step("filter", None)

    assert not rejected and rejected_plan.wheres == []
    assert not positional
    assert positional_plan.runner is None
    assert implicit
    assert implicit_plan.runner is None


@pytest.mark.parametrize(
    ("verb", "runner"),
    [
        ("all", "fetch"),
        ("get_or_none", "fetch_one"),
        ("get", "require_one"),
        ("count", "count"),
        ("exists", "exists"),
    ],
)
def test_read_verbs_set_their_exact_runner(verb: str, runner: str) -> None:
    accepted, plan = _step(verb, f"{verb}(active=True)")

    assert accepted
    assert plan.wheres == ["Reading.active == True"]
    assert plan.runner == runner


@pytest.mark.parametrize("verb", ["create", "update"])
def test_write_verbs_refuse_missing_or_positional_calls(verb: str) -> None:
    absent, absent_plan = _step(verb, None)
    positional, positional_plan = _step(verb, f"{verb}(value)")

    assert not absent and absent_plan.runner is None
    assert not positional and positional_plan.runner is None


def test_create_and_update_preserve_named_and_expanded_values() -> None:
    created, create_plan = _step("create", "create(name='Ada', **values)")
    empty_create, empty_create_plan = _step("create", "create()")
    empty_update, empty_update_plan = _step("update", "update()")
    updated, update_plan = _step("update", "update(name='Ada', **values)")

    assert created and create_plan.write_values == ["name='Ada'", "**values"]
    assert create_plan.runner == "create"
    assert empty_create and empty_create_plan.write_values == []
    assert empty_create_plan.runner == "create"
    assert not empty_update and empty_update_plan.runner is None
    assert updated and update_plan.write_values == ["name='Ada'", "**values"]
    assert update_plan.runner == "update_where"


def test_update_refuses_positional_arguments_even_with_keywords() -> None:
    accepted, plan = _step("update", "update(value, name='Ada')")

    assert not accepted
    assert plan.runner is None


@pytest.mark.parametrize(
    ("source", "accepted", "orders"),
    [
        (None, False, []),
        ("order_by(name)", False, []),
        ("order_by(3)", False, []),
        ("order_by('name')", True, ["Reading.name"]),
        ("order_by('-created_at')", True, ["Reading.created_at.desc()"]),
        ("order_by(Reading.name.desc())", True, ["Reading.name.desc()"]),
    ],
)
def test_order_by_accepts_only_mechanical_order_expressions(
    source: str | None, accepted: bool, orders: list[str]
) -> None:
    result, plan = _step("order_by", source)

    assert result is accepted
    assert plan.orders == orders


@pytest.mark.parametrize(
    "source",
    [None, "first(1)", "first(extra=True)"],
)
def test_first_requires_an_empty_call_after_an_order(source: str | None) -> None:
    result, plan = _step("first", source, prepare=("orders", ["Reading.id"]))

    assert not result
    assert plan.runner is None


def test_first_requires_an_order_and_then_sets_limit_and_runner() -> None:
    unordered, unordered_plan = _step("first", "first()")
    accepted, plan = _step("first", "first()", prepare=("orders", ["Reading.id"]))

    assert not unordered and unordered_plan.limit is None
    assert accepted
    assert plan.limit == "1"
    assert plan.runner == "fetch_one"


@pytest.mark.parametrize("verb", ["select_related", "prefetch_related"])
def test_eager_loading_requires_literal_resolvable_paths(verb: str) -> None:
    rejected = _PlanEmitter()
    rejected.rejected_includes = {"owner"}

    absent, absent_plan = _step(verb, None)
    dynamic, dynamic_plan = _step(verb, f"{verb}(path)")
    unresolved, unresolved_plan = _step(verb, f"{verb}('owner')", emitter=rejected)
    accepted, plan = _step(verb, f"{verb}('owner', 'tags')")

    assert not absent and absent_plan.includes == []
    assert not dynamic and dynamic_plan.includes == []
    assert not unresolved and unresolved_plan.includes == []
    assert accepted
    assert plan.includes == ["Reading.owner.selectin()", "Reading.tags.selectin()"]


@pytest.mark.parametrize("verb", ["limit", "offset"])
def test_limit_and_offset_require_one_positional_argument(verb: str) -> None:
    for source in (None, f"{verb}()", f"{verb}(1, 2)", f"{verb}(1, extra=True)"):
        accepted, plan = _step(verb, source)
        assert not accepted
        assert getattr(plan, verb) is None

    accepted, plan = _step(verb, f"{verb}(size)")
    assert accepted
    assert getattr(plan, verb) == "size"


def test_paginate_requires_both_values_and_sets_limit_and_offset() -> None:
    for source in (None, "paginate(1)", "paginate(page=1)", "paginate(1, 2, 3)"):
        accepted, plan = _step("paginate", source)
        assert not accepted
        assert plan.limit is None and plan.offset is None

    accepted, plan = _step("paginate", "paginate(page=current, page_size=size)")
    assert accepted
    assert plan.limit == "size"
    assert plan.offset == "(current - 1) * size"


@pytest.mark.parametrize("source", [None, "delete(1)", "delete(force=True)"])
def test_delete_requires_one_empty_call(source: str | None) -> None:
    accepted, plan = _step("delete", source)

    assert not accepted
    assert plan.runner is None


def test_empty_delete_sets_the_delete_runner() -> None:
    accepted, plan = _step("delete", "delete()")

    assert accepted
    assert plan.runner == "delete_where"


def test_unknown_query_verbs_are_refused() -> None:
    accepted, plan = _step("aggregate", "aggregate(total=Count('id'))")

    assert not accepted
    assert plan.runner is None


def test_response_and_test_client_attributes_are_rewritten_by_context() -> None:
    source = (
        "import httpx\n"
        "from fastapi import FastAPI\n"
        "from fastapi.testclient import TestClient\n"
        "app = FastAPI()\n"
        "client = TestClient(app)\n"
        "async def outbound():\n"
        "    async with httpx.AsyncClient() as remote:\n"
        "        response = await remote.get('/ready')\n"
        "        return response.status_code, response.content, response.text\n"
        "def test_check():\n"
        "    response = client.get('/ready')\n"
        "    return response.status_code, response.content, response.text\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "return response.status, response.body, response.body.decode('utf-8')" in emitted
    assert emitted.count("response.status") == 2
    assert "response.status_code" not in emitted


def test_response_like_attributes_without_a_tracked_response_stay_unchanged() -> None:
    source = "def read(record):\n    return record.status_code, record.content, record.text\n"

    emitted = port.emit_module(source, opinionated=True)

    assert "return record.status_code, record.content, record.text" in emitted


def test_tracked_response_attributes_are_unchanged_without_opinionated_output() -> None:
    source = (
        "import httpx\n"
        "async def outbound():\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        response = await client.get('/ready')\n"
        "        return response.status_code, response.content, response.text\n"
    )

    emitted = port.emit_module(source, opinionated=False)

    assert "return response.status_code, response.content, response.text" in emitted


def test_query_manager_attributes_distinguish_patch_chain_and_bare_value() -> None:
    source = (
        "def inspect():\n"
        "    patch = Reading.objects.__class__\n"
        "    manager = Reading.objects\n"
        "    query = Reading.objects.filter(active=True)\n"
        "    return patch, manager, query\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert emitted.count("[orm.manager_patch]") == 1
    assert emitted.count("[orm.manager_value]") == 1
    assert "query = Reading.select().where(Reading.active == True)" in emitted
    assert "[orm.query.filter_exact]" not in emitted


def test_non_manager_attributes_are_not_claimed_as_query_chains() -> None:
    source = "def inspect():\n    return Reading.repository.filter(active=True)\n"

    emitted = port.emit_module(source, opinionated=True)

    assert "Reading.repository.filter(active=True)" in emitted
    assert "Reading.select" not in emitted


def test_called_and_bare_query_verbs_remain_distinct() -> None:
    source = (
        "def inspect():\n"
        "    verb = Reading.objects.filter\n"
        "    query = Reading.objects.filter(active=True)\n"
        "    return verb, query\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "verb = Reading.select()" in emitted
    assert "query = Reading.select().where(Reading.active == True)" in emitted


def test_bare_manager_value_is_not_lost_beside_a_claimed_chain() -> None:
    source = (
        "def inspect():\n"
        "    query = Reading.objects.filter(active=True)\n"
        "    manager = Reading.objects\n"
        "    return query, manager\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert emitted.count("[orm.manager_value]") == 1
    assert "manager = Reading.objects" in emitted


def test_django_manager_ownership_controls_query_and_value_rewrites() -> None:
    source = (
        "from django.db import models\n"
        "plain = Plain.objects\n"
        "filtered = Filtered.objects\n"
        "query = Filtered.objects.filter(active=True)\n"
    )
    context = port.TreeContext(
        django=DjangoImage(
            models=frozenset({"Plain", "Filtered"}),
            plain_models=frozenset({"Plain"}),
        )
    )

    emitted = port.emit_module(source, context, opinionated=True)

    assert "[orm.manager_value]" in emitted
    assert emitted.count("[foreign.django.query]") == 2
    assert "query = Filtered.objects.filter(active=True)" in emitted


@pytest.mark.parametrize("opinionated", [False, True])
def test_call_rewrites_respect_the_opinionated_boundary(opinionated: bool) -> None:
    source = (
        "import arrow\n"
        "import cachetools\n"
        "import httpx\n"
        "from fastapi.encoders import jsonable_encoder\n"
        "timeout = httpx.Timeout(5)\n"
        "stamp = arrow.utcnow()\n"
        "cache = cachetools.TTLCache(maxsize=8, ttl=30)\n"
        "payload = jsonable_encoder({'ok': True})\n"
    )

    emitted = port.emit_module(source, opinionated=opinionated)

    assert ("ClientTimeout(total=5)" in emitted) is opinionated
    assert "temporal.now()" in emitted
    assert "BoundedCache(max_entries=8, ttl=30)" in emitted
    assert "jsonable_encoder" not in emitted


def test_unrelated_calls_do_not_acquire_special_rewrites() -> None:
    source = (
        "class Namespace:\n"
        "    pass\n"
        "def use(value):\n"
        "    return value.json(1), value.Timeout(2), Namespace(), value.utcnow(3)\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "value.json(1)" in emitted
    assert "value.Timeout(2)" in emitted
    assert "Namespace()" in emitted
    assert "value.utcnow(3)" in emitted


def test_local_test_client_rewrite_requires_opinionated_output() -> None:
    source = (
        "from fastapi.testclient import TestClient\n"
        "def test_ready():\n"
        "    client = TestClient(app)\n"
        "    response = client.get('/ready')\n"
        "    return response.status_code\n"
    )

    conservative = port.emit_module(source, opinionated=False)
    opinionated = port.emit_module(source, opinionated=True)

    assert "client = TestClient(app)" in conservative
    assert "response = client.get('/ready')" in conservative
    assert "async with TestClient(app) as client:" in opinionated
    assert "response = await client.get('/ready')" in opinionated


def test_tracked_http_response_json_rewrite_requires_an_empty_json_call() -> None:
    source = (
        "import httpx\n"
        "async def fetch():\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        response = await client.get('/ready')\n"
        "        plain = response.json()\n"
        "        positional = response.json(encoder)\n"
        "        keyword = response.json(loads=encoder)\n"
        "        other = response.decode()\n"
        "        return plain, positional, keyword, other\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "plain = loads(response.body)" in emitted
    assert "positional = response.json(encoder)" in emitted
    assert "keyword = response.json(loads=encoder)" in emitted
    assert "other = response.decode()" in emitted


def test_settings_binding_requires_an_empty_default_initializer() -> None:
    source = (
        "from pydantic_settings import BaseSettings\n"
        "class Settings(BaseSettings):\n"
        "    host: str\n"
        "default = Settings()\n"
        "positional = Settings('localhost')\n"
        "keyword = Settings(host='localhost')\n"
        "factory = namespace.Settings()\n"
    )
    context = port.TreeContext(
        index={"pydantic": set(), "settings": {"Settings"}, "orm": set(), "orm_mixin": set()}
    )

    emitted = port.emit_module(source, context, opinionated=True)

    assert "default = Environment(read_osenv()).bind(Settings)" in emitted
    assert "positional = Settings('localhost')" in emitted
    assert "keyword = Settings(host='localhost')" in emitted
    assert "factory = namespace.Settings()" in emitted


def test_jsonable_encoder_requires_exactly_one_argument() -> None:
    source = (
        "from fastapi.encoders import jsonable_encoder\n"
        "empty = jsonable_encoder()\n"
        "plain = jsonable_encoder(value)\n"
        "several = jsonable_encoder(value, custom_encoder=encoders)\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "empty = jsonable_encoder()" in emitted
    assert "plain = value" in emitted
    assert "several = jsonable_encoder(value, custom_encoder=encoders)" in emitted


def test_arrow_and_cache_rewrites_require_both_origin_and_known_tail() -> None:
    source = (
        "import arrow\n"
        "import cachetools\n"
        "import custom\n"
        "stamp = arrow.utcnow()\n"
        "shifted = arrow.shift()\n"
        "foreign_stamp = custom.utcnow()\n"
        "cache = cachetools.TTLCache(maxsize=4, ttl=2)\n"
        "decorated = cachetools.cached(cache)\n"
        "foreign_cache = custom.TTLCache(maxsize=4)\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "stamp = temporal.now()" in emitted
    assert "shifted = arrow.shift()" in emitted
    assert "foreign_stamp = custom.utcnow()" in emitted
    assert "cache = BoundedCache(max_entries=4, ttl=2)" in emitted
    assert "decorated = cachetools.cached(cache)" in emitted
    assert "foreign_cache = custom.TTLCache(maxsize=4)" in emitted


def test_pydantic_projection_requires_the_exact_projection_shape() -> None:
    source = (
        "from ormar import Model\n"
        "class Reading(Model):\n"
        "    pass\n"
        "Exact = Reading.get_pydantic(include={'id'})\n"
        "Dynamic = Reading.get_pydantic(include=fields)\n"
        "Foreign = namespace.get_pydantic(include={'id'})\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "Exact = model_dataclass(Reading, include={'id'}, name='Exact')" in emitted
    assert "Dynamic = Reading.get_pydantic(include=fields)" in emitted
    assert "Foreign = model_dataclass(namespace, include={'id'}, name='Foreign')" in emitted


@pytest.mark.parametrize(
    "source",
    [
        "app.add_middleware(RateLimitingMiddleware)",
        "app.add_middleware(RateLimitingMiddleware, provider=Other(limit=2, timespan=3))",
        "app.add_middleware(RateLimitingMiddleware, provider=InMemoryLimitProvider(limit=2))",
        "app.add_middleware(RateLimitingMiddleware, provider=InMemoryLimitProvider(timespan=3))",
    ],
)
def test_rate_limit_rewrite_refuses_incomplete_provider_shapes(source: str) -> None:
    emitted = port.emit_module(source, opinionated=True)

    assert "app.add_middleware" in emitted
    assert "[mw.custom]" in emitted


def test_rate_limit_rewrite_preserves_receiver_options_and_block_warning() -> None:
    source = (
        "api.add_middleware(\n"
        "    RateLimitingMiddleware,\n"
        "    included_routes=routes,\n"
        "    provider=middleware.InMemoryLimitProvider(\n"
        "        limit=count, timespan=window, block_duration=blocked\n"
        "    ),\n"
        ")\n"
    )

    emitted = port.emit_module(source, opinionated=True)

    assert "api.configure_http_policy(HttpPolicy(" in emitted
    assert "RateLimitPolicy(limit=count, window=window" in emitted
    assert "request.path.startswith(prefix) for prefix in routes" in emitted
    assert "[mw.custom]" in emitted
