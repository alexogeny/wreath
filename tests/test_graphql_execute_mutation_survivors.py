from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from wreath._graphql.ast import Field, SelectionSet
from wreath._graphql.parser import parse
from wreath._graphql.resolvers import ResolverSpec
from wreath._graphql.schema import ObjectType, RootField, Schema, SchemaField

execute_module = importlib.import_module("wreath._graphql.execute")


async def _true() -> bool:
    return True


def _schema(
    *,
    fields: dict[str, SchemaField] | None = None,
    roots: dict[str, RootField] | None = None,
    mutations: dict[str, RootField] | None = None,
) -> Schema:
    object_type = ObjectType("Thing", None, fields or {})
    return Schema(None, {"Thing": object_type}, roots or {}, mutations or {})


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    schema: Schema | None = None,
    document: Any = None,
    authorizer: Any = None,
    policy_schema: Any = None,
    on_denied: str = "error",
) -> Any:
    monkeypatch.setattr(execute_module._core, "graphql_policy_schema", lambda *_: "compiled")
    monkeypatch.setattr(execute_module._core, "graphql_policy_state", lambda value: [value])
    return execute_module._Run(
        schema or _schema(),
        document or parse("{ value }"),
        SimpleNamespace(),
        variables={},
        authorizer=authorizer,
        request="request",
        max_page_size=10,
        on_denied=on_denied,
        action="read",
        policy_schema=policy_schema,
    )


def test_policy_plan_lifetime_follows_the_authorizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled: list[Any] = []
    states: list[Any] = []
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_schema",
        lambda policies, mapper: compiled.append((policies, mapper)) or "compiled",
    )
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_state",
        lambda schema: states.append(schema) or [schema],
    )

    plain = execute_module._Run(
        _schema(),
        parse("{ value }"),
        None,
        variables={},
        authorizer=None,
        request=None,
        max_page_size=10,
        on_denied="error",
        action="read",
        policy_schema=None,
    )
    supplied = execute_module._Run(
        _schema(),
        parse("{ value }"),
        None,
        variables={},
        authorizer=SimpleNamespace(),
        request=None,
        max_page_size=10,
        on_denied="error",
        action="read",
        policy_schema="supplied",
    )
    compiled_run = execute_module._Run(
        _schema(),
        parse("{ value }"),
        None,
        variables={},
        authorizer=SimpleNamespace(),
        request=None,
        max_page_size=10,
        on_denied="error",
        action="read",
        policy_schema=None,
    )

    assert plain._policy_schema is None
    assert plain._policy_state is None
    assert supplied._policy_schema == "supplied"
    assert supplied._policy_state == ["supplied"]
    assert compiled_run._policy_schema == "compiled"
    assert compiled_run._policy_state == ["compiled"]
    assert len(compiled) == 1
    assert states == ["supplied", "compiled"]


