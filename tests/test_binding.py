"""Typed handler binding and validation tests."""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Annotated, Any

import pytest

from wreath import Wreath
from wreath.binding import (
    Body,
    Depends,
    Form,
    Path,
    Query,
    ValidationError,
    compile_binder,
    validate,
)
from wreath.orm.session import Session
from wreath.response import TextResponse


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


# --- validate() -----------------------------------------------------------------


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
    assert ("item", "name") in locs        # missing required
    assert ("item", "price") in locs       # wrong type
    assert ("quantity",) in locs           # wrong type
    assert ("extra",) in locs              # unexpected field


def test_validate_list_and_dict() -> None:
    assert validate(list[int], [1, 2]) == [1, 2]
    assert validate(dict[str, float], {"a": 1}) == {"a": 1.0}
    with pytest.raises(ValidationError):
        validate(list[int], [1, "two"])


# --- handler binding through the app ---------------------------------------------


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


# --- dependency injection ---------------------------------------------------------


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


# --- Query numeric constraints (overflow policies) ------------------------------


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
    """`binding.validation_error_response()` was dead and lossy at once.

    Nothing called it, it was not in `__all__`, and what it produced -- an
    `UnprocessableEntity` whose detail was the string "N validation error(s)" --
    dropped the per-field `errors` list that the real 422 path carries. A public
    helper that is strictly worse than the path the framework actually takes is
    a trap for the first caller who finds it, so it was removed rather than
    fixed: there is nowhere for the field list to go on an `HTTPException`.
    """
    import wreath.binding as binding

    assert not hasattr(binding, "validation_error_response")


@pytest.mark.asyncio
async def test_the_real_422_path_keeps_every_field_error() -> None:
    """What the removed helper dropped, and the reason nothing should use it."""
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


# --- the refusals nothing had ever made fire ----------------------------------
#
# `wreath mutant --operators guard.remove-raise` over the whole binding suite:
# 12 killed, 0 survived, and **22 unreached**. Every refusal below could be
# deleted outright and no test would notice. Two are runtime request validation;
# the rest are declaration-time errors that exist so a silently-wrong binding
# fails at import rather than answering the caller strangely.


async def test_a_value_below_the_minimum_is_rejected_under_the_error_policy() -> None:
    """The lower bound, which was only ever exercised with `overflow="clamp"`.

    `test_query_error_policy_rejects_out_of_range` refuses `n=99` against a
    maximum; nothing refused anything against a *minimum*, so half the range
    check was unwatched. `?page=-1` is the request that finds it.
    """
    async def handler(
        request: Any,
        n: Annotated[int, Query(minimum=1, maximum=10)] = 5,
    ) -> Any:
        return n

    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"n=1")) == 1        # the bound itself is fine
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=0"))
    assert caught.value.errors[0]["type"] == "minimum"
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=-7"))
    assert caught.value.errors[0]["type"] == "minimum"


async def test_a_lower_bound_alone_still_refuses() -> None:
    """No maximum at all, so the two clauses cannot cover for each other."""
    async def handler(
        request: Any, n: Annotated[int, Query(minimum=10)] = 10,
    ) -> Any:
        return n

    bound = compile_binder(handler, "/")
    assert await bound(_query_request(b"n=99999")) == 99999
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=9"))
    assert caught.value.errors[0]["type"] == "minimum"


async def test_an_annotation_binding_cannot_convert_is_a_validation_error() -> None:
    """A scalar source with a type the converter has no rule for.

    Reported against the parameter rather than raised as a `TypeError`, because
    it is discovered while converting a value the caller sent.
    """
    async def handler(request: Any, n: Annotated[complex, Query()] = 0j) -> Any:
        return n

    bound = compile_binder(handler, "/")
    with pytest.raises(ValidationError) as caught:
        await bound(_query_request(b"n=1"))
    assert caught.value.errors[0]["type"] == "unsupported"


# --- declaration-time refusals ------------------------------------------------


def test_a_handler_cannot_bind_varargs_or_kwargs() -> None:
    """There is no request source that could fill them."""
    async def star_args(request: Any, *args: Any) -> Any: ...
    async def star_kwargs(request: Any, **kwargs: Any) -> Any: ...

    for handler in (star_args, star_kwargs):
        with pytest.raises(TypeError, match=r"\*args/\*\*kwargs"):
            compile_binder(handler, "/")


def test_depends_inside_annotated_is_refused_with_the_fix_in_the_message() -> None:
    """A wiring bug that used to surface as the caller's fault.

    `Depends` is read from the default only, so inside `Annotated` it was
    invisible: the parameter fell through to the JSON body and a GET answered
    400 "invalid JSON body". A 400 tells the caller *they* broke, and they have
    no way to know it was this side.
    """
    async def handler(
        request: Any, value: Annotated[str, Depends(_module_level_dep)],
    ) -> Any: ...

    with pytest.raises(TypeError, match="Depends"):
        compile_binder(handler, "/")
    # The documented spelling compiles, so the refusal is about the placement
    # rather than about `Depends` itself.
    async def correct(request: Any, value: str = Depends(_module_level_dep)) -> Any: ...

    compile_binder(correct, "/")


