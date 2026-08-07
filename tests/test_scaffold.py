"""`wreath new` -- a project that already runs, so nobody has to assemble one.

The value of a scaffold is entirely that its output is *correct*, so most of
these tests do not read the generated text at all: they import the project,
drive a request through it, run its own suite, and generate a client from it.
A template that has drifted from the framework fails here rather than in
somebody's first hour.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from wreath._cli import main
from wreath.config import parse_dotenv
from wreath.testing import TestClient


def _new(target: Path, *args: str) -> int:
    return main(["new", target.name, "--directory", str(target.parent), *args])


@pytest.fixture
def importable() -> Iterator[list[Path]]:
    """Put generated projects on `sys.path`, and take them off again.

    A leaked entry makes a later test import a package from a `tmp_path` that
    has been deleted, which fails somewhere unrelated to whatever broke.
    """
    added: list[Path] = []
    before = set(sys.modules)
    yield added
    for path in added:
        if str(path) in sys.path:
            sys.path.remove(str(path))
    for name in set(sys.modules) - before:
        del sys.modules[name]


def _load(project: Path, added: list[Path]) -> object:
    sys.path.insert(0, str(project))
    added.append(project)
    return importlib.import_module(f"{project.name}.app")


def test_it_refuses_a_directory_that_already_has_something_in_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Never write over somebody's work, and say which path stopped it."""
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "notes.md").write_text("mine", encoding="utf-8")

    assert _new(target) == 2
    assert "occupied" in capsys.readouterr().err
    assert (target / "notes.md").read_text(encoding="utf-8") == "mine"


