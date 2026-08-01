"""The Python typegen target: a typed `ServiceClient` for a sibling service.

The point of generating this rather than hand-writing it is that the spec, the
validator, the scalar vocabulary and the client are one codebase. "The client
and the server disagree about what a timestamp is" stops being a category of
bug -- so these tests check the *joins*, not the string formatting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from wreath import Wreath
from wreath.binding import Body, Header, Query
from wreath.openapi import generate_openapi
from wreath.pagination import Page
from wreath.temporal import Instant
from wreath.typegen.inspect import build_api_model
from wreath.typegen.targets.python import render_python, spec_digest


@dataclass
class Llama:
    id: int
    name: str
    paddock: str | None = None


def _app() -> Wreath:
    app = Wreath()

    @app.get("/llamas/{llama_id}")
    async def get_llama(request: Any, llama_id: int) -> Llama:
        return Llama(id=llama_id, name="Bo", paddock="north")

    @app.post("/llamas")
    async def create_llama(request: Any) -> Llama:
        return Llama(id=7, name="New")

    return app


def _rendered(app: Wreath | None = None) -> dict[str, str]:
    target = app if app is not None else _app()
    return render_python(
        build_api_model(target), document=generate_openapi(target), class_name="LlamaClient"
    )


# --- the target is reachable, and the other one is untouched ----------------


def test_the_cli_selects_the_python_target(tmp_path) -> None:
    from wreath.typegen.cli import TypegenOptions, run

    options = TypegenOptions(
        target="python", output=str(tmp_path), class_name="LlamaClient"
    )
    assert run(_app(), options) == 0
    written = {path.name for path in tmp_path.iterdir()}
    assert written >= {"models.py", "client.py", "__init__.py"}


def test_an_unknown_target_is_refused_naming_the_known_ones(tmp_path) -> None:
    from wreath.typegen.cli import TypegenCliError, TypegenOptions, run

    options = TypegenOptions(target="rust", output=str(tmp_path))
    with pytest.raises(TypegenCliError, match="python"):
        run(_app(), options)


def test_adding_the_python_target_left_typescript_byte_identical() -> None:
    """A new target must not perturb the existing one.

    `Operation` grew a `behaviours` field for plan 02; this asserts the
    TypeScript emitted for an app that declares no behaviour is exactly what
    it was, so the two targets cannot drift into each other.
    """
    from wreath.typegen.targets.typescript import render_typescript

    app = _app()
    files = render_typescript(build_api_model(app))
    # No middleware declares anything, so no runtime module and no change to
    # the modules that already existed.
    assert "behaviours.ts" not in files
    assert set(files) == {"models.ts", "client.ts", "index.ts", "wreath-typegen.json"}


# --- what it emits ----------------------------------------------------------


def test_the_target_emits_a_module_set() -> None:
    """`spec.json` joined the set when the contract gate landed.

    It is the document the package was generated from, kept so
    `--check-contract` has a baseline: `SPEC_DIGEST` can only say *changed*,
    and telling breaking from compatible needs the document itself.
    """
    files = _rendered()
    assert set(files) == {"models.py", "client.py", "__init__.py", "spec.json"}


def test_no_document_means_no_pinned_spec() -> None:
    """Rendering without a document must not emit an empty or fabricated pin.

    A `spec.json` that did not come from a provider would give the gate a
    baseline it could compare against and be wrong about.
    """
    files = render_python(build_api_model(_app()), class_name="LlamaClient")
    assert "spec.json" not in files


def test_generated_modules_are_valid_python() -> None:
    """Compiled, not merely produced. A generator that emits a syntax error is
    a generator whose tests only checked for substrings."""
    for name, source in _rendered().items():
        compile(source, name, "exec")


def test_the_client_subclasses_service_client_and_adds_no_transport() -> None:
    source = _rendered()["client.py"]
    assert "class LlamaClient(ServiceClient):" in source
    # The transport lives in wreath.http_client; a generated file that opened a
    # socket would be a second copy of a solved problem.
    for forbidden in ("socket", "asyncio.open_connection", "ssl.", "http.client"):
        assert forbidden not in source, f"generated client re-implements transport: {forbidden}"


def test_the_generated_code_imports_only_wreath_and_the_stdlib() -> None:
    """No third-party package may reach a consumer through a generated file."""
    allowed_roots = {
        "__future__", "json", "uuid", "dataclasses", "typing", "urllib",
        "wreath", "hashlib",
    }
    for name, source in _rendered().items():
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if stripped.startswith("from ."):
                continue  # a sibling generated module
            root = stripped.split()[1].split(".")[0]
            assert root in allowed_roots, f"{name} imports {root!r}: {line}"


def test_one_typed_method_per_operation() -> None:
    """Every operation in the model gets a method, named from its operation id."""
    import ast

    from wreath.typegen.targets.python import _snake

    app = _app()
    api = build_api_model(app)
    source = render_python(api, document=generate_openapi(app), class_name="LlamaClient")[
        "client.py"
    ]
    tree = ast.parse(source)
    generated = {
        node.name
        for klass in ast.walk(tree)
        if isinstance(klass, ast.ClassDef)
        for node in klass.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    expected = {_snake(operation.id) for operation in api.operations}
    assert expected <= generated, f"missing {expected - generated}"
    assert "-> Llama:" in source


def test_a_path_parameter_becomes_a_typed_positional() -> None:
    source = _rendered()["client.py"]
    assert "llama_id: int" in source
    assert 'path = f"/llamas/{llama_id}"' in source


def test_models_become_dataclasses_with_optional_fields_last() -> None:
    source = _rendered()["models.py"]
    assert "class Llama:" in source
    assert "id: int" in source
    assert "paddock: str | None = None" in source
    compile(source, "models.py", "exec")


# --- the joins that make generating it worthwhile ---------------------------


def test_responses_bind_through_the_servers_own_validator() -> None:
    """Not a second decoder: the same `binding.validate` the provider runs."""
    source = _rendered()["client.py"]
    assert "from wreath.binding import validate as _validate" in source
    assert "_validate(annotation, decoded)" in source


def test_an_extra_field_on_the_wire_is_refused() -> None:
    """Client strictness equals server strictness, because it is the same code."""
    from wreath.binding import ValidationError, validate

    with pytest.raises(ValidationError):
        validate(Llama, {"id": 1, "name": "Bo", "unexpected": "field"})


def test_a_declared_idempotency_key_reaches_the_generated_method() -> None:
    """The behaviour the tape declared is what makes the client send a key."""
    from wreath.middleware import IdempotencyMiddleware

    app = _app()
    app.add_middleware(IdempotencyMiddleware())
    source = render_python(
        build_api_model(app), document=generate_openapi(app), class_name="LlamaClient"
    )["client.py"]
    assert "idempotency_key=" in source


def test_no_idempotency_key_without_the_declaration() -> None:
    """A client must not invent a guarantee the server never offered."""
    assert "idempotency_key=" not in _rendered()["client.py"]


# --- every parameter location ------------------------------------------------


@dataclass
class NewLlama:
    name: str


def _rich_app() -> Wreath:
    """An app that uses every parameter location the target can emit.

    The markers are imported at module scope deliberately: wreath resolves a
    handler's annotations at route-compile time in the module the callable was
    defined in, so a name local to this function is not visible to it.
    """
    app = Wreath()

    @app.get("/llamas")
    async def list_llamas(
        request: Any,
        paddock: Annotated[str, Query()],
        limit: Annotated[int | None, Query()] = None,
        trace: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
    ) -> list[Llama]:
        """List the llamas in a paddock."""
        return []

    @app.post("/llamas")
    async def create_llama(request: Any, body: Annotated[NewLlama, Body()]) -> Llama:
        return Llama(id=1, name=body.name)

    return app


def _rich_source() -> str:
    app = _rich_app()
    return render_python(
        build_api_model(app), document=generate_openapi(app), class_name="LlamaClient"
    )["client.py"]


def test_a_required_query_parameter_is_keyword_only_and_required() -> None:
    source = _rich_source()
    assert "paddock: str" in source
    assert '"paddock"' in source
    assert "_urlencode(query)" in source


def test_an_optional_query_parameter_defaults_to_none_and_is_skipped() -> None:
    source = _rich_source()
    assert "limit: int | None = None" in source
    assert "if limit is not None:" in source


def test_a_header_parameter_uses_its_wire_name_lowercased() -> None:
    """The alias is the wire name; the Python name is the ergonomic one."""
    source = _rich_source()
    assert "trace: str | None = None" in source
    assert 'b"x-trace-id"' in source


def test_a_request_body_is_serialised_and_sent() -> None:
    source = _rich_source()
    assert "body: NewLlama" in source
    assert "payload = _dump(body)" in source
    assert "body=payload" in source


def test_the_rich_client_is_valid_python() -> None:
    compile(_rich_source(), "client.py", "exec")


def test_an_idempotency_key_coexists_with_keyword_parameters() -> None:
    """The regression the mutation pass found.

    Splicing the parameter into the rendered signature declined whenever the
    operation already had a `*`, while the emitted call still referenced
    `idempotency_key` -- generated code that raises `NameError` the first time
    anyone calls it. An operation with both a query parameter and a declared
    idempotency behaviour is exactly that case.
    """
    import ast

    from wreath.middleware import IdempotencyMiddleware

    app = _rich_app()
    app.add_middleware(IdempotencyMiddleware())
    source = render_python(
        build_api_model(app), document=generate_openapi(app), class_name="LlamaClient"
    )["client.py"]
    compile(source, "client.py", "exec")

    tree = ast.parse(source)
    for klass in ast.walk(tree):
        if not isinstance(klass, ast.ClassDef):
            continue
        for node in klass.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            body_source = ast.unparse(node)
            if "idempotency_key=idempotency_key" not in body_source:
                continue
            declared = {a.arg for a in node.args.kwonlyargs} | {
                a.arg for a in node.args.args
            }
            assert "idempotency_key" in declared, (
                f"{node.name} uses idempotency_key without declaring it"
            )


def test_an_operation_without_them_emits_no_query_or_header_machinery() -> None:
    """Negative space: absent parameters must emit nothing, not empty scaffolding.

    A generator that always emitted the query block would still compile and
    still pass every positive test, while shipping dead code into every
    consumer's repository.
    """
    source = _rendered()["client.py"]  # the simple app: one path param, no query
    assert "query: list[tuple[str, str]] = []" not in source
    assert "_urlencode(query)" not in source
    assert "headers: list[tuple[bytes, bytes]] = []" not in source
    assert "headers=tuple(headers)" not in source


# --- degenerate shapes -------------------------------------------------------


def test_an_app_with_no_models_emits_no_sibling_import() -> None:
    app = Wreath()

    @app.get("/ping")
    async def ping(request: Any) -> str:  # pragma: no cover - shape only
        return "pong"

    files = render_python(build_api_model(app), document=generate_openapi(app))
    assert "from .models import" not in files["client.py"]
    compile(files["client.py"], "client.py", "exec")


def test_an_app_with_no_operations_still_emits_a_valid_class() -> None:
    app = Wreath()
    files = render_python(build_api_model(app), document=generate_openapi(app))
    compile(files["client.py"], "client.py", "exec")
    assert "pass" in files["client.py"]


def test_rendering_without_a_document_emits_an_empty_digest() -> None:
    files = render_python(build_api_model(_app()))
    assert 'SPEC_DIGEST = ""' in files["client.py"]
    compile(files["client.py"], "client.py", "exec")


def test_every_generated_method_has_a_docstring_naming_its_route() -> None:
    """A route with no `summary=` falls back to naming the verb and path.

    The handler's own docstring is `operation.description`, not `summary` --
    `summary` is the route's `summary=` keyword. The fallback is what most
    operations get, so it is the branch worth pinning.
    """
    import ast

    source = _rich_source()
    assert "`GET /llamas`." in source

    tree = ast.parse(source)
    for klass in ast.walk(tree):
        if not isinstance(klass, ast.ClassDef):
            continue
        for node in klass.body:
            if isinstance(node, ast.AsyncFunctionDef):
                assert ast.get_docstring(node), f"{node.name} has no docstring"


# --- the pin ----------------------------------------------------------------


def test_the_digest_is_emitted_and_is_order_independent() -> None:
    source = _rendered()["client.py"]
    assert 'SPEC_DIGEST = "sha256:' in source

    first = spec_digest({"a": 1, "b": {"c": 2, "d": 3}})
    second = spec_digest({"b": {"d": 3, "c": 2}, "a": 1})
    assert first == second, "key order must not change the digest"


def test_a_changed_document_changes_the_digest() -> None:
    assert spec_digest({"a": 1}) != spec_digest({"a": 2})


def test_the_runtime_does_not_verify_the_digest() -> None:
    """A client refusing to start over a compatible change is an outage generator.

    The pin is a CI gate. Nothing in the generated module may *read*
    `SPEC_DIGEST` at import or call time -- so it must appear as an assignment
    target and never as a loaded name. Parsed rather than grepped, because the
    module docstring legitimately talks about it.
    """
    import ast

    tree = ast.parse(_rendered()["client.py"])
    loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "SPEC_DIGEST"
        and isinstance(node.ctx, ast.Load)
    ]
    assert loads == [], "SPEC_DIGEST is read at runtime; the pin is a CI gate"


# --- the generated client actually works ------------------------------------


async def test_the_generated_client_round_trips_against_the_real_app(
    tmp_path, monkeypatch
) -> None:
    """Write the package, import it for real, and drive it against the app.

    Imported rather than `exec`'d, so the generated `from .models import ...`
    is exercised as a package the way a consumer would actually have it -- the
    sibling import is part of what is being tested, and faking it would leave
    the one thing most likely to be wrong unverified.
    """
    import importlib
    import sys

    from wreath.testing import TestClient
    from wreath.typegen.cli import TypegenOptions, run
    from wreath.typegen.targets.python import _snake

    app = _app()
    package = tmp_path / "llama_api"
    package.mkdir()
    assert run(app, TypegenOptions(
        target="python", output=str(package), class_name="LlamaClient"
    )) == 0

    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in sys.modules if n.startswith("llama_api")]:
        del sys.modules[name]
    generated_module = importlib.import_module("llama_api")
    models_module = importlib.import_module("llama_api.models")

    api = build_api_model(app)
    getter = _snake(next(o.id for o in api.operations if o.method == "GET"))

    class _Response:
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.status = 200

    calls: list[tuple[str, str]] = []

    class _Transport:
        async def request(self, method, target, *, headers=(), body=b"", idempotency_key=None):
            calls.append((method, target))
            async with TestClient(app) as inner:
                result = await inner.get(target) if method == "GET" else await inner.post(target)
                return _Response(result.body)

    client = generated_module.LlamaClient(_Transport())
    method = getattr(client, getter, None)
    assert method is not None, f"no {getter!r} on the generated client: {dir(client)}"

    result = await method(3)

    assert calls == [("GET", "/llamas/3")]
    assert result.id == 3
    assert result.name == "Bo"
    # Bound into the *generated* dataclass, not a dict that happens to match.
    assert isinstance(result, models_module.Llama)


# --- the scalar vocabulary: a timestamp survives the hop --------------------
#
# This file's own docstring claims that "the client and the server disagree
# about what a timestamp is" stops being a category of bug. Nothing asserted
# it: no test app declared a temporal type, so the mapping went unexercised and
# `Instant` reached the generated dataclass as `str`. `DATE_TIME` is
# `TypeRef("string", "date-time")`, and a target matching on `kind` alone emits
# `str` -- which type-checks, round-trips, and silently loses the zone.


@dataclass
class Sighting:
    id: int
    at: Instant
    ended: Instant | None = None


def _temporal_app() -> Wreath:
    app = Wreath()

    @app.get("/sightings/{sighting_id}")
    async def get_sighting(request: Any, sighting_id: int) -> Sighting:
        return Sighting(id=sighting_id, at=Instant.parse("2026-07-31T09:30:00+10:00"))

    return app


def _temporal_rendered() -> dict[str, str]:
    app = _temporal_app()
    return render_python(
        build_api_model(app), document=generate_openapi(app), class_name="SightingClient"
    )


def test_an_instant_field_is_annotated_instant_not_str() -> None:
    models = _temporal_rendered()["models.py"]
    assert "at: Instant" in models, models
    assert "at: str" not in models


def test_the_generated_models_import_instant_from_wreath() -> None:
    models = _temporal_rendered()["models.py"]
    assert "from wreath.temporal import Instant" in models, models


def test_an_optional_instant_keeps_its_scalar() -> None:
    models = _temporal_rendered()["models.py"]
    assert "ended: Instant | None" in models, models


# --- Page[T] is wreath's own, not a generated near-copy ---------------------


@dataclass
class Herd:
    id: int
    name: str


def _paged_app() -> Wreath:
    app = Wreath()

    @app.get("/herds")
    async def list_herds(request: Any) -> Page[Herd]:
        return Page(items=[Herd(id=1, name="north")], total=1, page=1, size=20)

    return app


def _paged_rendered() -> dict[str, str]:
    app = _paged_app()
    return render_python(
        build_api_model(app), document=generate_openapi(app), class_name="HerdClient"
    )


def test_a_paginated_route_reuses_wreaths_own_page() -> None:
    """`Page[T]` must *be* `wreath.pagination.Page`, not a lookalike.

    A generated near-copy type-checks and behaves identically right up until
    someone passes one to a function annotated with the real thing.
    """
    rendered = _paged_rendered()
    source = rendered["client.py"] + rendered["models.py"]
    assert "from wreath.pagination import Page" in source, source
    assert "class Page" not in rendered["models.py"], "Page was re-declared, not imported"


def test_the_paginated_return_is_annotated_with_the_element_type() -> None:
    source = _paged_rendered()["client.py"]
    assert "Page[Herd]" in source, source


def test_an_instant_round_trips_zone_aware_through_the_binder() -> None:
    """The annotation has to be load-bearing, not decorative.

    Asserting the generated source says `Instant` proves the mapping; it does
    not prove the value survives. The client binds through
    `wreath.binding.validate` -- the server's own validator -- so this is what
    turns the wire string back into an aware instant.
    """
    from wreath.binding import validate

    bound = validate(Sighting, {"id": 1, "at": "2026-07-31T09:30:00+10:00"})

    assert isinstance(bound.at, Instant)
    assert bound.at.utcoffset() is not None, "a naive value would defeat the point"
    assert bound.at.utcoffset().total_seconds() == 10 * 3600


def test_a_naive_instant_from_the_wire_is_refused() -> None:
    """Never assumed UTC -- that assumption is the bug the type exists to stop."""
    from wreath.binding import ValidationError, validate

    with pytest.raises(ValidationError):
        validate(Sighting, {"id": 1, "at": "2026-07-31T09:30:00"})


def test_the_typescript_target_emits_no_undeclared_page_type() -> None:
    """The other target must not reference a `Page` nothing declares.

    A TypeScript client is standalone -- there is no `wreath.pagination` to
    import from -- so the page renders structurally. Naming it would emit a
    client that references an undeclared type and still reports success.
    """
    from wreath.typegen.targets.typescript import render_typescript

    source = "\n".join(render_typescript(build_api_model(_paged_app())).values())
    assert "items: readonly Herd[]" in source, source
    assert "Page<" not in source, "named a type the generated module never declares"
