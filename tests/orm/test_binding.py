from __future__ import annotations

from typing import Annotated, Any

import pytest

from wreath import Wreath
from wreath.binding import compile_binder
from wreath.orm import FromORM, Session
from wreath.orm.errors import DeclarationError
from wreath.postgres import Connection, FromDatabase
from wreath.testing import TestClient

from .conftest import FakeDatabase, Membership, Post, User, user_row

pytestmark = pytest.mark.asyncio


def build_app(monkeypatch: pytest.MonkeyPatch) -> tuple[Wreath, FakeDatabase]:
    """A Wreath app whose 'main' database is the fake driver."""
    database = FakeDatabase()

    def postgres(self: Wreath, name: str, **kwargs: Any) -> FakeDatabase:
        database.name = name
        self._databases[name] = database
        self._dirty = True
        return database

    monkeypatch.setattr(Wreath, "postgres", postgres)
    app = Wreath()
    app.postgres("main", dsn="postgresql://localhost/app")
    return app, database


async def test_orm_registers_against_a_configured_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    registry = app.orm(database="main", models=[User, Post], validate_schema="off")
    assert registry.database is database
    assert app.state.orm_main is registry


async def test_an_unknown_database_fails_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    with pytest.raises(ValueError, match="unknown PostgreSQL database"):
        app.orm(database="missing", models=[User, Post], validate_schema="off")


async def test_one_registry_per_database(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")
    with pytest.raises(ValueError, match="duplicate ORM registry"):
        app.orm(database="main", models=[Membership], validate_schema="off")


async def test_invalid_models_fail_at_registration_not_at_request_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    with pytest.raises(DeclarationError):
        app.orm(database="main", models=[User], validate_schema="off")


async def test_two_apps_can_map_one_model_set_to_different_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_app, first_db = build_app(monkeypatch)
    first = first_app.orm(database="main", models=[User, Post], validate_schema="off")
    second_app, second_db = build_app(monkeypatch)
    second = second_app.orm(database="main", models=[User, Post], validate_schema="off")

    assert first is not second
    assert first.database is not second.database
    assert first.spec_for(User) is not second.spec_for(User)
    # Same declarations, so the fingerprints agree even though nothing is shared.
    assert first.fingerprint == second.fingerprint

    from wreath.orm.compiler import compile_select, shape_of

    # Caches are per registry: compiling in one leaves the other empty, and the
    # two never hand out the same plan object.
    compile_select(first, User.select())
    assert first.cached_plan_count == 1 and second.cached_plan_count == 0
    compile_select(second, User.select())
    key = shape_of(first, User.select())
    assert first.cached_plan(key) is not second.cached_plan(key)

    first_db.connection.script("users", [user_row(1)])
    second_db.connection.script("users", [user_row(1)])
    left = Session(first, "read")
    right = Session(second, "read")
    assert (await left.fetch(User.select()))[0] is not (await right.fetch(User.select()))[0]


async def test_a_handler_receives_a_request_scoped_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")
    seen: list[Session] = []

    @app.get("/users")
    async def list_users(
        request: Any, session: Annotated[Session, FromORM("main", workload="read")]
    ) -> Any:
        seen.append(session)
        database.connection.script("users", [user_row(1, "a@b.c")])
        return {"email": (await session.fetch(User.select()))[0].email}

    async with TestClient(app) as client:
        response = await client.get("/users")
    assert response.json() == {"email": "a@b.c"}
    assert seen[0].closed
    assert database.released == 1


async def test_one_session_per_registry_and_workload_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")
    captured: list[Any] = []

    @app.get("/dedupe")
    async def dedupe(
        request: Any,
        first: Annotated[Session, FromORM("main", workload="read")],
        second: Annotated[Session, FromORM("main", workload="read")],
        writer: Annotated[Session, FromORM("main", workload="write")],
    ) -> Any:
        captured.extend((first, second, writer))
        return {}

    async with TestClient(app) as client:
        await client.get("/dedupe")
    first, second, writer = captured
    assert first is second
    assert writer is not first


async def test_session_tenant_marker_resolves_for_the_request() -> None:
    from wreath.request import Request

    context = object()
    resolved: list[Any] = []
    captured: list[Any] = []

    class Marker:
        def resolve(self, request: Any) -> Any:
            resolved.append(request)
            return context

    marker = Marker()

    async def tenant_session(request: Any, session: Any) -> Any:
        captured.append(session._tenant)
        return None

    tenant_session.__annotations__["session"] = Annotated[Session, FromORM("main", tenant=marker)]

    class SchemaMode:
        kind = "isolated"

    class Registry:
        schema_mode = SchemaMode()
        statement_timeout = None

    bound = compile_binder(
        tenant_session,
        "/tenant",
        orm_registries={"main": Registry()},
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/tenant",
            "query_string": b"",
            "headers": [],
        },
        None,
        None,
    )

    await bound(request)

    assert len(resolved) == 1
    assert captured == [context]