@pytest.mark.asyncio
async def test_projection_uses_no_authorizer_when_the_plan_has_no_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Authorizer:
        async def authorize(self, *_args: Any) -> Any:
            raise AssertionError("an empty plan has nothing to authorize")

        async def _authorize_many(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("an empty plan has nothing to batch")

        async def _authorize_resources(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("an empty plan has nothing to batch")

    run = _run(monkeypatch, authorizer=Authorizer())
    monkeypatch.setattr(execute_module._core, "graphql_policy_prepare", lambda *_: "plan")
    monkeypatch.setattr(execute_module._core, "graphql_policy_resources", lambda _plan: ())
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_apply",
        lambda plan, state, decisions, stop: (
            None
            if decisions == () and stop
            else (_ for _ in ()).throw(AssertionError("wrong empty-plan decisions"))
        ),
    )
    monkeypatch.setattr(execute_module._core, "graphql_policy_result", lambda *_: 3)

    assert await run._projection_allowed(ObjectType("Thing", None, {}), [], "things") == (
        True,
        True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_count", [1, 2])
async def test_generic_batch_authorization_is_used_only_for_multiple_resources(
    monkeypatch: pytest.MonkeyPatch,
    resource_count: int,
) -> None:
    calls: list[str] = []

    class Authorizer:
        async def authorize(self, _request: Any, _requirement: Any) -> Any:
            calls.append("scalar")
            return SimpleNamespace(allowed=True)

        async def _authorize_many(
            self, _request: Any, requirements: Any, *, stop_on_denied: bool
        ) -> Any:
            calls.append(f"batch:{len(requirements)}:{stop_on_denied}")
            return tuple(SimpleNamespace(allowed=True) for _ in requirements)

    run = _run(monkeypatch, authorizer=Authorizer())
    resources = tuple(f"Thing::{index}" for index in range(resource_count))
    monkeypatch.setattr(execute_module._core, "graphql_policy_prepare", lambda *_: "plan")
    monkeypatch.setattr(execute_module._core, "graphql_policy_resources", lambda _plan: resources)
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_items",
        lambda *_: tuple((SimpleNamespace(), ()) for _ in resources),
    )
    monkeypatch.setattr(execute_module._core, "graphql_policy_apply", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_policy_result", lambda *_: 3)

    assert await run._projection_allowed(ObjectType("Thing", None, {}), [], "things") == (
        True,
        True,
    )
    assert calls == (["scalar"] if resource_count == 1 else ["batch:2:True"])


@pytest.mark.asyncio
async def test_noncallable_generic_batch_falls_back_to_scalar_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Authorizer:
        _authorize_many = None

        async def authorize(self, _request: Any, _requirement: Any) -> Any:
            calls.append("scalar")
            return SimpleNamespace(allowed=True)

    run = _run(monkeypatch, authorizer=Authorizer())
    monkeypatch.setattr(execute_module._core, "graphql_policy_prepare", lambda *_: "plan")
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_resources",
        lambda _plan: ("Thing::one", "Thing::two"),
    )
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_items",
        lambda *_: ((SimpleNamespace(), ()), (SimpleNamespace(), ())),
    )
    monkeypatch.setattr(execute_module._core, "graphql_policy_apply", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_policy_result", lambda *_: 3)

    await run._projection_allowed(ObjectType("Thing", None, {}), [], "things")

    assert calls == ["scalar", "scalar"]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_resource_batch_marks_only_the_native_engine(
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
) -> None:
    keyword_arguments: list[dict[str, Any]] = []

    class Authorizer:
        _engine = SimpleNamespace(_is_authorized_many_native=(lambda: None) if native else None)

        async def _authorize_resources(self, *_args: Any, **kwargs: Any) -> Any:
            keyword_arguments.append(kwargs)
            return (SimpleNamespace(allowed=True),)

    run = _run(monkeypatch, authorizer=Authorizer())
    monkeypatch.setattr(execute_module._core, "graphql_policy_prepare", lambda *_: "plan")
    monkeypatch.setattr(
        execute_module._core, "graphql_policy_resources", lambda _plan: ("Thing::id",)
    )
    monkeypatch.setattr(execute_module._core, "graphql_policy_apply", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_policy_result", lambda *_: 3)

    await run._projection_allowed(ObjectType("Thing", None, {}), [], "things")

    expected = {"stop_on_denied": True}
    if native:
        expected["native"] = True
    assert keyword_arguments == [expected]


@pytest.mark.asyncio
async def test_projection_denial_without_a_reason_names_the_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(monkeypatch, authorizer=SimpleNamespace())
    monkeypatch.setattr(execute_module._core, "graphql_policy_prepare", lambda *_: "plan")
    monkeypatch.setattr(execute_module._core, "graphql_policy_resources", lambda _plan: ())
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_apply",
        lambda *_: (None, ("things", "id"), "Thing.id"),
    )

    with pytest.raises(execute_module.ExecutionError) as caught:
        await run._projection_allowed(ObjectType("Thing", None, {}), [], "things")

    assert str(caught.value) == "not authorized to read Thing.id"
    assert caught.value.path == ("things", "id")


@pytest.mark.asyncio
async def test_projection_denial_preserves_the_authorizers_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(monkeypatch, authorizer=SimpleNamespace())
    monkeypatch.setattr(execute_module._core, "graphql_policy_prepare", lambda *_: "plan")
    monkeypatch.setattr(execute_module._core, "graphql_policy_resources", lambda _plan: ())
    monkeypatch.setattr(
        execute_module._core,
        "graphql_policy_apply",
        lambda *_: ("policy said no", ("things", "id"), "Thing.id"),
    )

    with pytest.raises(execute_module.ExecutionError, match="^policy said no$"):
        await run._projection_allowed(ObjectType("Thing", None, {}), [], "things")


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_resolvers_accept_synchronous_and_awaitable_results(
    monkeypatch: pytest.MonkeyPatch,
    batched: bool,
) -> None:
    run = _run(monkeypatch)
    parents = [1, 2]
    sync = ResolverSpec(
        "Thing",
        "value",
        (lambda values, _info: [value * 2 for value in values])
        if batched
        else (lambda value, _info: value * 2),
        batch=batched,
    )

    assert await run._call_resolver(sync, parents, Field("value", "value"), (), "Thing") == [
        2,
        4,
    ]


@pytest.mark.asyncio
async def test_plain_projection_is_used_only_without_authorization_or_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = Field("value", "value")
    object_type = ObjectType(
        "Thing", None, {"value": SchemaField("value", "Int", True, False, attribute="value")}
    )
    calls: list[str] = []
    monkeypatch.setattr(
        execute_module._core,
        "graphql_project_plain",
        lambda *_: calls.append("plain") or [{"value": 1}],
    )
    run = _run(monkeypatch)

    assert await run._project([SimpleNamespace(value=1)], object_type, [field], ()) == [
        {"value": 1}
    ]
    assert calls == ["plain"]


@pytest.mark.asyncio
async def test_tracing_bypasses_the_plain_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_type = ObjectType("Thing", None, {})
    run = _run(monkeypatch)
    monkeypatch.setattr(
        execute_module._core,
        "graphql_project_plain",
        lambda *_: (_ for _ in ()).throw(AssertionError("tracing needs the timed path")),
    )
    monkeypatch.setattr(execute_module._core, "graphql_new_results", lambda _instances: [])
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)
    token = execute_module._phase_marker.set(lambda *_: None)
    try:
        assert await run._project([], object_type, [], ()) == []
    finally:
        execute_module._phase_marker.reset(token)


