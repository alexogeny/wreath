"""Fixtures building the two-tenant deployment the isolation tests run against.

Everything here exists so those tests can be *falsified*. They point a hostile
query at another tenant and require the server to refuse, and that claim is only
worth making against a database with two tenants, a central schema, and an
application role that owns none of it.

The setup connection is the DSN's own role, which in the test container is a
superuser -- exactly the role `verify_isolation` refuses for the request path.
That is the point: it *builds* the deployment, and every assertion runs on a
second connection as an unprivileged `NOINHERIT` login role.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from wreath.postgres import connect
from wreath.tenancy import provision_tenant

from ._deployment import ACME, APP_PASSWORD, APP_ROLE, CENTRAL, DSN, GLOBEX, app_dsn

pytestmark = pytest.mark.skipif(DSN is None, reason="WREATH_TEST_POSTGRES_DSN is unset")


async def _teardown(connection) -> None:
    for tenant in (ACME, GLOBEX):
        await connection.execute(f'DROP SCHEMA IF EXISTS "{tenant.schema}" CASCADE')
    await connection.execute(f'DROP SCHEMA IF EXISTS "{CENTRAL}" CASCADE')
    for tenant in (ACME, GLOBEX):
        await connection.execute(f'DROP ROLE IF EXISTS "{tenant.role}"')
    await connection.execute(f'DROP ROLE IF EXISTS "{APP_ROLE}"')


@pytest.fixture(scope="module")
async def deployment() -> AsyncIterator[None]:
    """Two tenants, a central schema, and an unprivileged application role."""
    connection = await connect(str(DSN))
    try:
        await _teardown(connection)
        # NOINHERIT is the load-bearing word: without it this role holds every
        # tenant role's privileges the moment it is granted membership, and
        # `RESET ROLE` becomes an escape hatch instead of a dead end.
        await connection.execute(
            f"CREATE ROLE \"{APP_ROLE}\" LOGIN NOINHERIT PASSWORD '{APP_PASSWORD}'")
        await connection.execute(f'CREATE SCHEMA "{CENTRAL}"')
        await connection.execute(
            f'CREATE TABLE "{CENTRAL}".plan (id bigint primary key, name text not null)')
        await connection.execute(
            f"INSERT INTO \"{CENTRAL}\".plan (id, name) VALUES (1, 'free'), (2, 'pro')")

        for tenant in (ACME, GLOBEX):
            await provision_tenant(
                connection, key=tenant.key, schema=tenant.schema, role=tenant.role,
                central=CENTRAL, login_role=APP_ROLE,
            )
            # Created *after* provisioning, so the ALTER DEFAULT PRIVILEGES the
            # provisioner issued is what grants them. A fixture that granted by
            # hand here would hide the drift this is meant to prove is absent.
            await connection.execute(
                f'CREATE TABLE "{tenant.schema}".item '
                "(id bigint primary key, name text not null, plan_id bigint)")
            await connection.execute(
                f"INSERT INTO \"{tenant.schema}\".item (id, name, plan_id) "
                f"VALUES (1, '{tenant.key}-thing', 1)")
        yield
    finally:
        await _teardown(connection)
        await connection.close()


@pytest.fixture
async def app_connection(deployment) -> AsyncIterator:
    """A connection as the unprivileged application role."""
    connection = await connect(app_dsn())
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def acme(app_connection) -> AsyncIterator:
    """The application connection, inside a transaction bound to acme.

    `SET LOCAL`, inside a transaction, exactly as `Session` binds one -- so the
    tests exercise the real binding rather than a stronger one written for them.
    """
    await app_connection.execute("BEGIN")
    try:
        for statement in ACME.context()._bind_statements():
            await app_connection.execute(statement)
        yield app_connection
    finally:
        await app_connection.execute("ROLLBACK")
