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
    # The durable queue's tables are not in the migration artifact -- `wreath
    # migrations` derives that from the ORM models, and the queue is not one.
    # `app.jobs(...)` registers the runner as a schema component and wreath
    # creates its tables at lifespan startup, so an application never applies
    # this by hand. A fixture is the one caller that legitimately does: it
    # prepares a database *before* any app starts, and some tests here read the
    # schema without ever entering a lifespan.
    # So it asks the runner's own schema component for its statements, rather
    # than keeping an example-local copy of the DDL. The example used to export
    # a `queue_schema_sql` for this; that was a workaround for a gap wreath has
    # since closed.
    # Not `wreath.schema.emit_sql`, which is the DBA-facing spelling and wraps
    # its output in transaction control -- correct for `psql -f`, and rejected
    # by the driver, which refuses `BEGIN` on a connection with an operation
    # already in flight.
    from camera_trap.tasks import QUEUE

    from wreath.jobs import JobRunner

    queue = JobRunner(None, name=QUEUE, schema=SCHEMA)
    for statement in queue.component().sql().split(";\n"):
        if statement.strip():
            await connection.execute(statement)
    if seed_rows is not None:
        from camera_trap.seed import seed

        await seed(connection, sightings=seed_rows)


async def drop_schema(connection) -> None:
    """Remove the example's schema, so a failed run leaves nothing behind."""
    await connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
