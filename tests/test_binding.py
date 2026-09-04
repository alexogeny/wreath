from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import pytest

from wreath import Wreath
from wreath.binding import (
    AppScope,
    Body,
    Cookie,
    Depends,
    Form,
    Header,
    Path,
    Query,
    ValidationError,
    compile_binder,
    compile_response_validator,
    validate,
)
from wreath.binding import (
    Field as SchemaField,
)
from wreath.orm.session import Session
from wreath.postgres import Connection, FromDatabase
from wreath.response import (
    FileResponse,
    PreparedResponse,
    Response,
    StreamingResponse,
    TextResponse,
)


@dataclass
class Item:
    name: str
    price: float
    tags: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class Order:
    item: Item
    quantity: int


@dataclass
class PublicItem:
    name: str


def test_validate_scalars() -> None:
    assert validate(int, 5) == 5
    assert validate(float, 5) == 5.0
    assert validate(float, 2.5) == 2.5
    assert validate(str, "x") == "x"
    assert validate(bool, True) is True
    assert validate(int | None, None) is None


def test_validate_rejects_bool_as_int() -> None:
    with pytest.raises(ValidationError):
        validate(int, True)


def test_validate_refuses_more_than_one_field_annotation() -> None:
    annotation = Annotated[
        int,
        SchemaField(gt=0),
        SchemaField(lt=10),
    ]

    with pytest.raises(TypeError, match="at most one Field"):
        validate(annotation, 5)


def test_validate_dataclass_defaults_and_optionals() -> None:
    item = validate(Item, {"name": "spanner", "price": 9})
    assert item == Item(name="spanner", price=9.0, tags=[], note=None)


def test_validate_nested_dataclass() -> None:
    order = validate(Order, {"item": {"name": "bolt", "price": 0.5}, "quantity": 3})
    assert order.item.name == "bolt"
    assert order.quantity == 3


def test_validate_collects_all_errors_with_locations() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate(Order, {"item": {"price": "cheap"}, "quantity": "many", "extra": 1})
    locs = {tuple(e["loc"]) for e in excinfo.value.errors}
    assert ("item", "name") in locs  # missing required
    assert ("item", "price") in locs  # wrong type
    assert ("quantity",) in locs  # wrong type
    assert ("extra",) in locs  # unexpected field


def test_validate_list_and_dict() -> None:
    assert validate(list[int], [1, 2]) == [1, 2]
    assert validate(dict[str, float], {"a": 1}) == {"a": 1.0}
    with pytest.raises(ValidationError):
        validate(list[int], [1, "two"])


class ItemState(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass
class RichBody:
    item_id: UUID
    amount: Decimal
    state: ItemState
    kind: Literal["sale"]
    due: _dt.date
    display_name: Annotated[
        str,
        SchemaField(
            alias="displayName",
            min_length=3,
            max_length=12,
            pattern=r"^[A-Z]",
            description="Public item name",
            examples=("Wreath",),
        ),
    ]
    rating: Annotated[int, SchemaField(ge=1, le=5)]
    tags: Annotated[set[str], SchemaField(min_length=1, max_length=2)]


def test_validate_rich_types_constraints_and_field_aliases() -> None:
    item_id = UUID("cbfb7892-bbe8-4d26-9c5d-e12d17f404e2")

    result = validate(
        RichBody,
        {
            "item_id": str(item_id),
            "amount": "12.340",
            "state": "active",
            "kind": "sale",
            "due": "2026-08-01",
            "displayName": "Wreath",
            "rating": 5,
            "tags": ["python", "asgi"],
        },
    )

    assert result.item_id == item_id
    assert result.amount == Decimal("12.340")
    assert result.state is ItemState.ACTIVE
    assert result.due == _dt.date(2026, 8, 1)
    assert result.display_name == "Wreath"
    assert result.tags == {"python", "asgi"}


def test_validate_reports_constraints_at_the_wire_alias() -> None:
    with pytest.raises(ValidationError) as caught:
        validate(
            RichBody,
            {
                "item_id": "not-a-uuid",
                "amount": "not-a-decimal",
                "state": "unknown",
                "kind": "refund",
                "due": "tomorrow",
                "displayName": "x",
                "rating": 9,
                "tags": [],
            },
        )

    errors = {tuple(error["loc"]): error["type"] for error in caught.value.errors}
    assert errors[("item_id",)] == "uuid"
    assert errors[("amount",)] == "decimal"
    assert errors[("state",)] == "enum"
    assert errors[("kind",)] == "literal"
    assert errors[("due",)] == "date"
    assert errors[("displayName",)] == "min_length"
    assert errors[("rating",)] == "le"
    assert errors[("tags",)] == "min_length"


@pytest.mark.asyncio
async def test_return_annotation_filters_and_serializes_the_response() -> None:
    from wreath.testing import TestClient

    app = Wreath()

    @app.get("/item")
    async def item(request: Any) -> PublicItem:
        return cast(PublicItem, {"name": "visible", "secret": "hidden"})

    async with TestClient(app) as client:
        response = await client.get("/item")

    assert response.status == 200
    assert response.json() == {"name": "visible"}


@pytest.mark.parametrize(
    "annotation",
    [
        Response,
        TextResponse,
        StreamingResponse,
        FileResponse,
        PreparedResponse,
        dict,
        list,
        tuple,
        set,
        frozenset,
    ],
)
def test_explicit_response_annotations_need_no_runtime_contract_wrapper(
    annotation: type,
) -> None:
    async def endpoint(request: Any) -> Response:
        return Response(b"ok")

    assert compile_response_validator(endpoint, annotation) is endpoint


def scope_for(path: str, method: str = "GET", query: bytes = b"") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
    }


async def call(app: Wreath, scope: dict, body: bytes = b"") -> tuple[int, bytes]:
    sent: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], payload


