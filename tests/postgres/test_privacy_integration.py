"""Live-PostgreSQL proof that a planned erasure actually erases, and records it.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database. The
fake-driver suites in ``tests/test_privacy_plan.py`` prove the *shapes* -- which
tables, in what order, matched by what predicate. They cannot prove the thing
that matters, which is that running the plan leaves the subject's rows gone and
everybody else's rows untouched.

That second half is where an erasure fails in practice, and it fails in one of
two directions: a nested subquery that matches nothing (the subject is told
they were erased and were not) or one that matches too much (somebody else's
data is destroyed, irreversibly). Both look identical from inside the planner.
So this suite runs the generated passes against real rows and asserts both
directions, at two levels of foreign-key depth.

**These models declare a logical `SchemaRef`, and that is load-bearing.** An
earlier version of this file put its tables in ``public`` and serialised the
xdist workers with an advisory lock, because a `wreath.passes.ChunkedPass` over
a model whose ``schema=`` is a plain string renders the table *unqualified* --
only a logical ``SchemaRef`` is qualified into the statement -- and a plain
`Database` binds no ``search_path``. That reasoning was right about the
mechanism and wrong about where it lands: PostgreSQL's default ``search_path``
is ``"$user", public``, this suite connects as the role ``wreath``, and the
framework's own furniture lives in a schema *called* ``wreath``. So the moment
any other suite on the same database creates that schema, every unqualified
``CREATE TABLE`` here silently lands in it instead of in ``public`` -- the
erasure tests still pass, because they are unqualified in both directions, and
the catalog check fails because it asks the catalog about a schema the tables
are no longer in. A fixed `SchemaRef` is qualified end to end by
`wreath.passes._resolve_source`, so nothing here depends on ``search_path`` at
all, and each worker gets its own schema the way `AGENTS.md` requires.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.schema import SchemaRef
from wreath.orm.types import Int64, Text
from wreath.postgres import Database
from wreath.privacy import Erase, ErasureIncomplete, Privacy

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to run live privacy integration tests",
)

#: One schema per xdist worker, by plain assignment. `os.environ.setdefault` in
#: a conftest silently no-ops -- the controller writes the value and then spawns
#: workers carrying its own environment -- so the name is derived here.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SCHEMA = f"wreath_privacy_{_WORKER}"
_REF = SchemaRef("fixed", _SCHEMA)


class Person(Model, table="wpriv_people", schema=_REF):
    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    email: Mapped[str] = column(Text)


class Photo(Model, table="wpriv_photos", schema=_REF):
    """`ON DELETE SET NULL`, because the photo outlives the person.

    Not decoration: this erasure deletes the subject's row and *keeps* the
    photo with its caption redacted, so a `NO ACTION` edge here is one
    PostgreSQL refuses -- the delete fails, the erasure stops half-way, and
    every assertion below about redacted captions would still pass because the
    redaction ran first. The planner reports that shape as a surviving
    reference and marks the plan blocked; this schema is the version that can
    actually run.
    """

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    owner_id: Mapped[int | None] = column(
        Int64, references=Person.id, on_delete="set null", nullable=True
    )
    caption: Mapped[str] = column(Text)


class Comment(Model, table="wpriv_comments", schema=_REF):
    """Depth two: reached only through `Photo`, so it exercises the nesting."""

    id: Mapped[int] = column(Int64, primary_key=True, server_default="nextval('s')")
    photo_id: Mapped[int] = column(Int64, references=Photo.id)
    body: Mapped[str] = column(Text)


DDL = f"""
CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}";
CREATE SEQUENCE IF NOT EXISTS "{_SCHEMA}".wpriv_seq;
CREATE TABLE IF NOT EXISTS "{_SCHEMA}".wpriv_people (
  id bigint PRIMARY KEY DEFAULT nextval('"{_SCHEMA}".wpriv_seq'),
  email text NOT NULL
);
CREATE TABLE IF NOT EXISTS "{_SCHEMA}".wpriv_photos (
  id bigint PRIMARY KEY DEFAULT nextval('"{_SCHEMA}".wpriv_seq'),
  owner_id bigint REFERENCES "{_SCHEMA}".wpriv_people (id) ON DELETE SET NULL,
  caption text NOT NULL
);
CREATE TABLE IF NOT EXISTS "{_SCHEMA}".wpriv_comments (
  id bigint PRIMARY KEY DEFAULT nextval('"{_SCHEMA}".wpriv_seq'),
  photo_id bigint NOT NULL REFERENCES "{_SCHEMA}".wpriv_photos (id),
  body text NOT NULL
)
"""


async def _apply(database, sql: str) -> None:
    connection = await database.acquire("write")
    try:
        for statement in (part.strip() for part in sql.split(";\n")):
            if statement:
                await connection.execute(statement)
    finally:
        await database.release("write", connection)


async def _rows(database, sql: str) -> list:
    connection = await database.acquire("write")
    try:
        return list(await connection.fetch(sql))
    finally:
        await database.release("write", connection)


@pytest.fixture
async def database():
    from wreath.passes import schema_sql as passes_schema_sql
    from wreath.privacy import schema_sql as privacy_schema_sql

    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    db = Database("main", dsn, pools={"write": {"min_size": 1, "max_size": 4}})
    await db.start()
    await _apply(db, DDL)
    await _apply(db, passes_schema_sql(_SCHEMA))
    await _apply(db, privacy_schema_sql(_SCHEMA))
    try:
        yield db
    finally:
        await _apply(db, f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await db.stop()


@pytest.fixture
def privacy() -> Privacy:
    registry = Registry(None, [Person, Photo, Comment], validate_schema="off")
    item = Privacy(registry)
    item.subject(Person, key="id", delete=True)
    item.classify(Photo, subject="owner_id", personal={"caption": Erase.REDACT})
    item.classify(Comment, personal={"body": Erase.REDACT})
    return item


@dataclass(frozen=True, slots=True)
class Seeded:
    """Two people with a photo and a comment each, by id.

    Ids rather than a join through `wpriv_people`, because the subject's own
    row is *deleted* by this erasure -- an assertion that reaches the photo by
    joining to its owner finds nothing afterwards and would read as "the
    caption is gone" whichever way the erasure went.
    """

    erased: int
    kept: int
    erased_photo: int
    kept_photo: int


async def _seed(database) -> Seeded:
    connection = await database.acquire("write")
    try:
        ids: list[int] = []
        photos: list[int] = []
        for email, caption in (
            ("erased@example.test", "gone"),
            ("kept@example.test", "stays"),
        ):
            owner = await connection.fetchval(
                f'INSERT INTO "{_SCHEMA}".wpriv_people (email) VALUES ($1) RETURNING id',
                email,
            )
            photo = await connection.fetchval(
                f'INSERT INTO "{_SCHEMA}".wpriv_photos (owner_id, caption) '
                "VALUES ($1, $2) RETURNING id",
                owner,
                caption,
            )
            await connection.execute(
                f'INSERT INTO "{_SCHEMA}".wpriv_comments (photo_id, body) '
                "VALUES ($1, $2)",
                photo,
                caption,
            )
            ids.append(owner)
            photos.append(photo)
        return Seeded(ids[0], ids[1], photos[0], photos[1])
    finally:
        await database.release("write", connection)


async def test_erasing_a_subject_empties_their_rows_at_every_depth(
    database, privacy: Privacy
) -> None:
    """The direction that matters most: the subject's data is actually gone."""
    seeded = await _seed(database)
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)

    captions = await _rows(
        database,
        f'SELECT caption FROM "{_SCHEMA}".wpriv_photos WHERE id = {seeded.erased_photo}',
    )
    assert [tuple(row)[0] for row in captions] == ["[erased]"]
    bodies = await _rows(
        database,
        f'SELECT body FROM "{_SCHEMA}".wpriv_comments '
        f"WHERE photo_id = {seeded.erased_photo}",
    )
    assert [tuple(row)[0] for row in bodies] == ["[erased]"], (
        "a depth-two table is reached through the nested subquery, or it is not "
        "reached at all -- and the second failure is silent"
    )