@pytest.mark.asyncio
async def test_completed_plain_projection_does_not_enter_the_general_projector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(monkeypatch)
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: [])
    monkeypatch.setattr(
        execute_module._core,
        "graphql_new_results",
        lambda _instances: (_ for _ in ()).throw(AssertionError("plain projection completed")),
    )

    assert await run._project([], ObjectType("Thing", None, {}), [], ()) == []


@pytest.mark.asyncio
async def test_projector_orders_only_actual_resolvers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolverSpec("Thing", "computed", lambda values, _info: values)
    fields = {
        "value": SchemaField("value", "Int", True, False, attribute="value"),
        "computed": SchemaField(
            "computed", "Int", True, False, resolver=resolver, policy="Thing.computed"
        ),
    }
    captured: list[tuple[dict[str, Any], dict[str, Any]]] = []
    order_fields = execute_module.order_fields
    run = _run(monkeypatch, schema=_schema(fields=fields))
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_new_results", lambda _instances: [])
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)
    monkeypatch.setattr(
        execute_module,
        "order_fields",
        lambda selected, specs, **kwargs: captured.append((specs, kwargs)) or selected,
    )

    await run._project([], ObjectType("Thing", None, fields), [], ())

    assert len(captured) == 1
    assert captured[0][0] is fields
    assert captured[0][1] == {"type_name": "Thing", "schema_fields": True}
    selected = [Field("value", "value"), Field("computed", "computed")]
    assert order_fields(selected, captured[0][0], **captured[0][1]) == selected


@pytest.mark.asyncio
async def test_unknown_projected_field_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(monkeypatch)
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_new_results", lambda _instances: [])

    with pytest.raises(execute_module.ExecutionError, match="has no field 'missing'"):
        await run._project([], ObjectType("Thing", None, {}), [Field("missing", "missing")], ())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instances", "relationship", "loads"), [([], "rel", 0), ([1], "rel", 1), ([1], None, 0)]
)
async def test_hidden_relationship_dependencies_load_only_real_nonempty_levels(
    monkeypatch: pytest.MonkeyPatch,
    instances: list[Any],
    relationship: Any,
    loads: int,
) -> None:
    async def selected(values: Any, _info: Any) -> Any:
        return list(values)

    fields = {
        "dependency": SchemaField("dependency", "Thing", False, False, relationship=relationship),
        "computed": SchemaField(
            "computed",
            "Int",
            True,
            False,
            resolver=ResolverSpec("Thing", "computed", selected, requires=("dependency",)),
            policy="Thing.computed",
        ),
    }

    class Session:
        calls: list[Any] = []

        async def _load_relationship(self, value: Any, _parents: Any, _path: Any) -> None:
            self.calls.append(value)

    run = _run(monkeypatch, schema=_schema(fields=fields))
    run._session = Session()
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda values: [{} for _ in values]
    )
    monkeypatch.setattr(execute_module._core, "graphql_project_values", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)

    await run._project(
        instances, ObjectType("Thing", None, fields), [Field("computed", "computed")], ()
    )

    assert run._session.calls == ["rel"] * loads


