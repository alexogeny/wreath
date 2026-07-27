"""Server-side sessions: revocation, rotation, and the cookie-only default."""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.middleware.sessions import SessionMiddleware, rotate_session
from wreath.session_store import PostgresSessionStore
from wreath.testing import TestClient

pytestmark = pytest.mark.asyncio


class MemoryStore:
    """The SessionStore protocol, in a dict. Enough to test the middleware."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.saves = 0
        self.loads = 0

    async def load(self, sid: str) -> dict[str, Any] | None:
        self.loads += 1
        return self.rows.get(sid)

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        self.saves += 1
        self.rows[sid] = dict(data)

    async def delete(self, sid: str) -> None:
        self.rows.pop(sid, None)


def _app(store: Any) -> tuple[Wreath, MemoryStore]:
    app = Wreath()
    app.add_middleware(SessionMiddleware(secret="s" * 32, store=store))

    @app.get("/login")
    async def login(request: Any) -> dict:
        request.state.session["user"] = "ada"
        rotate_session(request)
        return {"ok": True}

    @app.get("/whoami")
    async def whoami(request: Any) -> dict:
        return {"user": request.state.session.get("user")}

    @app.get("/read")
    async def read(request: Any) -> dict:
        return {"n": len(request.state.session)}

    @app.get("/logout")
    async def logout(request: Any) -> dict:
        request.state.session.clear()
        return {"ok": True}

    return app, store


def _cookie(response: Any) -> str:
    header = response.header("set-cookie")
    assert header is not None, "expected a session cookie"
    return header.split(";")[0].split("=", 1)[1]


# --- round trip --------------------------------------------------------------


async def test_the_cookie_carries_only_an_id_and_contents_live_in_the_store() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)

    login = await client.get("/login")
    token = _cookie(login)

    # The value in the cookie is not the session content.
    assert "ada" not in token
    assert len(store.rows) == 1
    assert next(iter(store.rows.values())) == {"user": "ada"}

    who = await client.get("/whoami", headers={"cookie": f"wreath_session={token}"})
    assert who.json() == {"user": "ada"}


async def test_a_read_only_request_does_not_rewrite_the_row_or_the_cookie() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    token = _cookie(await client.get("/login"))
    saves_after_login = store.saves

    response = await client.get("/read", headers={"cookie": f"wreath_session={token}"})

    assert response.json() == {"n": 1}
    assert store.saves == saves_after_login      # no write
    assert response.header("set-cookie") is None  # no reissue


# --- revocation --------------------------------------------------------------


async def test_deleting_the_row_revokes_a_still_valid_cookie() -> None:
    """The property cookie-only sessions cannot offer."""
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    token = _cookie(await client.get("/login"))

    store.rows.clear()                            # an admin revokes it

    who = await client.get("/whoami", headers={"cookie": f"wreath_session={token}"})
    assert who.json() == {"user": None}


async def test_a_forged_cookie_is_rejected_without_touching_the_store() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    before = store.loads

    who = await client.get("/whoami", headers={"cookie": "wreath_session=a.b.c"})

    assert who.json() == {"user": None}
    assert store.loads == before                  # signature checked first


async def test_logout_deletes_the_row_and_clears_the_cookie() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    token = _cookie(await client.get("/login"))

    response = await client.get("/logout", headers={"cookie": f"wreath_session={token}"})

    assert store.rows == {}
    assert "Max-Age=0" in (response.header("set-cookie") or "")


# --- fixation ----------------------------------------------------------------


async def test_rotation_replaces_the_id_on_a_privilege_change() -> None:
    """An id fixed on the victim beforehand must not survive login."""
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)

    first = _cookie(await client.get("/login"))
    second = _cookie(
        await client.get("/login", headers={"cookie": f"wreath_session={first}"})
    )

    assert first != second
    assert len(store.rows) == 1                   # the old row is gone
    # The old cookie no longer resolves to anything.
    stale = await client.get("/whoami", headers={"cookie": f"wreath_session={first}"})
    assert stale.json() == {"user": None}


async def test_rotation_is_a_no_op_for_cookie_backed_sessions() -> None:
    app = Wreath()
    app.add_middleware(SessionMiddleware(secret="s" * 32))

    @app.get("/login")
    async def login(request: Any) -> dict:
        request.state.session["user"] = "ada"
        rotate_session(request)
        return {"ok": True}

    response = await TestClient(app).get("/login")
    assert (response.header("set-cookie") or "").startswith("wreath_session=")


# --- the cookie-only default is untouched ------------------------------------


async def test_without_a_store_the_session_still_travels_in_the_cookie() -> None:
    app = Wreath()
    app.add_middleware(SessionMiddleware(secret="s" * 32))

    @app.get("/set")
    async def set_value(request: Any) -> dict:
        request.state.session["user"] = "ada"
        return {"ok": True}

    @app.get("/get")
    async def get_value(request: Any) -> dict:
        return {"user": request.state.session.get("user")}

    client = TestClient(app)
    token = _cookie(await client.get("/set"))
    assert (
        await client.get("/get", headers={"cookie": f"wreath_session={token}"})
    ).json() == {"user": "ada"}


# --- PostgresSessionStore ----------------------------------------------------


class FakeStatement:
    def __init__(self, sql: str, workload: str, results: dict[str, Any]) -> None:
        self.calls: list[tuple] = []
        self.sql = sql
        self.workload = workload
        self._results = results

    async def fetchrow(self, *args: Any) -> Any:
        self.calls.append(args)
        return self._results.get(self.sql)

    async def execute(self, *args: Any) -> str:
        self.calls.append(args)
        return "OK"


class FakeDatabase:
    """Statements are registered on first use, so results are keyed by SQL."""

    def __init__(self) -> None:
        self.statements: dict[str, FakeStatement] = {}
        self.results: dict[str, Any] = {}

    def statement(self, name: str, sql: str, *, workload: str) -> FakeStatement:
        statement = FakeStatement(sql, workload, self.results)
        self.statements[name] = statement
        return statement


async def test_the_table_name_must_be_a_plain_identifier() -> None:
    with pytest.raises(ValueError, match="plain SQL identifier"):
        PostgresSessionStore(FakeDatabase(), table="sessions; DROP TABLE users")


async def test_the_schema_is_offered_not_applied() -> None:
    store = PostgresSessionStore(FakeDatabase())
    sql = store.schema_sql()
    assert "CREATE TABLE IF NOT EXISTS wreath_session" in sql
    assert "jsonb" in sql
    assert "expires" in sql


async def test_nothing_is_prepared_until_it_is_used() -> None:
    """The store is built while the app is described; the database is not up."""
    database = FakeDatabase()
    store = PostgresSessionStore(database)
    assert database.statements == {}

    await store.load("sid")

    assert list(database.statements) == ["wreath_session_read_live_wreath_session"]


async def test_reads_and_writes_use_the_right_workloads() -> None:
    database = FakeDatabase()
    store = PostgresSessionStore(database)
    await store.load("sid")
    await store.save("sid", {}, 60)
    assert database.statements["wreath_session_read_live_wreath_session"].workload == "read"
    assert database.statements["wreath_session_save_wreath_session"].workload == "write"


async def test_load_returns_none_for_a_missing_or_expired_row() -> None:
    store = PostgresSessionStore(FakeDatabase())
    assert await store.load("sid") is None


async def test_load_decodes_jsonb_handed_back_as_text() -> None:
    database = FakeDatabase()
    store = PostgresSessionStore(database)
    await store.load("sid")            # registers the statement
    sql = database.statements["wreath_session_read_live_wreath_session"].sql
    assert "expires >= clock_timestamp()" in sql   # an expired row never loads
    database.results[sql] = ['{"user":"ada"}']

    assert await store.load("sid") == {"user": "ada"}


async def test_expiry_is_pushed_to_the_database_clock() -> None:
    database = FakeDatabase()
    store = PostgresSessionStore(database)
    await store.save("sid", {"user": "ada"}, 60)
    save = database.statements["wreath_session_save_wreath_session"]
    # The lifetime is the caller's, but the deadline it becomes is the
    # database's: one clock, whatever the workers' clocks say.
    assert "clock_timestamp() + make_interval(secs => $3::float8)" in save.sql
    assert save.calls == [("sid", '{"user":"ada"}', 60.0)]