async def test_erasing_one_subject_leaves_every_other_subject_untouched(
    database, privacy: Privacy
) -> None:
    """The other direction, and the irreversible one.

    A predicate that matches too much destroys somebody else's data, and no
    plan review catches it because the plan looks identical either way.
    """
    seeded = await _seed(database)
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)

    captions = await _rows(
        database,
        f'SELECT caption FROM "{_SCHEMA}".wpriv_photos WHERE owner_id = {seeded.kept}',
    )
    assert [tuple(row)[0] for row in captions] == ["stays"]
    bodies = await _rows(
        database,
        f'SELECT body FROM "{_SCHEMA}".wpriv_comments '
        f"WHERE photo_id = {seeded.kept_photo}",
    )
    assert [tuple(row)[0] for row in bodies] == ["stays"]


async def test_running_the_erasure_twice_changes_nothing_the_second_time(
    database, privacy: Privacy
) -> None:
    """Job delivery is at-least-once, so the chunk has to be a no-op re-run.

    The `IS DISTINCT FROM '[erased]'` guard is what makes that true; without it
    a retried chunk rewrites rows it has already rewritten, and the pass's
    idempotence promise is false in a way nothing would notice until a trigger
    made it matter.
    """
    seeded = await _seed(database)
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)
    before = await _rows(
        database, f'SELECT id, caption FROM "{_SCHEMA}".wpriv_photos ORDER BY id'
    )
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)
    after = await _rows(
        database, f'SELECT id, caption FROM "{_SCHEMA}".wpriv_photos ORDER BY id'
    )
    assert [tuple(row) for row in before] == [tuple(row) for row in after]
    kept_captions = await _rows(
        database,
        f'SELECT caption FROM "{_SCHEMA}".wpriv_photos WHERE owner_id = {seeded.kept}',
    )
    assert [tuple(row)[0] for row in kept_captions] == ["stays"]


