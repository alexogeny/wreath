from __future__ import annotations

import pytest
from _pgfidelity import check_for

from wreath.orm import (
    CENTRAL_SCHEMA,
    TENANT_SCHEMA,
    Mapped,
    Model,
    SchemaMode,
    Session,
    SessionError,
    TenantContext,
    column,
)
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text


class RecordingConnection:
    """A connection that records every statement and replays scripted rows."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.responses: list[tuple[str, list]] = []
        self.closed = False

    def script(self, fragment: str, rows: list) -> None:
        self.responses.append((fragment, rows))

    def _result(self, sql: str) -> list:
        for fragment, rows in self.responses:
            if fragment in sql:
                return rows
        return []

    async def execute(self, sql: str, *args: object) -> str:
        check_for(self, sql, args)
        self.statements.append(sql)
        return "OK"

    async def fetch(self, sql: str, *args: object) -> list:
        check_for(self, sql, args)
        self.statements.append(sql)
        return list(self._result(sql))

    async def fetchrow(self, sql: str, *args: object) -> object:
        check_for(self, sql, args)
        self.statements.append(sql)
        rows = self._result(sql)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: object) -> object:
        check_for(self, sql, args)
        row = await self.fetchrow(sql, *args)
        return row[0] if row else None

    async def close(self) -> None:
        self.closed = True


class FakeDatabase:
    """Hands out one recording connection like a real pool would."""

    def __init__(self, name: str = "main") -> None:
        self.name = name
        self.connection = RecordingConnection()

    async def acquire(self, workload: str = "read") -> RecordingConnection:
        return self.connection

    async def release(self, workload: str, connection: RecordingConnection) -> None:
        pass


class Account(Model, table="accounts", schema=CENTRAL_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)


class Order(Model, table="orders", schema=TENANT_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)


def _isolated_registry(isolation: str = "namespace") -> Registry:
    return Registry(
        FakeDatabase(),
        [Account, Order],
        validate_schema="off",
        schema_mode=SchemaMode.isolated(central="wreath_core", isolation=isolation),
    )


def _single_registry() -> Registry:
    return Registry(
        FakeDatabase(),
        [Account, Order],
        validate_schema="off",
        schema_mode=SchemaMode.single("app"),
    )


def test_context_validates_identifiers() -> None:
    from wreath.orm.errors import DeclarationError

    TenantContext(schema="tenant_42")
    TenantContext(schema="tenant_42", role="tenant_42_role")
    with pytest.raises(DeclarationError):
        TenantContext(schema="drop table; --")
    with pytest.raises(DeclarationError):
        TenantContext(schema="tenant_42", role="1nvalid role")


def test_namespace_context_binds_only_search_path() -> None:
    assert TenantContext(schema="t1")._bind_statements() == ('SET LOCAL search_path = "t1"',)


def test_role_context_binds_search_path_then_role() -> None:
    assert TenantContext(schema="t1", role="t1_role")._bind_statements() == (
        'SET LOCAL search_path = "t1"',
        'SET LOCAL ROLE "t1_role"',
    )


def test_isolated_registry_requires_a_tenant_context() -> None:
    with pytest.raises(SessionError, match="needs a tenant context"):
        Session(_isolated_registry(), "read")


def test_tenant_context_is_rejected_for_a_single_registry() -> None:
    with pytest.raises(SessionError, match="only meaningful for an isolated"):
        Session(_single_registry(), "read", tenant=TenantContext(schema="t1"))


def test_isolated_registry_with_a_context_constructs() -> None:
    session = Session(_isolated_registry(), "write", tenant=TenantContext(schema="t1"))
    assert not session.closed


@pytest.mark.asyncio
async def test_begin_binds_search_path_transaction_locally() -> None:
    database = FakeDatabase()
    registry = Registry(
        database,
        [Account, Order],
        validate_schema="off",
        schema_mode=SchemaMode.isolated(central="wreath_core"),
    )
    session = Session(registry, "write", tenant=TenantContext(schema="tenant_7"))
    async with session.begin():
        pass
    statements = database.connection.statements
    assert statements[0] == "BEGIN"
    assert statements[1] == 'SET LOCAL search_path = "tenant_7"'
    assert statements[-1] == "COMMIT"


@pytest.mark.asyncio
async def test_role_isolation_binds_role_after_search_path() -> None:
    database = FakeDatabase()
    registry = Registry(
        database,
        [Account, Order],
        validate_schema="off",
        schema_mode=SchemaMode.isolated(central="wreath_core", isolation="role"),
    )
    session = Session(
        registry, "write", tenant=TenantContext(schema="tenant_7", role="tenant_7_role")
    )
    async with session.begin():
        pass
    statements = database.connection.statements
    assert statements[:3] == [
        "BEGIN",
        'SET LOCAL search_path = "tenant_7"',
        'SET LOCAL ROLE "tenant_7_role"',
    ]


@pytest.mark.asyncio
async def test_a_savepoint_does_not_rebind_the_context() -> None:
    database = FakeDatabase()
    registry = Registry(
        database,
        [Account, Order],
        validate_schema="off",
        schema_mode=SchemaMode.isolated(central="wreath_core"),
    )
    session = Session(registry, "write", tenant=TenantContext(schema="tenant_7"))
    async with session.begin():
        async with session.begin():
            pass
    binds = [s for s in database.connection.statements if s.startswith("SET LOCAL")]
    assert binds == ['SET LOCAL search_path = "tenant_7"']


@pytest.mark.asyncio
async def test_tenant_read_outside_a_transaction_is_refused() -> None:
    session = Session(_isolated_registry(), "read", tenant=TenantContext(schema="t1"))
    with pytest.raises(SessionError, match="must run inside an explicit transaction"):
        await session.fetch(Order.select())


@pytest.mark.asyncio
async def test_tenant_read_inside_a_transaction_runs_unqualified() -> None:
    database = FakeDatabase()
    registry = Registry(
        database,
        [Account, Order],
        validate_schema="off",
        schema_mode=SchemaMode.isolated(central="wreath_core"),
    )
    database.connection.script("orders", [])
    session = Session(registry, "read", tenant=TenantContext(schema="tenant_7"))
    async with session.begin():
        await session.fetch(Order.select())
    order_sql = [s for s in database.connection.statements if "orders" in s]
    assert order_sql, "the tenant query never ran"
    # Tenant SQL is unqualified: it names the bare table and relies on search_path.
    assert '"tenant_7".' not in order_sql[0]
    assert '"orders"' in order_sql[0]
