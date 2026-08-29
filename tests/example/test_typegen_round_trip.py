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
    assert (
        run(
            _app(),
            TypegenOptions(target="python", output=str(directory), class_name=class_name),
        )
        == 0
    )


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


def test_the_whole_example_generates() -> None:
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
    source = _client_source(generated)
    assert "async def get_admin_stations(" in source
    assert "async def delete_admin_stations_by_id(" in source


def test_a_path_parameter_is_positional_and_typed(generated: Path) -> None:
    node = _method(generated, "get_reserves_by_slug_stations_by_station_id")
    positional = [arg.arg for arg in node.args.args]
    assert positional == ["self", "slug", "station_id"]
    assert ast.unparse(node.args.args[2].annotation) == "int", (
        "a path parameter keeps the handler's type; `str` everywhere would "
        "type-check and lose every refusal the binder makes"
    )


def test_a_required_query_parameter_has_no_default(generated: Path) -> None:
    node = _method(generated, "get_reserves_by_slug_stations_by_station_id_sightings")
    names = [arg.arg for arg in node.args.kwonlyargs]
    defaults = dict(zip(names, node.args.kw_defaults, strict=True))
    assert defaults["since"] is None, "a required parameter must not be defaulted"


def test_an_optional_query_parameter_defaults_to_none(generated: Path) -> None:
    node = _method(generated, "get_reserves_by_slug_stations_by_station_id_sightings")
    names = [arg.arg for arg in node.args.kwonlyargs]
    defaults = dict(zip(names, node.args.kw_defaults, strict=True))
    for optional in ("days", "min_confidence", "page", "size", "sort"):
        assert defaults[optional] is not None, optional
        assert ast.unparse(defaults[optional]) == "None", optional
    annotations = {arg.arg: ast.unparse(arg.annotation) for arg in node.args.kwonlyargs}
    assert annotations["days"] == "int | None"


def test_a_named_scalar_survives_the_hop(generated: Path) -> None:
    node = _method(generated, "get_reserves_by_slug_stations_by_station_id_sightings")
    annotations = {arg.arg: ast.unparse(arg.annotation) for arg in node.args.kwonlyargs}
    assert annotations["since"] == "datetime.date"
    assert "import datetime" in _client_source(generated)


def test_the_simple_routes_emit_no_query_scaffolding(generated: Path) -> None:
    node = _method(generated, "get_species")
    assert "_urlencode" not in ast.unparse(node)


def test_the_route_summary_and_its_wire_shape_reach_the_docstring(
    generated: Path,
) -> None:
    node = _method(generated, "get_reserves_by_slug_stations_by_station_id_sightings")
    doc = ast.get_docstring(node) or ""
    assert "What a station recorded" in doc, doc
    assert "GET /reserves/{slug}/stations/{station_id}/sightings" in doc, doc


def test_the_contract_is_pinned_beside_the_client(generated: Path) -> None:
    package = generated / "camera_trap_client"
    assert {p.name for p in package.iterdir()} == {
        "__init__.py",
        "client.py",
        "models.py",
        "spec.json",
    }
    assert 'SPEC_DIGEST = "sha256:' in _client_source(generated)


def test_the_digest_is_never_read_at_runtime(generated: Path) -> None:
    tree = ast.parse(_client_source(generated))
    loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "SPEC_DIGEST"
        and isinstance(node.ctx, ast.Load)
    ]
    assert loads == []


def test_generating_twice_produces_byte_identical_files(tmp_path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    _generate(first / "camera_trap_client")
    _generate(second / "camera_trap_client")

    names = sorted(p.name for p in (first / "camera_trap_client").iterdir())
    assert names == sorted(p.name for p in (second / "camera_trap_client").iterdir())
    for name in names:
        left = (first / "camera_trap_client" / name).read_bytes()
        right = (second / "camera_trap_client" / name).read_bytes()
        assert left == right, f"{name} is not deterministic"


@skip_without_database
async def test_the_generated_client_answers_from_the_seeded_database(
    generated: Path, client, monkeypatch
) -> None:
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
        async def request(self, method, target, *, headers=(), body=b"", idempotency_key=None):
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
        async def request(self, method, target, *, headers=(), body=b"", idempotency_key=None):
            seen.append(target)
            result = await client.get(target)
            return _Response(result.body, result.status)

    import datetime

    generated_client = module.CameraTrapClient(_Transport())
    await generated_client.get_reserves_by_slug_stations_by_station_id_sightings(
        "nullarbor", 25, since=datetime.date(2025, 1, 1), days=30
    )
    assert seen == ["/reserves/nullarbor/stations/25/sightings?since=2025-01-01&days=30"], seen
    assert "min_confidence" not in seen[0], (
        "an optional parameter nobody passed must not appear on the wire"
    )


def test_the_example_contributes_no_response_models_and_that_is_the_finding() -> None:
    api = build_api_model(_app())
    assert api.models == (), (
        "the camera-trap example now declares a typed response -- move the "
        "field/nesting/collection assertions here, where they belong"
    )
    kinds = {operation.response_body.kind for operation in api.operations}
    assert kinds == {"record", "unknown"}, kinds