async def test_an_unused_session_leases_no_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")

    @app.get("/idle")
    async def idle(request: Any, session: Annotated[Session, FromORM("main")]) -> Any:
        return {}

    async with TestClient(app) as client:
        await client.get("/idle")
    assert database.acquired == 0
    assert database.released == 0


async def test_the_connection_returns_once_when_a_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")

    @app.get("/boom")
    async def boom(request: Any, session: Annotated[Session, FromORM("main")]) -> Any:
        database.connection.script("users", [user_row(1)])
        await session.fetch(User.select())
        raise RuntimeError("handler failed")

    async with TestClient(app) as client:
        response = await client.get("/boom")
    assert response.status == 500
    assert database.released == 1


async def test_an_open_transaction_is_rolled_back_when_the_request_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")

    @app.get("/leaky")
    async def leaky(
        request: Any, session: Annotated[Session, FromORM("main", workload="write")]
    ) -> Any:
        # Deliberately enter a transaction without leaving it.
        await session._acquire()
        await session.begin().__aenter__()
        return {}

    async with TestClient(app) as client:
        await client.get("/leaky")
    assert database.connection.statements == ["BEGIN", "ROLLBACK"]
    assert database.released == 1


async def test_sessions_close_in_reverse_acquisition_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")
    order: list[str] = []
    original = Session.close

    async def close(self: Session) -> None:
        order.append(self.workload)
        await original(self)

    monkeypatch.setattr(Session, "close", close)

    @app.get("/order")
    async def ordered(
        request: Any,
        reader: Annotated[Session, FromORM("main", workload="read")],
        writer: Annotated[Session, FromORM("main", workload="write")],
    ) -> Any:
        return {}

    async with TestClient(app) as client:
        await client.get("/order")
    assert order == ["write", "read"]


async def test_a_bare_session_annotation_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")

    async def handler(request: Any, session: Session) -> Any:
        return {}

    with pytest.raises(TypeError, match="Annotated\\[Session, FromORM"):
        compile_binder(handler, "/x", orm_registries={"main": object()})


async def test_an_unknown_registry_fails_while_compiling_the_handler() -> None:
    async def handler(request: Any, session: Annotated[Session, FromORM("nope")]) -> Any:
        return {}

    with pytest.raises(TypeError, match="unknown ORM registry"):
        compile_binder(handler, "/x", orm_registries={"main": object()})


async def test_an_ambiguous_registry_requires_a_name() -> None:
    async def handler(request: Any, session: Annotated[Session, FromORM()]) -> Any:
        return {}

    with pytest.raises(TypeError, match="more than one ORM registry"):
        compile_binder(handler, "/x", orm_registries={"a": object(), "b": object()})


async def test_an_invalid_workload_is_rejected_at_declaration() -> None:
    with pytest.raises(ValueError, match="workload"):
        FromORM("main", workload="nonsense")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="security_read"):
        FromORM("main", workload="security_read")


async def test_sessions_and_raw_connections_coexist_in_one_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = build_app(monkeypatch)
    app.orm(database="main", models=[User, Post], validate_schema="off")

    @app.get("/both")
    async def both(
        request: Any,
        session: Annotated[Session, FromORM("main", workload="read")],
        connection: Annotated[Connection, FromDatabase("main", workload="read")],
    ) -> Any:
        database.connection.script("users", [user_row(1)])
        database.connection.script("SELECT 1", [[1]])
        users = await session.fetch(User.select())
        raw = await connection.fetchval("SELECT 1")
        return {"users": len(users), "raw": raw}

    async with TestClient(app) as client:
        response = await client.get("/both")
    assert response.json() == {"users": 1, "raw": 1}
    # One for the injected connection, one for the session's own lease.
    assert database.released == 2