@pytest.mark.asyncio
async def test_typed_path_and_query_binding() -> None:
    app = Wreath()

    @app.get("/items/{item_id}")
    async def get_item(request: Any, item_id: int, verbose: bool = False, q: str = "") -> Any:
        return {"id": item_id, "verbose": verbose, "q": q}

    status, body = await call(app, scope_for("/items/42", query=b"verbose=true&q=find"))
    assert status == 200
    assert json.loads(body) == {"id": 42, "verbose": True, "q": "find"}


@pytest.mark.asyncio
async def test_invalid_path_param_is_422() -> None:
    app = Wreath()

    @app.get("/items/{item_id}")
    async def get_item(request: Any, item_id: int) -> Any:
        return {"id": item_id}

    status, body = await call(app, scope_for("/items/not-a-number"))
    assert status == 422
    errors = json.loads(body)["errors"]
    assert errors[0]["loc"] == ["path", "item_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42", 42),
        ("-42", -42),
        ("+42", 42),
        ("١٢", 12),
        ("18446744073709551616", 18446744073709551616),
    ],
)
async def test_native_path_int_keeps_python_integer_semantics(text: str, expected: int) -> None:
    app = Wreath()

    @app.get("/{item_id}")
    async def handler(request: Any, item_id: int) -> int:
        return item_id

    status, body = await call(app, scope_for(f"/{text}"))
    assert status == 200
    assert json.loads(body) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12.5", 12.5),
        ("-0.25", -0.25),
        ("1e20", 1e20),
        ("1_000.5", 1000.5),
        ("١٢.٥", 12.5),
    ],
)
async def test_native_path_float_keeps_python_number_semantics(text: str, expected: float) -> None:
    app = Wreath()

    @app.get("/{amount}")
    async def handler(request: Any, amount: float) -> float:
        return amount

    status, body = await call(app, scope_for(f"/{text}"))
    assert status == 200
    assert json.loads(body) == expected


@pytest.mark.asyncio
async def test_native_path_bool_keeps_case_and_non_ascii_semantics() -> None:
    from wreath.request import Request

    async def handler(request: Any, enabled: bool) -> bool:
        return enabled

    bound = compile_binder(handler, "/{enabled}")
    assert await bound(Request(scope_for("/TRUE"), None, {"enabled": "TRUE"})) is True
    assert await bound(Request(scope_for("/Off"), None, {"enabled": "Off"})) is False
    with pytest.raises(ValidationError) as caught:
        await bound(Request(scope_for("/invalid"), None, {"enabled": "\udcff"}))
    assert caught.value.errors[0]["type"] == "bool"

    class RequestProxy:
        path_params = {"enabled": "yes"}

    assert await bound(RequestProxy()) is True


@pytest.mark.asyncio
async def test_native_path_bool_honours_materialized_path_param_changes() -> None:
    from wreath.middleware import MiddlewareHooks

    app = Wreath()

    def rewrite_path(request: Any) -> None:
        request.path_params.update({"enabled": "false"})

    app.add_middleware(MiddlewareHooks(before_sync=rewrite_path))

    @app.get("/{enabled}")
    async def handler(request: Any, enabled: bool) -> bool:
        return enabled

    status, body = await call(app, scope_for("/true"))
    assert status == 200
    assert json.loads(body) is False


@pytest.mark.asyncio
async def test_body_dataclass_binding() -> None:
    app = Wreath()
    seen: list[Item] = []

    @app.post("/items")
    async def create(request: Any, item: Item) -> Any:
        seen.append(item)
        return {"ok": True}

    payload = json.dumps({"name": "gear", "price": 3, "tags": ["metal"]}).encode()
    status, _ = await call(app, scope_for("/items", "POST"), body=payload)
    assert status == 200
    assert seen == [Item(name="gear", price=3.0, tags=["metal"])]


@pytest.mark.asyncio
async def test_path_and_body_binding_use_the_full_binder() -> None:
    app = Wreath()

    @app.post("/items/{item_id}")
    async def update(request: Any, item_id: int, item: Item) -> Any:
        return {"id": item_id, "name": item.name}

    payload = json.dumps({"name": "gear", "price": 3}).encode()
    status, body = await call(app, scope_for("/items/42", "POST"), body=payload)

    assert status == 200
    assert json.loads(body) == {"id": 42, "name": "gear"}


@pytest.mark.asyncio
async def test_invalid_body_is_422_with_locations() -> None:
    app = Wreath()

    @app.post("/items")
    async def create(request: Any, item: Item) -> Any:
        return {"ok": True}

    status, body = await call(app, scope_for("/items", "POST"), body=b'{"price": "x"}')
    assert status == 422
    locs = {tuple(e["loc"]) for e in json.loads(body)["errors"]}
    assert ("body", "name") in locs
    assert ("body", "price") in locs


@pytest.mark.asyncio
async def test_missing_required_query_is_422() -> None:
    app = Wreath()

    @app.get("/search")
    async def search(request: Any, q: str) -> Any:
        return {"q": q}

    status, body = await call(app, scope_for("/search"))
    assert status == 422
    assert json.loads(body)["errors"][0]["loc"] == ["query", "q"]


@pytest.mark.asyncio
async def test_request_only_handler_unchanged() -> None:
    app = Wreath()

    @app.get("/plain")
    async def plain(request: Any) -> Any:
        return TextResponse("untouched")

    status, body = await call(app, scope_for("/plain"))
    assert status == 200 and body == b"untouched"


@pytest.mark.asyncio
async def test_malformed_json_body_is_400() -> None:
    app = Wreath()

    @app.post("/items")
    async def create(request: Any, item: Item) -> Any:
        return {"ok": True}

    status, _ = await call(app, scope_for("/items", "POST"), body=b"{nope")
    assert status == 400


