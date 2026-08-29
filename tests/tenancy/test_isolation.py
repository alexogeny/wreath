from __future__ import annotations

import pytest

from wreath.postgres import connect
from wreath.tenancy import (
    TenancyError,
    deprovision_tenant,
    isolation_report,
    provision_tenant,
    verify_isolation,
)

from ._deployment import _WORKER, ACME, APP_ROLE, CENTRAL, DSN, GLOBEX, app_dsn

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(DSN is None, reason="WREATH_TEST_POSTGRES_DSN is unset"),
]


async def test_a_tenant_role_cannot_read_another_tenants_table(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.fetch(f'SELECT * FROM "{GLOBEX.schema}".item')


async def test_a_tenant_role_cannot_write_another_tenants_table(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f"INSERT INTO \"{GLOBEX.schema}\".item (id, name) VALUES (99, 'x')")


async def test_a_tenant_role_cannot_delete_from_another_tenants_table(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f'DELETE FROM "{GLOBEX.schema}".item')


async def test_another_tenants_table_names_are_visible_in_the_catalog(acme) -> None:
    rows = await acme.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1", GLOBEX.schema)
    assert [str(row[0]) for row in rows] == ["b'item'"]


async def test_seeing_that_name_gives_no_access_to_the_table(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.fetch(f'SELECT count(*) FROM "{GLOBEX.schema}".item')


async def test_a_tenant_role_cannot_create_a_table_in_another_tenants_schema(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f'CREATE TABLE "{GLOBEX.schema}".sneak (id int)')


async def test_an_unqualified_read_resolves_to_this_tenants_own_table(acme) -> None:
    rows = await acme.fetch("SELECT name FROM item ORDER BY id")
    assert [str(row[0]) for row in rows] == [f"{ACME.key}-thing"]


async def test_a_tenant_role_can_read_the_central_schema(acme) -> None:
    rows = await acme.fetch(f'SELECT name FROM "{CENTRAL}".plan ORDER BY id')
    assert [str(row[0]) for row in rows] == ["free", "pro"]


async def test_a_tenant_role_cannot_write_the_central_schema(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f"UPDATE \"{CENTRAL}\".plan SET name = 'x'")


async def test_a_tenant_role_cannot_insert_into_the_central_schema(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f"INSERT INTO \"{CENTRAL}\".plan (id, name) VALUES (3, 'x')")


async def test_one_statement_may_join_a_tenant_table_to_a_central_table(acme) -> None:
    rows = await acme.fetch(
        f'SELECT i.name, p.name FROM item i JOIN "{CENTRAL}".plan p ON p.id = i.plan_id'
    )
    assert [(str(row[0]), str(row[1])) for row in rows] == [(f"{ACME.key}-thing", "free")]


async def test_reset_role_inside_the_transaction_is_a_dead_end(acme) -> None:
    await acme.execute("RESET ROLE")
    # Each read gets its own savepoint: the first `permission denied` aborts the
    # transaction, so a second statement in it fails with "current transaction
    # is aborted" -- which would pass a looser assertion for entirely the wrong
    # reason.
    for schema in (GLOBEX.schema, ACME.schema):
        await acme.execute("SAVEPOINT probe")
        with pytest.raises(Exception, match="permission denied"):
            await acme.fetch(f'SELECT * FROM "{schema}".item')
        await acme.execute("ROLLBACK TO SAVEPOINT probe")


async def test_the_binding_does_not_survive_the_transaction(app_connection) -> None:
    await app_connection.execute("BEGIN")
    for statement in ACME.context()._bind_statements():
        await app_connection.execute(statement)
    await app_connection.execute("COMMIT")

    rows = await app_connection.fetch("SELECT current_user, current_setting('search_path')")
    assert ACME.role not in str(rows[0][0])
    assert ACME.schema not in str(rows[0][1])


async def test_verify_isolation_refuses_an_inheriting_login_role(deployment) -> None:
    setup = await connect(str(DSN))
    try:
        await setup.execute(f'ALTER ROLE "{APP_ROLE}" INHERIT')
        connection = await connect(app_dsn())
        try:
            with pytest.raises(TenancyError, match="NOINHERIT"):
                await verify_isolation(connection)
        finally:
            await connection.close()
    finally:
        await setup.execute(f'ALTER ROLE "{APP_ROLE}" NOINHERIT')
        await setup.close()


async def test_verify_isolation_refuses_a_superuser() -> None:
    connection = await connect(str(DSN))
    try:
        with pytest.raises(TenancyError, match="superuser"):
            await verify_isolation(connection)
    finally:
        await connection.close()


async def test_verify_isolation_refuses_a_role_that_owns_a_tenant_schema(
    deployment,
) -> None:
    setup = await connect(str(DSN))
    try:
        await setup.execute(f'ALTER SCHEMA "{ACME.schema}" OWNER TO "{APP_ROLE}"')
        connection = await connect(app_dsn())
        try:
            with pytest.raises(TenancyError, match="owns"):
                await verify_isolation(connection, schemas=[ACME.schema])
        finally:
            await connection.close()
    finally:
        await setup.execute(f'ALTER SCHEMA "{ACME.schema}" OWNER TO CURRENT_USER')
        await setup.close()


async def test_an_unprivileged_non_owning_role_passes_verification(app_connection) -> None:
    await verify_isolation(app_connection, schemas=[ACME.schema, GLOBEX.schema])


async def test_the_report_names_exactly_the_two_schemas_a_tenant_may_read(
    deployment,
) -> None:
    connection = await connect(str(DSN))
    try:
        report = await isolation_report(connection, ACME)
    finally:
        await connection.close()
    assert set(report.readable_schemas) == {CENTRAL, ACME.schema}
    assert set(report.writable_schemas) == {ACME.schema}
    assert not report.crosses_into(GLOBEX.schema)


async def test_a_table_created_after_provisioning_is_granted_with_no_second_step(
    deployment,
) -> None:
    connection = await connect(str(DSN))
    try:
        report = await isolation_report(connection, ACME)
    finally:
        await connection.close()
    assert report.ungranted_tables == ()


async def test_provisioning_is_idempotent(deployment) -> None:
    connection = await connect(str(DSN))
    try:
        again = await provision_tenant(
            connection,
            key=ACME.key,
            schema=ACME.schema,
            role=ACME.role,
            central=CENTRAL,
            login_role=APP_ROLE,
        )
        assert again.schema == ACME.schema
        report = await isolation_report(connection, ACME)
        assert set(report.readable_schemas) == {CENTRAL, ACME.schema}
    finally:
        await connection.close()


async def test_deprovisioning_refuses_while_the_schema_holds_tables(deployment) -> None:
    connection = await connect(str(DSN))
    try:
        with pytest.raises(TenancyError, match="not empty"):
            await deprovision_tenant(connection, ACME)
    finally:
        await connection.close()


async def test_deprovisioning_removes_the_schema_and_the_role_when_forced(
    deployment,
) -> None:
    connection = await connect(str(DSN))
    try:
        throwaway = await provision_tenant(
            connection, key=f"wt_gone_{_WORKER}", central=CENTRAL, login_role=APP_ROLE
        )
        await deprovision_tenant(connection, throwaway, force=True)
        rows = await connection.fetch(
            "SELECT 1 FROM pg_namespace WHERE nspname = $1", throwaway.schema
        )
        assert list(rows) == []
        roles = await connection.fetch("SELECT 1 FROM pg_roles WHERE rolname = $1", throwaway.role)
        assert list(roles) == []
    finally:
        await connection.close()
