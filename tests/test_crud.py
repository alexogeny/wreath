from __future__ import annotations

import json
from typing import Any

import pytest

from wreath.crud import Access, sensitive_fields
from wreath.crud import crud_router as _crud_router


def crud_router(*args, **kwargs):
    kwargs.setdefault("authorize", Access.public())
    return _crud_router(*args, **kwargs)


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
    assert data["api_token"] == "TOK"  # explicitly exposed
    assert "password_hash" not in data  # still hidden


async def test_create_drops_sensitive_input() -> None:
    Account = _model()
    session = _FakeSession()
    router = crud_router(Account, lambda request: session)
    create = _routes(router)[("POST", "/account")]

    resp = await create(_Req(body={"name": "B", "email": "e", "password_hash": "INJECTED"}))
    assert resp.status == 201
    assert getattr(session.added[-1], "password_hash", None) is None  # not written
    assert "password_hash" not in json.loads(resp.body)


async def test_readonly_and_operations_subset() -> None:
    Account = _model()
    router = crud_router(Account, lambda request: _FakeSession(), operations=("list", "retrieve"))
    methods = {method for method, _ in _routes(router)}
    assert methods == {"GET"}  # no POST/PATCH/DELETE generated


# The three tests below exist because `wreath mutant` removed each of these
# clauses from `writable_fields` / `output_fields` and the whole suite stayed
# green. Each control was correct; none of them was watched. The test above is
# where they looked like they lived -- it is named for `readonly=` and asserts
# only the `operations=` subset, which is exactly the shape of a check with nothing to check.


