"""The `pytest11` plugin, exercised the way a user's repository sees it.

Every test here runs a *nested* pytest through `pytester` rather than calling the
fixtures directly, because what is being asserted is discovery: that an installed
Wreath is enough, with no `conftest.py` import and no copied boilerplate. A test
that called the fixture functions itself would pass even if the entry point were
missing, which is the one failure that matters.
"""

from __future__ import annotations

import os

import pytest

from wreath import _pytest_plugin

pytest_plugins = ["pytester"]

_INI = """
[pytest]
asyncio_mode = auto
"""


def test_the_plugin_is_registered_by_entry_point_alone(pytester: pytest.Pytester) -> None:
    """No `pytest_plugins`, no conftest -- installing Wreath is the whole setup."""
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        def test_registered(request):
            assert request.config.pluginmanager.hasplugin("wreath")
        """
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=1)


def test_wreath_client_runs_lifespan_around_the_test(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makeconftest(
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
        """
    )
    pytester.makepyfile(
        """
        from conftest import events

        async def test_request_and_lifespan(wreath_client):
            response = await wreath_client.get("/ping")
            assert response.status == 200
            assert response.json() == {"ok": True}
            assert events == ["startup"]
        """
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=1)


def test_a_missing_wreath_app_fixture_says_what_to_define(pytester: pytest.Pytester) -> None:
    """The error a user hits first must name the fixture, not fail inside Wreath."""
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        async def test_needs_an_app(wreath_client):
            pass
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*wreath_app*"])


def test_wreath_email_captures_instead_of_sending(pytester: pytest.Pytester) -> None:
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        async def test_capture(wreath_email):
            await wreath_email.send_verification("a@example.test", "https://x.test/v")
            assert wreath_email.verifications == [
                ("a@example.test", "https://x.test/v")
            ]
        """
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=1)


def test_email_capture_is_fresh_per_test(pytester: pytest.Pytester) -> None:
    """Function-scoped, or one test's mail leaks into the next one's assertions."""
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        async def test_one(wreath_email):
            await wreath_email.send_verification("a@example.test", "l")
            assert len(wreath_email.verifications) == 1

        async def test_two(wreath_email):
            assert wreath_email.verifications == []
        """
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=2)


def test_postgres_fixture_skips_naming_the_variable(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent skip is the failure mode this repo already learned once.

    `tests/conftest.py` exists because database-gated suites skipped invisibly for
    a long time. A plugin shipped to other people must not reintroduce that: the
    skip reason names the variable to set.

    The variable is unset for the nested run rather than assumed absent -- the
    subprocess inherits this environment, and `AGENTS.md` tells whoever is running
    this to export it, so assuming would make the test fail exactly for the people
    following the instructions.
    """
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
    """Every fixture is `wreath_`-prefixed, because this plugin loads for everyone.

    An installed Wreath activates in every repository that has it, including ones
    with their own `client` or `db` fixture. A bare name here would shadow
    somebody else's and the collision would read as their bug.
    """
    declared = {
        name
        for name in dir(_pytest_plugin)
        if hasattr(getattr(_pytest_plugin, name), "_fixture_function_marker")
    }
    assert declared, "no fixtures found on the plugin module"
    assert all(name.startswith("wreath_") for name in declared), sorted(declared)


@pytest.mark.database
def test_wreath_db_rolls_back_what_a_test_wrote(pytester: pytest.Pytester) -> None:
    """The isolation claim, proved across two tests rather than asserted in one.

    A single test cannot show rollback: it would pass just as well if the fixture
    committed. So the first test writes a row and sees it, the second looks for it
    and must not find it, and only the teardown between them can be responsible.
    """
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
    """Rollback lives in a `finally`, which is the difference from cleanup-at-the-end.

    A suite that tidies up on the last line of each test leaves its rows behind the
    first time one fails, and every later test in the file then fails for reasons
    unrelated to what it asserts.
    """
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