@pytest.mark.asyncio
async def test_resolver_timing_uses_the_measured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(values: Any, _info: Any) -> Any:
        return list(values)

    resolver = ResolverSpec("Thing", "computed", resolve)
    fields = {
        "computed": SchemaField(
            "computed", "Int", True, False, resolver=resolver, policy="Thing.computed"
        )
    }
    durations: list[int] = []
    ticks = iter((100, 145))
    run = _run(monkeypatch, schema=_schema(fields=fields))
    monkeypatch.setattr(execute_module, "_monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda values: [{} for _ in values]
    )
    monkeypatch.setattr(execute_module._core, "graphql_project_values", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)
    token = execute_module._phase_marker.set(
        lambda _phase, _dependency, _coverage, duration: durations.append(duration)
    )
    try:
        await run._project(
            [1], ObjectType("Thing", None, fields), [Field("computed", "computed")], ()
        )
    finally:
        execute_module._phase_marker.reset(token)

    assert durations == [45]


@pytest.mark.asyncio
async def test_untraced_general_projection_does_not_read_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field_schema = SchemaField("value", "Int", True, False, attribute="value")
    run = _run(
        monkeypatch, schema=_schema(fields={"value": field_schema}), authorizer=SimpleNamespace()
    )
    monkeypatch.setattr(
        execute_module,
        "_monotonic_ns",
        lambda: (_ for _ in ()).throw(AssertionError("untraced projection has no timing cost")),
    )
    monkeypatch.setattr(execute_module._Run, "_allowed", lambda *_: _true())
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda values: [{} for _ in values]
    )
    monkeypatch.setattr(execute_module._core, "graphql_project_attribute", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)

    await run._project(
        [SimpleNamespace(value=1)],
        ObjectType("Thing", None, {"value": field_schema}),
        [Field("value", "value")],
        (),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("known_output", [False, True])
async def test_object_resolver_results_recurse_only_for_schema_types(
    monkeypatch: pytest.MonkeyPatch,
    known_output: bool,
) -> None:
    async def resolve(values: Any, _info: Any) -> Any:
        return list(values)

    resolver = ResolverSpec("Thing", "computed", resolve, type_name_out="Child")
    field_schema = SchemaField(
        "computed", "Child", False, False, resolver=resolver, policy="Thing.computed"
    )
    thing = ObjectType("Thing", None, {"computed": field_schema})
    types = {"Thing": thing}
    if known_output:
        types["Child"] = ObjectType("Child", None, {})
    run = _run(monkeypatch, schema=Schema(None, types, {}))
    recursed: list[list[Any]] = []

    async def project_children(_self: Any, values: list[Any], *_args: Any) -> list[Any]:
        recursed.append(values)
        return ["projected"]

    monkeypatch.setattr(execute_module._Run, "_project_children", project_children)
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda values: [{} for _ in values]
    )
    monkeypatch.setattr(execute_module._core, "graphql_project_values", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)

    await run._project([1], thing, [Field("computed", "computed")], ())

    assert recursed == ([[1]] if known_output else [])


@pytest.mark.asyncio
async def test_hidden_resolver_dependencies_are_computed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def dependency(parents: Any, _info: Any) -> Any:
        calls.append("dependency")
        return list(parents)

    async def selected(parents: Any, _info: Any) -> Any:
        return list(parents)

    fields = {
        "dependency": SchemaField(
            "dependency",
            "Int",
            True,
            False,
            resolver=ResolverSpec("Thing", "dependency", dependency),
            policy="Thing.dependency",
        ),
        "first": SchemaField(
            "first",
            "Int",
            True,
            False,
            resolver=ResolverSpec("Thing", "first", selected, requires=("dependency",)),
            policy="Thing.first",
        ),
        "second": SchemaField(
            "second",
            "Int",
            True,
            False,
            resolver=ResolverSpec("Thing", "second", selected, requires=("dependency",)),
            policy="Thing.second",
        ),
    }
    object_type = ObjectType("Thing", None, fields)
    run = _run(monkeypatch, schema=_schema(fields=fields))
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda instances: [{} for _ in instances]
    )
    monkeypatch.setattr(
        execute_module._core,
        "graphql_project_values",
        lambda results, key, values: [
            row.__setitem__(key, value) for row, value in zip(results, values, strict=True)
        ],
    )
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)

    projected = await run._project(
        [1], object_type, [Field("first", "first"), Field("second", "second")], ()
    )

    assert projected == [{"first": 1, "second": 1}]
    assert calls == ["dependency"]