def test_it_refuses_a_name_that_is_not_an_importable_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The name becomes a package directory, so the rule is Python's, not ours.

    Asserting the distinct message rather than only the exit code: every refusal
    here names the project, so a test that checked for the name alone would pass
    on whichever branch fired.
    """
    assert _new(tmp_path / "my-app") == 2
    assert "importable Python package name" in capsys.readouterr().err


def test_it_emits_a_project_whose_layout_matches_the_documented_one(
    tmp_path: Path,
) -> None:
    target = tmp_path / "shop"
    assert _new(target) == 0
    written = {path.relative_to(target).as_posix() for path in target.rglob("*")
               if path.is_file()}
    assert written == {
        ".env.example",
        ".gitignore",
        "README.md",
        "pyproject.toml",
        "shop/__init__.py",
        "shop/app.py",
        "shop/config.py",
        "shop/routers/__init__.py",
        "shop/routers/items.py",
        "tests/test_items.py",
    }


def test_the_generated_application_answers_a_request(
    tmp_path: Path, importable: list[Path],
) -> None:
    """The load-bearing test. Everything else is about the packaging."""
    target = tmp_path / "answering"
    assert _new(target) == 0
    module = _load(target, importable)

    async def drive() -> None:
        async with TestClient(module.app) as client:
            response = await client.get("/items")
            assert response.status == 200
            assert response.json()["items"] == []
            created = await client.post("/items", json={"name": "broom", "price": 4.5})
            assert created.status == 201
            listed = await client.get("/items")
            assert [item["name"] for item in listed.json()["items"]] == ["broom"]

    import asyncio

    asyncio.run(drive())


def test_the_generated_env_example_parses_under_wreath_s_own_dialect(
    tmp_path: Path,
) -> None:
    """The trap this scaffold exists to stop somebody walking into.

    `wreath.config`'s dotenv dialect has no comment syntax at all, so a `#` line
    is a `ValueError` naming the line number rather than a line that is skipped.
    A template carrying explanatory comments produces a `.env` that fails to
    load on its first line, and the failure lands in the least helpful moment.
    """
    target = tmp_path / "dotenv"
    assert _new(target) == 0
    raw = (target / ".env.example").read_bytes()
    assert b"#" not in raw
    assert parse_dotenv(raw)


def _run_generated_suite(target: Path) -> subprocess.CompletedProcess[str]:
    """Do exactly what the generated README's quickstart says, in that order.

    `cp .env.example .env` first, because that is step one everywhere the
    scaffold documents itself -- in the README, and in the `next:` block the
    command prints. Running the suite without it would be testing a sequence
    nobody is told to follow.
    """
    (target / ".env").write_text(
        (target / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=target, capture_output=True, text=True, check=False, timeout=300,
    )


def test_the_generated_project_passes_its_own_tests(tmp_path: Path) -> None:
    """Delivered green, or the scaffold is just a pile of files to debug."""
    target = tmp_path / "green"
    assert _new(target) == 0
    result = _run_generated_suite(target)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_printed_next_steps_are_the_order_that_actually_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """`cp .env.example .env` has to come before `pytest`, and be said.

    With `--database postgres` the settings have no default DSN, so importing
    the application without the copied file refuses by name -- correct, and
    confusing as the first thing you meet. The instruction is what makes the
    refusal never happen; a reordering here silently breaks the first minute.
    """
    target = tmp_path / "ordered"
    assert _new(target, "--database", "postgres") == 0
    # Compared as whole lines rather than string offsets: the `cd` line echoes
    # the target path, and under pytest that path contains the word "pytest".
    steps = [line.strip() for line in
             capsys.readouterr().out.partition("next:")[2].splitlines() if line.strip()]
    assert steps.index("cp .env.example .env") < steps.index("pytest")
    readme = (target / "README.md").read_text(encoding="utf-8")
    assert readme.index("cp .env.example .env") < readme.index("pytest")


def test_the_frontend_option_wires_typegen_to_the_route_table(
    tmp_path: Path, importable: list[Path],
) -> None:
    """A generated client is only useful if it is generated from *this* app."""
    target = tmp_path / "withui"
    assert _new(target, "--frontend", "react") == 0
    assert (target / "web" / "package.json").is_file()
    assert (target / "web" / "src" / "App.tsx").is_file()
    # The generated directory is not committed: it is a build product of the
    # route table, and a stale copy in git is a client that lies about the API.
    assert "web/src/api/" in (target / ".gitignore").read_text(encoding="utf-8")

    sys.path.insert(0, str(target))
    importable.append(target)
    output = target / "web" / "src" / "api"
    assert main(["typegen", f"{target.name}.app:app", "--output", str(output),
                 "--react-query"]) == 0
    assert (output / "client.ts").is_file()
    assert (output / "react-query.ts").is_file()


def test_the_frontend_readme_names_the_regeneration_command(tmp_path: Path) -> None:
    """The one command somebody has to re-run after changing a route."""
    target = tmp_path / "readme"
    assert _new(target, "--frontend", "react") == 0
    assert "wreath typegen" in (target / "README.md").read_text(encoding="utf-8")


def test_without_the_frontend_option_nothing_typescript_is_written(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bare"
    assert _new(target) == 0
    assert not (target / "web").exists()
    assert "typegen" not in (target / "README.md").read_text(encoding="utf-8")


def test_the_database_option_refuses_to_import_without_a_dsn_and_names_it(
    tmp_path: Path, importable: list[Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No default DSN, and the refusal names the variable.

    Guessing at `localhost` and connecting to the wrong database is worse than
    refusing to start, so the generated `Settings` has no default for it -- the
    same choice the camera-trap example makes. What matters is that the failure
    says *which* variable, rather than surfacing as a connection error later.
    """
    from wreath.config import SettingsError

    target = tmp_path / "withdb"
    assert _new(target, "--database", "postgres") == 0
    assert (target / "withdb" / "models.py").is_file()
    monkeypatch.delenv("WITHDB_DATABASE_URL", raising=False)
    with pytest.raises(SettingsError) as raised:
        _load(target, importable)
    assert "WITHDB_DATABASE_URL" in str(raised.value.errors)