def _owned_model():
    """`crud_router`'s own docstring example: a server-set `owner_id`."""
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Widget(Model, table="crud_widgets"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        owner_id: Mapped[int] = column(Int64, nullable=True)

    return Widget


async def test_a_readonly_column_is_silently_dropped_from_input() -> None:
    Widget = _owned_model()

    class _ServerSets(_FakeSession):
        """What a column default or a trigger does, which is why `readonly=` exists."""

        def add(self, instance):
            super().add(instance)
            instance.owner_id = 7

    session = _ServerSets({1: Widget(id=1, name="A", owner_id=7)})
    routes = _routes(crud_router(Widget, lambda request: session, readonly=("owner_id",)))

    created = await routes[("POST", "/widget")](_Req(body={"name": "B", "owner_id": 99}))
    assert created.status == 201
    assert session.added[-1].owner_id == 7  # the server's value, not the body's

    patched = await routes[("PATCH", "/widget/{id}")](
        _Req(path_params={"id": "1"}, body={"name": "New", "owner_id": 99})
    )
    assert patched.status == 200
    assert session.rows[1].name == "New"  # the writable one landed
    assert session.rows[1].owner_id == 7  # the readonly one did not


async def test_the_primary_key_is_not_client_writable() -> None:
    Widget = _owned_model()
    session = _FakeSession({1: Widget(id=1, name="A", owner_id=7)})
    routes = _routes(crud_router(Widget, lambda request: session))

    patched = await routes[("PATCH", "/widget/{id}")](
        _Req(path_params={"id": "1"}, body={"id": 999, "name": "New"})
    )
    assert patched.status == 200
    assert session.rows[1].id == 1  # not repointed
    assert session.rows[1].name == "New"

    created = await routes[("POST", "/widget")](
        _Req(body={"id": 999, "name": "B", "owner_id": None})
    )
    assert created.status == 201
    assert session.added[-1].id == 100  # assigned by the store, not by the body
    assert 999 not in session.rows


async def test_an_excluded_column_is_never_serialized() -> None:
    Account = _model()
    row = Account(id=1, name="A", email="private@x.io")
    session = _FakeSession({1: row})

    retrieve = _routes(crud_router(Account, lambda request: session, exclude=("email",)))[
        ("GET", "/account/{id}")
    ]
    body = json.loads((await retrieve(_Req(path_params={"id": "1"}))).body)
    assert "email" not in body and body["name"] == "A"

    # `fields=` names what may leave; `exclude=` still subtracts from it, so the
    # two cannot be combined into a way of publishing an excluded column.
    narrowed = _routes(
        crud_router(Account, lambda request: session, fields=("name", "email"), exclude=("email",))
    )
    body = json.loads(
        (await narrowed[("GET", "/account/{id}")](_Req(path_params={"id": "1"}))).body
    )
    assert "email" not in body and body["name"] == "A"


async def test_delete_returns_204_and_404() -> None:
    Account = _model()
    acct = Account(id=1, name="A", email="e")
    session = _FakeSession({1: acct})
    delete = _routes(crud_router(Account, lambda request: session))[("DELETE", "/account/{id}")]

    assert (await delete(_Req(path_params={"id": "1"}))).status == 204
    assert (await delete(_Req(path_params={"id": "999"}))).status == 404


async def test_rule_resolution_prefers_specific_over_group_over_default() -> None:
    from wreath.crud import _rule_for

    authorize = {
        "*": Access.authenticated(),
        "read": Access.public(),
        "update": Access.roles("admin"),
    }
    assert _rule_for(authorize, "list").kind == "public"  # via "read" group
    assert _rule_for(authorize, "retrieve").kind == "public"  # via "read" group
    assert _rule_for(authorize, "update").kind == "roles"  # specific op wins
    assert _rule_for(authorize, "create").kind == "authenticated"  # "*" default
    with pytest.raises(ValueError, match="delete"):
        _rule_for({}, "delete")
    assert _rule_for(Access.deny(), "list").kind == "deny"  # single rule for all


async def test_deny_operation_answers_403_without_touching_db() -> None:
    Account = _model()
    session = _FakeSession({1: Account(id=1, name="A", email="e")})
    router = crud_router(
        Account,
        lambda request: session,
        operations=("delete",),
        authorize={"delete": Access.deny()},
    )
    routes = _routes(router)
    assert ("DELETE", "/account/{id}") in routes  # route still exists
    resp = await routes[("DELETE", "/account/{id}")](_Req(path_params={"id": "1"}))
    assert resp.status == 403 and session.deleted == []  # nobody, ever


@pytest.mark.parametrize(
    ("method", "path", "incoming"),
    [
        ("GET", "/account", _Req()),
        ("POST", "/account", _Req(body={"name": "B", "email": "b@example.com"})),
    ],
)
async def test_deny_short_circuits_list_and_create(
    method: str,
    path: str,
    incoming: _Req,
) -> None:
    Account = _model()
    existing = Account(id=1, name="A", email="a@example.com")
    session = _FakeSession({1: existing})
    routes = _routes(crud_router(Account, lambda request: session, authorize=Access.deny()))

    response = await routes[(method, path)](incoming)

    assert response.status == 403
    assert session.added == []
    assert session.rows == {1: existing}


async def test_create_refuses_a_non_object_json_body() -> None:
    Account = _model()
    session = _FakeSession()
    create = _routes(crud_router(Account, lambda request: session))[("POST", "/account")]

    response = await create(_Req(body=[{"name": "B"}]))

    assert response.status == 400
    assert json.loads(response.body) == {"error": "request body must be a JSON object"}
    assert session.added == []


async def test_object_authorizer_enforces_row_level_ownership() -> None:
    Account = _model()
    owned = Account(id=1, name="Ada", email="ada@x.io")
    session = _FakeSession({1: owned})

    def only_owner(request, op, instance):
        # A stand-in for a real ownership/tenant check that needs the loaded row.
        return instance.email == request.path_params.get("who")

    update = _routes(crud_router(Account, lambda request: session, object_authorizer=only_owner))[
        ("PATCH", "/account/{id}")
    ]

    denied = await update(_Req(path_params={"id": "1", "who": "eve@x.io"}, body={"name": "X"}))
    assert denied.status == 403 and owned.name == "Ada"  # not mutated
    ok = await update(_Req(path_params={"id": "1", "who": "ada@x.io"}, body={"name": "New"}))
    assert ok.status == 200 and owned.name == "New"


async def test_create_awaits_an_async_object_authorizer() -> None:
    Account = _model()
    session = _FakeSession()

    async def deny(request, op, instance):
        return False

    create = _routes(crud_router(Account, lambda request: session, object_authorizer=deny))[
        ("POST", "/account")
    ]

    response = await create(_Req(body={"name": "B", "email": "b@example.com"}))

    assert response.status == 403
    assert session.added == []


async def test_delete_honours_an_authorization_decision() -> None:
    from wreath._auth.models import AuthorizationDecision

    Account = _model()
    row = Account(id=1, name="A", email="a@example.com")
    session = _FakeSession({1: row})

    def deny(request, op, instance):
        return AuthorizationDecision(False, "not this row")

    delete = _routes(crud_router(Account, lambda request: session, object_authorizer=deny))[
        ("DELETE", "/account/{id}")
    ]

    response = await delete(_Req(path_params={"id": "1"}))

    assert response.status == 403
    assert session.deleted == []
    assert session.rows == {1: row}


async def test_authorize_attaches_enforceable_metadata() -> None:
    from wreath._auth.requirements import requirement_for

    Account = _model()
    router = crud_router(
        Account,
        lambda request: _FakeSession(),
        authorize={
            "read": Access.public(),
            "create": Access.permissions("account:create"),
            "update": Access.roles("admin"),
            "delete": Access.deny(),
        },
    )
    routes = _routes(router)
    assert requirement_for(routes[("GET", "/account")]).access_level == 0  # public
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
    app.include_router(
        crud_router(
            Account,
            lambda request: session,
            authorize={
                "read": Access.public(),
                "create": Access.deny(),
                "update": Access.roles("admin"),
                "delete": Access.deny(),
            },
        )
    )

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
        await app(
            {"type": "http", "method": method, "path": path, "headers": headers}, receive, send
        )
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert await drive("GET", "/account/1") == 200  # public read, no token
    assert await drive("PATCH", "/account/1", token="user", body={"name": "N"}) == 403
    assert session.rows[1].name == "A"  # untouched by denial
    assert await drive("PATCH", "/account/1", token="admin", body={"name": "N"}) == 200
    assert await drive("DELETE", "/account/1", token="admin") == 403  # deny: nobody


async def test_within_composes_step_up_with_the_rule_rather_than_replacing_it() -> None:
    from wreath._auth.requirements import requirement_for

    Account = _model()
    router = crud_router(
        Account,
        lambda request: _FakeSession(),
        authorize={
            "read": Access.authenticated(),
            "write": Access.deny(),
            "delete": Access.roles("admin").within(300),
        },
    )
    routes = _routes(router)
    rule = requirement_for(routes[("DELETE", "/account/{id}")])
    assert rule.role_checks[0].values == frozenset({"admin"})  # the kind survives
    assert rule.second_factor == 300.0  # and the window is on top
    assert rule.authenticated is True
    # Not applied to the operations that did not ask for it.
    assert requirement_for(routes[("GET", "/account/{id}")]).second_factor is None


async def test_within_leaves_the_rule_it_was_called_on_alone() -> None:
    base = Access.roles("admin")
    stepped = base.within(300)
    assert base.second_factor is None and stepped.second_factor == 300.0
    assert stepped.values == ("admin",) and stepped.kind == "roles"


async def test_within_is_refused_where_it_would_mean_nothing() -> None:
    # The two say *why* they are contradictions, and the reasons are opposite:
    # one admits too many callers and the other admits none.
    with pytest.raises(ValueError, match="admits callers who have none"):
        Access.public().within(300)
    with pytest.raises(ValueError, match="admits nobody at all"):
        Access.deny().within(300)
    with pytest.raises(ValueError, match="no caller can satisfy"):
        Access.roles("admin").within(0)


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), float("-inf")])
async def test_within_refuses_non_finite_freshness_windows(seconds: float) -> None:
    with pytest.raises(ValueError, match="positive finite number"):
        Access.roles("admin").within(seconds)