def test_request_only_handler_skips_type_hint_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from wreath import binding

    async def handler(request: Any) -> None:
        return None

    def fail_get_type_hints(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("request-only handlers must not resolve type hints")

    monkeypatch.setattr(binding.typing, "get_type_hints", fail_get_type_hints)
    assert binding.inspect_handler(handler, "/plain") is None


@pytest.mark.asyncio
async def test_depends_resolution_and_cache() -> None:
    calls: list[str] = []

    async def get_settings(request: Any) -> dict:
        calls.append("settings")
        return {"env": "test"}

    def get_label(request: Any, settings: dict = Depends(get_settings)) -> str:
        calls.append("label")
        return settings["env"] + "-label"

    app = Wreath()

    @app.get("/wired")
    async def wired(
        request: Any,
        settings: dict = Depends(get_settings),
        label: str = Depends(get_label),
    ) -> Any:
        return {"env": settings["env"], "label": label}

    status, body = await call(app, scope_for("/wired"))
    assert status == 200
    assert json.loads(body) == {"env": "test", "label": "test-label"}
    # get_settings resolved once (cached), even though two paths need it.
    assert calls == ["settings", "label"]


@pytest.mark.asyncio
async def test_generator_dependency_cleanup_runs_on_error() -> None:
    events: list[str] = []

    async def resource(request: Any):
        events.append("open")
        try:
            yield "resource"
        finally:
            events.append("close")

    app = Wreath()

    @app.get("/boom")
    async def boom(request: Any, r: str = Depends(resource)) -> Any:
        events.append(f"handler:{r}")
        raise RuntimeError("handler failure")

    status, _ = await call(app, scope_for("/boom"))
    assert status == 500
    assert events == ["open", "handler:resource", "close"]


@pytest.mark.asyncio
async def test_resource_release_runs_every_leg_and_reraises_first_failure() -> None:
    from wreath.binding import _release

    events: list[str] = []

    class BrokenSession:
        async def close(self) -> None:
            events.append("close session")
            raise RuntimeError("session close failed")

    class BrokenDatabase:
        async def release(self, workload: str, connection: object) -> None:
            events.append(f"release {workload}")
            raise ValueError("connection release failed")

    with pytest.raises(RuntimeError, match="session close failed"):
        await _release(
            [(BrokenDatabase(), object(), "request")],
            [BrokenSession()],
        )

    assert events == ["close session", "release request"]


@pytest.mark.asyncio
async def test_depends_combines_with_typed_params() -> None:
    def get_prefix(request: Any) -> str:
        return "item"

    app = Wreath()

    @app.get("/items/{item_id}")
    async def get_item(
        request: Any, item_id: int, prefix: str = Depends(get_prefix), q: str = ""
    ) -> Any:
        return {"tag": f"{prefix}-{item_id}", "q": q}

    status, body = await call(app, scope_for("/items/7", query=b"q=x"))
    assert status == 200
    assert json.loads(body) == {"tag": "item-7", "q": "x"}


def test_circular_dependency_rejected() -> None:
    from wreath.binding import Depends, compile_binder

    def dep_a(request: Any, b: Any = None) -> Any: ...

    async def handler(request: Any, a: Any = Depends(dep_a)) -> Any: ...

    # Build the cycle after definition so each function object exists.
    def dep_b(request: Any, a: Any = Depends(dep_a)) -> Any: ...

    dep_a.__defaults__ = (Depends(dep_b),)
    dep_a.__signature__ = None  # force re-inspection

    import inspect as _inspect

    dep_a.__signature__ = _inspect.Signature(
        [
            _inspect.Parameter("request", _inspect.Parameter.POSITIONAL_OR_KEYWORD),
            _inspect.Parameter(
                "b", _inspect.Parameter.POSITIONAL_OR_KEYWORD, default=Depends(dep_b)
            ),
        ]
    )
    with pytest.raises(TypeError, match="circular"):
        compile_binder(handler, "/x")


def test_dependency_later_required_parameter_is_rejected() -> None:
    def dependency(request: Any, missing: int) -> int:
        return missing

    async def handler(request: Any, value: int = Depends(dependency)) -> int:
        return value

    with pytest.raises(TypeError, match="must be a Depends or have a default"):
        compile_binder(handler, "/x")


def _query_request(query: bytes):
    from wreath.request import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query,
        "headers": [],
    }
    return Request(scope, None, None)


async def test_query_clamp_and_default() -> None:

    async def handler(
        request: Any,
        n: Annotated[int, Query(minimum=1, maximum=500, overflow="clamp")] = 1,
    ) -> Any:
        return n

    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"")) == 1  # missing -> default
    assert await bound(_query_request(b"n=0")) == 1  # clamp to minimum
    assert await bound(_query_request(b"n=42")) == 42  # in range
    assert await bound(_query_request(b"n=99999999999999")) == 500  # clamp, no overflow


async def test_query_error_policy_rejects_out_of_range() -> None:

    async def handler(
        request: Any,
        n: Annotated[int, Query(minimum=1, maximum=10)] = 1,
    ) -> Any:
        return n

    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"n=5")) == 5
    with pytest.raises(ValidationError) as excinfo:
        await bound(_query_request(b"n=99"))
    assert excinfo.value.errors[0]["type"] == "maximum"


async def test_query_invalid_syntax_still_errors_before_clamp() -> None:

    async def handler(
        request: Any,
        n: Annotated[int, Query(minimum=1, maximum=10, overflow="clamp")] = 1,
    ) -> Any:
        return n

    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as excinfo:
        await bound(_query_request(b"n=notanint"))
    assert excinfo.value.errors[0]["type"] == "int"


