"""Restart and concurrency facts for the durable organisation store."""

from __future__ import annotations

import os
import uuid

import pytest

from wreath.organizations import Membership, PostgresOrganizationStore
from wreath.postgres import Database

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.database,
    pytest.mark.skipif(
        not _DSN,
        reason="set WREATH_TEST_POSTGRES_DSN for organization restart tests",
    ),
]


async def _database(name: str) -> Database:
    database = Database(
        name,
        _DSN or "",
        pools={
            "write": {"min_size": 1, "max_size": 2},
            "read": {"min_size": 1, "max_size": 2},
        },
    )
    await database.start()
    return database


async def _execute(database: Database, statements: tuple[str, ...]) -> None:
    connection = await database.acquire("write")
    try:
        for statement in statements:
            await connection.execute(statement)
    finally:
        await database.release("write", connection)


async def test_accepted_membership_and_consumed_token_survive_an_api_restart() -> None:
    table = f"org_restart_{uuid.uuid4().hex[:10]}"
    first_database = await _database("organizations_before_restart")
    first = PostgresOrganizationStore(
        first_database, roles={"admin", "member"}, table=table
    )
    await _execute(first_database, first.component().statements())
    invitation = await first.invite(
        "acme", "ada@example.test", roles={"member"}, ttl=3600
    )
    assert await first.accept(invitation.token, "ada") == Membership(
        "acme", "ada", frozenset({"member"})
    )
    await first_database.stop()

    # A different Database and store instance is the relevant restart boundary:
    # neither retains an in-process row or prepared statement from the first.
    second_database = await _database("organizations_after_restart")
    second = PostgresOrganizationStore(
        second_database, roles={"admin", "member"}, table=table
    )
    try:
        assert await second.memberships("ada") == (
            Membership("acme", "ada", frozenset({"member"})),
        )
        held = await second.invitations("acme")
        assert len(held) == 1 and held[0].accepted_by == "ada"
        with pytest.raises(ValueError, match="already been accepted"):
            await second.accept(invitation.token, "mallory")
    finally:
        await _execute(
            second_database,
            (
                f"DROP TABLE IF EXISTS {table}_invitation",
                f"DROP TABLE IF EXISTS {table}_membership",
                f"DROP TABLE IF EXISTS {table}",
            ),
        )
        await second_database.stop()
