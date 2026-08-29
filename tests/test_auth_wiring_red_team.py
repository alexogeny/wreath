from __future__ import annotations

import json
import os
from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import SessionIdentityBackend, authenticated
from wreath.policy import HttpPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.router import Router
from wreath.testing import TestClient

pytestmark = pytest.mark.asyncio

SECRET = "s" * 32


def _require(condition: object, message: str) -> None:
    """`assert`, except that `python -O` cannot delete it.

    Every check in this file goes through here. A red-team suite that empties
    itself under the one interpreter mode nothing else tests is the pattern this
    file is about, one level up.
    """
    if not condition:
        raise AssertionError(message)


def _cookie(response: Any) -> str:
    value = response.header("set-cookie")
    return value.split(";", 1)[0] if value else ""


# `SessionIdentityBackend` reads `request.state.session` during authentication.
# `SessionPolicy` registered anywhere on the *route* pipeline publishes it
# afterwards, so the backend is asked for an identity before the session exists.
# `Wreath` refuses that pairing at route-compile time -- but the scan only ever
# looked at `add_middleware()`, and a router is the other place the mistake is
# made.


def _session_app(*, scope: str) -> Wreath:
    """A session-authenticated app with the session middleware in `scope`.

    `global` is the correct wiring. The other four are the same mistake made in
    four supported places, and only one of them used to be refused.
    """
    app = Wreath()
    app.configure_auth(SessionIdentityBackend())
    session = SessionPolicy(secret=SECRET, secure=False)

    routes = Router()
    if scope == "router":
        routes = Router(middleware=[session])
    elif scope == "nested":
        routes = Router()

    @routes.post("/sign-in", middleware=[session] if scope == "route" else ())
    async def sign_in(request: Any) -> dict[str, Any]:
        request.state.session["principal"] = {"sub": "ada", "type": "User"}
        return {"ok": True}

    @routes.get("/me", middleware=[session] if scope == "route" else ())
    @authenticated()
    async def me(request: Any) -> dict[str, Any]:
        return {"id": request.identity.id}

    if scope == "global":
        app.configure_http_policy(HttpPolicy(session=session))
    elif scope == "app":
        app.add_middleware(session)

    if scope == "nested":
        # Two routers deep. `Router.routes` folds an included router's
        # middleware into each route, so nesting needs no special case in the
        # refusal -- this is what proves it.
        outer = Router(middleware=[session])
        outer.include_router(routes)
        app.include_router(outer)
    else:
        app.include_router(routes)
    return app


async def _outcome(app: Wreath) -> tuple[Any, Any]:
    """Either `("refused", message)`, or the two statuses a caller observes.

    The property, not the symptom: what must never happen is a sign-in that
    succeeds followed by a 401 on the cookie it just issued. Refusing at compile
    time is one acceptable answer and admitting the cookie is the other; this
    returns whichever happened so a test can reject the third.
    """
    try:
        app._compile_routes()
    except TypeError as refusal:
        return ("refused", str(refusal))
    async with TestClient(app) as client:
        signed_in = await client.post("/sign-in")
        me = await client.get("/me", headers={"cookie": _cookie(signed_in)})
        return (signed_in.status, me.status)


async def _scope_outcome(scope: str) -> tuple[Any, Any]:
    """Include registration-time refusals in the observable outcome."""
    try:
        app = _session_app(scope=scope)
    except TypeError as refusal:
        return ("refused", str(refusal))
    return await _outcome(app)


@pytest.mark.parametrize("scope", ["router", "nested", "route"])
async def test_route_scoped_session_middleware_never_401s_a_cookie_it_just_issued(
    scope: str,
) -> None:
    outcome = await _scope_outcome(scope)

    _require(
        outcome[0] == "refused",
        f"a {scope}-scoped session middleware was admitted and answered {outcome}",
    )
    # The remedy has to be in the message: the failure it replaces is a 401,
    # which says nothing about where the middleware was registered.
    message = str(outcome[1])
    for fragment in ("HttpPolicy", "configure_http_policy", "SessionPolicy"):
        _require(fragment in message, f"the refusal never names {fragment!r}: {message}")


async def test_the_application_scoped_registration_is_still_refused() -> None:
    outcome = await _scope_outcome("app")

    _require(outcome[0] == "refused", f"add_middleware() stopped being refused: {outcome}")


async def test_the_correct_global_registration_admits_a_valid_cookie() -> None:
    outcome = await _scope_outcome("global")

    _require(outcome == (200, 200), f"the global wiring did not sign anybody in: {outcome}")


