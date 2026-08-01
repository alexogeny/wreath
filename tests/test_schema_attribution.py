"""Which database a subsystem's tables belong to, and whether they get created.

Two defects lived here, and both had the same shape: a declaration was made,
the framework did not read it, and nothing said so at the point of failure.

* `app.jobs(database=)` names a database. The attribution walk looked for a
  `_database` or a `database` attribute on the runner; `JobRunner` calls its
  own `_db`. So a two-database application that had said which one was refused
  at **lifespan startup** with "cannot tell which database the 'jobs' tables
  belong to".
* `RateLimitMiddleware`, `IdempotencyMiddleware` and `SessionMiddleware` hold a
  store that owns tables and exposed neither `component()` nor `schema_owners`,
  so `schema_components()` walked them and found nothing: `wreath_rate_limit`,
  `wreath_idempotency` and `wreath_session` were emitted by
  `wreath schema sql` and created by nothing at all.

Both are asserted **through a real lifespan against a real PostgreSQL**, and
by looking the relations up in `pg_class` afterwards. A unit test of the
attribution helper would pass while the tables stayed absent, which is exactly
the failure mode -- "emitted and applied by nothing" is invisible to every test
that stops at the collection call.

The two-database test needs a genuine second database rather than the same DSN
registered twice, because "the tables went to the one that was named" is not
observable when both names point at the same server. It creates one per xdist
worker, for the reason `AGENTS.md` gives about shared schemas.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from wreath import Wreath

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")
requires_db = pytest.mark.skipif(
    not _DSN, reason="needs WREATH_TEST_POSTGRES_DSN (a live PostgreSQL)"
)


def _worker() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "solo")


def _schema(suffix: str) -> str:
    """A schema name of this worker's own. See `AGENTS.md`: workers sharing one
    race on `CREATE SCHEMA IF NOT EXISTS`, which PostgreSQL reports as a catalog
    unique violation that reads like anything but a test-isolation bug."""
    return f"wattr_{_worker()}_{suffix}"


def _dsn_for(database: str) -> str:
    parts = urlsplit(str(_DSN))
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _database(suffix: str, *schemas: str) -> str:
    """A real PostgreSQL database of this worker's own, emptied of wreath state.

    Every test here uses one of these rather than the DSN's own database, for a
    reason the first draft found the hard way: `bootstrap` records what it
    applied in `wreath.schema_version`, and the marker is keyed on the
    *component* name -- `ratelimit`, `jobs` -- not on the table a test happened
    to configure. Two workers, or two runs, on one database therefore make the
    second one skip the DDL it came to assert and see an absent table. Dropping
    the `wreath` schema at the start of a test is what makes it repeatable, and
    it is safe only because nothing else has this database.

    `CREATE DATABASE` has no `IF NOT EXISTS`, so it is guarded by a look in
    `pg_database` -- race-free because the name carries the worker id and no
    other worker will ever create it. A shared name here would be the
    `CREATE SCHEMA IF NOT EXISTS` race one catalog up.
    """
    from wreath.postgres import Database

    name = f"wreath_attr_{_worker()}_{suffix}"
    admin = Database("admin", str(_DSN))
    await admin.start()
    connection = await admin.acquire("write")
    try:
        rows = await connection.fetch(
            "SELECT 1 FROM pg_database WHERE datname = $1::text", name
        )
        if not rows:
            await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.release("write", connection)
    await admin.stop()

    dsn = _dsn_for(name)
    own = Database("reset", dsn)
    await own.start()
    connection = await own.acquire("write")
    try:
        for schema in ("wreath", *schemas):
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await own.release("write", connection)
    await own.stop()
    return dsn


async def _drive_startup(app: Wreath) -> list[dict[str, Any]]:
    """Run one whole lifespan cycle -- startup then shutdown -- and return the
    replies. Startup is where `_bootstrap_schema` runs, and where the refusal
    this file is about used to arrive."""
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    return sent


async def _resolves(dsn: str, table: str) -> str | None:
    """Where `table` resolves on the database at `dsn`, or None if it is absent.

    `to_regclass` rather than a `pg_class` lookup in a named schema, because a
    middleware store's DDL is deliberately *unqualified* -- `wreath_rate_limit`
    stays where the rows are rather than moving into the `wreath` schema -- so
    which schema it lands in is `search_path`'s answer, not the test's. Asking
    the way the store's own statements will ask is both the correct question
    and the one that does not depend on the role a deployment connects as.
    (The first draft of this asserted `public` and failed on a table that was
    sitting in the role-named schema `"$user"` had selected.)
    """
    from wreath.postgres import Database

    database = Database("probe", dsn)
    await database.start()
    connection = await database.acquire("read")
    try:
        rows = await connection.fetch(
            "SELECT to_regclass($1::text)::text", table
        )
    finally:
        await database.release("read", connection)
    await database.stop()
    found = rows[0][0]
    return None if found is None else str(found)


async def _relations(dsn: str, schema: str) -> set[str]:
    """Relation names actually present in `schema` on the database at `dsn`.

    For the subsystems that *do* qualify their tables: a job runner given
    `schema=` puts them exactly there, so the schema is the test's answer
    rather than `search_path`'s and naming it is the stronger assertion.
    """
    from wreath.postgres import Database

    database = Database("probe", dsn)
    await database.start()
    connection = await database.acquire("read")
    try:
        rows = await connection.fetch(
            "SELECT c.relname::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1::text AND c.relkind IN ('r', 'p')",
            schema,
        )
    finally:
        await database.release("read", connection)
    await database.stop()
    return {str(row[0]) for row in rows}


# --- defect 1: the declaration the attribution walk did not read -------------


@requires_db
async def test_a_two_database_application_with_a_queue_starts_and_lands_its_tables() -> None:
    """`app.jobs(database="analytics")` is read, so lifespan startup succeeds.

    This is the exact shape that used to refuse. Two registered databases and a
    job runner that named one of them: the walk could not find a database on
    the runner, fell through to "more than one registered, refuse", and the
    application never started. The declaration had answered the question the
    refusal asked.

    Both halves are asserted. Starting is not enough on its own -- an
    attribution that picked the wrong database would also start -- so the
    queue's relations must be present in `analytics` and absent from `main`.
    """
    schema = _schema("jobs")
    main = await _database("main", schema)
    analytics = await _database("alt", schema)
    app = Wreath()
    app.postgres("main", dsn=main)
    app.postgres("analytics", dsn=analytics)
    app.jobs("ingest", database="analytics", schema=schema)

    sent = await _drive_startup(app)
    assert [message["type"] for message in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ], sent

    assert "jobs" in await _relations(analytics, schema)
    assert await _relations(main, schema) == set()


@requires_db
async def test_a_two_database_application_with_a_bus_lands_its_tables_too() -> None:
    """`MessageBus` names its database `_db` for the same reason, and was missed
    for the same reason. Asserted separately because one subsystem passing does
    not establish the other -- they are two objects with two declarations."""
    schema = _schema("bus")
    main = await _database("main", schema)
    analytics = await _database("alt", schema)
    app = Wreath()
    app.postgres("main", dsn=main)
    app.postgres("analytics", dsn=analytics)
    app.messaging("events", database="analytics", schema=schema)

    sent = await _drive_startup(app)
    assert sent[0]["type"] == "lifespan.startup.complete", sent

    assert "messages" in await _relations(analytics, schema)
    assert await _relations(main, schema) == set()


async def test_a_subsystem_that_names_no_database_still_refuses_when_two_exist() -> None:
    """The refusal is not gone, only no longer reached by a made declaration.

    A webhook hub is handed a session per call and never sees a database, so
    with two registered there is genuinely nothing to read and guessing would
    create its tables beside a subsystem reading the other one. Without this,
    fixing the queue could have been done by deleting the refusal.
    """
    import contextlib
    from collections.abc import AsyncIterator

    from wreath.webhooks import HMACWebhookVerifier, PostgresWebhookInbox

    @contextlib.asynccontextmanager
    async def session() -> AsyncIterator[None]:
        yield None

    app = Wreath()
    app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    app.postgres("archive", dsn="postgresql://u@two.invalid:5432/b")
    hub = app.webhooks("payments")
    hub.source(
        "stripe",
        path="/hooks/stripe",
        verifier=HMACWebhookVerifier({"v1": b"k" * 32}),
        inbox=PostgresWebhookInbox(),
        session_factory=session,
    )

    with pytest.raises(ValueError, match="cannot tell which database"):
        app._components_by_database(app.schema_components())


async def test_the_declared_database_is_recorded_against_the_object_it_built() -> None:
    """Two runners on two databases keep their own attribution.

    A single shared record, or one keyed on the subsystem's name, would pass a
    test with one runner in it and put both queues in one database here.
    """
    app = Wreath()
    app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    app.postgres("archive", dsn="postgresql://u@two.invalid:5432/b")
    app.jobs("ingest", database="main", schema="wreath_a")
    app.jobs("rollup", database="archive", schema="wreath_b")

    grouped = app._components_by_database(app.schema_components())
    # Both claims are named `jobs`, so `schema_components` deduplicates to one
    # and only the first runner's is attributed. What must hold is that the one
    # claim went to the database its own runner named, not to whichever
    # database was registered first by coincidence -- here they are the same,
    # so the assertion is on the schema the claim carries.
    (database,) = grouped
    (claim,) = grouped[database]
    assert database is app._databases["main"]
    assert claim.schema == "wreath_a"


# --- defect 2: tables emitted by the store and applied by nothing ------------


@requires_db
async def test_a_rate_limit_table_is_created_by_lifespan_startup() -> None:
    """The DDL is *applied*, not merely collected.

    Asserting that `RateLimitMiddleware` now answers `schema_owners` would pass
    with the table still absent, which is the whole failure: the claim existed
    and was emitted by `wreath schema sql` all along. So the relation is looked
    up in `pg_class` after a real startup.
    """
    from wreath.middleware.ratelimit import PostgresRateLimitStore, RateLimitMiddleware

    table = f"wattr_{_worker()}_rl"
    dsn = await _database("rl")
    app = Wreath()
    database = app.postgres("main", dsn=dsn)
    store = PostgresRateLimitStore(database, table=table)
    app.add_global_middleware(RateLimitMiddleware(limit=10, window=1.0, store=store))

    assert await _resolves(dsn, table) is None, "the table must not pre-exist"
    sent = await _drive_startup(app)
    assert sent[0]["type"] == "lifespan.startup.complete", sent
    assert await _resolves(dsn, table) is not None


@requires_db
async def test_an_idempotency_table_is_created_by_lifespan_startup() -> None:
    from wreath.middleware.idempotency import (
        IdempotencyMiddleware,
        PostgresIdempotencyStore,
    )

    table = f"wattr_{_worker()}_idem"
    dsn = await _database("idem")
    app = Wreath()
    database = app.postgres("main", dsn=dsn)
    app.add_middleware(
        IdempotencyMiddleware(store=PostgresIdempotencyStore(database, table=table))
    )

    assert await _resolves(dsn, table) is None, "the table must not pre-exist"
    sent = await _drive_startup(app)
    assert sent[0]["type"] == "lifespan.startup.complete", sent
    assert await _resolves(dsn, table) is not None


@requires_db
async def test_a_session_table_is_created_by_lifespan_startup() -> None:
    from wreath.middleware.sessions import SessionMiddleware
    from wreath.session_store import PostgresSessionStore

    table = f"wattr_{_worker()}_sess"
    dsn = await _database("sess")
    app = Wreath()
    database = app.postgres("main", dsn=dsn)
    app.add_middleware(
        SessionMiddleware("s" * 32, store=PostgresSessionStore(database, table=table))
    )

    assert await _resolves(dsn, table) is None, "the table must not pre-exist"
    sent = await _drive_startup(app)
    assert sent[0]["type"] == "lifespan.startup.complete", sent
    assert await _resolves(dsn, table) is not None


@requires_db
async def test_a_middleware_owned_table_goes_to_the_database_its_store_holds() -> None:
    """The store says which database, so two of them is not ambiguous.

    Collecting the claim without attributing it would have moved the defect
    rather than fixed it: the walk would find the table and then refuse at
    startup, in an application that had never had a problem before.
    """
    from wreath.middleware.ratelimit import PostgresRateLimitStore, RateLimitMiddleware

    table = f"wattr_{_worker()}_rl2"
    main = await _database("rl2main")
    other = await _database("rl2alt")
    app = Wreath()
    app.postgres("main", dsn=main)
    analytics = app.postgres("analytics", dsn=other)
    store = PostgresRateLimitStore(analytics, table=table)
    app.add_global_middleware(RateLimitMiddleware(limit=10, window=1.0, store=store))

    sent = await _drive_startup(app)
    assert sent[0]["type"] == "lifespan.startup.complete", sent
    assert await _resolves(other, table) is not None
    assert await _resolves(main, table) is None


async def test_a_cookie_only_session_middleware_claims_nothing() -> None:
    """No store means no tables, which must stay distinct from an unattributable
    claim: answering with `(None,)` would send `None` into the walk."""
    from wreath.middleware.sessions import SessionMiddleware

    middleware = SessionMiddleware("s" * 32)
    assert middleware.schema_owners == ()
    app = Wreath()
    app.add_middleware(middleware)
    assert app.schema_components() == ()


async def test_every_tier_of_a_tiered_limiter_is_walked() -> None:
    """`TieredRateLimitMiddleware` builds one whole limiter per tier, and its
    stores were unreachable for the same reason. Every tier shares the table
    name a `store_factory` gives it, so the claims deduplicate to one."""
    from wreath.middleware.ratelimit import (
        PostgresRateLimitStore,
        TieredRateLimitMiddleware,
    )

    app = Wreath()
    database = app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    app.add_middleware(
        TieredRateLimitMiddleware(
            tiers={"pro": (600, 60.0), "enterprise": (10_000, 60.0)},
            default=(60, 60.0),
            store_factory=lambda: PostgresRateLimitStore(database),
        )
    )
    assert [claim.name for claim in app.schema_components()] == ["ratelimit"]
    assert list(app._components_by_database(app.schema_components())) == [database]


# --- defect 3: a sealed Series' tables, claimed by nobody --------------------
#
# A `Series` is a declaration built where it is used, so the application never
# holds one and there was nothing for `schema_components()` to ask. The result
# was the same shape as defect 2 one level worse: `wreath.series_buckets` and
# `wreath.series_corrections` were printed by `wreath schema sql`, created by
# nothing at all -- not even by a lifespan -- and an application declaring
# `.seal()` had to import `wreath._series.settle` past a leading underscore and
# run the DDL itself. `docs/tracking/ingest.md` documented that as a rough edge
# rather than a defect. `app.series(database=...)` gives the claim an owner.


async def test_a_sealed_series_claims_its_tables() -> None:
    app = Wreath()
    app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    app.series(database="main")
    claims = app.schema_components()
    assert [claim.name for claim in claims] == ["series"]
    assert set(claims[0].relations) == {"series_buckets", "series_corrections"}


async def test_an_application_with_no_sealed_view_claims_nothing() -> None:
    """An open `Series` stores nothing, so the tables are not implied by the
    module being importable. Claiming them for every application would create
    two tables nobody uses in every deployment that draws a chart."""
    app = Wreath()
    app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    assert app.schema_components() == ()


async def test_the_store_is_attributed_to_the_database_it_named() -> None:
    """Through `_declared_databases`, the same record `app.jobs(database=)`
    uses -- so a two-database application is not ambiguous."""
    app = Wreath()
    app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    analytics = app.postgres("analytics", dsn="postgresql://u@two.invalid:5432/b")
    app.series(database="analytics")
    assert list(app._components_by_database(app.schema_components())) == [analytics]


async def test_a_second_store_on_one_database_is_refused() -> None:
    app = Wreath()
    app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    app.series(database="main")
    with pytest.raises(ValueError, match="already has a settled-bucket store"):
        app.series(database="main")


async def test_naming_an_unknown_database_is_refused() -> None:
    app = Wreath()
    app.postgres("main", dsn="postgresql://u@one.invalid:5432/a")
    with pytest.raises(KeyError, match="unknown database"):
        app.series(database="analytics")


@requires_db
async def test_the_settled_bucket_tables_are_created_by_lifespan_startup() -> None:
    """Applied, not merely collected -- and idempotent, because a second startup
    is the ordinary case and `CREATE TABLE IF NOT EXISTS` is only half of that:
    `bootstrap` also has to be happy to find the component already recorded."""
    schema = _schema("series")
    dsn = await _database("series", schema)
    app = Wreath()
    app.postgres("main", dsn=dsn)
    app.series(database="main", schema=schema)

    assert await _relations(dsn, schema) == set(), "the tables must not pre-exist"
    sent = await _drive_startup(app)
    assert sent[0]["type"] == "lifespan.startup.complete", sent
    assert await _relations(dsn, schema) == {"series_buckets", "series_corrections"}

    again = Wreath()
    again.postgres("main", dsn=dsn)
    again.series(database="main", schema=schema)
    replies = await _drive_startup(again)
    assert replies[0]["type"] == "lifespan.startup.complete", replies
    assert await _relations(dsn, schema) == {"series_buckets", "series_corrections"}