async def test_exact_query_binding_keeps_first_alias_and_form_decoding() -> None:

    async def handler(
        request: Any,
        label: Annotated[str, Query(alias="display")],
        repeated: Annotated[str, Query(alias="display")],
        enabled: Annotated[bool, Query()],
        ratio: Annotated[float, Query()],
    ) -> Any:
        return label, repeated, enabled, ratio

    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError):
        await bound(
            _query_request(
                b"noise=discarded&dis%70lay=first+value&display=second&enabled=YES&ratio=1.25"
            )
        )


def test_query_bad_overflow_policy_rejected() -> None:

    with pytest.raises(ValueError):
        Query(overflow="bogus")


def test_query_range_on_non_numeric_rejected() -> None:

    async def handler(
        request: Any,
        s: Annotated[str, Query(minimum=1)] = "x",
    ) -> Any:
        return s

    with pytest.raises(TypeError):
        compile_binder(handler, "/")


def test_the_lossy_validation_error_converter_is_gone() -> None:
    import wreath.binding as binding

    assert not hasattr(binding, "validation_error_response")


@pytest.mark.asyncio
async def test_the_real_422_path_keeps_every_field_error() -> None:
    from wreath.testing import TestClient

    app = Wreath()

    @app.get("/items")
    async def items(
        request: Any,
        limit: Annotated[int, Query()],
        offset: Annotated[int, Query()],
    ) -> Any:
        return {"limit": limit, "offset": offset}

    async with TestClient(app) as client:
        response = await client.get("/items?limit=nope&offset=also-nope")

    assert response.status == 422
    body = response.json()
    assert body["detail"] == "Request validation failed"
    # The `errors` member, with a `loc` naming the source and the field, is
    # exactly what `validation_error_response()` threw away.
    assert body["errors"][0]["loc"] == ["query", "limit"]
    assert body["errors"][0]["type"] == "int"
    # And *both* bad parameters are reported, not just the first. Scalar
    # binding is fail-complete, like the body validator -- this assertion is
    # what pins that, and it read `>= 1` before the two paths agreed.
    assert [error["loc"] for error in body["errors"]] == [
        ["query", "limit"],
        ["query", "offset"],
    ]


def _module_level_dep(request: Any) -> str:
    """A dependency resolvable from module scope, which annotations require."""
    return "x"


# `wreath mutant --operators guard.remove-raise` over the whole binding suite:
# 12 killed, 0 survived, and **22 unreached**. Every refusal below could be
# deleted outright and no test would notice. Two are runtime request validation;
# the rest are declaration-time errors that exist so a silently-wrong binding
# fails at import rather than answering the caller strangely.


async def test_a_value_below_the_minimum_is_rejected_under_the_error_policy() -> None:
    async def handler(
        request: Any,
        n: Annotated[int, Query(minimum=1, maximum=10)] = 5,
    ) -> Any:
        return n

    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"n=1")) == 1  # the bound itself is fine
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=0"))
    assert caught.value.errors[0]["type"] == "minimum"
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=-7"))
    assert caught.value.errors[0]["type"] == "minimum"


async def test_a_lower_bound_alone_still_refuses() -> None:
    async def handler(
        request: Any,
        n: Annotated[int, Query(minimum=10)] = 10,
    ) -> Any:
        return n

    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"n=99999")) == 99999
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=9"))
    assert caught.value.errors[0]["type"] == "minimum"


async def test_an_annotation_binding_cannot_convert_is_a_validation_error() -> None:
    async def handler(request: Any, n: Annotated[complex, Query()] = 0j) -> Any:
        return n

    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=1"))
    assert caught.value.errors[0]["type"] == "unsupported"


def test_a_handler_cannot_bind_varargs_or_kwargs() -> None:
    async def star_args(request: Any, *args: Any) -> Any: ...
    async def star_kwargs(request: Any, **kwargs: Any) -> Any: ...

    for handler in (star_args, star_kwargs):
        with pytest.raises(TypeError, match=r"\*args/\*\*kwargs"):
            compile_binder(handler, "/")


def test_depends_inside_annotated_is_refused_with_the_fix_in_the_message() -> None:
    async def handler(
        request: Any,
        value: Annotated[str, Depends(_module_level_dep)],
    ) -> Any: ...

    with pytest.raises(TypeError, match="Depends"):
        compile_binder(handler, "/")

    # The documented spelling compiles, so the refusal is about the placement
    # rather than about `Depends` itself.
    async def correct(request: Any, value: str = Depends(_module_level_dep)) -> Any: ...

    compile_binder(correct, "/")


def test_a_bare_session_parameter_is_refused() -> None:
    async def handler(request: Any, session: Session) -> Any: ...

    with pytest.raises(TypeError, match="FromORM"):
        compile_binder(handler, "/")


def test_a_path_marker_naming_a_placeholder_the_route_lacks_is_refused() -> None:
    async def handler(request: Any, ident: Annotated[int, Path("item_id")]) -> Any: ...

    with pytest.raises(TypeError, match="not present in"):
        compile_binder(handler, "/items/{id}")
    # And the correct spelling compiles, so this is not "Path is broken".
    compile_binder(handler, "/items/{item_id}")


async def test_an_async_native_path_adapter_keeps_the_async_call_convention() -> None:
    import inspect as _inspect

    from wreath.request import Request

    async def handler(request: Any, item_id: int) -> int:
        return item_id

    bound = compile_binder(handler, "/items/{item_id}")
    request = Request(scope_for("/items/7"), None, {"item_id": "7"})

    assert _inspect.iscoroutinefunction(bound)
    assert await bound(request) == 7


def test_two_body_parameters_are_refused_in_both_spellings() -> None:
    async def explicit(
        request: Any,
        a: Annotated[Item, Body()],
        b: Annotated[Item, Body()],
    ) -> Any: ...

    async def implicit(request: Any, a: Item, b: Item) -> Any: ...

    for handler in (explicit, implicit):
        with pytest.raises(TypeError, match="two body parameters"):
            compile_binder(handler, "/")