async def test_a_router_middleware_that_publishes_no_session_is_not_refused() -> None:
    class _Noop:
        async def before(self, request: Any) -> None:
            return None

    app = Wreath()
    app.configure_auth(SessionIdentityBackend())
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret=SECRET, secure=False)))
    routes = Router(middleware=[_Noop()])

    @routes.get("/me")
    @authenticated()
    async def me(request: Any) -> dict[str, Any]:
        return {"id": request.identity.id}

    app.include_router(routes)
    app._compile_routes()  # must not raise


async def test_a_backend_that_reads_no_session_is_not_refused() -> None:
    app = Wreath()

    routes = Router(middleware=[SessionPolicy(secret=SECRET, secure=False)])

    @routes.get("/visit")
    async def visit(request: Any) -> dict[str, Any]:
        return {"n": len(request.state.session)}

    app.include_router(routes)
    try:
        app._compile_routes()
    except TypeError as refusal:
        message = str(refusal)
        for fragment in ("HttpPolicy", "configure_http_policy", "SessionPolicy"):
            _require(fragment in message, f"the refusal never names {fragment!r}: {message}")
    else:
        raise AssertionError("SessionPolicy was compiled onto a middleware tape")


class _KeyedSessionStore:
    """A session store that records which key it was asked to enumerate by.

    Its `delete_for` takes the key rather than assuming one, which is the
    capability the shipped `PostgresSessionStore` grew. `enumerated` is what
    makes the attack legible: the old code always said `principal`.
    """

    def __init__(self, session_key: str = "principal") -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.enumerated: list[str] = []
        self._session_key = session_key

    async def load(self, sid: str) -> dict[str, Any] | None:
        return self.rows.get(sid)

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        self.rows[sid] = dict(data)

    async def delete(self, sid: str) -> None:
        self.rows.pop(sid, None)

    async def delete_for(self, subject: str, session_key: str | None = None) -> int:
        key = self._session_key if session_key is None else session_key
        self.enumerated.append(key)
        gone = [
            sid for sid, data in self.rows.items() if (data.get(key) or {}).get("sub") == subject
        ]
        for sid in gone:
            del self.rows[sid]
        return len(gone)


class _LegacySessionStore(_KeyedSessionStore):
    """The pre-existing shape: `delete_for` takes a subject and nothing else.

    A double is never more capable than the real thing, and this one is
    deliberately *less*: it is the store somebody already wrote
    against the published signature, and the control below pins that the default
    wiring never hands it an argument it cannot take.
    """

    async def delete_for(self, subject: str) -> int:  # type: ignore[override]
        return await super().delete_for(subject)


def _reset_app(store: Any, *, session_key: str) -> tuple[Wreath, Any]:
    from wreath.users import InMemoryUserStore, user_router

    users = InMemoryUserStore()
    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(session=SessionPolicy(secret=SECRET, secure=False, store=store))
    )
    app.include_router(user_router(users, secret=SECRET, session_key=session_key, sessions=store))
    return app, users


async def _reset_token(users: Any, email: str) -> str:
    from wreath._userkit import fingerprint, sign_token

    user = await users.get_by_email(email)
    return sign_token(SECRET, "reset", user.id, ttl=3600, bound=fingerprint(user.hashed_password))


async def _sign_in_then_reset(store: Any, *, session_key: str) -> dict[str, Any]:
    from wreath.users import hash_password

    app, users = _reset_app(store, session_key=session_key)
    password = "correct horse battery staple"
    async with TestClient(app) as client:
        await users.create("ann@example.test", hash_password(password))
        signed_in = await client.post(
            "/users/login", json={"email": "ann@example.test", "password": password}
        )
        _require(signed_in.status == 200, f"the sign-in failed: {signed_in.json()}")
        _require(len(store.rows) == 1, f"no session row was written: {store.rows}")
        reset = await client.post(
            "/users/reset-password",
            json={
                "token": await _reset_token(users, "ann@example.test"),
                "password": "a-much-better-one",
            },
        )
    return {"reset": reset.status, "body": reset.json(), "rows": dict(store.rows)}


async def test_a_reset_under_a_renamed_session_key_actually_ends_the_session() -> None:
    store = _KeyedSessionStore(session_key="principal")

    result = await _sign_in_then_reset(store, session_key="account")

    _require(result["reset"] == 200, f"the reset itself failed: {result}")
    _require(
        store.enumerated == ["account"],
        f"the reset enumerated by {store.enumerated} rather than the router's key",
    )
    _require(result["rows"] == {}, f"the session survived its own password reset: {result}")