@pytest.mark.asyncio
async def test_missing_hidden_dependency_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ResolverSpec(
        "Thing", "computed", lambda values, _info: values, requires=("missing",)
    )
    field_schema = SchemaField(
        "computed", "Int", True, False, resolver=resolver, policy="Thing.computed"
    )
    object_type = ObjectType("Thing", None, {"computed": field_schema})
    run = _run(monkeypatch, schema=Schema(None, {"Thing": object_type}, {}))
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda values: [{} for _ in values]
    )
    monkeypatch.setattr(execute_module._core, "graphql_project_values", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)

    assert await run._project([1], object_type, [Field("computed", "computed")], ()) == [{}]


@pytest.mark.asyncio
async def test_root_resolver_preserves_none_and_sync_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(monkeypatch)
    scalar = RootField(
        "value",
        "Int",
        False,
        resolver=ResolverSpec("Query", "value", lambda _info: None, batch=False),
    )
    listed = RootField(
        "values",
        "Int",
        True,
        resolver=ResolverSpec("Query", "values", lambda _info: (1, 2), batch=False),
    )

    assert await run._root_instances(scalar, Field("value", "value")) == []
    assert await run._root_instances(listed, Field("values", "values")) == [1, 2]


@pytest.mark.asyncio
async def test_relationship_requires_a_selection_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relationship = SimpleNamespace(index=0)
    field_schema = SchemaField(
        "child", "Thing", False, False, relationship=relationship, policy="Thing.child"
    )
    run = _run(monkeypatch, schema=_schema(fields={"child": field_schema}))
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda values: [{} for _ in values]
    )

    with pytest.raises(execute_module.ExecutionError, match="needs a selection set"):
        await run._project(
            [SimpleNamespace()],
            ObjectType("Thing", None, {"child": field_schema}),
            [Field("child", "child")],
            (),
        )


@pytest.mark.asyncio
async def test_empty_relationship_level_does_not_load_or_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relationship = SimpleNamespace(index=0)
    field_schema = SchemaField(
        "child", "Thing", False, False, relationship=relationship, policy="Thing.child"
    )

    class Session:
        async def _load_relationship(self, *_args: Any) -> None:
            raise AssertionError("an empty level has nothing to load")

    run = _run(monkeypatch, schema=_schema(fields={"child": field_schema}))
    run._session = Session()
    marker_calls: list[Any] = []
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_new_results", lambda _values: [])
    monkeypatch.setattr(execute_module._core, "graphql_flatten_relationship", lambda *_: ([], []))
    monkeypatch.setattr(execute_module._core, "graphql_restore_layout", lambda *_: None)
    monkeypatch.setattr(execute_module._core, "graphql_finish_results", lambda results: results)
    token = execute_module._phase_marker.set(lambda *args: marker_calls.append(args))
    try:
        await run._project(
            [],
            ObjectType("Thing", None, {"child": field_schema}),
            [Field("child", "child", selection_set=SelectionSet(()))],
            (),
        )
    finally:
        execute_module._phase_marker.reset(token)

    assert len(marker_calls) == 1
    assert marker_calls[0][1] == 0


@pytest.mark.asyncio
async def test_unknown_relationship_target_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relationship = SimpleNamespace(index=0)
    field_schema = SchemaField(
        "child", "Missing", False, False, relationship=relationship, policy="Thing.child"
    )

    class Session:
        async def _load_relationship(self, *_args: Any) -> None:
            return None

    run = _run(monkeypatch, schema=_schema(fields={"child": field_schema}))
    run._session = Session()
    monkeypatch.setattr(execute_module._core, "graphql_project_plain", lambda *_: None)
    monkeypatch.setattr(
        execute_module._core, "graphql_new_results", lambda values: [{} for _ in values]
    )

    with pytest.raises(execute_module.ExecutionError, match="unknown type 'Missing'"):
        await run._project(
            [SimpleNamespace()],
            ObjectType("Thing", None, {"child": field_schema}),
            [parse("{ child { id } }").operation().selection_set.selections[0]],
            (),
        )