async def test_within_refuses_a_window_too_large_for_finite_seconds() -> None:
    with pytest.raises(ValueError, match="positive finite number"):
        Access.roles("admin").within(10**1000)


@pytest.mark.parametrize("seconds", [True, "300", None])
async def test_within_refuses_non_numeric_freshness_windows(seconds: Any) -> None:
    with pytest.raises(TypeError, match="positive finite number"):
        Access.roles("admin").within(seconds)


async def test_step_up_on_a_generated_delete_is_enforced_through_the_app() -> None:
    import time

    from wreath import Wreath
    from wreath.auth import BearerTokenBackend, Identity

    Account = _model()
    session = _FakeSession({1: Account(id=1, name="A", email="e")})
    now = int(time.time())
    identities = {
        # Same roles, same everything, differing only in when they last proved a
        # factor -- which is the distinction step-up exists to make.
        "stale": Identity(
            "admin", roles=frozenset({"admin"}), claims={"second_factor_at": now - 4000}
        ),
        "fresh": Identity("admin", roles=frozenset({"admin"}), claims={"second_factor_at": now}),
        "never": Identity("admin", roles=frozenset({"admin"})),
    }

    async def verify(token: str):
        return identities.get(token)

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))
    app.include_router(
        crud_router(
            Account,
            lambda request: session,
            authorize={
                "read": Access.public(),
                "write": Access.deny(),
                "delete": Access.roles("admin").within(300),
            },
        )
    )

    async def drive(method, path, *, token=None):
        sent: list = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        await app(
            {"type": "http", "method": method, "path": path, "headers": headers}, receive, send
        )
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert await drive("DELETE", "/account/1", token="never") == 403
    assert await drive("DELETE", "/account/1", token="stale") == 403
    assert session.deleted == []  # neither one got through
    assert await drive("DELETE", "/account/1", token="fresh") == 204
    assert [row.id for row in session.deleted] == [1]


