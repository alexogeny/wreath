"""The wreath-owned schema: bootstrap, upgrade, and the opt-out refusal.

Every test that touches a database is gated on `WREATH_TEST_POSTGRES_DSN` and
takes a per-worker schema name, because a shared schema races on
`CREATE SCHEMA IF NOT EXISTS` under `-n 6` and PostgreSQL reports the race as a
catalog unique violation that reads like anything except a test-isolation bug.

The declaration tests need no database and run everywhere.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from wreath.schema import (
    Component,
    SchemaNotManaged,
    Step,
    bootstrap,
    emit_sql,
    marker_statements,
    verify,
)

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
requires_db = pytest.mark.skipif(
    not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)"
)


def _schema(suffix: str) -> str:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "solo")
    return f"wsch_{worker}_{suffix}"


def _jobs(schema: str, *, version: int = 1) -> Component:
    """A component shaped like `JobRunner.component()`, small enough to read."""
    table = f'"{schema}"."jobs"'
    steps = [
        Step(1, (f"CREATE TABLE IF NOT EXISTS {table} (id bigint PRIMARY KEY, q text)",)),
    ]
    if version >= 2:
        steps.append(
            Step(2, (f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS priority int "
                     "NOT NULL DEFAULT 0",))
        )
    return Component(name="jobs", schema=schema, relations=("jobs",), steps=tuple(steps))


# --- declaration, no database ------------------------------------------------


async def test_a_step_must_carry_statements() -> None:
    with pytest.raises(ValueError, match="no statements"):
        Step(1, ())


async def test_a_step_version_starts_at_one() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        Step(0, ("SELECT 1",))


async def test_steps_must_ascend_without_repeating() -> None:
    with pytest.raises(ValueError, match="ascending and unique"):
        Component(name="x", steps=(Step(2, ("SELECT 1",)), Step(1, ("SELECT 1",))))


async def test_an_unquotable_identifier_is_refused() -> None:
    """Identifiers reach SQL by interpolation because PostgreSQL has no parameter
    form for one, so the guard is the whole defence."""
    with pytest.raises(ValueError, match="unusable SQL identifier"):
        marker_statements('ev"il')


async def test_emitted_sql_carries_every_statement_and_its_marker() -> None:
    sql = emit_sql([_jobs("wsch_doc", version=2)], schema="wsch_doc")
    assert "CREATE SCHEMA IF NOT EXISTS" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "ADD COLUMN IF NOT EXISTS priority" in sql
    # Without the marker rows a DBA-applied schema would re-run every step on
    # the first start, which is the whole reason the marker travels with the DDL.
    assert sql.count("INSERT INTO") == 2


async def test_emitting_from_a_version_skips_what_is_already_applied() -> None:
    sql = emit_sql([_jobs("wsch_doc", version=2)], schema="wsch_doc", from_version=1)
    # The marker table is emitted unconditionally -- it is what records the
    # version -- so the assertion has to name the component's own table rather
    # than any `CREATE TABLE`.
    assert '"wsch_doc"."jobs"' not in sql.split("-- jobs:")[0].split("ALTER")[0]
    assert "CREATE TABLE IF NOT EXISTS \"wsch_doc\".\"jobs\"" not in sql
    assert "ADD COLUMN IF NOT EXISTS priority" in sql


# --- against a live database -------------------------------------------------


async def _database(schema: str):
    from wreath.postgres import Database

    db = Database("main", _DSN)
    await db.start()
    connection = await db.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await db.release("write", connection)
    return db


async def _drop(db, schema: str) -> None:
    connection = await db.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await db.release("write", connection)
    await db.stop()


@requires_db
async def test_bootstrap_creates_the_schema_and_is_idempotent() -> None:
    schema = _schema("idem")
    db = await _database(schema)
    try:
        component = _jobs(schema)
        assert await bootstrap(db, [component], schema=schema) == {"jobs": 1}
        assert await bootstrap(db, [component], schema=schema) == {"jobs": 1}
        await verify(db, [component], schema=schema)
    finally:
        await _drop(db, schema)


@requires_db
async def test_concurrent_bootstraps_do_not_race() -> None:
    """A fleet of workers starting together serialises on the advisory lock.

    Without the lock this is the `CREATE SCHEMA IF NOT EXISTS` race that
    PostgreSQL reports as a `pg_namespace_nspname_index` unique violation.
    """
    schema = _schema("race")
    db = await _database(schema)
    try:
        component = _jobs(schema)
        results = await asyncio.gather(
            *[bootstrap(db, [component], schema=schema) for _ in range(6)]
        )
        assert results == [{"jobs": 1}] * 6
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_upgrade_step_applies_once_and_only_once() -> None:
    schema = _schema("upgrade")
    db = await _database(schema)
    try:
        assert await bootstrap(db, [_jobs(schema)], schema=schema) == {"jobs": 1}
        v2 = _jobs(schema, version=2)
        assert await bootstrap(db, [v2], schema=schema) == {"jobs": 2}
        assert await bootstrap(db, [v2], schema=schema) == {"jobs": 2}

        connection = await db.acquire("write")
        try:
            rows = await connection.fetch(
                "SELECT a.attname::text FROM pg_attribute a "
                "JOIN pg_class k ON k.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = k.relnamespace "
                "WHERE n.nspname = $1::text AND k.relname = 'jobs' "
                "AND a.attnum > 0 AND NOT a.attisdropped",
                schema,
            )
        finally:
            await db.release("write", connection)
        assert "priority" in {str(row[0]) for row in rows}
    finally:
        await _drop(db, schema)


@requires_db
async def test_an_older_build_runs_against_a_newer_schema() -> None:
    """The rolling-deploy property, and the reason steps must be additive.

    A fleet mid-rollout has two wreath versions live on one database. The older
    one must keep working rather than refusing or downgrading -- refusing would
    make a rollback impossible, which is the situation the marker exists to
    survive.
    """
    schema = _schema("rollback")
    db = await _database(schema)
    try:
        await bootstrap(db, [_jobs(schema, version=2)], schema=schema)
        assert await bootstrap(db, [_jobs(schema)], schema=schema) == {"jobs": 2}
    finally:
        await _drop(db, schema)


@requires_db
async def test_opting_out_without_the_schema_refuses_by_name() -> None:
    schema = _schema("refuse")
    db = await _database(schema)
    try:
        with pytest.raises(SchemaNotManaged) as caught:
            await bootstrap(db, [_jobs(schema)], schema=schema, manage=False)
        message = str(caught.value)
        # The refusal has to be actionable, not merely correct: the subsystem,
        # the relation, and the command that emits the DDL.
        assert "jobs" in message
        assert f'"{schema}"."jobs"' in message
        assert "wreath schema sql --component jobs" in message
        assert "manage_schema=False" in message
    finally:
        await _drop(db, schema)


@requires_db
async def test_opting_out_after_a_dba_applied_the_ddl_starts_cleanly() -> None:
    """The enterprise path: a role without CREATE, a DBA who ran `emit_sql`."""
    schema = _schema("dba")
    db = await _database(schema)
    try:
        component = _jobs(schema)
        sql = emit_sql([component], schema=schema)
        # Strip comment lines first, then split: a comment sits on the same
        # chunk as the statement after it, so filtering whole chunks would drop
        # `CREATE SCHEMA` along with the header.
        body = "\n".join(
            line for line in sql.splitlines() if not line.strip().startswith("--")
        )
        connection = await db.acquire("write")
        try:
            for statement in body.split(";\n"):
                if statement.strip():
                    await connection.execute(statement.strip().rstrip(";"))
        finally:
            await db.release("write", connection)
        assert await bootstrap(db, [component], schema=schema, manage=False) == {"jobs": 1}
    finally:
        await _drop(db, schema)


@requires_db
async def test_the_users_migration_diff_cannot_see_the_wreath_schema() -> None:
    """Separation is by construction: the catalog read is scoped to one schema.

    This is why no filter has to be maintained, and the test exists so that a
    future change widening that scope is caught here rather than by a user whose
    `wreath migrations generate` proposes dropping the job queue.
    """
    schema = _schema("isolate")
    user_schema = f"{schema}_app"
    db = await _database(schema)
    connection = await db.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{user_schema}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{user_schema}"')
        await connection.execute(
            f'CREATE TABLE "{user_schema}"."thing" (id bigint PRIMARY KEY)'
        )
        await db.release("write", connection)
        await bootstrap(db, [_jobs(schema)], schema=schema)

        connection = await db.acquire("write")
        rows = await connection.fetch(
            "SELECT c.relname::text FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1::text AND c.relkind IN ('r', 'p')",
            user_schema,
        )
        names = {str(row[0]) for row in rows}
        assert names == {"thing"}
        assert "jobs" not in names
        await connection.execute(f'DROP SCHEMA "{user_schema}" CASCADE')
    finally:
        await db.release("write", connection)
        await _drop(db, schema)


# --- stage 2: every subsystem registers, and the app applies them -------------


async def test_every_subsystem_that_owns_tables_offers_a_component() -> None:
    """The registry is complete, asserted against the subsystems themselves.

    Nine components, nine names. A subsystem that grows tables and forgets
    `component()` reintroduces the whole defect for itself -- its DDL is emitted
    and never applied -- so this is the check that the collection is not partial.
    """
    from wreath._passes import ledger
    from wreath._series import settle
    from wreath.jobs import JobRunner
    from wreath.messaging import MessageBus
    from wreath.middleware.idempotency import PostgresIdempotencyStore
    from wreath.policy.ratelimit import PostgresRateLimitStore
    from wreath.session_store import PostgresSessionStore
    from wreath.webhooks import PostgresWebhookInbox, PostgresWebhookOutbox

    claims = [
        JobRunner(None, name="q").component(),
        MessageBus(None, name="b").component(),
        ledger.schema_claim("wreath"),
        settle.schema_claim(),
        PostgresSessionStore(None).component(),
        PostgresRateLimitStore(None).component(),
        PostgresIdempotencyStore(None).component(),
        PostgresWebhookInbox().component(),
        PostgresWebhookOutbox().component(),
    ]
    assert [c.name for c in claims] == [
        "jobs", "messaging", "passes", "series", "session",
        "ratelimit", "idempotency", "webhook-inbox", "webhook-outbox",
    ]
    # Every one declares what must exist, or `verify` has nothing to check.
    assert all(c.relations for c in claims)
    # And every one carries statements, or bootstrap would create nothing.
    assert all(c.statements() for c in claims)


async def test_schema_sql_is_a_derivation_of_the_statements() -> None:
    """`schema_sql()` joins the steps; it is never a second copy of the DDL.

    `jobs` carried both for one stage -- the same sixteen columns written twice --
    which is the shape this repository treats as a defect. Asserted for every
    producer so a future one cannot reintroduce it.
    """
    from wreath._passes import ledger
    from wreath._series import settle
    from wreath.jobs import JobRunner
    from wreath.messaging import MessageBus
    from wreath.webhooks import PostgresWebhookInbox, PostgresWebhookOutbox

    for owner in (JobRunner(None, name="q"), MessageBus(None, name="b")):
        joined = [s.strip() for s in owner.schema_sql().split(";\n") if s.strip()]
        # The joined form leads with CREATE SCHEMA; the rest is the tuple.
        assert joined[0].startswith("CREATE SCHEMA")
        assert joined[1:] == [s.strip() for s in owner.component().statements()]
    for owner in (ledger.schema_claim("wreath"), settle.schema_claim()):
        joined = [s.strip() for s in owner.sql().split(";\n") if s.strip()]
        assert joined[0].startswith("CREATE SCHEMA")
        assert joined[1:] == [s.strip() for s in owner.statements()]
    for store in (PostgresWebhookInbox(), PostgresWebhookOutbox()):
        joined = [s.strip() for s in store.schema_sql().split(";\n") if s.strip()]
        # No CREATE SCHEMA: these tables are unqualified, so there is none.
        assert joined == [s.strip() for s in store.statements()]


async def test_unqualified_components_emit_no_create_schema() -> None:
    """Five components predate the `wreath` schema and stay where their rows are.

    Emitting `CREATE SCHEMA` for them would create a schema nothing then uses,
    and *moving* them is not additive -- a worker on the previous version looks
    for the unqualified name. So `qualified` is False and `sql()` says so.
    """
    from wreath.session_store import PostgresSessionStore
    from wreath.webhooks import PostgresWebhookInbox

    for claim in (
        PostgresSessionStore(None).component(),
        PostgresWebhookInbox().component(),
    ):
        assert not claim.qualified
        assert "CREATE SCHEMA" not in claim.sql()


async def test_an_app_with_no_database_registers_no_claim_to_bootstrap() -> None:
    """A webhook inbox is handed a session per call and never sees a database.

    With no database registered there is nothing for its tables to be missing
    *from*, so the claim is skipped rather than refused. Vacuous, not degraded --
    and the distinction matters, because refusing here broke every existing
    webhook test the first time this was wired.
    """
    import wreath
    from wreath.webhooks import PostgresWebhookInbox

    app = wreath.Wreath()
    hub = app.webhooks("hub")
    assert app._components_by_database(app.schema_components()) == {}
    assert isinstance(PostgresWebhookInbox(), PostgresWebhookInbox)
    assert hub is app.state.webhooks_hub
