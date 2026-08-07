"""The boundary, proved against a live server.

Every test here points a deliberately hostile query at another tenant and
requires PostgreSQL to refuse it. A test asserting that the *right* rows came
back would prove only that the happy path works, which was never in doubt; the
claim worth making is that the wrong rows are unreachable, and the only thing
that can establish it is the server saying no.

Migrated from `tests/thesis/test_tenancy_contract.py`.
"""

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

from ._deployment import ACME, APP_ROLE, CENTRAL, DSN, GLOBEX, app_dsn

pytestmark = [
    pytest.mark.database,
    pytest.mark.skipif(DSN is None, reason="WREATH_TEST_POSTGRES_DSN is unset"),
]


# --- what a tenant cannot reach ---------------------------------------------


async def test_a_tenant_role_cannot_read_another_tenants_table(acme) -> None:
    """The headline, and the one `search_path` alone cannot make true.

    Naming the other schema explicitly walks straight past a search path. Only a
    GRANT can refuse it.
    """
    with pytest.raises(Exception, match="permission denied"):
        await acme.fetch(f'SELECT * FROM "{GLOBEX.schema}".item')


async def test_a_tenant_role_cannot_write_another_tenants_table(acme) -> None:
    """Separately from reading, because `INSERT` is a separate privilege.

    A grant set that gets `SELECT` right and leaves `INSERT` open passes the
    test above and still loses the data.
    """
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(
            f"INSERT INTO \"{GLOBEX.schema}\".item (id, name) VALUES (99, 'x')")


async def test_a_tenant_role_cannot_delete_from_another_tenants_table(acme) -> None:
    """And `DELETE` is a third. The three are granted together and could be
    revoked apart, so they are asserted apart."""
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f'DELETE FROM "{GLOBEX.schema}".item')


async def test_another_tenants_table_names_are_visible_in_the_catalog(acme) -> None:
    """**The limit of this boundary, asserted so nobody assumes otherwise.**

    The checklist for this module originally claimed a tenant could not
    enumerate another tenant's tables. That is false, and it is false about
    PostgreSQL rather than about this implementation: `pg_catalog` is readable
    by every role, and revoking `USAGE` on a schema prevents *reaching* its
    objects, not *seeing* that they exist. There is no grant that hides a row of
    `pg_class` from a peer.

    So the honest statement is: names leak, data does not. If a deployment's
    table names are themselves confidential -- a schema per customer named after
    the customer is the usual way this bites -- the answer is a database per
    tenant, not a grant. It is asserted here rather than left unsaid, because an
    isolation claim that is nearly true is worse than none.
    """
    rows = await acme.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = $1", GLOBEX.schema)
    assert [str(row[0]) for row in rows] == ["b'item'"]


async def test_seeing_that_name_gives_no_access_to_the_table(acme) -> None:
    """The half that does hold, stated next to the half that does not."""
    with pytest.raises(Exception, match="permission denied"):
        await acme.fetch(f'SELECT count(*) FROM "{GLOBEX.schema}".item')


async def test_a_tenant_role_cannot_create_a_table_in_another_tenants_schema(acme) -> None:
    """Writing to a neighbour is not only about existing tables."""
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f'CREATE TABLE "{GLOBEX.schema}".sneak (id int)')


async def test_an_unqualified_read_resolves_to_this_tenants_own_table(acme) -> None:
    """The ergonomics half. Tenant-local SQL stays unqualified and readable,
    and the search path is what makes it land in the right schema."""
    rows = await acme.fetch("SELECT name FROM item ORDER BY id")
    assert [str(row[0]) for row in rows] == [f"{ACME.key}-thing"]


# --- the central schema -----------------------------------------------------


async def test_a_tenant_role_can_read_the_central_schema(acme) -> None:
    """The case the whole design has to keep working.

    Immutable general-purpose rows live once and every tenant inherits them. If
    isolation cost this, applications would copy the table per tenant and it
    would stop being one vocabulary.
    """
    rows = await acme.fetch(f'SELECT name FROM "{CENTRAL}".plan ORDER BY id')
    assert [str(row[0]) for row in rows] == ["free", "pro"]


async def test_a_tenant_role_cannot_write_the_central_schema(acme) -> None:
    """"Immutable" that a tenant can `UPDATE` is a word, not a property."""
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f'UPDATE "{CENTRAL}".plan SET name = \'x\'')


async def test_a_tenant_role_cannot_insert_into_the_central_schema(acme) -> None:
    with pytest.raises(Exception, match="permission denied"):
        await acme.execute(f"INSERT INTO \"{CENTRAL}\".plan (id, name) VALUES (3, 'x')")


async def test_one_statement_may_join_a_tenant_table_to_a_central_table(acme) -> None:
    """The join has to work in one query rather than two round trips.

    If a central reference cost a second statement and an application-side join,
    nobody would use the central schema and the vocabulary would be copied.
    """
    rows = await acme.fetch(
        f'SELECT i.name, p.name FROM item i JOIN "{CENTRAL}".plan p ON p.id = i.plan_id')
    assert [(str(row[0]), str(row[1])) for row in rows] == [(f"{ACME.key}-thing", "free")]


