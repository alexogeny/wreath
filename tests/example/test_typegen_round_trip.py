"""`wreath typegen --target python` against the canonical example.

Plan 03 generated its round-trip proof from a purpose-built two-route app, and
said so: *"Pointing it at the camera-trap example is the remaining work, and it
is where operations this simple app does not have would actually be exercised."*
Doing that found a defect the small app could not have:

**A handler annotated `-> Response` was reported as an unsupported annotation,
and one fatal diagnostic refuses the whole application.** Fourteen of the
example's routes are spelled that way — every `crud_router` route, the media
`PUT`, the two progress routes, the session `DELETE` — so
`build_api_model(camera_trap.app.build())` raised `TypegenError` and the example
generated *nothing at all*. `-> Response` is a declaration ("I am producing the
bytes myself"), not an omission, so it is now `unknown` and the other nineteen
operations generate around it.

What this file asserts, in the order the properties matter:

1. Every operation is generated, and the count is the route table's.
2. Path and query parameters, **including optionality**: a required query
   parameter is keyword-only with no default, an optional one defaults to
   `None`, and neither is confused with the other.
3. The scalar vocabulary crosses: `since: datetime.date` is a `date` and not a
   `str`.
4. Metadata — each route's summary and its wire method and path — reaches the
   generated docstring, so a reader of the client can find the endpoint.
5. **Deterministic output**: generating twice into two directories produces
   byte-identical files. A generator with set iteration in it passes every
   other test here and produces a diff on every CI run.
6. The client is *callable* against the real application through the in-process
   transport, and the values it returns are the seeded database's.

And one property asserted as a **limit** rather than a feature: the example's
handlers return `dict`, so it contributes no response models and the
fields/nesting/collections half of the contract is not reachable from here. It
is covered by `_rich_app` in `tests/test_typegen_python_target.py`; the test at
the bottom of this file pins the gap so it cannot widen silently — or close
without somebody noticing that the example became typed.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

import pytest
from camera_trap.app import build

from wreath.typegen.cli import TypegenOptions, run
from wreath.typegen.inspect import build_api_model

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the camera-trap typegen round trip",
)


def _app():
    """The example, without the schema check a database-less test cannot pass."""
    return build(validate_schema="off")


def _generate(directory: Path, *, class_name: str = "CameraTrapClient") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    assert run(
        _app(),
        TypegenOptions(target="python", output=str(directory), class_name=class_name),
    ) == 0


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("camera-trap-client")
    _generate(root / "camera_trap_client")
    return root


@pytest.fixture
async def client():
    """The example on a freshly built and seeded schema, signed in as a ranger.

    The same fixture shape `test_read_api.py` uses, and for the same reason:
    the read API needs an account, and running as the role that is refused
    nothing keeps an authorization failure from showing up here as a typegen
    failure.
    """
    from _camera_trap import build_schema, drop_schema

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=200)
    finally:
        await connection.close()

    async with TestClient(build()) as test_client:
        yield test_client.acting_as("ranger-1", roles=["ranger"], type="Observer")

    connection = await connect(_DSN)
    try:
        await drop_schema(connection)
    finally:
        await connection.close()


def _client_source(generated: Path) -> str:
    return (generated / "camera_trap_client" / "client.py").read_text(encoding="utf-8")


def _method(generated: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(_client_source(generated))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no {name!r} in the generated client")


# --- every operation, and nothing invented -----------------------------------


def test_the_whole_example_generates() -> None:
    """The row plan 03 left open. Before the `-> Response` fix this raised."""
    api = build_api_model(_app())
    assert len(api.operations) >= 30, "the example has more routes than this"


def test_every_operation_becomes_a_method(generated: Path) -> None:
    api = build_api_model(_app())
    tree = ast.parse(_client_source(generated))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    }
    assert len(methods) == len(api.operations), sorted(methods)


def test_an_opaque_response_route_is_generated_rather_than_fatal(
    generated: Path,
) -> None:
    """A `crud_router` route returns `Response`, so its body is opaque -- but the
    *operation* is real and a consumer must be able to call it."""
    source = _client_source(generated)
    assert "async def get_admin_stations(" in source
    assert "async def delete_admin_stations_by_id(" in source


# --- parameters, and optionality ---------------------------------------------


def test_a_path_parameter_is_positional_and_typed(generated: Path) -> None:
    node = _method(generated, "get_reserves_by_slug_stations_by_station_id")
    positional = [arg.arg for arg in node.args.args]
    assert positional == ["self", "slug", "station_id"]
    assert ast.unparse(node.args.args[2].annotation) == "int", (
        "a path parameter keeps the handler's type; `str` everywhere would "
        "type-check and lose every refusal the binder makes"
    )


def test_a_required_query_parameter_has_no_default(generated: Path) -> None:
    node = _method(
        generated, "get_reserves_by_slug_stations_by_station_id_sightings"
    )
    names = [arg.arg for arg in node.args.kwonlyargs]
    defaults = dict(zip(names, node.args.kw_defaults, strict=True))
    assert defaults["since"] is None, "a required parameter must not be defaulted"


def test_an_optional_query_parameter_defaults_to_none(generated: Path) -> None:
    """The pair. A generator that defaulted everything would pass the test above
    only by accident, and one that defaulted nothing would make every optional
    parameter mandatory."""
    node = _method(
        generated, "get_reserves_by_slug_stations_by_station_id_sightings"
    )
    names = [arg.arg for arg in node.args.kwonlyargs]
    defaults = dict(zip(names, node.args.kw_defaults, strict=True))
    for optional in ("days", "min_confidence", "page", "size", "sort"):
        assert defaults[optional] is not None, optional
        assert ast.unparse(defaults[optional]) == "None", optional
    annotations = {
        arg.arg: ast.unparse(arg.annotation) for arg in node.args.kwonlyargs
    }
    assert annotations["days"] == "int | None"


def test_a_named_scalar_survives_the_hop(generated: Path) -> None:
    """`since: datetime.date`, not `str`. The named-scalar table is the thing
    most likely to be silently wrong: `str` type-checks and round-trips."""
    node = _method(
        generated, "get_reserves_by_slug_stations_by_station_id_sightings"
    )
    annotations = {
        arg.arg: ast.unparse(arg.annotation) for arg in node.args.kwonlyargs
    }
    assert annotations["since"] == "datetime.date"
    assert "import datetime" in _client_source(generated)


def test_the_simple_routes_emit_no_query_scaffolding(generated: Path) -> None:
    """Negative space. A generator that always emitted the query builder would
    compile and pass every positive test above while shipping dead code."""
    node = _method(generated, "get_species")
    assert "_urlencode" not in ast.unparse(node)


# --- metadata ----------------------------------------------------------------


def test_the_route_summary_and_its_wire_shape_reach_the_docstring(
    generated: Path,
) -> None:
    node = _method(
        generated, "get_reserves_by_slug_stations_by_station_id_sightings"
    )
    doc = ast.get_docstring(node) or ""
    assert "What a station recorded" in doc, doc
    assert "GET /reserves/{slug}/stations/{station_id}/sightings" in doc, doc


def test_the_contract_is_pinned_beside_the_client(generated: Path) -> None:
    package = generated / "camera_trap_client"
    assert {p.name for p in package.iterdir()} == {
        "__init__.py", "client.py", "models.py", "spec.json"
    }
    assert "SPEC_DIGEST = \"sha256:" in _client_source(generated)


def test_the_digest_is_never_read_at_runtime(generated: Path) -> None:
    """A client that refused to start because the provider added an optional
    field would be an outage generator. The pin is a CI gate."""
    tree = ast.parse(_client_source(generated))
    loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "SPEC_DIGEST"
        and isinstance(node.ctx, ast.Load)
    ]
    assert loads == []


# --- determinism -------------------------------------------------------------


def test_generating_twice_produces_byte_identical_files(tmp_path) -> None:
    """The property a generator loses the moment a set iteration creeps in --
    and it fails as a diff on every CI run rather than as a broken client, which
    is why it needs its own test rather than being noticed."""
    first, second = tmp_path / "one", tmp_path / "two"
    _generate(first / "camera_trap_client")
    _generate(second / "camera_trap_client")

    names = sorted(p.name for p in (first / "camera_trap_client").iterdir())
    assert names == sorted(p.name for p in (second / "camera_trap_client").iterdir())
    for name in names:
        left = (first / "camera_trap_client" / name).read_bytes()
        right = (second / "camera_trap_client" / name).read_bytes()
        assert left == right, f"{name} is not deterministic"


# --- callable, against the real application ----------------------------------


@skip_without_database
async def test_the_generated_client_answers_from_the_seeded_database(
    generated: Path, client, monkeypatch
) -> None:
    """Imported as a package and driven through the in-process transport.

    Imported rather than `exec`'d so the emitted `from .models import ...` is
    exercised the way a consumer would have it, and driven against the *real*
    example so the values are the seed's rather than a fixture's.
    """
    monkeypatch.syspath_prepend(str(generated))
    for name in [n for n in sys.modules if n.startswith("camera_trap_client")]:
        del sys.modules[name]
    module = importlib.import_module("camera_trap_client")

    class _Response:
        def __init__(self, body: bytes, status: int) -> None:
            self.body = body
            self.status = status

    calls: list[tuple[str, str]] = []

    class _Transport:
        async def request(
            self, method, target, *, headers=(), body=b"", idempotency_key=None
        ):
            calls.append((method, target))
            result = await client.get(target)
            return _Response(result.body, result.status)

    generated_client = module.CameraTrapClient(_Transport())
    reserves = await generated_client.get_reserves()

    assert calls == [("GET", "/reserves")]
    items = reserves["items"] if isinstance(reserves, dict) else reserves
    assert items, "the seeded database has reserves"
    assert all("slug" in row for row in items), items[:2]


@skip_without_database
async def test_a_query_parameter_reaches_the_wire_in_the_right_place(
    generated: Path, client, monkeypatch
) -> None:
    """The optional/required split above is a claim about the *signature*; this
    is the claim about the URL, which is the one a server can disagree with."""
    monkeypatch.syspath_prepend(str(generated))
    for name in [n for n in sys.modules if n.startswith("camera_trap_client")]:
        del sys.modules[name]
    module = importlib.import_module("camera_trap_client")

    seen: list[str] = []

    class _Response:
        def __init__(self, body: bytes, status: int) -> None:
            self.body = body
            self.status = status

    class _Transport:
        async def request(
            self, method, target, *, headers=(), body=b"", idempotency_key=None
        ):
            seen.append(target)
            result = await client.get(target)
            return _Response(result.body, result.status)

    import datetime

    generated_client = module.CameraTrapClient(_Transport())
    await generated_client.get_reserves_by_slug_stations_by_station_id_sightings(
        "nullarbor", 25, since=datetime.date(2025, 1, 1), days=30
    )
    assert seen == [
        "/reserves/nullarbor/stations/25/sightings?since=2025-01-01&days=30"
    ], seen
    assert "min_confidence" not in seen[0], (
        "an optional parameter nobody passed must not appear on the wire"
    )


# --- the limit this example does not reach -----------------------------------


def test_the_example_contributes_no_response_models_and_that_is_the_finding() -> None:
    """Pinned, because it is the one property the plan hoped for and did not get.

    Every read handler here returns `dict`, so the generated client's responses
    are `dict[str, Any]` and there is no field, no optional field, no nested
    model and no collection-of-model to check. That is a fact about the
    *example*, not about typegen: `tests/test_typegen_python_target.py::_rich_app`
    exercises all four against a dataclass-returning app, including a
    `Page[Herd]`.

    This test exists so the gap cannot widen unnoticed, and so that the day the
    example starts returning dataclasses somebody has to come here and say so.
    """
    api = build_api_model(_app())
    assert api.models == (), (
        "the camera-trap example now declares a typed response -- move the "
        "field/nesting/collection assertions here, where they belong"
    )
    kinds = {operation.response_body.kind for operation in api.operations}
    assert kinds == {"record", "unknown"}, kinds
