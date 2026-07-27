"""Shared fixture machinery for the camera-trap example's database tests.

Three suites build the same schema the same way — replay the shipped migration
artifact, seed it, hand back a client, drop it — and they had three copies of
the path constant and the replay loop between them. One copy, here, in the
`tests/_name.py` style the repository already uses for `_replaydrive`,
`_gated_skips` and `_pgfidelity`: `tests/` is on `sys.path`, so these import as
plain modules rather than through a package.

The artifact is v1's shipped DDL and names its schema literally, which is
correct for the thing it is — a migration someone applies to production. The
tests need it somewhere else, because they run in a per-worker namespace, so
`statements()` rewrites the one token. That substitution is confined to the
test fixtures on purpose: rewriting the artifact itself, or teaching the
migration system to parameterise a schema it deliberately hard-codes, would
change what the artifact *is* to make a test easier.
"""

from __future__ import annotations

import pathlib

from camera_trap.models import DEFAULT_SCHEMA, SCHEMA

ARTIFACT = pathlib.Path(__file__).resolve().parents[1] / "example" / "migrations" / "migration.sql"


def statements() -> list[str]:
    """The artifact's non-empty statements, retargeted at the live schema.

    Returned without trailing semicolons because the driver takes one statement
    per call; the artifact writes them the way `psql` wants them.
    """
    text = ARTIFACT.read_text(encoding="utf-8")
    if SCHEMA != DEFAULT_SCHEMA:
        text = text.replace(f'"{DEFAULT_SCHEMA}"', f'"{SCHEMA}"')
    return [line.rstrip().rstrip(";") for line in text.splitlines() if line.strip()]


async def build_schema(connection, *, seed_rows: int | None = None) -> None:
    """Drop, recreate and populate the example's schema on `connection`.

    `DROP ... CASCADE` first rather than `CREATE ... IF NOT EXISTS`: a leftover
    schema from a killed run holds rows that would make a count assertion pass
    or fail for reasons that have nothing to do with the test.
    """
    await connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    await connection.execute(f'CREATE SCHEMA "{SCHEMA}"')
    for statement in statements():
        await connection.execute(statement)
    # The durable queue's tables are not in the artifact -- `wreath migrations`
    # derives that from the ORM models, and the queue is not one. See
    # `camera_trap.tasks.queue_schema_sql`, which is the single name for this
    # DDL so the quickstart and these fixtures cannot drift apart.
    from camera_trap.tasks import queue_schema_sql

    for statement in queue_schema_sql(SCHEMA).split(";"):
        if statement.strip():
            await connection.execute(statement)
    if seed_rows is not None:
        from camera_trap.seed import seed

        await seed(connection, sightings=seed_rows)


async def drop_schema(connection) -> None:
    """Remove the example's schema, so a failed run leaves nothing behind."""
    await connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