async def test_a_reset_on_the_default_key_still_ends_the_session() -> None:
    store = _KeyedSessionStore()

    result = await _sign_in_then_reset(store, session_key="principal")

    _require(result["reset"] == 200, f"the reset itself failed: {result}")
    _require(result["rows"] == {}, f"the session survived its own password reset: {result}")


async def test_a_store_from_before_the_second_parameter_still_works() -> None:
    store = _LegacySessionStore()

    result = await _sign_in_then_reset(store, session_key="principal")

    _require(result["reset"] == 200, f"the reset itself failed: {result}")
    _require(result["rows"] == {}, f"the session survived its own password reset: {result}")


async def test_a_store_that_cannot_enumerate_at_all_still_resets() -> None:
    from wreath.users import InMemoryUserStore, hash_password, reset_password_endpoint

    users = InMemoryUserStore()
    await users.create("ann@example.test", hash_password("correct horse battery staple"))

    class _Minimal:
        async def load(self, sid: str) -> None:
            return None

        async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
            return None

        async def delete(self, sid: str) -> None:
            return None

    done = await reset_password_endpoint(
        users,
        _Minimal(),
        secret=SECRET,
        token=await _reset_token(users, "ann@example.test"),
        new_password="a-much-better-one",
    )
    _require(done is True, "a store with no delete_for failed the reset")


class _FakeStatement:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def execute(self, *args: Any) -> str:
        self.calls.append(args)
        return "DELETE 2"


class _FakeDatabase:
    def __init__(self) -> None:
        self.statements: dict[str, tuple[str, _FakeStatement]] = {}

    def statement(self, name: str, sql: str, *, workload: str = "write") -> _FakeStatement:
        statement = _FakeStatement()
        self.statements[name] = (sql, statement)
        return statement


async def test_the_postgres_store_binds_the_session_key_it_was_given() -> None:
    from wreath.session_store import PostgresSessionStore

    database = _FakeDatabase()
    store = PostgresSessionStore(database, session_key="account")

    removed = await store.delete_for("u1")

    sql, statement = database.statements["wreath_session_delete_for_wreath_session"]
    _require(removed == 2, f"the delete count was not parsed: {removed}")
    _require("'principal'" not in sql, f"the session key is still hardcoded: {sql}")
    _require("'account'" not in sql, f"the session key was interpolated into SQL: {sql}")
    _require("data -> $2" in sql, f"the session key is not a bound parameter: {sql}")
    _require(
        statement.calls == [("u1", "account")],
        f"the configured key was not bound: {statement.calls}",
    )


async def test_the_postgres_store_defaults_to_principal() -> None:
    from wreath.session_store import PostgresSessionStore

    database = _FakeDatabase()
    store = PostgresSessionStore(database)

    await store.delete_for("u1")

    _, statement = database.statements["wreath_session_delete_for_wreath_session"]
    _require(
        statement.calls == [("u1", "principal")],
        f"the default key changed: {statement.calls}",
    )


_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
requires_db = pytest.mark.skipif(
    not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)"
)