# --- the escape hatches -----------------------------------------------------


async def test_reset_role_inside_the_transaction_is_a_dead_end(acme) -> None:
    """`SET LOCAL ROLE` is only a boundary if undoing it reaches nothing.

    The application's login role is `NOINHERIT`, so membership of the tenant
    roles gives it no ambient privilege: after `RESET ROLE` it holds nothing on
    any tenant schema, including the one it was just reading.
    """
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
    """`SET LOCAL` gives this; the test is what keeps it true.

    One `SET` that lost its `LOCAL` in a refactor is a tenant binding that
    outlives its transaction and serves the next borrower of that pooled
    connection from the wrong schema. Asserted from outside rather than trusted
    to the spelling.
    """
    await app_connection.execute("BEGIN")
    for statement in ACME.context()._bind_statements():
        await app_connection.execute(statement)
    await app_connection.execute("COMMIT")

    rows = await app_connection.fetch("SELECT current_user, current_setting('search_path')")
    assert ACME.role not in str(rows[0][0])
    assert ACME.schema not in str(rows[0][1])


# --- the startup refusals ---------------------------------------------------


async def test_verify_isolation_refuses_an_inheriting_login_role() -> None:
    """An inheriting role holds every tenant's privileges with no `SET ROLE`,
    which makes the whole boundary ambient and `RESET ROLE` an escape hatch.

    Asserting the distinct message: this refusal and the superuser one below
    both name the role, so a test matching the role name would pass on whichever
    branch fired.
    """
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
    """A superuser bypasses every GRANT, so the boundary would be decorative.

    The fixture's own DSN is a superuser, which is exactly why the assertions
    above run as somebody else.
    """
    connection = await connect(str(DSN))
    try:
        with pytest.raises(TenancyError, match="superuser"):
            await verify_isolation(connection)
    finally:
        await connection.close()


async def test_verify_isolation_refuses_a_role_that_owns_a_tenant_schema() -> None:
    """An owner's privileges are implicit and cannot be revoked from itself, so
    no grant set can compensate. The schemas have to be owned by a migration
    role the request path never connects as."""
    setup = await connect(str(DSN))
    try:
        await setup.execute(
            f'ALTER SCHEMA "{ACME.schema}" OWNER TO "{APP_ROLE}"')
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
    """The green half, so the refusals above are not passing for free.

    Without this, a `verify_isolation` that raised unconditionally would satisfy
    all three refusal tests.
    """
    await verify_isolation(app_connection, schemas=[ACME.schema, GLOBEX.schema])


# --- the audit --------------------------------------------------------------


async def test_the_report_names_exactly_the_two_schemas_a_tenant_may_read(
    deployment,
) -> None:
    """Isolation you cannot audit is isolation you are hoping for.

    Read out of `information_schema`, so the answer comes from the database
    rather than from the code that intended it.
    """
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
    """`ALTER DEFAULT PRIVILEGES` is what stops the grants drifting.

    Without it every migration that adds a table adds one the tenant cannot
    read, found by a 500 rather than by the deploy. The fixture creates `item`
    *after* provisioning precisely so this is a real question.
    """
    connection = await connect(str(DSN))
    try:
        report = await isolation_report(connection, ACME)
    finally:
        await connection.close()
    assert report.ungranted_tables == ()


# --- provisioning -----------------------------------------------------------


async def test_provisioning_is_idempotent(deployment) -> None:
    """A run stopped by a lock or a deploy is finished by running it again,
    rather than by reasoning about which of eleven statements completed."""
    connection = await connect(str(DSN))
    try:
        again = await provision_tenant(
            connection, key=ACME.key, schema=ACME.schema, role=ACME.role,
            central=CENTRAL, login_role=APP_ROLE)
        assert again.schema == ACME.schema
        report = await isolation_report(connection, ACME)
        assert set(report.readable_schemas) == {CENTRAL, ACME.schema}
    finally:
        await connection.close()


async def test_deprovisioning_refuses_while_the_schema_holds_tables(deployment) -> None:
    """Irreversible, so it asks. `privacy.erase` sets the precedent."""
    connection = await connect(str(DSN))
    try:
        with pytest.raises(TenancyError, match="not empty"):
            await deprovision_tenant(connection, ACME)
    finally:
        await connection.close()


async def test_deprovisioning_removes_the_schema_and_the_role_when_forced() -> None:
    """The green half of the refusal above, on a tenant of its own so it cannot
    disturb the shared fixture."""
    connection = await connect(str(DSN))
    try:
        throwaway = await provision_tenant(
            connection, key="wt_gone", central=CENTRAL, login_role=APP_ROLE)
        await deprovision_tenant(connection, throwaway, force=True)
        rows = await connection.fetch(
            "SELECT 1 FROM pg_namespace WHERE nspname = $1", throwaway.schema)
        assert list(rows) == []
        roles = await connection.fetch(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", throwaway.role)
        assert list(roles) == []
    finally:
        await connection.close()
