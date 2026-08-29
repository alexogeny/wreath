from __future__ import annotations

import pathlib
from typing import Any

from tracking.config import DEFAULT_SCHEMA, SCHEMA

ARTIFACT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "example"
    / "tracking"
    / "migrations"
    / "migration.sql"
)


def statements() -> list[str]:
    """The artifact's non-empty statements, retargeted at the live schema.

    Returned without trailing semicolons because the driver takes one statement
    per call; the artifact writes them the way `psql` wants them.
    """
    text = ARTIFACT.read_text(encoding="utf-8")
    if SCHEMA != DEFAULT_SCHEMA:
        text = text.replace(f'"{DEFAULT_SCHEMA}"', f'"{SCHEMA}"')
    return [line.rstrip().rstrip(";") for line in text.splitlines() if line.strip()]


async def build_schema(connection: Any, *, seed_rows: bool = True, fixes: bool = True) -> None:
    """Drop, recreate and populate the example's schema on `connection`.

    `DROP ... CASCADE` first rather than `CREATE ... IF NOT EXISTS`: a leftover
    schema from a killed run holds rows that would make a count assertion pass
    or fail for reasons that have nothing to do with the test.
    """
    await connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    await connection.execute(f'CREATE SCHEMA "{SCHEMA}"')
    for statement in statements():
        await connection.execute(statement)

    # The message bus's tables are not in the migration artifact -- `wreath
    # migrations` derives that from the ORM models, and a bus is not one.
    # `app.messaging(...)` registers it as a schema component and wreath creates
    # its tables at lifespan startup, so a deployed application never applies
    # this by hand. A fixture is the one caller that legitimately does: it
    # prepares a database *before* any app starts, and several tests here never
    # enter a lifespan at all.
    from tracking.config import SETTINGS

    from wreath.messaging import MessageBus

    bus = MessageBus(None, name="live", schema=SETTINGS.schema)
    for statement in bus.component().sql().split(";\n"):
        if statement.strip():
            await connection.execute(statement.strip())

    # The settled-bucket tables. The application declares these with
    # `app.series(database="main")` and lifespan startup creates them, but these
    # fixtures build a schema directly rather than driving a lifespan, so the
    # same claim is applied here -- through the public `SettledStore`, the way
    # the bus above uses its own `component()`.
    # Applied for every fixture rather than only the sealing suite, because the
    # daily-chart route reads a sealed view: a suite that did not create them
    # passed only on a database where another suite already had, which is a
    # green run that depends on the order somebody ran things in.
    from wreath.series import SettledStore

    for statement in SettledStore().schema_sql().split(";\n"):
        if statement.strip():
            await connection.execute(statement.strip())

    if seed_rows:
        from tracking.seed import seed

        await seed(connection, fixes=fixes)


async def drop_schema(connection: Any) -> None:
    """Remove the example's schema, so a failed run leaves nothing behind."""
    await connection.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')


async def clear_settled(database: Any, view: Any, params: dict[str, Any], zone: str) -> None:
    """Forget what a sealed view has already settled, for one set of parameters.

    Settled buckets live in wreath's own `wreath` schema, which `drop_schema`
    correctly does not touch — so a previous run of a sealing test leaves a
    settled bucket and a correction behind, and the next run's first read would
    fold them in and measure the wrong thing.

    Cleared by the view's own identity, so a sibling suite's settled rows are
    left alone. That identity is *schema-blind*: `view_key` digests the model's
    module and qualname rather than the schema it resolves to, so two
    deployments of one application against different schemas file their settled
    rows under the same key. Safe here because xdist gives one test to one
    worker, and worth knowing before a second suite seals the same view.
    """
    view_id, params_id = view._identity(zone, params)
    connection = await database.acquire("write")
    try:
        for table in ("series_buckets", "series_corrections"):
            await connection.execute(
                f'DELETE FROM "wreath"."{table}" WHERE view = $1 AND params = $2',
                view_id,
                params_id,
            )
    finally:
        await database.release("write", connection)
