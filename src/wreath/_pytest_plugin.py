"""Wreath's pytest fixtures, loaded by installing Wreath and nothing else.

Registered through the `pytest11` entry point, which is the whole mechanism
behind `pytest-django` and `pytest-asyncio`: pytest imports this module at
startup for any interpreter that has Wreath installed, so its fixtures resolve
in a repository with no `conftest.py` at all.

The fixtures exist because four things were being hand-written in every project's
conftest, each of them a thing Wreath already owns and none of them interesting:
entering `TestClient` so the lifespan actually runs, capturing outbound mail
instead of sending it, finding the test database, and rolling back what a test
wrote to it.

**Every fixture is `wreath_`-prefixed.** This plugin activates in every
repository that installs Wreath, including ones with their own `client` or `db`
fixture, and a bare name here would shadow somebody else's -- a collision that
reads as a bug in *their* code, in a file they did not write. `wreath_app` is the
one fixture meant to be *overridden* rather than used, and its default exists
only to fail with instructions.

Async fixtures need an async pytest plugin, which is not Wreath's to install.
`pytest-asyncio` is used when present, including its own decorator so the
fixtures work under `asyncio_mode = strict` as well as `auto`; under `anyio` the
plain `pytest.fixture` fallback is what applies.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest


def pytest_configure(config: object) -> None:
    """Install ``wreath test`` activity hooks only for an activated run.

    The import is deliberately lazy.  Installing Wreath registers this module
    in every pytest process, while the test runner itself is an opt-in command
    and must add no import cost to an ordinary pytest invocation.
    """
    if "WREATH_TEST_ACTIVITY_CONTROLLER_PID" not in os.environ:
        return

    from ._test_runner import install_activity_plugin

    install_activity_plugin(config)


def _resolve_async_fixture() -> Any:
    """`pytest_asyncio.fixture` when it is installed, else `pytest.fixture`.

    pytest-asyncio refuses to run a bare `@pytest.fixture` async generator in its
    default strict mode, so using its own decorator is what makes the async
    fixtures below work in *both* of its modes. The fallback keeps this module
    importable without it, so a project on anyio -- or one using none of the async
    fixtures -- still loads the plugin.

    A function rather than a `try`/`except` at module level because the two
    branches bind different callables to one name, which is a conflicting
    declaration to the type checker; returning from one place has a single type.
    """
    try:
        from pytest_asyncio import fixture
    except ImportError:  # pragma: no cover - depends on the installed environment
        return pytest.fixture
    return fixture


_async_fixture: Any = _resolve_async_fixture()

#: The environment variable naming the test database. The same name the framework's
#: own suite uses, so one exported value serves both.
DSN_ENV = "WREATH_TEST_POSTGRES_DSN"

_NO_APP = """\
The `wreath_client` fixture needs an application to drive, and no `wreath_app`
fixture was found. Define one in your `conftest.py` returning a `Wreath`:

    import pytest
    from wreath import Wreath

    @pytest.fixture
    def wreath_app():
        from myproject.app import app
        return app

Return the application; `wreath_client` enters it, so startup and shutdown
handlers run around each test. Override it at any scope -- a `session`-scoped
`wreath_app` is the usual choice once building the app costs anything.\
"""

_NO_DSN = (
    f"needs {DSN_ENV} (a live PostgreSQL). Wreath's database fixtures are gated on "
    "it rather than faked, because parameter type inference, query plans, lock "
    "behaviour and DST boundaries are what they exist to test. Export it, e.g. "
    f'{DSN_ENV}="postgresql://user:pass@127.0.0.1:5432/mydb_test"'
)


@pytest.fixture
def wreath_app() -> Any:
    """The application under test. **Override this in your own `conftest.py`.**

    Wreath cannot guess where your app object lives, so the shipped default does
    the next best thing and fails with the three lines you need to write. It is a
    fixture rather than a hook so that you can override it at any scope, and so
    that one project can have several -- a `session`-scoped real app and a
    `function`-scoped one built per test are both ordinary pytest overrides.
    """
    raise LookupError(_NO_APP)


@_async_fixture
async def wreath_client(wreath_app: Any) -> AsyncIterator[Any]:
    """A `TestClient` bound to `wreath_app`, with the lifespan run around the test.

    This is the fixture the plugin exists for. Entering `TestClient` is what runs
    startup and shutdown handlers, and a test suite that forgets to enter it
    tests an application whose pools were never opened -- which usually surfaces
    as an unrelated error deep in a handler.

    Requests execute the app directly, with no socket and no server, so the test
    observes exactly the ASGI messages a real server would send. The response
    status is `response.status`, never `response.status_code`.
    """
    from .testing import TestClient

    async with TestClient(wreath_app) as client:
        yield client


@pytest.fixture
def wreath_email() -> Any:
    """A `CapturingEmailSender` that records `(email, link)` pairs instead of sending.

    Function-scoped deliberately: a shared one would leak one test's mail into
    the next test's assertions, and the failure would appear in whichever test
    happened to run second.

    Pass it wherever the app takes an `EmailSender` and read `verifications` or
    `resets` afterwards.
    """
    from .users import CapturingEmailSender

    return CapturingEmailSender()


@pytest.fixture
def wreath_postgres_dsn() -> str:
    """The DSN from the environment, or a skip whose reason names the variable.

    The reason matters more than it looks. Database-gated suites in Wreath's own
    repository went a long time without executing even once, and the reason was
    not that nobody could run them -- it was that skipping was *invisible*, since
    a skip reason only appears under `-rs`. A plugin shipped to other people must
    not reintroduce that quietly, so the reason spells out both the variable and
    a usable value.
    """
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        pytest.skip(_NO_DSN)
    return dsn


@_async_fixture
async def wreath_database(wreath_postgres_dsn: str) -> AsyncIterator[Any]:
    """A started `Database` against the test DSN, stopped when the test ends.

    A small pool, because a fixture that opens a production-sized one makes a
    parallel test run the thing that exhausts `max_connections`.
    """
    from .postgres import Database

    database = Database(
        "test", wreath_postgres_dsn, pools={"write": {"min_size": 1, "max_size": 2}}
    )
    await database.start()
    try:
        yield database
    finally:
        await database.stop()


@_async_fixture
async def wreath_db(wreath_database: Any) -> AsyncIterator[Any]:
    """A connection inside a transaction that is **rolled back** after the test.

    The isolation people write by hand, and the reason to take it from here
    instead: rollback in a `finally`, so a test that raises rolls back too. A
    suite that cleans up with `DELETE` at the end of each test leaves rows behind
    on the first failure, and every later test in that file then fails for a
    reason that has nothing to do with what it asserts.

    Committing inside the test defeats this, which is a genuine limitation rather
    than an oversight: code under test that commits its own transaction needs a
    fixture that truncates instead. Nothing here can detect that for you.
    """
    connection = await wreath_database.acquire("write")
    await connection.execute("BEGIN")
    try:
        yield connection
    finally:
        await connection.execute("ROLLBACK")
        await wreath_database.release("write", connection)


__all__ = [
    "DSN_ENV",
    "wreath_app",
    "wreath_client",
    "wreath_database",
    "wreath_db",
    "wreath_email",
    "wreath_postgres_dsn",
]
