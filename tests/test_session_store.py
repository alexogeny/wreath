from __future__ import annotations

from typing import Any

import pytest
from _pgfidelity import check_for

from wreath import Wreath
from wreath.policy import HttpPolicy
from wreath.policy.sessions import SessionPolicy, rotate_session
from wreath.session_store import PostgresSessionStore
from wreath.state import State
from wreath.testing import TestClient

pytestmark = pytest.mark.asyncio


class MemoryStore:
    """The SessionStore protocol, in a dict. Enough to test the middleware."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.saves = 0
        self.loads = 0
        self.deletes: list[str] = []

    async def load(self, sid: str) -> dict[str, Any] | None:
        self.loads += 1
        return self.rows.get(sid)

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        self.saves += 1
        self.rows[sid] = dict(data)

    async def save_if_present(
        self, sid: str, data: dict[str, Any], max_age: int
    ) -> bool:
        if sid not in self.rows:
            return False
        await self.save(sid, data, max_age)
        return True

    async def delete(self, sid: str) -> None:
        self.deletes.append(sid)
        self.rows.pop(sid, None)


class StateRequest:
    def __init__(self) -> None:
        self.state = State()


class CookieResponse:
    def __init__(self) -> None:
        self.cookies: list[tuple[str, tuple, dict]] = []

    def set_cookie(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.cookies.append((f"set:{name}", args, kwargs))

    def delete_cookie(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.cookies.append((f"delete:{name}", args, kwargs))


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_age": 0}, "max_age must be positive"),
        ({"cookie": "bad;name"}, "cookie"),
        ({"same_site": "sometimes"}, "samesite"),
    ],
)
def test_session_policy_refuses_invalid_cookie_configuration(
    options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SessionPolicy(secret="s" * 32, **options)


def _app(store: Any) -> tuple[Wreath, MemoryStore]:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, store=store)))

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


async def test_the_cookie_carries_only_an_id_and_contents_live_in_the_store() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)

    login = await client.get("/login")
    token = _cookie(login)

    # The signed payload is the opaque session id, not the session content.
    middleware = SessionPolicy(secret="s" * 32, store=store)
    sid = middleware._load_sid(token)
    assert sid is not None and sid in store.rows
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
    assert store.saves == saves_after_login  # no write
    assert response.header("set-cookie") is None  # no reissue


async def test_deleting_the_row_revokes_a_still_valid_cookie() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    token = _cookie(await client.get("/login"))

    store.rows.clear()  # an admin revokes it

    who = await client.get("/whoami", headers={"cookie": f"wreath_session={token}"})
    assert who.json() == {"user": None}


async def test_a_forged_cookie_is_rejected_without_touching_the_store() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    before = store.loads

    who = await client.get("/whoami", headers={"cookie": "wreath_session=a.b.c"})

    assert who.json() == {"user": None}
    assert store.loads == before  # signature checked first


async def test_logout_deletes_the_row_and_clears_the_cookie() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    token = _cookie(await client.get("/login"))

    response = await client.get("/logout", headers={"cookie": f"wreath_session={token}"})

    assert store.rows == {}
    assert "Max-Age=0" in (response.header("set-cookie") or "")


async def test_rotation_replaces_the_id_on_a_privilege_change() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)

    first = _cookie(await client.get("/login"))
    second = _cookie(await client.get("/login", headers={"cookie": f"wreath_session={first}"}))

    assert first != second
    assert len(store.rows) == 1  # the old row is gone
    stale = await client.get("/whoami", headers={"cookie": f"wreath_session={first}"})
    assert stale.json() == {"user": None}


async def test_rotation_is_a_no_op_for_cookie_backed_sessions() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32)))

    @app.get("/login")
    async def login(request: Any) -> dict:
        request.state.session["user"] = "ada"
        rotate_session(request)
        return {"ok": True}

    response = await TestClient(app).get("/login")
    assert (response.header("set-cookie") or "").startswith("wreath_session=")


async def test_without_a_store_the_session_still_travels_in_the_cookie() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32)))

    @app.get("/set")
    async def set_value(request: Any) -> dict:
        request.state.session["user"] = "ada"
        return {"ok": True}

    @app.get("/get")
    async def get_value(request: Any) -> dict:
        return {"user": request.state.session.get("user")}

    client = TestClient(app)
    token = _cookie(await client.get("/set"))
    assert (await client.get("/get", headers={"cookie": f"wreath_session={token}"})).json() == {
        "user": "ada"
    }


def test_cookie_session_loader_rejects_expiry_and_non_object_json() -> None:
    import time

    middleware = SessionPolicy(secret="s" * 32, max_age=60)
    expired = middleware._sign(b'{"user":"ada"}', int(time.time()) - 61)
    sequence = middleware._sign(b"[]", int(time.time()))
    assert middleware._load(expired) is None
    assert middleware._load(sequence) is None


def test_cookie_session_loader_marks_only_previous_secret_payloads_for_resigning() -> None:
    import time

    payload = b'{"user":"ada"}'
    current = SessionPolicy(secret="n" * 32, previous_secrets=("o" * 32,))
    old = SessionPolicy(secret="o" * 32)
    now = int(time.time())
    assert current._load(current._sign(payload, now)) == ({"user": "ada"}, payload)
    assert current._load(old._sign(payload, now)) == ({"user": "ada"}, b"")


def test_session_policy_schema_owners_follow_store_presence() -> None:
    store = MemoryStore()
    assert SessionPolicy(secret="s" * 32).schema_owners == ()
    assert SessionPolicy(secret="s" * 32, store=store).schema_owners == (store,)


class FakeStatement:
    def __init__(self, sql: str, workload: str, results: dict[str, Any]) -> None:
        self.calls: list[tuple] = []
        self.sql = sql
        self.workload = workload
        self._results = results

    async def fetchrow(self, *args: Any) -> Any:
        check_for(self, self.sql, args)
        self.calls.append(args)
        return self._results.get(self.sql)

    async def execute(self, *args: Any) -> str:
        check_for(self, self.sql, args)
        self.calls.append(args)
        return self._results.get(self.sql, "OK")


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
    await store.load("sid")  # registers the statement
    sql = database.statements["wreath_session_read_live_wreath_session"].sql
    assert "expires >= clock_timestamp()" in sql  # an expired row never loads
    database.results[sql] = ['{"user":"ada"}']

    assert await store.load("sid") == {"user": "ada"}


async def test_load_accepts_decoded_objects_and_refuses_other_json_shapes() -> None:
    database = FakeDatabase()
    store = PostgresSessionStore(database)
    await store.load("sid")
    sql = database.statements["wreath_session_read_live_wreath_session"].sql

    database.results[sql] = [{"user": "ada"}]
    assert await store.load("sid") == {"user": "ada"}

    database.results[sql] = [["not", "an", "object"]]
    assert await store.load("sid") is None


@pytest.mark.parametrize(
    ("session_key", "expected_key"),
    [(None, "principal"), ("account", "account")],
)
async def test_delete_for_uses_the_selected_session_key_and_returns_zero_for_unknown_status(
    session_key: str | None,
    expected_key: str,
) -> None:
    database = FakeDatabase()
    store = PostgresSessionStore(database, session_key="principal")

    assert await store.delete_for("ada", session_key=session_key) == 0
    statement = database.statements["wreath_session_delete_for_wreath_session"]
    assert statement.calls == [("ada", expected_key)]

    database.results[statement.sql] = "DELETE 3"
    assert await store.delete_for("ada", session_key=session_key) == 3


async def test_expiry_is_pushed_to_the_database_clock() -> None:
    database = FakeDatabase()
    store = PostgresSessionStore(database)
    await store.save("sid", {"user": "ada"}, 60)
    save = database.statements["wreath_session_save_wreath_session"]
    # The lifetime is the caller's, but the deadline it becomes is the
    # database's: one clock, whatever the workers' clocks say.
    assert "clock_timestamp() + make_interval(secs => $3::float8)" in save.sql
    assert save.calls == [("sid", '{"user":"ada"}', 60.0)]


async def test_after_degrades_when_before_did_not_publish_a_baseline() -> None:
    store = MemoryStore()
    middleware = SessionPolicy(secret="s" * 32, store=store)

    class Response:
        def __init__(self) -> None:
            self.cookies: list[tuple] = []

        def set_cookie(self, *args: Any, **kwargs: Any) -> None:
            self.cookies.append((args, kwargs))

        def delete_cookie(self, *args: Any, **kwargs: Any) -> None:
            self.cookies.append((args, kwargs))

    class Req:
        def __init__(self) -> None:
            from wreath.state import State

            self.state = State()

    # `before` got as far as publishing the session and stopped.
    request = Req()
    request.state.session = {"user": "ada"}

    response = Response()
    assert await middleware._after_stored(request, response) is response
    assert response.cookies == [], "no cookie from a baseline that does not exist"
    assert store.saves == 0, "nothing written from half-initialised state"

    # And the cookie-only path, which had the same asymmetry.
    plain = SessionPolicy(secret="s" * 32)
    request2 = Req()
    request2.state.session = {"user": "ada"}
    response2 = Response()
    assert await plain.after(request2, response2) is response2
    assert response2.cookies == []


async def test_after_stored_with_no_session_does_not_clear_or_delete_anything() -> None:
    store = MemoryStore()
    middleware = SessionPolicy(secret="s" * 32, store=store)
    request = StateRequest()
    request.state._session_loaded = b"{}"
    response = CookieResponse()
    assert await middleware._after_stored(request, response) is response
    assert response.cookies == []
    assert store.deletes == []


async def test_empty_new_stored_session_needs_no_delete_cookie_capability() -> None:
    store = MemoryStore()
    middleware = SessionPolicy(secret="s" * 32, store=store)
    request = StateRequest()
    request.state.session = {}
    request.state._session_loaded = b"{}"
    request.state._session_sid = None
    request.state._session_rotate = False
    response = object()
    assert await middleware._after_stored(request, response) is response
    assert store.deletes == []


async def test_rotating_an_unchanged_session_neither_touches_nor_double_deletes() -> None:
    store = TouchableStore()
    store.rows["sid"] = {"user": "ada"}
    middleware = SessionPolicy(secret="s" * 32, store=store)
    request = StateRequest()
    request.state.session = {"user": "ada"}
    request.state._session_loaded = b'{"user":"ada"}'
    request.state._session_sid = "sid"
    request.state._session_rotate = True
    await middleware._after_stored(request, CookieResponse())
    assert store.touches == []
    assert store.deletes == ["sid"]


async def test_a_new_stored_session_never_touches_or_deletes_a_missing_id() -> None:
    store = TouchableStore()
    middleware = SessionPolicy(secret="s" * 32, store=store)
    request = StateRequest()
    request.state.session = {"user": "ada"}
    request.state._session_loaded = b'{"user":"ada"}'
    request.state._session_sid = None
    request.state._session_rotate = True
    await middleware._after_stored(request, CookieResponse())
    assert store.touches == []
    assert store.deletes == []


async def test_an_unchanged_new_session_does_not_touch_a_missing_id() -> None:
    store = TouchableStore()
    middleware = SessionPolicy(secret="s" * 32, store=store)
    request = StateRequest()
    request.state.session = {"user": "ada"}
    request.state._session_loaded = b'{"user":"ada"}'
    request.state._session_sid = None
    request.state._session_rotate = False
    await middleware._after_stored(request, CookieResponse())
    assert store.touches == []


async def test_a_changed_stored_session_tolerates_a_response_without_cookies() -> None:
    store = MemoryStore()
    store.rows["sid"] = {}
    middleware = SessionPolicy(secret="s" * 32, store=store)
    request = StateRequest()
    request.state.session = {"user": "ada"}
    request.state._session_loaded = b"{}"
    request.state._session_sid = "sid"
    request.state._session_rotate = False
    response = object()
    assert await middleware._after_stored(request, response) is response
    assert store.saves == 1


async def test_cookie_only_after_tolerates_missing_session_or_cookie_capability() -> None:
    middleware = SessionPolicy(secret="s" * 32)
    missing = StateRequest()
    missing.state._session_loaded = b"{}"
    response = CookieResponse()
    assert await middleware.after(missing, response) is response
    assert response.cookies == []

    changed = StateRequest()
    changed.state.session = {"user": "ada"}
    changed.state._session_loaded = b"{}"
    bare = object()
    assert await middleware.after(changed, bare) is bare


async def test_a_session_that_cannot_be_serialized_publishes_no_state() -> None:
    store = MemoryStore()
    store.rows["sid"] = {"bad": object()}  # not JSON-serializable
    middleware = SessionPolicy(secret="s" * 32, store=store)

    class Req:
        def __init__(self) -> None:
            from wreath.state import State

            self.state = State()
            self.cookies: dict[str, str] = {}

    request = Req()
    request.cookies = {"wreath_session": middleware._sign(b"sid", 2_000_000_000)}

    with pytest.raises(TypeError):
        await middleware._before_stored(request)

    assert request.state.get("session") is None, (
        "a failed serialization must leave no session behind"
    )
    assert request.state.get("_session_loaded") is None


# `wreath mutant` deleted the HMAC comparison in `_load_sid` and every test
# stayed green -- including `test_a_forged_cookie_is_rejected_without_touching_
# the_store`, whose comment says "signature checked first". Its cookie is
# `a.b.c`, so with the signature check gone `int("b")` raises and the cookie is
# still rejected: the test proves the store was not touched, not that the MAC
# was verified. The cookie-only tamper test in `test_client_sessions_forms.py`
# exercises a different code path entirely, because it has no store.
# A cookie that isolates the MAC has to be *otherwise perfect*: real structure,
# a current timestamp, a correctly encoded id -- and one wrong signature.


def _forge(sid: str, secret: bytes, *, stamp: int | None = None) -> str:
    """A session cookie signed with `secret`, in the middleware's own format."""
    import base64
    import hashlib
    import hmac
    import time

    if stamp is None:
        stamp = int(time.time())
    body = base64.urlsafe_b64encode(sid.encode("ascii")).decode("ascii").rstrip("=")
    signed = f"{body}.{stamp}".encode("ascii")
    mac = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    return f"{body}.{stamp}.{mac}"