def test_a_form_model_cannot_be_combined_or_repeated() -> None:
    async def two_models(
        request: Any,
        a: Annotated[Item, Form()],
        b: Annotated[Item, Form()],
    ) -> Any: ...

    async def model_and_field(
        request: Any,
        a: Annotated[Item, Form()],
        name: Annotated[str, Form()] = "",
    ) -> Any: ...

    with pytest.raises(TypeError, match="two form-model parameters"):
        compile_binder(two_models, "/")
    with pytest.raises(TypeError, match="form-model with individual"):
        compile_binder(model_and_field, "/")


def test_a_body_cannot_be_combined_with_form_or_file_parameters() -> None:
    async def handler(
        request: Any,
        body: Annotated[Item, Body()],
        name: Annotated[str, Form()] = "",
    ) -> Any: ...

    with pytest.raises(TypeError, match="body and form/file"):
        compile_binder(handler, "/")


@pytest.mark.asyncio
async def test_a_malformed_multipart_form_is_a_bad_request() -> None:
    app = Wreath()

    @app.post("/form")
    async def handler(request: Any, name: Annotated[str, Form()]) -> str:
        return name

    scope = scope_for("/form", "POST")
    scope["headers"] = [(b"content-type", b"multipart/form-data")]
    status, raw = await call(app, scope, body=b"not a multipart body")

    assert status == 400
    assert json.loads(raw)["detail"].startswith("invalid form body:")


def test_an_annotation_naming_something_module_scope_cannot_see_is_blamed() -> None:
    def build():
        class Local:
            pass

        async def handler(request: Any, value: Local) -> Any: ...

        return handler

    with pytest.raises(TypeError, match="unresolvable name") as caught:
        compile_binder(build(), "/")
    message = str(caught.value)
    assert "'value'" in message  # names the parameter ...
    assert "Local" in message  # ... and the name it could not resolve


@pytest.mark.parametrize(
    ("annotation", "raw", "expected_type"),
    [
        (float, b"n=notanumber", "float"),
        (float, b"n=", "float"),
        (bool, b"n=maybe", "bool"),
        (bool, b"n=2", "bool"),
        (_dt.date, b"n=2026-13-45", "date"),
        (_dt.date, b"n=not-a-date", "date"),
        (_dt.datetime, b"n=2026-07-30", "instant"),  # no offset: refused, not UTC
        (_dt.datetime, b"n=nonsense", "instant"),
    ],
)
async def test_every_scalar_converter_refuses_what_it_cannot_parse(
    annotation: Any,
    raw: bytes,
    expected_type: str,
) -> None:
    async def handler(request: Any, n: Any = None) -> Any:
        return n

    handler.__annotations__["n"] = Annotated[annotation, Query()]
    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(raw))
    assert caught.value.errors[0]["type"] == expected_type
    assert caught.value.errors[0]["loc"] == ["query", "n"]


@pytest.mark.parametrize(
    ("annotation", "raw", "expected"),
    [
        (float, b"n=1.5", 1.5),
        (bool, b"n=true", True),
        (bool, b"n=off", False),
        (_dt.date, b"n=2026-07-30", _dt.date(2026, 7, 30)),
    ],
)
async def test_every_scalar_converter_still_accepts_what_it_should(
    annotation: Any,
    raw: bytes,
    expected: Any,
) -> None:
    async def handler(request: Any, n: Any = None) -> Any:
        return n

    handler.__annotations__["n"] = Annotated[annotation, Query()]
    bound = compile_binder(handler, "/")
    assert await bound(_query_request(raw)) == expected


# A mutation sweep of `binding.py` reported these guards as `survived`: tests
# reached every one of them, and not one test could tell whether the guard was
# there. They are all in `_validate`, which is what runs for the shapes the flat
# plan cannot express -- so a hole here is a hole in the definition of what a
# valid body is.


def test_any_and_an_unannotated_parameter_both_accept_anything() -> None:
    import inspect as _inspect

    sentinel = object()
    assert validate(Any, sentinel) is sentinel
    assert validate(_inspect.Parameter.empty, sentinel) is sentinel


def test_the_null_annotation_refuses_everything_that_is_not_none() -> None:
    for annotation in (None, type(None)):
        assert validate(annotation, None) is None
        with pytest.raises(ValidationError) as caught:
            validate(annotation, 0)
        assert caught.value.errors[0]["type"] == "null"


def test_a_bool_is_not_a_number_either() -> None:
    with pytest.raises(ValidationError) as caught:
        validate(float, True)
    assert caught.value.errors[0]["type"] == "float"


def test_an_annotation_with_no_rule_is_reported_not_passed_through() -> None:
    with pytest.raises(ValidationError) as caught:
        validate(complex, 1)
    assert caught.value.errors[0]["type"] == "unsupported"


def test_a_union_without_none_does_not_accept_none() -> None:
    assert validate(int | None, None) is None
    with pytest.raises(ValidationError) as caught:
        validate(int | str, None)
    assert caught.value.errors[0]["type"] == "union"


def test_literal_matching_keeps_bool_distinct_from_int() -> None:
    assert validate(Literal[1], 1) == 1
    with pytest.raises(ValidationError) as caught:
        validate(Literal[1], True)
    assert caught.value.errors[0]["type"] == "literal"


def test_a_non_array_is_refused_as_an_array_rather_than_iterated() -> None:
    with pytest.raises(ValidationError) as caught:
        validate(list[int], "abc")
    assert [e["type"] for e in caught.value.errors] == ["list"]


def test_a_non_object_is_refused_as_an_object() -> None:
    with pytest.raises(ValidationError) as caught:
        validate(dict[str, int], "abc")
    assert [e["type"] for e in caught.value.errors] == ["dict"]