@pytest.mark.asyncio
async def test_list_root_applies_offset_only_when_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, int] | tuple[str, int, int]] = []

    class Query:
        def limit(self, value: int) -> Any:
            queries.append(("limit", value))
            return self

        def offset(self, value: int) -> Any:
            queries.append(("offset", value))
            return self

    class Session:
        async def fetch(self, _query: Any) -> list[Any]:
            return []

    model = SimpleNamespace(select=lambda: Query())
    root = RootField("things", "Thing", True, spec=SimpleNamespace(model_type=model))
    run = _run(monkeypatch)
    run._session = Session()

    await run._root_instances(
        root, parse("{ things(limit: 4) }").operation().selection_set.selections[0]
    )
    await run._root_instances(
        root, parse("{ things(limit: 4, offset: 3) }").operation().selection_set.selections[0]
    )

    assert queries == [("limit", 4), ("limit", 4), ("offset", 3)]


@pytest.mark.asyncio
async def test_single_id_root_refuses_composite_primary_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = RootField(
        "thing",
        "Thing",
        False,
        spec=SimpleNamespace(
            model_type=SimpleNamespace(select=lambda: None),
            primary_key=(SimpleNamespace(), SimpleNamespace()),
        ),
    )
    run = _run(monkeypatch)

    with pytest.raises(execute_module.ExecutionError, match="composite primary key"):
        await run._root_instances(
            root, parse("{ thing(id: 1) }").operation().selection_set.selections[0]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pg_name", "identifier", "expected"),
    [("int8", "7", 7), ("uuid", "7", "7")],
)
async def test_model_root_coerces_ids_by_postgres_type(
    monkeypatch: pytest.MonkeyPatch,
    pg_name: str,
    identifier: Any,
    expected: Any,
) -> None:
    comparisons: list[Any] = []

    class Column:
        def __eq__(self, value: Any) -> Any:
            comparisons.append(value)
            return ("id", value)

    class Query:
        def where(self, condition: Any) -> Any:
            return condition

    model = SimpleNamespace(select=lambda: Query(), id=Column())
    primary = SimpleNamespace(
        python_name="id",
        pg_type=SimpleNamespace(name=pg_name, coerce=lambda value: value),
    )
    fetched: list[Any] = []

    class Session:
        async def fetch(self, query: Any) -> list[Any]:
            fetched.append(query)
            return []

    run = _run(monkeypatch)
    run._session = Session()
    run._variables = {"id": identifier}
    root = RootField(
        "thing",
        "Thing",
        False,
        spec=SimpleNamespace(model_type=model, primary_key=(primary,)),
    )

    await run._root_instances(
        root,
        parse("query($id: ID!) { thing(id: $id) { id } }").operation().selection_set.selections[0],
    )

    assert comparisons == [expected]
    assert fetched == [("id", expected)]