async def test_the_forging_helper_produces_a_cookie_the_middleware_accepts() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    store.rows["sid-1"] = {"user": "ada"}

    good = _forge("sid-1", b"s" * 32)
    who = await client.get("/whoami", headers={"cookie": f"wreath_session={good}"})
    assert who.json() == {"user": "ada"}  # loaded, so the format is right


async def test_a_cookie_signed_with_another_secret_is_rejected() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    store.rows["sid-1"] = {"user": "ada"}
    before = store.loads

    forged = _forge("sid-1", b"attacker-secret-of-the-same-length")
    who = await client.get("/whoami", headers={"cookie": f"wreath_session={forged}"})

    assert who.json() == {"user": None}
    assert store.loads == before  # and the store was never asked


async def test_a_cookie_whose_signature_is_one_character_off_is_rejected() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    store.rows["sid-1"] = {"user": "ada"}

    good = _forge("sid-1", b"s" * 32)
    body, stamp, mac = good.split(".")
    flipped = mac[:-1] + ("0" if mac[-1] != "0" else "1")
    tampered = f"{body}.{stamp}.{flipped}"

    who = await client.get("/whoami", headers={"cookie": f"wreath_session={tampered}"})
    assert who.json() == {"user": None}


async def test_a_correctly_signed_but_expired_cookie_is_rejected() -> None:
    import time

    store = MemoryStore()
    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(session=SessionPolicy(secret="s" * 32, store=store, max_age=60))
    )

    @app.get("/whoami")
    async def whoami(request: Any) -> dict:
        return {"user": request.state.session.get("user")}

    client = TestClient(app)
    store.rows["sid-1"] = {"user": "ada"}
    before = store.loads

    stale = _forge("sid-1", b"s" * 32, stamp=int(time.time()) - 61)
    who = await client.get("/whoami", headers={"cookie": f"wreath_session={stale}"})
    assert who.json() == {"user": None}
    assert store.loads == before  # expired before the store is asked

    fresh = _forge("sid-1", b"s" * 32, stamp=int(time.time()) - 59)
    who = await client.get("/whoami", headers={"cookie": f"wreath_session={fresh}"})
    assert who.json() == {"user": "ada"}  # inside the window, still good