def test_an_unparameterised_container_is_reported_not_silently_accepted() -> None:
    for annotation in (list, dict):
        with pytest.raises(ValidationError) as caught:
            validate(annotation, [1, "a"])
        assert caught.value.errors[0]["type"] == "unsupported"
        assert annotation.__name__ in caught.value.errors[0]["msg"]


def _validation_bomb(depth: int) -> tuple[Any, Any]:
    """A union nest that costs `2**depth` visits, with the value that triggers it.

    Each level is a union of two dataclasses with identical shape, so a failure
    at the leaf makes every level re-explore both arms. This is the input
    `_VALIDATE_MAX_STEPS` exists for: ~200 bytes of JSON against a small
    annotation, and unbounded work.
    """
    annotation: Any = int
    for level in range(depth):
        left = dataclasses.make_dataclass(f"_BombL{level}", [("v", annotation)])
        right = dataclasses.make_dataclass(f"_BombR{level}", [("v", annotation)])
        annotation = left | right
    value: Any = "not-an-int"
    for _ in range(depth):
        value = {"v": value}
    return annotation, value


def test_a_validation_bomb_stops_at_the_step_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath.binding as binding

    # Pin the production boundary separately, then exercise the same mechanism
    # at a smaller budget. Spending two million recursive visits on every suite
    # run proved elapsed time, not a stronger invariant; a mutant that widens
    # the real constant still dies on this assertion, while one that removes a
    # budget check dies in the bounded bomb below.
    assert binding._VALIDATE_MAX_STEPS == 2_000_000
    monkeypatch.setattr(binding, "_VALIDATE_MAX_STEPS", 4_096)
    annotation, value = _validation_bomb(14)
    with pytest.raises(ValidationError) as caught:
        validate(annotation, value)
    assert [e["type"] for e in caught.value.errors] == ["too_complex"]
    assert caught.value.errors[0]["loc"] == []


async def test_an_optional_scalar_parameter_converts_through_its_one_option() -> None:
    async def handler(request: Any, n: int | None = None) -> Any:
        return n

    handler.__annotations__["n"] = Annotated[int | None, Query()]
    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"n=7")) == 7
    assert await bound(_query_request(b"")) is None
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=nope"))
    assert caught.value.errors[0]["type"] == "int"


async def test_a_union_of_two_real_options_is_still_refused() -> None:
    async def handler(request: Any, n: Any = None) -> Any:
        return n

    handler.__annotations__["n"] = Annotated[int | str, Query()]
    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=1"))
    assert caught.value.errors[0]["type"] == "unsupported"


async def test_an_any_and_an_unannotated_query_parameter_arrive_as_strings() -> None:
    async def typed(request: Any, n: Any = None) -> Any:
        return n

    typed.__annotations__["n"] = Annotated[Any, Query()]
    assert await compile_binder(typed, "/")(_query_request(b"n=raw")) == "raw"

    async def untyped(request: Any, n=None) -> Any:
        return n

    untyped.__annotations__.pop("n", None)
    untyped.__annotations__["n"] = Annotated[Any, Query()]
    assert await compile_binder(untyped, "/")(_query_request(b"n=raw")) == "raw"


async def test_route_dependencies_alone_still_produce_a_working_binder() -> None:
    ran: list[str] = []

    def audit(request: Any) -> None:
        ran.append("audit")

    async def handler(request: Any) -> Any:
        return "answered"

    assert compile_binder(handler, "/") is handler  # nothing to bind, no wrapper
    bound = compile_binder(handler, "/", dependencies=(Depends(audit),))
    assert bound is not handler
    assert await bound(_query_request(b"")) == "answered"
    assert ran == ["audit"]


def test_route_resource_binding_propagates_app_scope_to_dependency_compilation() -> None:
    async def per_request(request: Any) -> str:
        return "leaky"

    async def singleton(request: Any, inner=Depends(per_request)) -> str:
        return inner

    async def handler(request: Any) -> Any:
        return "answered"

    with pytest.raises(TypeError, match="outlive the request"):
        compile_binder(
            handler,
            "/",
            dependencies=(Depends(singleton, scope="app"),),
            app_scope=AppScope(),
        )


# All four `raise TypeError` arms below were `unreached`. They are declaration-time
# refusals -- they fire while a route compiles, not while it serves -- so the cost
# of missing one is a route that starts and then answers wrongly.


def _connection_handler(marker: Any):
    """A handler with one injected `Connection`, carrying `marker`.

    The annotation is assigned rather than written, because `from __future__ import
    annotations` defers it to a string that `binding` resolves in this module's
    globals — where a closure variable named `marker` does not exist.
    """

    async def handler(request: Any, conn: Any) -> Any: ...

    handler.__annotations__["conn"] = Annotated[Connection, marker]
    return handler


def test_a_security_read_connection_cannot_be_injected_into_a_handler() -> None:
    with pytest.raises(TypeError, match="security_read"):
        compile_binder(
            _connection_handler(FromDatabase(workload="security_read")),
            "/",
            databases={"main": object()},
        )


@pytest.mark.parametrize(
    ("databases", "label"),
    [(None, "none configured"), ({"a": object(), "b": object()}, "two configured")],
)
def test_an_unnamed_connection_needs_exactly_one_configured_database(
    databases: Any,
    label: str,
) -> None:
    with pytest.raises(TypeError, match="requires FromDatabase"):
        compile_binder(
            _connection_handler(FromDatabase()),
            "/",
            databases=databases,
        )


def test_an_unnamed_connection_resolves_when_exactly_one_is_configured() -> None:
    compile_binder(
        _connection_handler(FromDatabase()),
        "/",
        databases={"only": object()},
    )


def test_a_named_connection_that_is_not_configured_is_named_in_the_refusal() -> None:
    with pytest.raises(TypeError, match="unknown PostgreSQL database: nope"):
        compile_binder(
            _connection_handler(FromDatabase("nope")),
            "/",
            databases={"main": object()},
        )