@pytest.mark.asyncio
@pytest.mark.parametrize("as_json", [False, True])
async def test_execute_refuses_non_served_operations(
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    document = parse("{ value }")
    operation = document.operation()
    unsupported = SimpleNamespace(
        operation=lambda _name=None: SimpleNamespace(
            operation="subscription",
            variables=operation.variables,
            selection_set=operation.selection_set,
        )
    )
    function = execute_module.execute_json if as_json else execute_module.execute

    with pytest.raises(execute_module.ExecutionError, match="only query and mutation"):
        await function(_schema(), unsupported, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("as_json", [False, True])
async def test_execute_applies_variable_defaults(
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    document = parse("query($value: Int = 7) { value }")
    captured: list[dict[str, Any]] = []

    async def run(self: Any, operation: Any, *, json_output: bool = False) -> dict[str, Any]:
        captured.append(self._variables)
        return {"value": json_output}

    monkeypatch.setattr(execute_module._Run, "run", run)
    function = execute_module.execute_json if as_json else execute_module.execute

    await function(_schema(), document, None)

    assert captured == [{"value": 7}]


@pytest.mark.asyncio
async def test_execute_json_preserves_supplied_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = parse("query($value: Int!) { value }")
    captured: list[dict[str, Any]] = []

    async def run(self: Any, operation: Any, *, json_output: bool = False) -> dict[str, Any]:
        captured.append(self._variables)
        return {}

    monkeypatch.setattr(execute_module._Run, "run", run)

    await execute_module.execute_json(_schema(), document, None, variables={"value": 9})

    assert captured == [{"value": 9}]


@pytest.mark.asyncio
async def test_optional_variable_may_be_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = parse("query($value: Int) { value }")

    async def run(self: Any, operation: Any, *, json_output: bool = False) -> dict[str, Any]:
        return self._variables

    monkeypatch.setattr(execute_module._Run, "run", run)

    assert await execute_module.execute(_schema(), document, None) == {}


@pytest.mark.asyncio
async def test_run_keeps_query_and_mutation_namespaces_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_root = RootField(
        "readValue",
        "Int",
        False,
        resolver=ResolverSpec("Query", "readValue", lambda _info: 1, batch=False),
        policy="Query.readValue",
    )
    mutation_root = RootField(
        "writeValue",
        "Int",
        False,
        resolver=ResolverSpec("Mutation", "writeValue", lambda _info: 2, batch=False),
        policy="Mutation.writeValue",
    )
    schema = _schema(roots={"readValue": query_root}, mutations={"writeValue": mutation_root})

    query_document = parse("{ readValue }")
    query_run = _run(monkeypatch, schema=schema, document=query_document)
    mutation_document = parse("mutation { writeValue }")
    mutation_run = _run(monkeypatch, schema=schema, document=mutation_document)

    assert await query_run.run(query_document.operation()) == {"readValue": 1}
    assert await mutation_run.run(mutation_document.operation()) == {"writeValue": 2}


@pytest.mark.asyncio
async def test_unknown_mutation_field_is_named_as_a_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = parse("mutation { missing }")
    run = _run(monkeypatch, document=document)

    with pytest.raises(execute_module.ExecutionError, match="unknown mutation field 'missing'"):
        await run.run(document.operation())


@pytest.mark.asyncio
async def test_root_fragments_use_the_operation_object_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = RootField(
        "writeValue",
        "Int",
        False,
        resolver=ResolverSpec("Mutation", "writeValue", lambda _info: 2, batch=False),
        policy="Mutation.writeValue",
    )
    document = parse("mutation { ... on Mutation { writeValue } }")
    run = _run(monkeypatch, schema=_schema(mutations={"writeValue": root}), document=document)

    assert await run.run(document.operation()) == {"writeValue": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("json_output", "traced", "object_type", "selection", "batch", "optimized"),
    [
        (False, False, True, True, True, False),
        (True, True, True, True, True, False),
        (True, False, False, True, True, False),
        (True, False, True, False, True, False),
        (True, False, True, True, False, False),
        (True, False, True, True, True, True),
    ],
)
async def test_native_json_authorization_requires_every_fast_path_precondition(
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
    traced: bool,
    object_type: bool,
    selection: bool,
    batch: bool,
    optimized: bool,
) -> None:
    field_schema = SchemaField("value", "Int", True, False, attribute="value")
    types = {"Thing": ObjectType("Thing", None, {"value": field_schema})} if object_type else {}
    root = RootField(
        "thing",
        "Thing" if object_type else "Int",
        False,
        resolver=ResolverSpec(
            "Query", "thing", lambda _info: SimpleNamespace(value=1), batch=False
        ),
        policy="Query.thing",
    )
    source = "{ thing { value } }" if selection else "{ thing }"
    document = parse(source)
    authorizer = SimpleNamespace(_authorize_many=(lambda: None) if batch else None)
    run = _run(
        monkeypatch,
        schema=Schema(None, types, {"thing": root}),
        document=document,
        authorizer=authorizer,
    )
    calls: list[str] = []

    async def projection_allowed(*_args: Any, **_kwargs: Any) -> tuple[bool, bool]:
        calls.append("optimized")
        return True, False

    async def allowed(*_args: Any) -> bool:
        calls.append("scalar")
        return True

    async def project(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(execute_module._Run, "_projection_allowed", projection_allowed)
    monkeypatch.setattr(execute_module._Run, "_allowed", allowed)
    monkeypatch.setattr(execute_module._Run, "_project", project)
    marker_token = execute_module._phase_marker.set(lambda *_: None) if traced else None
    try:
        await run.run(document.operation(), json_output=json_output)
    finally:
        if marker_token is not None:
            execute_module._phase_marker.reset(marker_token)

    if optimized:
        assert calls == ["optimized"]
    elif json_output and not traced and object_type and selection:
        assert calls == ["scalar", "optimized"]
    else:
        assert calls == ["scalar"]


@pytest.mark.asyncio
async def test_denied_root_does_not_run_its_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = RootField(
        "value",
        "Int",
        False,
        resolver=ResolverSpec(
            "Query",
            "value",
            lambda _info: (_ for _ in ()).throw(AssertionError("denied resolver ran")),
            batch=False,
        ),
        policy="Query.value",
    )
    document = parse("{ value }")
    run = _run(monkeypatch, schema=_schema(roots={"value": root}), document=document)

    async def denied(*_args: Any) -> bool:
        return False

    monkeypatch.setattr(execute_module._Run, "_allowed", denied)

    assert await run.run(document.operation()) == {"value": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_list", "result", "expected"),
    [(False, None, None), (False, 3, 3), (True, (), [])],
)
async def test_scalar_roots_preserve_list_and_empty_shapes(
    monkeypatch: pytest.MonkeyPatch,
    is_list: bool,
    result: Any,
    expected: Any,
) -> None:
    root = RootField(
        "value",
        "Int",
        is_list,
        resolver=ResolverSpec("Query", "value", lambda _info: result, batch=False),
        policy="Query.value",
    )
    document = parse("{ value }")
    run = _run(monkeypatch, schema=Schema(None, {}, {"value": root}), document=document)

    assert await run.run(document.operation()) == {"value": expected}


@pytest.mark.asyncio
async def test_native_json_projection_reuses_prepared_fields_and_completed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field_schema = SchemaField("value", "Int", True, False, attribute="value")
    object_type = ObjectType("Thing", None, {"value": field_schema})
    root = RootField(
        "thing",
        "Thing",
        False,
        resolver=ResolverSpec(
            "Query", "thing", lambda _info: SimpleNamespace(value=1), batch=False
        ),
        policy="Query.thing",
    )
    document = parse("{ thing { value } }")
    authorizer = SimpleNamespace(_authorize_many=lambda: None)
    run = _run(
        monkeypatch,
        schema=Schema(None, {"Thing": object_type}, {"thing": root}),
        document=document,
        authorizer=authorizer,
    )
    flattened: list[str] = []
    original_flatten = execute_module._flatten

    def flatten(*args: Any, **kwargs: Any) -> Any:
        flattened.append(args[2])
        return original_flatten(*args, **kwargs)

    async def projection_allowed(*_args: Any, **_kwargs: Any) -> tuple[bool, bool]:
        return True, True

    async def forbidden_project(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("completed native JSON projection fell back")

    monkeypatch.setattr(execute_module, "_flatten", flatten)
    monkeypatch.setattr(execute_module._Run, "_projection_allowed", projection_allowed)
    monkeypatch.setattr(execute_module._Run, "_project", forbidden_project)
    monkeypatch.setattr(execute_module._core, "graphql_project_json", lambda *_: b"projected")

    assert await run.run(document.operation(), json_output=True) == {"thing": b"projected"}
    assert flattened == ["Query", "Thing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projection_allowed", "native_projection"), [(False, b"unused"), (True, None)]
)
async def test_native_json_projection_falls_back_when_declined(
    monkeypatch: pytest.MonkeyPatch,
    projection_allowed: bool,
    native_projection: Any,
) -> None:
    field_schema = SchemaField("value", "Int", True, False, attribute="value")
    object_type = ObjectType("Thing", None, {"value": field_schema})
    root = RootField(
        "thing",
        "Thing",
        False,
        resolver=ResolverSpec(
            "Query", "thing", lambda _info: SimpleNamespace(value=1), batch=False
        ),
        policy="Query.thing",
    )
    document = parse("{ thing { value } }")
    run = _run(
        monkeypatch,
        schema=Schema(None, {"Thing": object_type}, {"thing": root}),
        document=document,
        authorizer=SimpleNamespace(_authorize_many=lambda: None),
    )
    project_json_calls: list[Any] = []

    async def allowed(*_args: Any, **_kwargs: Any) -> tuple[bool, bool]:
        return True, projection_allowed

    async def project(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [{"value": 2}]

    monkeypatch.setattr(execute_module._Run, "_projection_allowed", allowed)
    monkeypatch.setattr(execute_module._Run, "_project", project)
    monkeypatch.setattr(
        execute_module._core,
        "graphql_project_json",
        lambda *_: project_json_calls.append(True) or native_projection,
    )

    assert await run.run(document.operation(), json_output=True) == {"thing": {"value": 2}}
    assert project_json_calls == ([True] if projection_allowed else [])
