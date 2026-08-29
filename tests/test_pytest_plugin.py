from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from wreath import _pytest_plugin

pytest_plugins = ["pytester"]

_INI = """
[pytest]
asyncio_mode = auto
"""


def test_installed_plugin_contracts_share_one_fresh_pytest_process(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        def test_registered(request):
            assert request.config.pluginmanager.hasplugin("wreath")

        async def test_capture(wreath_email):
            await wreath_email.send_verification("a@example.test", "https://x.test/v")
            assert wreath_email.verifications == [
                ("a@example.test", "https://x.test/v")
            ]

        async def test_email_one(wreath_email):
            await wreath_email.send_verification("a@example.test", "l")
            assert len(wreath_email.verifications) == 1

        async def test_email_two(wreath_email):
            assert wreath_email.verifications == []

        async def test_missing_app_names_the_fixture(wreath_client):
            pass
        """
    )
    with_app = pytester.path / "with_app"
    with_app.mkdir()
    (with_app / "conftest.py").write_text(
        """
import pytest
from wreath import Wreath

events = []

@pytest.fixture
def wreath_app():
    app = Wreath()

    @app.on_startup
    async def up(app):
        events.append("startup")

    @app.on_shutdown
    async def down(app):
        events.append("shutdown")

    @app.get("/ping")
    async def ping(request):
        return {"ok": True}

    return app
""".lstrip(),
        encoding="utf-8",
    )
    (with_app / "test_client.py").write_text(
        """
from conftest import events

async def test_request_and_lifespan(wreath_client):
    response = await wreath_client.get("/ping")
    assert response.status == 200
    assert response.json() == {"ok": True}
    assert events == ["startup"]
""".lstrip(),
        encoding="utf-8",
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=5, errors=1)
    result.stdout.fnmatch_lines(["*wreath_app*"])


def test_postgres_fixture_skips_naming_the_variable(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_pytest_plugin.DSN_ENV, raising=False)
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        async def test_needs_a_database(wreath_db):
            pass
        """
    )
    result = pytester.runpytest_subprocess("-rs", "-p", "no:cacheprovider")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*WREATH_TEST_POSTGRES_DSN*"])


def test_plugin_declares_only_prefixed_fixtures() -> None:
    declared = {
        name
        for name in dir(_pytest_plugin)
        if hasattr(getattr(_pytest_plugin, name), "_fixture_function_marker")
    }
    assert declared, "no fixtures found on the plugin module"
    assert all(name.startswith("wreath_") for name in declared), sorted(declared)


@pytest.mark.database
def test_wreath_db_rolls_back_what_a_test_wrote(pytester: pytest.Pytester) -> None:
    if not os.environ.get(_pytest_plugin.DSN_ENV):
        pytest.skip("needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        TABLE = "wreath_plugin_rollback_probe"

        async def test_writes_a_row(wreath_db):
            await wreath_db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id int)")
            await wreath_db.execute(f"INSERT INTO {TABLE} VALUES (1)")
            rows = await wreath_db.fetch(f"SELECT id FROM {TABLE}")
            assert len(rows) == 1

        async def test_does_not_see_the_row(wreath_db):
            existing = await wreath_db.fetch(
                "SELECT to_regclass(%s) AS present" % f"'{TABLE}'"
            )
            assert existing[0]["present"] is None
        """
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=2)


@pytest.mark.database
def test_wreath_db_rolls_back_even_when_the_test_raises(pytester: pytest.Pytester) -> None:
    if not os.environ.get(_pytest_plugin.DSN_ENV):
        pytest.skip("needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)")
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        TABLE = "wreath_plugin_failure_probe"

        async def test_writes_then_fails(wreath_db):
            await wreath_db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id int)")
            await wreath_db.execute(f"INSERT INTO {TABLE} VALUES (1)")
            raise AssertionError("deliberate")

        async def test_sees_nothing_left_behind(wreath_db):
            existing = await wreath_db.fetch(
                "SELECT to_regclass(%s) AS present" % f"'{TABLE}'"
            )
            assert existing[0]["present"] is None
        """
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=1, failed=1)


def _imported_modules(statement: str) -> set[str]:
    """The `wreath.*` modules a fresh interpreter loads for `statement`."""
    probe = (
        f"{statement}\n"
        "import json, sys\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('wreath'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


def test_loading_the_plugin_does_not_import_the_framework() -> None:
    loaded = _imported_modules("import wreath._pytest_plugin")
    assert "wreath.app" not in loaded, sorted(loaded)
    assert "wreath.binding" not in loaded, sorted(loaded)


def test_importing_the_package_alone_does_not_import_the_framework() -> None:
    assert "wreath.app" not in _imported_modules("import wreath")


@pytest.mark.parametrize(
    "line",
    [
        "from wreath import Request, Wreath",
        "from wreath import Request",
        "from wreath import Depends",
        "from wreath import Response",
        "from wreath import Router",
        "import wreath; wreath.Request",
    ],
)
def test_every_public_name_can_be_the_first_one_asked_for(line: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", line], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_public_names_still_resolve_to_their_modules() -> None:
    import wreath
    from wreath.app import Wreath
    from wreath.binding import Depends
    from wreath.request import Request
    from wreath.response import JSONResponse, Response
    from wreath.router import Router

    assert wreath.Wreath is Wreath
    assert wreath.Depends is Depends
    assert wreath.Request is Request
    assert wreath.Response is Response
    assert wreath.JSONResponse is JSONResponse
    assert wreath.Router is Router
    assert set(wreath.__all__) <= set(dir(wreath))
    with pytest.raises(AttributeError, match="no attribute 'Nonexistent'"):
        wreath.Nonexistent  # noqa: B018