def _mixed_request(query: bytes, headers: list[tuple[bytes, bytes]] | None = None):
    from wreath.request import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/items/7",
        "query_string": query,
        "headers": headers or [],
        "path_params": {"item_id": "not-an-int"},
    }
    request = Request(scope, None, None)
    request.path_params = {"item_id": "not-an-int"}
    return request


async def test_every_failing_parameter_is_reported_not_just_the_first() -> None:
    async def handler(
        request: Any,
        item_id: Annotated[int, Path()],
        n: Annotated[int, Query()],
        x_trace: Annotated[int, Header(alias="x-trace")],
        session: Annotated[int, Cookie(alias="session")],
    ) -> Any: ...

    bound = compile_binder(handler, "/items/{item_id}")
    request = _mixed_request(
        b"n=also-not-an-int",
        [(b"x-trace", b"nor-this"), (b"cookie", b"session=and-not-this")],
    )
    with pytest.raises(ValidationError) as caught:
        await bound(request)
    assert [(e["loc"], e["type"]) for e in caught.value.errors] == [
        (["path", "item_id"], "int"),
        (["query", "n"], "int"),
        (["header", "x-trace"], "int"),
        (["cookie", "session"], "int"),
    ]


async def test_several_missing_required_parameters_are_reported_together() -> None:
    async def handler(
        request: Any,
        n: Annotated[int, Query()],
        x_trace: Annotated[int, Header(alias="x-trace")],
        session: Annotated[int, Cookie(alias="session")],
    ) -> Any: ...

    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as caught:
        await bound(_mixed_request(b"", [(b"cookie", b"unrelated=1")]))
    assert [(e["loc"], e["type"]) for e in caught.value.errors] == [
        (["query", "n"], "missing"),
        (["header", "x-trace"], "missing"),
        (["cookie", "session"], "missing"),
    ]


# The accumulation expressions have two arms and need two different requests to hold
# both. `test_every_failing_parameter_is_reported_not_just_the_first` above supplies
# the "errors already collected" arm for query, header and cookie; these supply the
# "this is the first error" arm, where `errors` is still `None` and the else arm
# would raise `TypeError` on it. A site with only one of the two tests is a site
# where one arm can be deleted.


async def test_a_header_conversion_failure_can_be_the_first_error() -> None:
    async def handler(
        request: Any,
        x_trace: Annotated[int, Header(alias="x-trace")],
    ) -> Any: ...

    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as caught:
        await bound(_mixed_request(b"", [(b"x-trace", b"nope")]))
    assert [(e["loc"], e["type"]) for e in caught.value.errors] == [
        (["header", "x-trace"], "int"),
    ]


async def test_a_cookie_conversion_failure_can_be_the_first_error() -> None:
    async def handler(
        request: Any,
        session: Annotated[int, Cookie(alias="session")],
    ) -> Any: ...

    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as caught:
        await bound(_mixed_request(b"", [(b"cookie", b"session=nope")]))
    assert [(e["loc"], e["type"]) for e in caught.value.errors] == [
        (["cookie", "session"], "int"),
    ]


async def test_two_bad_path_parameters_are_both_reported() -> None:
    from wreath.request import Request

    async def handler(
        request: Any,
        left: Annotated[int, Path()],
        right: Annotated[int, Path()],
    ) -> Any: ...

    bound = compile_binder(handler, "/{left}/{right}")
    request = Request(
        {"type": "http", "method": "GET", "path": "/a/b", "query_string": b"", "headers": []},
        None,
        None,
    )
    request.path_params = {"left": "not-an-int", "right": "also-not"}
    with pytest.raises(ValidationError) as caught:
        await bound(request)
    assert [(e["loc"], e["type"]) for e in caught.value.errors] == [
        (["path", "left"], "int"),
        (["path", "right"], "int"),
    ]


async def test_an_absent_header_or_cookie_with_a_default_is_not_an_error() -> None:
    async def handler(
        request: Any,
        x_trace: Annotated[str, Header(alias="x-trace")] = "none",
        session: Annotated[str, Cookie(alias="session")] = "anonymous",
    ) -> Any:
        return (x_trace, session)

    bound = compile_binder(handler, "/")
    assert await bound(_mixed_request(b"", [])) == ("none", "anonymous")


# `_body_validator` compiles a body annotation into a flat plan once, and the native
# validator executes it. `_compile_plan` mirrors `_validate` "exactly" -- the
# docstring's word -- so the same shapes tested against `validate()` above have to be
# tested through a *request*, which is the only thing that runs the plan. Nothing
# had, so a plan that disagreed with the reference validator would not have shown up.


@dataclass
class _PlanShapes:
    anything: Any
    nothing: None
    items: list[int]
    labels: dict[str, str]
    maybe: int | None
    either: int | str


@dataclass
class _PlanOddField:
    weird: complex


@dataclass
class _PlanAliasedField:
    value: Annotated[int, SchemaField(alias="wireValue")]


@pytest.mark.parametrize(
    "annotation",
    [
        Decimal,
        Literal["one", "two"],
        tuple[int, int],
    ],
)
def test_native_plan_falls_back_for_shapes_it_cannot_represent(
    annotation: Any,
) -> None:
    from wreath.binding import _compile_plan, _PlanUnsupported

    with pytest.raises(_PlanUnsupported):
        _compile_plan(annotation, frozenset())


def test_native_plan_does_not_compile_an_unknown_generic_as_a_dictionary() -> None:
    from wreath.binding import _OP_UNSUPPORTED, _compile_plan

    plan = _compile_plan(Sequence[int], frozenset())

    assert plan[0] == _OP_UNSUPPORTED
    assert "unsupported annotation" in plan[1]