async def test_crud_is_off_until_enabled_at_the_app_level() -> None:
    from wreath import Wreath

    Account = _model()
    app = Wreath()
    with pytest.raises(RuntimeError, match="enable_crud"):
        app.crud(Account, lambda request: _FakeSession())
    app.enable_crud()
    app.crud(Account, lambda request: _FakeSession(), authorize=Access.public())


# `wreath.crud` predates `Vector` and `TsVector`. Its defaults -- serialize every
# column that does not look like a secret, accept every column that is not the
# primary key -- were right for the types that existed then, and wrong for a
# column that *indexes* content rather than carrying it.


def _doc_model():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text, TsVector, Vector

    class Doc(Model, table="crud_docs"):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text)
        # Nullable because that is the shape an application with a background
        # embedder has: the row is created from what a person typed, and the
        # vector arrives later. A NOT NULL vector column and a generated `POST`
        # are incompatible once the column stops being writable -- `expose=` it,
        # or give it a server default.
        embedding: Mapped[list] = column(Vector(3), nullable=True)
        search: Mapped[bytes] = column(TsVector("english", sources=("title",)), index="gin")

    return Doc


def _doc(model, identifier=1, title="llamas", embedding=(1.0, 0.0, 0.0)):
    """One row as a `fetch` would hand it back, generated column included."""
    instance = model(id=identifier, title=title, embedding=list(embedding))
    instance._orm_set_loaded(model.__wreath_column_map__["search"].index, b"'llama':1")
    return instance


async def test_a_vector_column_is_not_client_writable_by_default() -> None:
    Doc = _doc_model()
    session = _FakeSession({1: _doc(Doc)})
    update = _routes(crud_router(Doc, lambda request: session))[("PATCH", "/doc/{id}")]

    response = await update(
        _Req(
            path_params={"id": "1"},
            body={
                "title": "alpacas",
                "embedding": [0.0, 0.0, 1.0],
            },
        )
    )
    assert response.status == 200
    assert session.rows[1].title == "alpacas"  # ordinary content, written
    assert session.rows[1].embedding == [1.0, 0.0, 0.0]  # the index, untouched


async def test_a_generated_column_is_dropped_from_input_not_rejected() -> None:
    Doc = _doc_model()
    session = _FakeSession()
    create = _routes(crud_router(Doc, lambda request: session))[("POST", "/doc")]

    response = await create(_Req(body={"title": "llamas", "search": "'llama':1"}))
    assert response.status == 201
    assert session.added[-1].title == "llamas"


async def test_retrieval_columns_are_not_serialized_by_default() -> None:
    Doc = _doc_model()
    session = _FakeSession({1: _doc(Doc)})
    routes = _routes(crud_router(Doc, lambda request: session))

    row = json.loads((await routes[("GET", "/doc/{id}")](_Req(path_params={"id": "1"}))).body)
    assert row == {"id": 1, "title": "llamas"}
    page = json.loads((await routes[("GET", "/doc")](_Req())).body)
    assert page["items"] == [{"id": 1, "title": "llamas"}]


async def test_expose_opts_a_vector_back_into_output_and_input() -> None:
    Doc = _doc_model()
    session = _FakeSession({1: _doc(Doc)})
    routes = _routes(crud_router(Doc, lambda request: session, expose=("embedding",)))

    row = json.loads((await routes[("GET", "/doc/{id}")](_Req(path_params={"id": "1"}))).body)
    assert row["embedding"] == [1.0, 0.0, 0.0]
    assert "search" not in row
    response = await routes[("PATCH", "/doc/{id}")](
        _Req(path_params={"id": "1"}, body={"embedding": [0.0, 1.0, 0.0]})
    )
    assert response.status == 200
    assert session.rows[1].embedding == [0.0, 1.0, 0.0]


async def test_expose_can_read_a_generated_column_but_never_write_one() -> None:
    Doc = _doc_model()
    session = _FakeSession({1: _doc(Doc)})
    routes = _routes(crud_router(Doc, lambda request: session, expose=("search",)))

    row = json.loads((await routes[("GET", "/doc/{id}")](_Req(path_params={"id": "1"}))).body)
    assert row["search"] == "J2xsYW1hJzox"
    response = await routes[("PATCH", "/doc/{id}")](
        _Req(path_params={"id": "1"}, body={"search": "'alpaca':1"})
    )
    assert response.status == 200  # dropped, never rejected
    assert session.rows[1].search == b"'llama':1"