def test_a_bare_session_parameter_is_refused() -> None:
    """`Session` alone does not say which registry or workload it wants."""
    async def handler(request: Any, session: Session) -> Any: ...

    with pytest.raises(TypeError, match="FromORM"):
        compile_binder(handler, "/")


def test_a_path_marker_naming_a_placeholder_the_route_lacks_is_refused() -> None:
    """A typo here binds nothing and the parameter silently becomes a query."""
    async def handler(request: Any, ident: Annotated[int, Path("item_id")]) -> Any: ...

    with pytest.raises(TypeError, match="not present in"):
        compile_binder(handler, "/items/{id}")
    # And the correct spelling compiles, so this is not "Path is broken".
    compile_binder(handler, "/items/{item_id}")


def test_two_body_parameters_are_refused_in_both_spellings() -> None:
    """Explicit `Body()`, and the implicit dataclass-annotation form."""
    async def explicit(
        request: Any, a: Annotated[Item, Body()], b: Annotated[Item, Body()],
    ) -> Any: ...

    async def implicit(request: Any, a: Item, b: Item) -> Any: ...

    for handler in (explicit, implicit):
        with pytest.raises(TypeError, match="two body parameters"):
            compile_binder(handler, "/")


def test_a_form_model_cannot_be_combined_or_repeated() -> None:
    """One form model, and never beside individual `Form()` fields.

    Both would read the same parsed form, and the model would be built from a
    subset of it while the individual fields claimed the rest.
    """
    async def two_models(
        request: Any, a: Annotated[Item, Form()], b: Annotated[Item, Form()],
    ) -> Any: ...

    async def model_and_field(
        request: Any, a: Annotated[Item, Form()], name: Annotated[str, Form()] = "",
    ) -> Any: ...

    with pytest.raises(TypeError, match="two form-model parameters"):
        compile_binder(two_models, "/")
    with pytest.raises(TypeError, match="form-model with individual"):
        compile_binder(model_and_field, "/")


def test_a_body_cannot_be_combined_with_form_or_file_parameters() -> None:
    """One request body, read one way."""
    async def handler(
        request: Any, body: Annotated[Item, Body()],
        name: Annotated[str, Form()] = "",
    ) -> Any: ...

    with pytest.raises(TypeError, match="body and form/file"):
        compile_binder(handler, "/")


def test_an_annotation_naming_something_module_scope_cannot_see_is_blamed() -> None:
    """The refusal that caught this file's own first draft.

    Annotations are resolved at route-compile time in the module the callable
    was defined in, so a name local to a function -- or imported only under
    `if TYPE_CHECKING:` -- is invisible. Without this the `NameError` surfaces
    from inside `typing.get_type_hints` naming neither the handler nor the
    parameter, and it happens at import, far from the line that caused it.
    """
    def build():
        class Local:
            pass

        async def handler(request: Any, value: Local) -> Any: ...

        return handler

    with pytest.raises(TypeError, match="unresolvable name") as caught:
        compile_binder(build(), "/")
    message = str(caught.value)
    assert "'value'" in message          # names the parameter ...
    assert "Local" in message            # ... and the name it could not resolve


@pytest.mark.parametrize(
    ("annotation", "raw", "expected_type"),
    [
        (float, b"n=notanumber", "float"),
        (float, b"n=", "float"),
        (bool, b"n=maybe", "bool"),
        (bool, b"n=2", "bool"),
        (_dt.date, b"n=2026-13-45", "date"),
        (_dt.date, b"n=not-a-date", "date"),
        (_dt.datetime, b"n=2026-07-30", "instant"),      # no offset: refused, not UTC
        (_dt.datetime, b"n=nonsense", "instant"),
    ],
)
async def test_every_scalar_converter_refuses_what_it_cannot_parse(
    annotation: Any, raw: bytes, expected_type: str,
) -> None:
    """Only the `int` branch had a test; the other four were unreached.

    Each answers a 422 naming which conversion failed, and the `type` is what a
    client keys on -- so a branch that silently fell through to another
    converter's message would be a different error for the same request.

    The `datetime` cases are the sharp ones: a value with no offset is
    *refused* rather than read as UTC, which is the mistake `Instant` exists to
    make impossible.
    """
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
    annotation: Any, raw: bytes, expected: Any,
) -> None:
    """The other half, so "refuse everything" cannot pass the tests above."""
    async def handler(request: Any, n: Any = None) -> Any:
        return n

    handler.__annotations__["n"] = Annotated[annotation, Query()]
    bound = compile_binder(handler, "/")
    assert await bound(_query_request(raw)) == expected