@pytest.mark.asyncio
async def test_uncached_nested_dependency_does_not_seed_a_later_cached_read() -> None:
    calls: list[int] = []

    def leaf(request: Any) -> int:
        calls.append(len(calls) + 1)
        return calls[-1]

    def outer(request: Any, value: int = Depends(leaf, use_cache=False)) -> int:
        return value

    async def handler(
        request: Any,
        nested: int = Depends(outer),
        direct: int = Depends(leaf),
    ) -> tuple[int, int]:
        return nested, direct

    bound = compile_binder(handler, "/")

    assert await bound(_query_request(b"")) == (1, 2)
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_cached_nested_dependency_seeds_a_later_direct_read() -> None:
    calls: list[int] = []

    def leaf(request: Any) -> int:
        calls.append(len(calls) + 1)
        return calls[-1]

    def outer(request: Any, value: int = Depends(leaf)) -> int:
        return value

    async def handler(
        request: Any,
        nested: int = Depends(outer),
        direct: int = Depends(leaf),
    ) -> tuple[int, int]:
        return nested, direct

    bound = compile_binder(handler, "/")

    assert await bound(_query_request(b"")) == (1, 1)
    assert calls == [1]


@pytest.mark.asyncio
async def test_uncached_route_dependency_runs_after_a_cached_copy() -> None:
    calls: list[int] = []

    def audit(request: Any) -> None:
        calls.append(len(calls) + 1)

    async def handler(request: Any) -> None:
        return None

    bound = compile_binder(
        handler,
        "/",
        dependencies=(Depends(audit), Depends(audit, use_cache=False)),
    )

    await bound(_query_request(b""))
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_uncached_handler_dependency_does_not_seed_a_cached_copy() -> None:
    calls: list[int] = []

    def leaf(request: Any) -> int:
        calls.append(len(calls) + 1)
        return calls[-1]

    async def handler(
        request: Any,
        first: int = Depends(leaf, use_cache=False),
        second: int = Depends(leaf),
    ) -> tuple[int, int]:
        return first, second

    bound = compile_binder(handler, "/")

    assert await bound(_query_request(b"")) == (1, 2)
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_python_query_fallback_accumulates_multiple_missing_values() -> None:
    async def handler(
        request: Any,
        first: Annotated[Literal["a"], Query()],
        second: Annotated[Literal["b"], Query()],
    ) -> None:
        return None

    bound = compile_binder(handler, "/")

    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b""))
    assert [error["loc"] for error in caught.value.errors] == [
        ["query", "first"],
        ["query", "second"],
    ]


def test_native_plan_represents_field_constraints_and_wire_aliases() -> None:
    from wreath.binding import _compile_plan

    constrained = _compile_plan(Annotated[int, SchemaField(gt=0)], frozenset())
    aliased = _compile_plan(_PlanAliasedField, frozenset())

    assert constrained[0] != 10
    assert aliased[0] != 10


def _plan_payload(**overrides: Any) -> bytes:
    body: dict[str, Any] = {
        "anything": {"free": "form"},
        "nothing": None,
        "items": [1, 2],
        "labels": {"k": "v"},
        "maybe": 5,
        "either": 7,
    }
    body.update(overrides)
    return json.dumps(body).encode()


@pytest.mark.asyncio
async def test_a_planned_body_accepts_every_shape_the_reference_validator_does() -> None:
    app = Wreath()
    seen: list[_PlanShapes] = []

    @app.post("/shapes")
    async def create(request: Any, body: _PlanShapes) -> Any:
        seen.append(body)
        return {"ok": True}

    status, _ = await call(app, scope_for("/shapes", "POST"), body=_plan_payload())
    assert status == 200
    assert seen[0].anything == {"free": "form"}
    assert seen[0].nothing is None
    assert seen[0].items == [1, 2]
    assert seen[0].labels == {"k": "v"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "field", "kind"),
    [
        ({"nothing": 0}, "nothing", "null"),
        ({"maybe": "not-an-int"}, "maybe", "union"),
        ({"either": None}, "either", "union"),
    ],
)
async def test_a_planned_body_refuses_what_the_reference_validator_refuses(
    overrides: dict[str, Any],
    field: str,
    kind: str,
) -> None:
    app = Wreath()

    @app.post("/shapes")
    async def create(request: Any, body: _PlanShapes) -> Any:
        return {"ok": True}

    status, raw = await call(app, scope_for("/shapes", "POST"), body=_plan_payload(**overrides))
    assert status == 422
    errors = json.loads(raw)["errors"]
    assert [(e["loc"], e["type"]) for e in errors] == [(["body", field], kind)]


@pytest.mark.asyncio
async def test_a_planned_body_reports_a_field_it_has_no_rule_for() -> None:
    app = Wreath()

    @app.post("/odd")
    async def create(request: Any, body: _PlanOddField) -> Any:
        return {"ok": True}

    status, raw = await call(app, scope_for("/odd", "POST"), body=json.dumps({"weird": 1}).encode())
    assert status == 422
    assert json.loads(raw)["errors"][0]["type"] == "unsupported"


@pytest.mark.asyncio
async def test_an_unannotated_path_parameter_arrives_as_a_string() -> None:
    app = Wreath()

    @app.get("/echo/{segment}")
    async def echo(request: Any, segment) -> Any:
        return {"segment": segment, "type": type(segment).__name__}

    status, body = await call(app, scope_for("/echo/42"))
    assert status == 200
    assert json.loads(body) == {"segment": "42", "type": "str"}


async def test_an_instant_query_parameter_is_parsed_and_requires_an_offset() -> None:
    from wreath.temporal import Instant

    async def handler(request: Any, at: Any = None) -> Any:
        return at

    handler.__annotations__["at"] = Annotated[Instant, Query()]
    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"at=2026-07-30T12:00:00Z")) == Instant.parse(
        "2026-07-30T12:00:00Z"
    )
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"at=2026-07-30T12:00:00"))
    assert caught.value.errors[0]["type"] == "instant"