async def test_a_session_secret_that_is_too_short_is_refused() -> None:
    from wreath.policy.sessions import MIN_SECRET_BYTES

    with pytest.raises(ValueError, match="at least"):
        SessionPolicy(secret="s" * (MIN_SECRET_BYTES - 1))
    assert SessionPolicy(secret="s" * MIN_SECRET_BYTES) is not None


class TouchableStore(MemoryStore):
    """A store that can extend a row's life without rewriting it."""

    def __init__(self) -> None:
        super().__init__()
        self.touches: list[tuple[str, int]] = []

    async def touch(self, sid: str, max_age: int) -> None:
        self.touches.append((sid, max_age))


async def test_an_unchanged_session_in_use_has_its_expiry_extended() -> None:
    store = TouchableStore()
    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(session=SessionPolicy(secret="s" * 32, store=store, max_age=600))
    )

    @app.get("/read")
    async def read(request: Any) -> dict:
        return {"user": request.state.session.get("user")}

    client = TestClient(app)
    store.rows["sid-1"] = {"user": "ada"}
    cookie = f"wreath_session={_forge('sid-1', b's' * 32)}"

    saves = store.saves
    response = await client.get("/read", headers={"cookie": cookie})

    assert response.json() == {"user": "ada"}
    assert store.touches == [("sid-1", 600)]  # extended ...
    assert store.saves == saves  # ... without rewriting the row
    assert response.header("set-cookie") is None  # and without reissuing the cookie


async def test_a_changed_session_is_saved_rather_than_touched() -> None:
    store = TouchableStore()
    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(session=SessionPolicy(secret="s" * 32, store=store, max_age=600))
    )

    @app.get("/write")
    async def write(request: Any) -> dict:
        request.state.session["seen"] = True
        return {"ok": True}

    client = TestClient(app)
    store.rows["sid-1"] = {"user": "ada"}

    await client.get("/write", headers={"cookie": f"wreath_session={_forge('sid-1', b's' * 32)}"})

    assert store.touches == []
    assert store.saves == 1


async def test_a_store_that_cannot_extend_a_row_is_left_alone() -> None:
    store = MemoryStore()
    app, _ = _app(store)
    client = TestClient(app)
    store.rows["sid-1"] = {"user": "ada"}

    saves = store.saves
    who = await client.get(
        "/whoami", headers={"cookie": f"wreath_session={_forge('sid-1', b's' * 32)}"}
    )
    assert who.json() == {"user": "ada"}
    assert store.saves == saves