def _table() -> str:
    """A per-worker table name.

    The session table is unqualified by design, so isolation is the table name
    rather than a schema -- and a shared one would race under `-n 6` exactly as
    a shared schema does (AGENTS.md).
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    return f"wsk_{worker}_session"


@requires_db
@pytest.mark.network
async def test_a_real_postgres_deletes_by_the_bound_session_key() -> None:
    from wreath.postgres import Database
    from wreath.session_store import PostgresSessionStore

    table = _table()
    database = Database(
        "main",
        os.environ["WREATH_TEST_POSTGRES_DSN"],
        pools={"write": {"min_size": 1, "max_size": 2}},
    )
    await database.start()
    try:
        store = PostgresSessionStore(database, table=table, session_key="account")
        connection = await database.acquire("write")
        try:
            await connection.execute(f"DROP TABLE IF EXISTS {table}")
            for statement in store.schema_sql().split(";\n"):
                if statement.strip():
                    await connection.execute(statement.strip())
            rows = {
                "mine-1": {"account": {"sub": "ada"}},
                "mine-2": {"account": {"sub": "ada"}},
                "theirs": {"account": {"sub": "grace"}},
                # Written under the *old* key: it must survive, because this
                # store was told the application renamed it.
                "stale": {"principal": {"sub": "ada"}},
            }
            for sid, data in rows.items():
                await connection.execute(
                    f"INSERT INTO {table} (sid, data, expires) VALUES "
                    f"($1, $2::jsonb, clock_timestamp() + interval '1 hour')",
                    sid,
                    json.dumps(data),
                )
        finally:
            await database.release("write", connection)

        removed = await store.delete_for("ada")

        _require(removed == 2, f"the real DELETE removed {removed} rows, not 2")
        connection = await database.acquire("write")
        try:
            left = await connection.fetch(f"SELECT sid FROM {table} ORDER BY sid")
            names = sorted(row[0] for row in left)
        finally:
            await database.release("write", connection)
        _require(names == ["stale", "theirs"], f"the wrong rows were deleted: {names}")

        # And the per-call override reaches the same prepared statement.
        _require(
            await store.delete_for("ada", "principal") == 1,
            "the per-call session key did not reach the statement",
        )
    finally:
        connection = await database.acquire("write")
        try:
            await connection.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            await database.release("write", connection)
        await database.stop()


class _Response:
    __slots__ = ("body", "status")

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def header(self, name: bytes) -> None:
        """No `Cache-Control`, so `JwksCache` keeps its default TTL."""
        return None


class _FakeIdp:
    """An HTTP client pinned to one origin, as `OidcProvider` is handed one.

    It answers the discovery path with whatever document the test wants and
    every other path with an empty JWKS, so `discover()` gets all the way
    through on a well-formed document.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document
        self.fetched: list[str] = []

    async def get(self, path: str) -> _Response:
        self.fetched.append(path)
        if path.startswith("/.well-known/"):
            return _Response(200, json.dumps(self._document).encode())
        return _Response(200, b'{"keys": []}')


_ISSUER = "https://idp.example"


def _document(**overrides: Any) -> dict[str, Any]:
    document = {
        "issuer": _ISSUER,
        "jwks_uri": f"{_ISSUER}/jwks",
        "token_endpoint": f"{_ISSUER}/token",
        "authorization_endpoint": f"{_ISSUER}/authorize",
    }
    document.update(overrides)
    return document


def _provider(document: dict[str, Any]) -> Any:
    from wreath._auth.oidc import OidcProvider

    return OidcProvider("idp", issuer=_ISSUER, audience=None, http_client=_FakeIdp(document))


async def _discover(provider: Any) -> str | None:
    try:
        await provider.discover()
    except ValueError as refusal:
        return str(refusal)
    return None


async def test_a_hostile_discovery_document_cannot_move_the_authorization_endpoint() -> None:
    provider = _provider(_document(authorization_endpoint="https://evil.test/authorize"))

    refusal = await _discover(provider)

    _require(refusal is not None, "an off-origin authorization_endpoint was accepted")
    _require("pinned issuer origin" in refusal, f"the refusal does not say why: {refusal}")
    _require(
        provider.authorization_endpoint is None,
        f"the off-origin endpoint was published anyway: {provider.authorization_endpoint}",
    )


async def test_a_same_origin_authorization_endpoint_stays_an_absolute_url() -> None:
    provider = _provider(_document())

    _require(await _discover(provider) is None, "a well-formed document was refused")
    _require(
        provider.authorization_endpoint == f"{_ISSUER}/authorize",
        f"the endpoint was rewritten: {provider.authorization_endpoint}",
    )


async def test_a_provider_that_publishes_no_authorization_endpoint_discovers() -> None:
    document = _document()
    del document["authorization_endpoint"]
    provider = _provider(document)

    _require(
        await _discover(provider) is None,
        "a document with no authorization_endpoint was refused",
    )
    _require(provider.authorization_endpoint is None, "an endpoint appeared from nowhere")


async def test_the_other_two_endpoints_are_still_pinned() -> None:
    refusal = await _discover(_provider(_document(jwks_uri="https://evil.test/jwks")))
    _require(refusal is not None, "an off-origin jwks_uri was accepted")

    from wreath._auth.oidc import _same_origin_path

    try:
        _same_origin_path(_ISSUER, "https://evil.test/token")
    except ValueError:
        pass
    else:
        raise AssertionError("an off-origin token endpoint was reduced to a path")
    _require(
        _same_origin_path(_ISSUER, f"{_ISSUER}/token?x=1") == "/token?x=1",
        "the same-origin path lost its query",
    )