def test_the_database_option_compiles_its_model_once_a_dsn_is_supplied(
    tmp_path: Path, importable: list[Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared, not connected: a DSN is a string until the lifespan runs."""
    target = tmp_path / "withdsn"
    assert _new(target, "--database", "postgres") == 0
    monkeypatch.setenv(
        "WITHDSN_DATABASE_URL", "postgresql://wreath@127.0.0.1:55432/wreath_test")
    module = _load(target, importable)
    assert module.app is not None
    models = importlib.import_module("withdsn.models")
    assert [model.__name__ for model in models.MODELS] == ["Item"]


def test_the_database_option_puts_the_dsn_in_the_env_example(tmp_path: Path) -> None:
    target = tmp_path / "dsn"
    assert _new(target, "--database", "postgres") == 0
    keys = parse_dotenv((target / ".env.example").read_bytes())
    assert "DSN_DATABASE_URL" in keys


def test_the_generated_pyproject_is_valid_toml_naming_the_package(
    tmp_path: Path,
) -> None:
    import tomllib

    target = tmp_path / "packaged"
    assert _new(target) == 0
    data = tomllib.loads((target / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "packaged"
    assert any(dep.startswith("wreath") for dep in data["project"]["dependencies"])


def test_the_generated_application_passes_its_own_preflight(
    tmp_path: Path, importable: list[Path], capsys: pytest.CaptureFixture[str],
) -> None:
    """The two new commands have to agree about the project one of them wrote.

    A scaffold that emits an application `wreath doctor preflight` reports as
    blocking would be shipping the defect it exists to prevent.
    """
    target = tmp_path / "flightready"
    assert _new(target) == 0
    sys.path.insert(0, str(target))
    importable.append(target)
    assert main(["doctor", "preflight", f"{target.name}.app:app"]) == 0


def test_the_json_summary_lists_every_file_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "listed"
    assert _new(target, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "listed"
    assert "listed/app.py" in payload["files"]


# --- the front end, compiled -------------------------------------------------
#
# Skipped without a Node toolchain, which is a capability of the *environment*
# rather than of wreath -- the sanctioned exception, and the same gate
# `tests/typegen/test_consumer.py` uses. Run `npm ci` in `tests/typegen/consumer`
# to have it run; nothing else is needed, because the pinned React and TanStack
# Query types there are exactly what the scaffold's `web/` declares.

_CONSUMER = Path(__file__).parent / "typegen" / "consumer"
_node = shutil.which("node")


@pytest.mark.skipif(
    _node is None or not (_CONSUMER / "node_modules").exists(),
    reason="node toolchain or tests/typegen/consumer/node_modules not available",
)
def test_the_generated_react_app_typechecks_against_its_generated_client(
    tmp_path: Path, importable: list[Path],
) -> None:
    """The one that proves the frontend half is real rather than plausible.

    `App.tsx` is hand-written and the client beside it is generated, so every
    name crossing between them -- the hook, its parameters, the mutation's
    variables, and each field of `Item` -- is an assumption this is the only
    thing that checks. It has been wrong twice already: the hooks were
    `useGetItems`/`usePostItems` until the routes declared an `operation_id`,
    and the mutation took the body directly rather than wrapped.

    Under `--strict`, so `items.data?.items.map((item) => item.price)` passing
    also proves the handlers' return annotations reached TypeScript as real
    types. A handler annotated `-> dict` would generate `Record<string,
    unknown>` here, and this would fail.
    """
    target = tmp_path / "typechecked"
    assert _new(target, "--frontend", "react") == 0
    sys.path.insert(0, str(target))
    importable.append(target)
    assert main(["typegen", f"{target.name}.app:app",
                 "--output", str(target / "web" / "src" / "api"), "--react-query"]) == 0

    (target / "web" / "node_modules").symlink_to(_CONSUMER / "node_modules")
    compiled = subprocess.run(
        [_node, str(_CONSUMER / "node_modules" / "typescript" / "bin" / "tsc"),
         "--noEmit", "--strict", "--target", "ES2022", "--module", "ESNext",
         "--moduleResolution", "Bundler", "--jsx", "react-jsx", "--skipLibCheck",
         "--lib", "ES2022,DOM,DOM.Iterable", "src/App.tsx"],
        cwd=target / "web", capture_output=True, text=True, check=False, timeout=180,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr


def test_the_database_project_also_passes_its_own_tests(tmp_path: Path) -> None:
    """`--database postgres` must be green too, with nothing started.

    This was not covered when the option landed, and it was not green: the
    generated application registered a database, so `TestClient` ran a lifespan
    that read the catalog and raised `SchemaMismatchError` for a table no
    migration had created yet. "Already green" has to mean every variant, or the
    promise is worth nothing on the one somebody actually wanted.
    """
    target = tmp_path / "greendb"
    assert _new(target, "--database", "postgres") == 0
    result = _run_generated_suite(target)
    assert result.returncode == 0, result.stdout + result.stderr
    # ... and green because it ran, not because it collected nothing.
    assert "passed" in result.stdout