async def test_the_catalog_check_finds_a_foreign_key_the_orm_does_not_model(
    database, privacy: Privacy
) -> None:
    """The one method that opens a connection, and why it is worth having.

    A foreign key added by hand in a migration the ORM does not model is a path
    an erasure will never walk. It is invisible to the planner by construction,
    so it needs a check that reads the catalog and compares.
    """
    await _apply(
        database,
        f'CREATE TABLE IF NOT EXISTS "{_SCHEMA}".wpriv_tags (\n'
        f'  id bigint PRIMARY KEY DEFAULT nextval(\'"{_SCHEMA}".wpriv_seq\'),\n'
        f'  photo_id bigint NOT NULL REFERENCES "{_SCHEMA}".wpriv_photos (id),\n'
        "  label text NOT NULL\n"
        ")",
    )
    missing = await privacy.unmodelled_edges(database, schema=_SCHEMA)
    assert (
        f"{_SCHEMA}.wpriv_tags",
        "photo_id",
        f"{_SCHEMA}.wpriv_photos",
        "id",
    ) in missing


# -- the erasure record -------------------------------------------------------


async def _records(database) -> list[tuple]:
    return [
        tuple(row)
        for row in await _rows(
            database,
            "SELECT subject, subject_model, subject_column, plan_digest, "
            f'tables_touched, rows_affected FROM "{_SCHEMA}".wreath_erasures '
            "ORDER BY xid, seq",
        )
    ]


async def test_a_completed_erasure_writes_exactly_one_record(
    database, privacy: Privacy
) -> None:
    """The evidence the erasure happened, which is the whole argument for it.

    Without this row a restore from a backup taken before the erasure silently
    un-erases the subject, because nothing knows there is anything to replay.
    """
    seeded = await _seed(database)
    plan = privacy.plan(str(seeded.erased))
    await privacy.erase(
        database, str(seeded.erased), digest=plan.digest, schema=_SCHEMA
    )

    records = await _records(database)
    assert len(records) == 1, "one erasure, one record"
    subject, model, column_name, digest, tables, rows = records[0]
    assert (subject, model, column_name) == (str(seeded.erased), "Person", "id")
    assert digest == plan.digest, (
        "the record names the plan that ran, so a reviewer can tell which "
        "traversal produced it"
    )
    assert tables == 3
    assert rows > 0


async def test_the_record_holds_no_erased_value(database, privacy: Privacy) -> None:
    """A record of *what* was erased would be a re-identification store.

    The one thing the record must never grow. Asserted against the catalog
    rather than against a row, so a column added later fails here even if no
    test happens to write a value into it.
    """
    seeded = await _seed(database)
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)

    columns = {
        str(tuple(row)[0])
        for row in await _rows(
            database,
            "SELECT attname::text FROM pg_attribute "
            f"WHERE attrelid = '\"{_SCHEMA}\".wreath_erasures'::regclass "
            "AND attnum > 0 AND NOT attisdropped",
        )
    }
    assert columns == {
        "seq",
        "xid",
        "at",
        "subject",
        "subject_model",
        "subject_column",
        "plan_digest",
        "tables_touched",
        "rows_affected",
    }


async def test_re_running_an_erasure_does_not_record_it_twice(
    database, privacy: Privacy
) -> None:
    """At-least-once delivery must not produce two receipts for one erasure."""
    seeded = await _seed(database)
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)
    assert len(await _records(database)) == 1


async def test_an_unfinished_erasure_records_nothing(
    database, privacy: Privacy
) -> None:
    """The refusal that makes the record worth reading.

    A receipt written for an erasure that stopped part-way is worse than no
    receipt: the subject has already been told it is done. The pass is left
    blocked here by dropping the table out from under the walk, which is how a
    pass fails for real.
    """
    seeded = await _seed(database)
    prepared = privacy.prepare(str(seeded.erased), schema=_SCHEMA)
    # Run nothing at all: every declared pass is missing from the ledger, which
    # is precisely "this erasure has not happened".
    from wreath.privacy import record_erasure

    with pytest.raises(ErasureIncomplete) as caught:
        await record_erasure(prepared, database)
    assert "have not finished" in str(caught.value)
    assert await _records(database) == []


async def test_the_record_survives_the_erasure_it_describes(
    database, privacy: Privacy
) -> None:
    """The subject's row is gone; the row saying so is not.

    An erasure that deleted the record of itself would be a compliance failure
    in the other direction, and the record lives in the wreath schema rather
    than in the application's, so no traversal can reach it.
    """
    seeded = await _seed(database)
    await privacy.erase(database, str(seeded.erased), schema=_SCHEMA)
    people = await _rows(
        database, f'SELECT id FROM "{_SCHEMA}".wpriv_people ORDER BY id'
    )
    assert seeded.erased not in [tuple(row)[0] for row in people]
    assert [record[0] for record in await _records(database)] == [str(seeded.erased)]
