"""Auto-CRUD: the double opt-in and the sensitive-field guard."""
from __future__ import annotations

import json

import pytest

from wreath.crud import Access, crud_router, sensitive_fields


def _model():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Account(Model, table="crud_accounts"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        email: Mapped[str] = column(Text)
        password_hash: Mapped[str] = column(Text, nullable=True)
        api_token: Mapped[str] = column(Text, nullable=True)

    return Account


class _Null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.added: list = []
        self.deleted: list = []
        self._next = 100

    async def get(self, model, pk):
        return self.rows.get(pk)

    async def fetch(self, query):
        return list(self.rows.values())

    def add(self, instance):
        if getattr(instance, "id", None) is None:
            instance.id = self._next
            self._next += 1
        self.rows[instance.id] = instance
        self.added.append(instance)

    def delete(self, instance):
        self.deleted.append(instance)
        self.rows.pop(instance.id, None)

    async def flush(self):
        pass

    def begin(self):
        return _Null()

    async def close(self):
        pass


class _Req:
    def __init__(self, path_params=None, query=b"", body=None):
        self.path_params = path_params or {}
        self.query_string = query
        self._body = body

    async def json(self):
        return self._body


def _routes(router):
    return {(r.methods[0], r.path): r.endpoint for r in router.routes}


pytestmark = pytest.mark.asyncio


async def test_sensitive_fields_are_detected() -> None:
    fields = sensitive_fields(_model())
    assert "password_hash" in fields and "api_token" in fields
    assert "name" not in fields and "email" not in fields


async def test_retrieve_hides_sensitive_fields_by_default() -> None:
    Account = _model()
    acct = Account(id=1, name="Ada", email="ada@x.io", password_hash="HASH", api_token="TOK")
    router = crud_router(Account, lambda request: _FakeSession({1: acct}))
    retrieve = _routes(router)[("GET", "/account/{id}")]

    data = json.loads((await retrieve(_Req(path_params={"id": "1"}))).body)
    assert data["name"] == "Ada" and data["email"] == "ada@x.io"
    assert "password_hash" not in data and "api_token" not in data


async def test_expose_opts_a_sensitive_field_into_output() -> None:
    Account = _model()
    acct = Account(id=1, name="Ada", email="a", password_hash="HASH", api_token="TOK")
    router = crud_router(Account, lambda request: _FakeSession({1: acct}), expose=("api_token",))
    retrieve = _routes(router)[("GET", "/account/{id}")]

    data = json.loads((await retrieve(_Req(path_params={"id": "1"}))).body)
    assert data["api_token"] == "TOK"          # explicitly exposed
    assert "password_hash" not in data         # still hidden


async def test_create_drops_sensitive_input() -> None:
    Account = _model()
    session = _FakeSession()
    router = crud_router(Account, lambda request: session)
    create = _routes(router)[("POST", "/account")]

    resp = await create(_Req(body={"name": "B", "email": "e", "password_hash": "INJECTED"}))
    assert resp.status == 201
    assert getattr(session.added[-1], "password_hash", None) is None   # not written
    assert "password_hash" not in json.loads(resp.body)


async def test_readonly_and_operations_subset() -> None:
    Account = _model()
    router = crud_router(Account, lambda request: _FakeSession(),
                         operations=("list", "retrieve"))
    methods = {method for method, _ in _routes(router)}
    assert methods == {"GET"}                  # no POST/PATCH/DELETE generated


async def test_delete_returns_204_and_404() -> None:
    Account = _model()
    acct = Account(id=1, name="A", email="e")
    session = _FakeSession({1: acct})
    delete = _routes(crud_router(Account, lambda request: session))[("DELETE", "/account/{id}")]

    assert (await delete(_Req(path_params={"id": "1"}))).status == 204
    assert (await delete(_Req(path_params={"id": "999"}))).status == 404


# --- authorization -----------------------------------------------------------


async def test_rule_resolution_prefers_specific_over_group_over_default() -> None:
    from wreath.crud import _rule_for

    authorize = {
        "*": Access.authenticated(),
        "read": Access.public(),
        "update": Access.roles("admin"),
    }
    assert _rule_for(authorize, "list").kind == "public"      # via "read" group
    assert _rule_for(authorize, "retrieve").kind == "public"  # via "read" group
    assert _rule_for(authorize, "update").kind == "roles"     # specific op wins
    assert _rule_for(authorize, "create").kind == "authenticated"  # "*" default
    assert _rule_for(None, "delete").kind == "public"         # no rules → public
    assert _rule_for(Access.deny(), "list").kind == "deny"    # single rule for all


async def test_deny_operation_answers_403_without_touching_db() -> None:
    Account = _model()
    session = _FakeSession({1: Account(id=1, name="A", email="e")})
    router = crud_router(Account, lambda request: session,
                         authorize={"delete": Access.deny()})
    routes = _routes(router)
    assert ("DELETE", "/account/{id}") in routes                 # route still exists
    resp = await routes[("DELETE", "/account/{id}")](_Req(path_params={"id": "1"}))
    assert resp.status == 403 and session.deleted == []          # nobody, ever


async def test_object_authorizer_enforces_row_level_ownership() -> None:
    Account = _model()
    owned = Account(id=1, name="Ada", email="ada@x.io")
    session = _FakeSession({1: owned})

    def only_owner(request, op, instance):
        # A stand-in for a real ownership/tenant check that needs the loaded row.
        return instance.email == request.path_params.get("who")

    update = _routes(crud_router(Account, lambda request: session,
                                 object_authorizer=only_owner))[("PATCH", "/account/{id}")]

    denied = await update(_Req(path_params={"id": "1", "who": "eve@x.io"}, body={"name": "X"}))
    assert denied.status == 403 and owned.name == "Ada"          # not mutated
    ok = await update(_Req(path_params={"id": "1", "who": "ada@x.io"}, body={"name": "New"}))
    assert ok.status == 200 and owned.name == "New"


async def test_authorize_attaches_enforceable_metadata() -> None:
    from wreath._auth.requirements import requirement_for

    Account = _model()
    router = crud_router(Account, lambda request: _FakeSession(), authorize={
        "read": Access.public(),
        "create": Access.permissions("account:create"),
        "update": Access.roles("admin"),
    })
    routes = _routes(router)
    assert requirement_for(routes[("GET", "/account")]).access_level == 0      # public
    upd = requirement_for(routes[("PATCH", "/account/{id}")])
    assert upd.authenticated and upd.role_checks[0].values == frozenset({"admin"})
    cre = requirement_for(routes[("POST", "/account")])
    assert cre.permission_checks[0].values == frozenset({"account:create"})


async def test_end_to_end_role_enforcement_through_the_app() -> None:
    import json as _json

    from wreath import Wreath
    from wreath.auth import BearerTokenBackend, Identity

    Account = _model()
    session = _FakeSession({1: Account(id=1, name="A", email="e")})
    identities = {
        "admin": Identity("admin", roles=frozenset({"admin"})),
        "user": Identity("user", roles=frozenset()),
    }

    async def verify(token: str):
        return identities.get(token)

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))
    app.include_router(crud_router(Account, lambda request: session, authorize={
        "read": Access.public(),
        "update": Access.roles("admin"),
        "delete": Access.deny(),
    }))

    async def drive(method, path, *, token=None, body=None):
        sent: list = []
        payload = _json.dumps(body).encode() if body is not None else b""

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            sent.append(message)

        headers = []
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if body is not None:
            headers.append((b"content-type", b"application/json"))
        await app({"type": "http", "method": method, "path": path, "headers": headers},
                  receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert await drive("GET", "/account/1") == 200                       # public read, no token
    assert await drive("PATCH", "/account/1", token="user", body={"name": "N"}) == 403
    assert session.rows[1].name == "A"                                   # untouched by denial
    assert await drive("PATCH", "/account/1", token="admin", body={"name": "N"}) == 200
    assert await drive("DELETE", "/account/1", token="admin") == 403     # deny: nobody


async def test_crud_is_off_until_enabled_at_the_app_level() -> None:
    from wreath import Wreath

    Account = _model()
    app = Wreath()
    with pytest.raises(RuntimeError, match="enable_crud"):
        app.crud(Account, lambda request: _FakeSession())
    app.enable_crud()
    app.crud(Account, lambda request: _FakeSession())     # now allowed
