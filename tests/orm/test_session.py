from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.compiler import MAX_SELECTIN_KEYS
from wreath.orm.errors import (
    DetachedInstanceError,
    MappingError,
    MultipleResultsError,
    NoResultError,
    ORMError,
    SessionClosedError,
    SessionError,
    StaleDataError,
    UnloadedRelationshipError,
)
from wreath.orm.model import DELETED, DETACHED, PERSISTENT
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text, Timestamp, TsVector
from wreath.postgres import PipelineFullError

from .conftest import FakeDatabase, Membership, Post, User, post_row, user_row

pytestmark = pytest.mark.asyncio


async def test_a_session_acquires_its_connection_lazily(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "read")
    assert database.acquired == 0
    assert session.connection is None
    database.connection.script("users", [user_row(1)])
    await session.fetch(User.select())
    assert database.acquired == 1


async def test_closing_an_unused_session_returns_nothing(
    registry: Registry, database: FakeDatabase
) -> None:
    await Session(registry, "read").close()
    assert database.released == 0


async def test_close_returns_the_connection_exactly_once(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "read")
    database.connection.script("users", [user_row(1)])
    await session.fetch(User.select())
    await session.close()
    await session.close()
    assert database.released == 1


async def test_use_after_close_is_rejected(registry: Registry) -> None:
    session = Session(registry, "read")
    await session.close()
    with pytest.raises(SessionClosedError):
        await session.fetch(User.select())


async def test_closing_detaches_objects_but_keeps_loaded_values_readable(
    registry: Registry, database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1, "a@b.c")])
    user = (await session.fetch(User.select()))[0]
    await session.close()
    assert user._orm_state == DETACHED
    assert user.email == "a@b.c"


async def test_repeated_rows_for_one_key_produce_one_object(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1), user_row(1), user_row(1)])
    users = await session.fetch(User.select())
    assert len(users) == 1


async def test_one_identity_is_reused_across_queries(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1)])
    first = (await session.fetch(User.select()))[0]
    second = (await session.fetch(User.select()))[0]
    assert first is second


async def test_a_partial_projection_merges_into_an_existing_object(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("id", [[1, "a@b.c"]])
    partial = (await session.fetch(User.select(User.id, User.email)))[0]
    assert not partial._orm_is_loaded(2)
    database.connection.responses.clear()
    database.connection.script("users", [user_row(1, "a@b.c", "Full")])
    full = (await session.fetch(User.select()))[0]
    assert full is partial
    assert full.name == "Full"


async def test_a_refetch_does_not_revert_a_dirty_field(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1, "a@b.c")])
    user = (await session.fetch(User.select()))[0]
    user.email = "pending@x.y"
    refetched = (await session.fetch(User.select()))[0]
    assert refetched is user
    assert user.email == "pending@x.y"
    assert user._orm_is_dirty(1)


async def test_composite_identities_key_on_every_component(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("memberships", [[1, 1, "a"], [1, 2, "b"], [2, 1, "c"]])
    rows = await session.fetch(Membership.select())
    assert len(rows) == 3
    assert {(item.org_id, item.user_id) for item in rows} == {(1, 1), (1, 2), (2, 1)}


async def test_sessions_do_not_share_identity_objects(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    first = Session(registry, "read")
    second = Session(registry, "read")
    assert (await first.fetch(User.select()))[0] is not (await second.fetch(User.select()))[0]


async def test_get_compiles_a_primary_key_query(database: FakeDatabase, session: Session) -> None:
    database.connection.script("users", [user_row(7)])
    user = await session.get(User, 7)
    assert user is not None and user.id == 7
    sql, args = database.connection.calls[0]
    assert '"t0"."id" = $1' in sql and "LIMIT" in sql
    assert args == (7, 1)


async def test_get_returns_none_when_nothing_matches(
    database: FakeDatabase, session: Session
) -> None:
    assert await session.get(User, 404) is None


async def test_require_names_the_missing_primary_key(session: Session) -> None:
    with pytest.raises(NoResultError, match=r"User.*404.*does not exist"):
        await session.require(User, 404)


async def test_require_one_rejects_an_empty_query(session: Session) -> None:
    with pytest.raises(NoResultError, match=r"matched no rows for User"):
        await session.require_one(User.select().where(User.email == "gone@example.test"))


async def test_get_requires_a_full_composite_key(session: Session) -> None:
    with pytest.raises(TypeError, match="2-column primary key"):
        await session.get(Membership, 1)


async def test_fetch_one_bounds_the_query_and_rejects_two_rows(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1), user_row(2)])
    with pytest.raises(MultipleResultsError, match="matched 2 rows"):
        await session.fetch_one(User.select())
    assert database.connection.calls[0][1] == (2,)


async def test_fetch_one_keeps_a_stricter_limit(database: FakeDatabase, session: Session) -> None:
    database.connection.script("users", [user_row(1)])
    await session.fetch_one(User.select().limit(1))
    assert database.connection.calls[0][1] == (1,)


async def test_selectin_batches_a_collection_without_n_plus_one(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1), user_row(2), user_row(3)])
    database.connection.script("posts", [post_row(10, 1), post_row(11, 1), post_row(12, 3)])
    users = await session.fetch(User.select().include(User.posts.selectin()))
    assert len(database.connection.calls) == 2
    assert [len(item.posts) for item in users] == [2, 0, 1]
    assert users[0].posts[0].id == 10


async def test_selectin_deduplicates_keys(database: FakeDatabase, session: Session) -> None:
    database.connection.script("posts", [post_row(1, 5), post_row(2, 5)])
    database.connection.script("users", [user_row(5)])
    posts = await session.fetch(Post.select().include(Post.author.selectin()))
    _, args = database.connection.calls[1]
    assert args == (5,)
    assert posts[0].author is posts[1].author


async def test_a_null_foreign_key_loads_as_none_without_a_query() -> None:
    from wreath.orm import relationship

    class Team(Model, table="teams"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Player(Model, table="players"):
        id: Mapped[int] = column(Int64, primary_key=True)
        team_id: Mapped[int] = column(Int64, nullable=True, references=Team.id)
        team = relationship(Team, foreign_key=team_id, load="raise")

    database = FakeDatabase()
    registry = Registry(database, [Team, Player], validate_schema="off")
    session = Session(registry, "read")
    database.connection.script("players", [[1, None]])
    players = await session.fetch(Player.select().include(Player.team.selectin()))
    assert players[0].team is None
    assert len(database.connection.calls) == 1


async def test_joined_to_one_assembles_without_a_second_query(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("LEFT JOIN", [[10, 1, "t", *user_row(1, "a@b.c")]])
    posts = await session.fetch(Post.select().include(Post.author.joined()))
    assert len(database.connection.calls) == 1
    assert posts[0].author.email == "a@b.c"


async def test_a_joined_row_that_matched_nothing_becomes_none(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("LEFT JOIN", [[10, 1, "t", None, None, None, None]])
    posts = await session.fetch(Post.select().include(Post.author.joined()))
    assert posts[0].author is None


async def test_explicit_load_batches_across_instances(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1), user_row(2)])
    users = await session.fetch(User.select())
    with pytest.raises(UnloadedRelationshipError):
        users[0].posts  # noqa: B018 - the read is the subject
    database.connection.script("posts", [post_row(10, 1), post_row(11, 2)])
    await session.load(users, User.posts)
    assert len(database.connection.calls) == 2
    assert users[0].posts[0].id == 10
    assert users[1].posts[0].id == 11


async def test_load_accepts_a_single_instance(database: FakeDatabase, session: Session) -> None:
    database.connection.script("users", [user_row(1)])
    user = (await session.fetch(User.select()))[0]
    database.connection.script("posts", [post_row(10, 1)])
    await session.load(user, User.posts)
    assert len(user.posts) == 1


async def test_load_rejects_a_relationship_from_another_model(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1)])
    user = (await session.fetch(User.select()))[0]
    with pytest.raises(TypeError, match="declared on"):
        await session.load(user, Post.author)


async def test_selectin_batches_stay_within_the_configured_bound(
    database: FakeDatabase, session: Session
) -> None:
    total = MAX_SELECTIN_KEYS + 5
    database.connection.script("users", [user_row(i) for i in range(total)])
    users = await session.fetch(User.select())
    database.connection.calls.clear()
    database.connection.script("posts", [])
    await session.load(users, User.posts)
    assert len(database.connection.calls) == 2
    assert len(database.connection.calls[0][1]) == MAX_SELECTIN_KEYS
    # The tail batch is padded up to one of the allowed statement widths, so a
    # run of odd key counts does not mint a plan-cache entry each. The five real
    # keys are still the only distinct ones -- `IN` collapses the repeats -- and
    # the width never exceeds the bound this test is named for.
    from wreath.orm.session import _batch_widths

    tail = len(database.connection.calls[1][1])
    assert 5 <= tail <= MAX_SELECTIN_KEYS
    assert tail in _batch_widths()
    assert len(set(database.connection.calls[1][1])) == 5


async def test_nested_selectin_loads_the_next_level(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1)])
    database.connection.script("posts", [post_row(10, 1)])
    users = await session.fetch(User.select().include(User.posts.selectin(Post.author.selectin())))
    assert users[0].posts[0].author is users[0]


async def test_raw_returns_driver_records_untouched(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("custom", [["anything"]])
    rows = await session.raw("SELECT 1 AS custom").fetch()
    # Asserted through the `Record` surface rather than against a list: `raw`
    # returns what the driver returned, and what the driver returns is not a
    # list. Comparing to one passed only because the fake handed back the
    # container it was scripted with.
    assert len(rows) == 1
    assert rows[0][0] == "anything"
    assert not hasattr(rows[0], "append")
    assert database.connection.calls[0] == ("SELECT 1 AS custom", ())


async def test_raw_passes_arguments_through(database: FakeDatabase, session: Session) -> None:
    await session.raw("SELECT $1::int", 5).fetchval()
    assert database.connection.calls[0] == ("SELECT $1::int", (5,))


async def test_raw_models_validates_names_and_oids(
    database: FakeDatabase, session: Session
) -> None:
    sql = "SELECT id, email, name, created_at FROM users"
    database.connection.script("users", [user_row(1, "a@b.c")])
    database.connection.describe(
        sql, ("id", "email", "name", "created_at"), (Int64.oid, Text.oid, Text.oid, Timestamp.oid)
    )
    users = await session.raw(sql).models(User)
    assert users[0].email == "a@b.c"
    assert users[0]._orm_state == PERSISTENT


async def test_raw_models_rejects_a_missing_column(
    database: FakeDatabase, session: Session
) -> None:
    sql = "SELECT id, email FROM users"
    database.connection.script("users", [[1, "a@b.c"]])
    database.connection.describe(sql, ("id", "email"), (Int64.oid, Text.oid))
    with pytest.raises(MappingError, match="missing created_at, name"):
        await session.raw(sql).models(User)


async def test_raw_models_rejects_an_extra_column(database: FakeDatabase, session: Session) -> None:
    sql = "SELECT id, email, name, created_at, extra FROM users"
    database.connection.script("users", [[1, "a@b.c", "A", None, "x"]])
    database.connection.describe(
        sql,
        ("id", "email", "name", "created_at", "extra"),
        (Int64.oid, Text.oid, Text.oid, Timestamp.oid, Text.oid),
    )
    with pytest.raises(MappingError, match="unexpected extra"):
        await session.raw(sql).models(User)


async def test_raw_models_rejects_a_type_mismatch(database: FakeDatabase, session: Session) -> None:
    sql = "SELECT id, email, name, created_at FROM users"
    database.connection.script("users", [user_row(1)])
    # `checked=False`: the plan disagreeing with the model *is* the subject
    # here -- `id` is declared `Text` against an `Int64` column so `models()`
    # has an OID mismatch to reject. The fake's row-fidelity guard would
    # otherwise refuse the scripted `int` before the code under test ran.
    database.connection.describe(
        sql,
        ("id", "email", "name", "created_at"),
        (Text.oid, Text.oid, Text.oid, Timestamp.oid),
        checked=False,
    )
    with pytest.raises(MappingError, match="OID"):
        await session.raw(sql).models(User)


async def test_raw_models_rejects_duplicate_names(database: FakeDatabase, session: Session) -> None:
    sql = "SELECT id, id, name, created_at FROM users"
    database.connection.script("users", [[1, 1, "A", None]])
    database.connection.describe(
        sql, ("id", "id", "name", "created_at"), (Int64.oid, Int64.oid, Text.oid, Timestamp.oid)
    )
    with pytest.raises(MappingError, match="more than once"):
        await session.raw(sql).models(User)


async def test_begin_commits_on_clean_exit(database: FakeDatabase, session: Session) -> None:
    async with session.begin():
        await session.raw("SELECT 1").execute()
    assert database.connection.statements == ["BEGIN", "SELECT 1", "COMMIT"]


async def test_begin_rolls_back_on_error(database: FakeDatabase, session: Session) -> None:
    with pytest.raises(RuntimeError):
        async with session.begin():
            raise RuntimeError("boom")
    assert database.connection.statements == ["BEGIN", "ROLLBACK"]


async def test_begin_rolls_back_on_cancellation(database: FakeDatabase, session: Session) -> None:
    with pytest.raises(asyncio.CancelledError):
        async with session.begin():
            raise asyncio.CancelledError
    assert database.connection.statements == ["BEGIN", "ROLLBACK"]


async def test_nested_transactions_use_deterministic_savepoints(
    database: FakeDatabase, session: Session
) -> None:
    async with session.begin():
        async with session.begin():
            async with session.begin():
                pass
    assert database.connection.statements == [
        "BEGIN",
        "SAVEPOINT wreath_sp_1",
        "SAVEPOINT wreath_sp_2",
        "RELEASE SAVEPOINT wreath_sp_2",
        "RELEASE SAVEPOINT wreath_sp_1",
        "COMMIT",
    ]


async def test_an_inner_failure_rolls_back_to_its_savepoint(
    database: FakeDatabase, session: Session
) -> None:
    async with session.begin():
        with pytest.raises(RuntimeError):
            async with session.begin():
                raise RuntimeError("inner")
    assert database.connection.statements == [
        "BEGIN",
        "SAVEPOINT wreath_sp_1",
        "ROLLBACK TO SAVEPOINT wreath_sp_1",
        "RELEASE SAVEPOINT wreath_sp_1",
        "COMMIT",
    ]


async def test_a_failed_rollback_discards_the_connection(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "write")
    database.connection.fail_on["ROLLBACK"] = RuntimeError("lost")
    with pytest.raises(RuntimeError):
        async with session.begin():
            raise ValueError("boom")
    await session.close()
    # Transaction state cannot be proven, so the pool must not reuse it.
    assert database.connection.closed
    assert database.released == 1


async def test_for_update_requires_a_write_session(registry: Registry) -> None:
    session = Session(registry, "read")
    async with session.begin():
        with pytest.raises(SessionError, match="write-workload"):
            await session.fetch(User.select().for_update())


async def test_for_update_requires_an_explicit_transaction(session: Session) -> None:
    with pytest.raises(SessionError, match="explicit transaction"):
        await session.fetch(User.select().for_update())


async def test_create_uses_the_model_constructor_and_flush_path(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[17, None]])
    user = await session.create(User, email="new@example.test", name="New")
    assert user.id == 17
    assert any(statement.startswith("INSERT") for statement in database.connection.statements)


async def test_update_where_is_predicate_bounded_and_returns_affected_rows(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script_command("UPDATE", "UPDATE 2")
    count = await session.update_where(
        User.select().where(User.email.ilike("%@example.test")), name="Moved"
    )
    assert count == 2
    sql, args = next(
        (sql, args) for sql, args in database.connection.calls if sql.startswith("UPDATE")
    )
    assert sql == ('UPDATE "public"."users" AS "t0" SET "name" = $1 WHERE "t0"."email" ILIKE $2')
    assert args == ("Moved", "%@example.test")


async def test_delete_where_refuses_a_predicate_free_query(session: Session) -> None:
    with pytest.raises(ORMError, match="requires an explicit where"):
        await session.delete_where(User.select())


async def test_bulk_write_detaches_loaded_objects(database: FakeDatabase, session: Session) -> None:
    database.connection.script("users", [user_row(1)])
    database.connection.script("posts", [post_row(2, 1)])
    user = await session.require(User, 1)
    post = await session.require(Post, 2)
    database.connection.script_command("DELETE", "DELETE 1")
    assert await session.delete_where(User.select().where(User.id == 1)) == 1
    assert "detached" in repr(user)
    assert post._orm_state == PERSISTENT
    assert post._orm_owner is session


async def test_bulk_write_keeps_loaded_objects_when_its_commit_fails(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1)])
    user = await session.require(User, 1)
    database.connection.script_command("DELETE", "DELETE 1")
    database.connection.fail_on["COMMIT"] = RuntimeError("commit lost")
    with pytest.raises(RuntimeError, match="commit lost"):
        await session.delete_where(User.select().where(User.id == 1))
    assert user._orm_state == PERSISTENT
    assert user._orm_owner is session


async def test_bulk_write_refuses_read_only_query_shape(session: Session) -> None:
    with pytest.raises(ORMError, match="read semantics"):
        await session.update_where(User.select().where(User.id == 1).limit(1), name="Changed")


@pytest.mark.parametrize(
    "query",
    [
        User.select(User.id).where(User.id == 1),
        User.select().where(User.id == 1).include(User.posts.selectin()),
        User.select().where(User.id == 1).order_by(User.id),
        User.select().where(User.id == 1).offset(1),
        User.select().where(User.id == 1).for_update(),
    ],
)
async def test_bulk_write_refuses_each_read_only_query_feature(session: Session, query) -> None:
    with pytest.raises(ORMError, match="read semantics"):
        await session.delete_where(query)


async def test_bulk_write_refuses_invalid_assignments(session: Session) -> None:
    query = User.select().where(User.id == 1)
    with pytest.raises(ORMError, match="at least one"):
        await session.update_where(query)
    with pytest.raises(ORMError, match="no column"):
        await session.update_where(query, missing="value")
    with pytest.raises(ORMError, match="primary key"):
        await session.update_where(query, id=2)


async def test_bulk_update_refuses_generated_columns(database: FakeDatabase) -> None:
    class Document(Model, table="bulk_documents"):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text)
        search: Mapped[str] = column(TsVector(sources=("title",)))

    registry = Registry(database, [Document], validate_schema="off")
    session = Session(registry, "write")
    with pytest.raises(ORMError, match="generated column"):
        await session.update_where(Document.select().where(Document.id == 1), search="ignored")


async def test_bulk_write_refuses_relationship_predicates(session: Session) -> None:
    predicate = Post.author.name == "Ada"
    with pytest.raises(ORMError, match="relationship predicates"):
        await session.update_where(Post.select().where(predicate), title="Moved")
    with pytest.raises(ORMError, match="relationship predicates"):
        await session.delete_where(Post.select().where(predicate))


async def test_bulk_update_refuses_cross_field_rules(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setattr(User, "__wreath_compiled_rules__", (object(),))
    with pytest.raises(SessionError, match="cross-field rules"):
        await session.update_where(User.select().where(User.id == 1), name="Moved")


async def test_bulk_writes_refuse_audited_models(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setattr(User, "__wreath_facets__", {"audit": object()})
    query = User.select().where(User.id == 1)
    with pytest.raises(SessionError, match="audited"):
        await session.update_where(query, name="Moved")
    with pytest.raises(SessionError, match="audited"):
        await session.delete_where(query)


async def test_bulk_writes_check_session_lifecycle(session: Session) -> None:
    await session.close()
    query = User.select().where(User.id == 1)
    with pytest.raises(SessionClosedError):
        await session.update_where(query, name="Moved")
    with pytest.raises(SessionClosedError):
        await session.delete_where(query)


@pytest.mark.parametrize(
    "status",
    [None, "OK", "DELETE 1", "UPDATE", "UPDATE ", "UPDATE ١", "UPDATE 1.0"],
)
async def test_bulk_update_refuses_untruthful_command_tags(
    database: FakeDatabase, session: Session, status
) -> None:
    database.connection.script_command("UPDATE", status)
    with pytest.raises(SessionError, match="expected a UPDATE <row-count>"):
        await session.update_where(User.select().where(User.id == 1), name="Moved")


async def test_insert_uses_returning_for_unloaded_columns(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    user = User(email="a@b.c", name="A")
    session.add(user)
    await session.flush()
    sql, args = database.connection.calls[1]
    assert sql == (
        'INSERT INTO "public"."users" ("email", "name") VALUES ($1, $2) '
        'RETURNING "id", "created_at"'
    )
    assert args == ("a@b.c", "A")
    assert user.id == 7
    assert user._orm_state == PERSISTENT


async def test_insert_batches_adjacent_rows_with_the_same_complete_shape(
    database: FakeDatabase, session: Session
) -> None:
    users = [
        User(id=index, email=f"{index}@b.c", name=f"U{index}", created_at=None)
        for index in range(1, 4)
    ]
    for user in users:
        session.add(user)

    await session.flush()

    inserts = [(sql, args) for sql, args in database.connection.calls if sql.startswith("INSERT")]
    assert inserts == [
        (
            'INSERT INTO "public"."users" ("id", "email", "name", "created_at") '
            "VALUES ($1, $2, $3, $4), ($5, $6, $7, $8), ($9, $10, $11, $12)",
            (
                1,
                "1@b.c",
                "U1",
                None,
                2,
                "2@b.c",
                "U2",
                None,
                3,
                "3@b.c",
                "U3",
                None,
            ),
        )
    ]
    assert all(user._orm_state == PERSISTENT for user in users)
    assert [
        session._identity[(session._registry.spec_for(User), (index,))]
        for index in range(1, 4)
    ] == users


async def test_insert_does_not_batch_across_a_different_shape(
    database: FakeDatabase, session: Session
) -> None:
    complete_one = User(id=1, email="1@b.c", name="U1", created_at=None)
    partial = User(id=2, email="2@b.c", name="U2")
    complete_two = User(id=3, email="3@b.c", name="U3", created_at=None)
    database.connection.script("RETURNING", [[None]])
    for user in (complete_one, partial, complete_two):
        session.add(user)

    await session.flush()

    inserts = [(sql, args) for sql, args in database.connection.calls if sql.startswith("INSERT")]
    assert len(inserts) == 3
    assert [args[0] for _sql, args in inserts] == [1, 2, 3]


async def test_insert_batches_rows_that_return_server_generated_keys(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None], [8, None]])
    users = [User(email="a@b.c", name="A"), User(email="b@b.c", name="B")]
    for user in users:
        session.add(user)

    await session.flush()

    inserts = [call for call in database.connection.calls if call[0].startswith("INSERT")]
    assert inserts == [
        (
            'INSERT INTO "public"."users" ("email", "name") '
            'VALUES ($1, $2), ($3, $4) RETURNING "id", "created_at"',
            ("a@b.c", "A", "b@b.c", "B"),
        )
    ]
    assert [user.id for user in users] == [7, 8]


async def test_insert_batch_stays_within_the_parameter_limit(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    monkeypatch.setattr("wreath.orm.session.MAX_BIND_PARAMETERS", 8)
    users = [
        User(id=index, email=f"{index}@b.c", name=f"U{index}", created_at=None)
        for index in range(1, 4)
    ]
    for user in users:
        session.add(user)

    await session.flush()

    inserts = [(sql, args) for sql, args in database.connection.calls if sql.startswith("INSERT")]
    assert [len(args) for _sql, args in inserts] == [8, 4]


async def test_returning_insert_batch_stays_within_the_parameter_limit(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    monkeypatch.setattr("wreath.orm.session.MAX_BIND_PARAMETERS", 4)
    users = [
        User(email=f"{index}@b.c", name=f"U{index}")
        for index in range(1, 4)
    ]

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        database.connection._record(sql, args)
        return [
            (int(str(args[offset]).split("@", 1)[0]), None)
            for offset in range(0, len(args), 2)
        ]

    monkeypatch.setattr(database.connection, "fetch", fetch)
    for user in users:
        session.add(user)

    await session.flush()

    inserts = [(sql, args) for sql, args in database.connection.calls if sql.startswith("INSERT")]
    assert [len(args) for _sql, args in inserts] == [4, 2]
    assert [user.id for user in users] == [1, 2, 3]


async def test_insert_batch_splits_when_the_encoded_packet_is_too_large(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    execute = database.connection.execute

    async def bounded_execute(sql: str, *args: object) -> str:
        if sql.startswith("INSERT") and "), (" in sql:
            raise PipelineFullError("operation exceeds maximum outbound batch")
        return await execute(sql, *args)

    monkeypatch.setattr(database.connection, "execute", bounded_execute)
    users = [
        User(id=index, email=f"{index}@b.c", name=f"U{index}", created_at=None)
        for index in range(1, 4)
    ]
    for user in users:
        session.add(user)

    await session.flush()

    inserts = [sql for sql in database.connection.statements if sql.startswith("INSERT")]
    assert len(inserts) == 3
    assert all(user._orm_state == PERSISTENT for user in users)


async def test_a_failed_insert_batch_preserves_every_pending_instance(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.fail_on["INSERT"] = RuntimeError("constraint")
    users = [
        User(id=index, email=f"{index}@b.c", name=f"U{index}", created_at=None)
        for index in range(1, 4)
    ]
    for user in users:
        session.add(user)

    with pytest.raises(RuntimeError, match="constraint"):
        await session.flush()

    assert session._new == users
    assert all(user._orm_state != PERSISTENT for user in users)
    assert "ROLLBACK" in database.connection.statements


async def test_a_late_returning_insert_chunk_failure_preserves_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    monkeypatch.setattr("wreath.orm.session.MAX_BIND_PARAMETERS", 4)
    users = [
        User(email=f"{index}@b.c", name=f"U{index}")
        for index in range(1, 4)
    ]
    calls = 0

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        nonlocal calls
        database.connection._record(sql, args)
        calls += 1
        if calls == 2:
            raise RuntimeError("constraint")
        return [(7, None), (8, None)]

    monkeypatch.setattr(database.connection, "fetch", fetch)
    for user in users:
        session.add(user)

    with pytest.raises(RuntimeError, match="constraint"):
        await session.flush()

    assert session._new == users
    assert all(user._orm_state != PERSISTENT for user in users)
    assert all(user._orm_primary_key() is None for user in users)


async def test_an_inserted_object_enters_the_identity_map(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    user = User(email="a@b.c", name="A")
    session.add(user)
    await session.flush()
    database.connection.script("SELECT", [user_row(7, "a@b.c")])
    assert (await session.get(User, 7)) is user


async def test_a_staged_generated_key_is_present_in_the_insert_audit(
    monkeypatch: pytest.MonkeyPatch, registry: Registry, database: FakeDatabase
) -> None:
    class Facet:
        redact = frozenset()

    class Audit:
        def __init__(self) -> None:
            self.changes: list[Any] = []

        def attribute(self) -> str:
            return "test:actor"

        async def record_many(self, changes: list[Any], *, connection: Any) -> None:
            self.changes += changes

    monkeypatch.setattr(User, "__wreath_facets__", {"audit": Facet()})
    audit = Audit()
    audited = Session(registry, "write", audit=audit)
    database.connection.script("INSERT", [[7, None]])
    user = User(email="a@b.c", name="A")
    audited.add(user)

    await audited.flush()

    assert audit.changes[0].key == "7"
    assert audit.changes[0].fields["id"] == 7


async def test_update_writes_only_dirty_columns(database: FakeDatabase, session: Session) -> None:
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    user = (await session.fetch(User.select()))[0]
    user.name = "B"
    database.connection.script("UPDATE", [[1]])
    database.connection.calls.clear()
    await session.flush()
    sql, args = database.connection.calls[1]
    assert sql == (
        'UPDATE "public"."users" AS "wreath_target" '
        'SET "name" = "wreath_batch"."name" '
        'FROM UNNEST($1::text[], $2::bigint[]) AS "wreath_batch" ("name", "id") '
        'WHERE "wreath_target"."id" = "wreath_batch"."id" '
        'RETURNING "wreath_target"."id"'
    )
    assert args == (["B"], [1])


async def test_updates_group_by_model_and_dirty_shape(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script(
        "SELECT", [user_row(1, "1@b.c", "A"), user_row(2, "2@b.c", "B")]
    )
    users = await session.fetch(User.select())
    users[0].name = "A2"
    users[1].name = "B2"
    database.connection.script("UPDATE", [[1], [2]])
    database.connection.calls.clear()

    await session.flush()

    updates = [call for call in database.connection.calls if call[0].startswith("UPDATE")]
    assert len(updates) == 1
    assert "UNNEST($1::text[], $2::bigint[])" in updates[0][0]
    assert updates[0][1] == (["A2", "B2"], [1, 2])
    assert not users[0]._orm_has_changes()
    assert not users[1]._orm_has_changes()


async def test_updates_consolidate_nonadjacent_equal_dirty_shapes(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    database.connection.script(
        "SELECT",
        [
            user_row(1, "1@b.c", "A"),
            user_row(2, "2@b.c", "B"),
            user_row(3, "3@b.c", "C"),
        ],
    )
    users = await session.fetch(User.select())
    users[0].name = "A2"
    users[1].email = "two@b.c"
    users[2].name = "C2"

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        database.connection._record(sql, args)
        return [(key,) for key in cast(list[object], args[-1])]

    monkeypatch.setattr(database.connection, "fetch", fetch)
    database.connection.calls.clear()

    await session.flush()

    updates = [call for call in database.connection.calls if call[0].startswith("UPDATE")]
    assert len(updates) == 2
    assert updates[0][1] == (["A2", "C2"], [1, 3])
    assert updates[1][1] == (["two@b.c"], [2])


async def test_update_batch_chunks_at_the_array_row_limit(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    monkeypatch.setattr("wreath.orm.session.MAX_BIND_PARAMETERS", 2)
    database.connection.script(
        "SELECT",
        [
            user_row(1, "1@b.c", "A"),
            user_row(2, "2@b.c", "B"),
            user_row(3, "3@b.c", "C"),
        ],
    )
    users = await session.fetch(User.select())
    for user in users:
        user.name += "2"

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        database.connection._record(sql, args)
        return [(key,) for key in cast(list[object], args[-1])]

    monkeypatch.setattr(database.connection, "fetch", fetch)
    database.connection.calls.clear()

    await session.flush()

    updates = [call for call in database.connection.calls if call[0].startswith("UPDATE")]
    assert [args for _sql, args in updates] == [
        (["A2", "B2"], [1, 2]),
        (["C2"], [3]),
    ]


async def test_update_unnest_batch_splits_when_the_packet_is_too_large(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    database.connection.script(
        "SELECT", [user_row(1, name="A"), user_row(2, name="B")]
    )
    users = await session.fetch(User.select())
    for user in users:
        user.name += "2"

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        keys = cast(list[object], args[-1])
        if len(keys) > 1:
            raise PipelineFullError("operation exceeds maximum outbound batch")
        database.connection._record(sql, args)
        return [(keys[0],)]

    monkeypatch.setattr(database.connection, "fetch", fetch)
    database.connection.calls.clear()

    await session.flush()

    updates = [call for call in database.connection.calls if call[0].startswith("UPDATE")]
    assert [args for _sql, args in updates] == [(["A2"], [1]), (["B2"], [2])]
    assert all(not user._orm_has_changes() for user in users)


async def test_a_stale_update_batch_keeps_every_dirty_state(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script(
        "SELECT", [user_row(1, "1@b.c", "A"), user_row(2, "2@b.c", "B")]
    )
    users = await session.fetch(User.select())
    for user in users:
        user.name += "2"
    database.connection.script("UPDATE", [[1]])

    with pytest.raises(StaleDataError, match=r"User.*\(2,\)"):
        await session.flush()

    assert all(user._orm_has_changes() for user in users)


async def test_a_stale_update_batch_rolls_back_its_explicit_transaction_savepoint(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script(
        "SELECT", [user_row(1, "1@b.c", "A"), user_row(2, "2@b.c", "B")]
    )
    users = await session.fetch(User.select())
    for user in users:
        user.name += "2"
    database.connection.script("UPDATE", [[1]])
    database.connection.calls.clear()

    async with session.begin():
        with pytest.raises(StaleDataError, match=r"User.*\(2,\)"):
            await session.flush()

    statements = database.connection.statements
    assert statements[:2] == ["BEGIN", "SAVEPOINT wreath_sp_1"]
    assert statements[2].startswith("UPDATE")
    assert statements[3:] == [
        "ROLLBACK TO SAVEPOINT wreath_sp_1",
        "RELEASE SAVEPOINT wreath_sp_1",
        "COMMIT",
    ]


async def test_a_later_stale_update_group_keeps_an_earlier_group_dirty(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    database.connection.script(
        "SELECT", [user_row(1, "1@b.c", "A"), user_row(2, "2@b.c", "B")]
    )
    users = await session.fetch(User.select())
    users[0].name = "changed"
    users[1].email = "changed@b.c"

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        database.connection._record(sql, args)
        return [(1,)] if 'SET "name"' in sql else []

    monkeypatch.setattr(database.connection, "fetch", fetch)
    database.connection.calls.clear()

    async with session.begin():
        with pytest.raises(StaleDataError, match=r"User.*\(2,\)"):
            await session.flush()

    assert all(user._orm_has_changes() for user in users)
    assert database.connection.statements[-3:] == [
        "ROLLBACK TO SAVEPOINT wreath_sp_1",
        "RELEASE SAVEPOINT wreath_sp_1",
        "COMMIT",
    ]


async def test_a_stale_update_after_an_insert_restores_the_new_instance(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "1@b.c", "A")])
    existing = (await session.fetch(User.select()))[0]
    existing.name = "changed"
    inserted = User(email="new@b.c", name="new")
    session.add(inserted)

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        database.connection._record(sql, args)
        return [(7, None)] if sql.startswith("INSERT") else []

    monkeypatch.setattr(database.connection, "fetch", fetch)
    database.connection.calls.clear()

    async with session.begin():
        with pytest.raises(StaleDataError, match=r"User.*\(1,\)"):
            await session.flush()

    assert inserted._orm_state != PERSISTENT
    assert inserted._orm_primary_key() is None
    assert inserted in session._new
    assert existing._orm_has_changes()


async def test_an_object_with_no_changes_emits_no_sql(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1)])
    await session.fetch(User.select())
    database.connection.calls.clear()
    await session.flush()
    assert database.connection.calls == []


async def test_delete_uses_the_complete_primary_key(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [[1, 1, "admin"]])
    membership = (await session.fetch(Membership.select()))[0]
    session.delete(membership)
    database.connection.script("DELETE", [[1, 1]])
    database.connection.calls.clear()
    await session.flush()
    sql, args = database.connection.calls[1]
    assert sql == (
        'DELETE FROM "public"."memberships" AS "wreath_target" '
        'USING UNNEST($1::bigint[], $2::bigint[]) '
        'AS "wreath_batch" ("org_id", "user_id") '
        'WHERE "wreath_target"."org_id" = "wreath_batch"."org_id" AND '
        '"wreath_target"."user_id" = "wreath_batch"."user_id" '
        'RETURNING "wreath_target"."org_id", "wreath_target"."user_id"'
    )
    assert args == ([1], [1])


async def test_deletes_batch_by_model_with_composite_stale_detection(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [[1, 1, "admin"], [1, 2, "member"]])
    memberships = await session.fetch(Membership.select())
    for membership in memberships:
        session.delete(membership)
    database.connection.script("DELETE", [[1, 1], [1, 2]])
    database.connection.calls.clear()

    await session.flush()

    deletes = [call for call in database.connection.calls if call[0].startswith("DELETE")]
    assert len(deletes) == 1
    assert deletes[0][1] == ([1, 1], [1, 2])
    assert all(item._orm_state == DETACHED for item in memberships)


async def test_delete_batch_chunks_at_the_array_row_limit(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    monkeypatch.setattr("wreath.orm.session.MAX_BIND_PARAMETERS", 2)
    database.connection.script(
        "SELECT", [[1, 1, "admin"], [1, 2, "member"], [1, 3, "member"]]
    )
    memberships = await session.fetch(Membership.select())
    for membership in memberships:
        session.delete(membership)

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        database.connection._record(sql, args)
        return list(zip(*args, strict=True))

    monkeypatch.setattr(database.connection, "fetch", fetch)
    database.connection.calls.clear()

    await session.flush()

    deletes = [call for call in database.connection.calls if call[0].startswith("DELETE")]
    assert [args for _sql, args in deletes] == [
        ([1, 1], [1, 2]),
        ([1], [3]),
    ]


async def test_a_stale_delete_batch_keeps_every_identity_mapped(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [[1, 1, "admin"], [1, 2, "member"]])
    memberships = await session.fetch(Membership.select())
    for membership in memberships:
        session.delete(membership)
    database.connection.script("DELETE", [[1, 1]])

    with pytest.raises(StaleDataError, match=r"Membership.*\(1, 2\)"):
        await session.flush()

    spec = session._registry.spec_for(Membership)
    assert all(item._orm_state == DELETED for item in memberships)
    assert [session._identity[(spec, (1, key))] for key in (1, 2)] == memberships


async def test_a_stale_delete_batch_rolls_back_its_explicit_transaction_savepoint(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [[1, 1, "admin"], [1, 2, "member"]])
    memberships = await session.fetch(Membership.select())
    for membership in memberships:
        session.delete(membership)
    database.connection.script("DELETE", [[1, 1]])
    database.connection.calls.clear()

    async with session.begin():
        with pytest.raises(StaleDataError, match=r"Membership.*\(1, 2\)"):
            await session.flush()

    statements = database.connection.statements
    assert statements[:2] == ["BEGIN", "SAVEPOINT wreath_sp_1"]
    assert statements[2].startswith("DELETE")
    assert statements[3:] == [
        "ROLLBACK TO SAVEPOINT wreath_sp_1",
        "RELEASE SAVEPOINT wreath_sp_1",
        "COMMIT",
    ]


async def test_a_later_stale_delete_group_keeps_an_earlier_group_attached(
    monkeypatch: pytest.MonkeyPatch, database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "1@b.c", "A")])
    user = (await session.fetch(User.select()))[0]
    post = _persistent_post(session)
    session.delete(user)
    session.delete(post)

    async def fetch(sql: str, *args: object) -> list[tuple[object, ...]]:
        database.connection._record(sql, args)
        return [(5,)] if '"posts"' in sql else []

    monkeypatch.setattr(database.connection, "fetch", fetch)
    database.connection.calls.clear()

    async with session.begin():
        with pytest.raises(StaleDataError, match=r"User.*\(1,\)"):
            await session.flush()

    spec = session._registry.spec_for(Post)
    assert post._orm_state == DELETED
    assert post._orm_owner is session
    assert session._identity[(spec, (5,))] is post


async def test_flush_opens_and_commits_its_own_transaction(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    session.add(User(email="a@b.c", name="A"))
    await session.flush()
    assert database.connection.statements[0] == "BEGIN"
    assert database.connection.statements[-1] == "COMMIT"


async def test_insert_flush_inside_a_transaction_uses_a_savepoint(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    async with session.begin():
        session.add(User(email="a@b.c", name="A"))
        await session.flush()
    assert database.connection.statements.count("BEGIN") == 1
    assert "SAVEPOINT wreath_sp_1" in database.connection.statements


async def test_a_single_update_inside_a_transaction_uses_a_savepoint(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    user = (await session.fetch(User.select()))[0]
    user.name = "changed"
    database.connection.script("UPDATE", [[1]])
    database.connection.calls.clear()

    async with session.begin():
        await session.flush()

    assert "SAVEPOINT wreath_sp_1" in database.connection.statements


async def test_a_successful_update_batch_releases_its_flush_savepoint(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script(
        "SELECT", [user_row(1, "1@b.c", "A"), user_row(2, "2@b.c", "B")]
    )
    users = await session.fetch(User.select())
    for user in users:
        user.name = "changed"
    database.connection.script("UPDATE", [[1], [2]])
    database.connection.calls.clear()

    async with session.begin():
        await session.flush()

    statements = database.connection.statements
    assert statements[:2] == ["BEGIN", "SAVEPOINT wreath_sp_1"]
    assert statements[2].startswith("UPDATE")
    assert statements[3:] == ["RELEASE SAVEPOINT wreath_sp_1", "COMMIT"]


async def test_outer_rollback_invalidates_an_insert_for_retry(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    user = User(email="a@b.c", name="A")

    with pytest.raises(RuntimeError, match="rollback"):
        async with session.begin():
            session.add(user)
            await session.flush()
            assert user._orm_state == PERSISTENT
            assert user.id == 7
            raise RuntimeError("rollback")

    assert user._orm_state == DETACHED
    assert user._orm_owner is None
    retry = User(email="a@b.c", name="A")
    database.connection.responses.clear()
    database.connection.script("INSERT", [[8, None]])
    session.add(retry)
    await session.flush()
    assert retry._orm_state == PERSISTENT
    assert retry.id == 8


async def test_outer_rollback_restores_an_update_for_retry(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    user = (await session.fetch(User.select()))[0]
    user.name = "changed"
    database.connection.script("UPDATE", [[1]])

    with pytest.raises(RuntimeError, match="rollback"):
        async with session.begin():
            await session.flush()
            assert not user._orm_has_changes()
            raise RuntimeError("rollback")

    assert user._orm_state == DETACHED
    assert user._orm_owner is None
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    user = (await session.fetch(User.select()))[0]
    user.name = "changed"
    await session.flush()
    assert not user._orm_has_changes()


async def test_outer_rollback_restores_a_delete_for_retry(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [[1, 1, "admin"]])
    membership = (await session.fetch(Membership.select()))[0]
    session.delete(membership)
    database.connection.script("DELETE", [[1, 1]])

    with pytest.raises(RuntimeError, match="rollback"):
        async with session.begin():
            await session.flush()
            assert membership._orm_state == DETACHED
            raise RuntimeError("rollback")

    assert membership._orm_state == DETACHED
    assert membership._orm_owner is None
    database.connection.script("SELECT", [[1, 1, "admin"]])
    membership = (await session.fetch(Membership.select()))[0]
    session.delete(membership)
    await session.flush()
    assert membership._orm_state == DETACHED


async def test_outer_rollback_invalidates_every_successful_flush(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    existing = (await session.fetch(User.select()))[0]
    database.connection.script("UPDATE", [[1]])
    inserted = User(email="new@b.c", name="New")

    with pytest.raises(RuntimeError, match="rollback"):
        async with session.begin():
            existing.name = "changed"
            await session.flush()
            database.connection.responses.clear()
            database.connection.script("INSERT", [[7, None]])
            session.add(inserted)
            await session.flush()
            database.connection.responses.clear()
            database.connection.script("UPDATE", [[7]])
            inserted.name = "Newer"
            await session.flush()
            raise RuntimeError("rollback")

    assert existing._orm_state == DETACHED
    assert existing._orm_owner is None
    assert inserted._orm_state == DETACHED
    assert inserted._orm_owner is None
    assert inserted.name == "Newer"


async def test_flush_orders_inserts_then_updates_then_deletes(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    existing = (await session.fetch(User.select()))[0]
    existing.name = "changed"
    database.connection.responses.clear()
    database.connection.script("INSERT", [[7, None]])
    database.connection.script("UPDATE", [[1]])
    database.connection.script("DELETE", [[5]])
    session.add(User(email="new@x.y", name="N"))
    doomed = Post._orm_new()
    doomed._orm_set_loaded(0, 5)
    doomed._orm_set_loaded(1, 1)
    doomed._orm_set_loaded(2, "t")
    doomed._orm_state = PERSISTENT
    doomed._orm_owner = session
    session._identity[(session._registry.spec_for(Post), (5,))] = doomed
    session.delete(doomed)
    database.connection.calls.clear()
    await session.flush()

    kinds = [sql.split()[0] for sql in database.connection.statements]
    assert kinds == ["BEGIN", "INSERT", "UPDATE", "DELETE", "COMMIT"]


async def test_a_write_error_leaves_object_state_intact(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.fail_on["INSERT"] = RuntimeError("constraint")
    user = User(email="a@b.c", name="A")
    session.add(user)
    with pytest.raises(RuntimeError):
        await session.flush()
    assert user._orm_state != PERSISTENT
    assert user in session._new
    assert "ROLLBACK" in database.connection.statements


async def test_an_object_cannot_belong_to_two_sessions(
    registry: Registry, database: FakeDatabase
) -> None:
    database.connection.script("users", [user_row(1)])
    first = Session(registry, "write")
    user = (await first.fetch(User.select()))[0]
    second = Session(registry, "write")
    with pytest.raises(DetachedInstanceError, match="two sessions"):
        second.add(user)


async def test_deleting_a_transient_object_just_unschedules_it(session: Session) -> None:
    user = User(email="a@b.c", name="A")
    session.add(user)
    session.delete(user)
    assert user not in session._new


async def test_interleaved_models_flush_in_model_then_insertion_order(
    database: FakeDatabase, session: Session
) -> None:
    # Registration order is User, Post, Membership: inserts must group by model
    # in that order, and preserve add() order within each model.
    for index in range(3):
        session.add(Post(id=index, author_id=1, title=f"p{index}"))
        session.add(User(id=index, email=f"{index}@b.c", name=f"u{index}", created_at=None))
    await session.flush()
    inserts = [s for s in database.connection.statements if s.startswith("INSERT")]
    tables = ["users" if '"users"' in item else "posts" for item in inserts]
    assert tables == ["users", "posts"]
    post_args = next(args for sql, args in database.connection.calls if '"posts"' in sql)
    titles = list(post_args[2::3])
    assert titles == ["p0", "p1", "p2"]


async def test_adding_the_same_object_twice_schedules_it_once(session: Session) -> None:
    user = User(email="a@b.c", name="A")
    session.add(user)
    session.add(user)
    assert session._new.count(user) == 1
    assert len(session._new_ids) == 1


async def test_equal_but_distinct_objects_are_scheduled_separately(
    session: Session,
) -> None:
    first = User(id=1, email="a@b.c", name="A")
    second = User(id=1, email="a@b.c", name="A")
    session.add(first)
    session.add(second)
    assert len(session._new) == 2
    assert session._new[0] is first
    assert session._new[1] is second


async def test_pathological_equality_does_not_affect_identity_ownership(
    database: FakeDatabase,
) -> None:
    class Trap(Model, table="traps"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)

        def __eq__(self, other: object) -> bool:
            raise AssertionError("session bookkeeping must not use __eq__")

        def __hash__(self) -> int:
            raise AssertionError("session bookkeeping must not use __hash__")

    session = Session(Registry(database, [Trap], validate_schema="off"), "write")
    first, second = Trap(id=1, name="A"), Trap(id=2, name="B")
    session.add(first)
    session.add(second)
    session.add(first)
    assert len(session._new) == 2
    session.delete(first)
    assert len(session._new) == 1
    assert session._new[0] is second


async def test_deleting_a_transient_object_clears_every_bookkeeping_entry(
    session: Session,
) -> None:
    user = User(email="a@b.c", name="A")
    session.add(user)
    session.delete(user)
    assert session._new == []
    assert session._new_ids == set()
    session.add(user)
    assert session._new == [user]
    assert len(session._new_ids) == 1


async def test_flush_and_close_leave_no_stale_bookkeeping(
    database: FakeDatabase, session: Session
) -> None:
    session.add(User(id=1, email="a@b.c", name="A", created_at=None))
    await session.flush()
    assert session._new == []
    assert session._new_ids == set()
    assert session._deleted == []
    assert session._deleted_ids == set()
    await session.close()
    assert session._new_ids == set()


async def test_a_failed_insert_preserves_pending_bookkeeping(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.fail_on["INSERT"] = RuntimeError("constraint")
    user = User(email="a@b.c", name="A")
    session.add(user)
    with pytest.raises(RuntimeError):
        await session.flush()
    assert session._new == [user]
    assert id(user) in session._new_ids


async def test_scheduling_and_ordering_work_stays_linear(session: Session) -> None:
    from wreath.orm.session import _count_probes

    def probes_for(count: int) -> int:
        local = Session(session._registry, "write")
        users = [User(id=i, email=f"{i}@b.c", name="u") for i in range(count)]
        with _count_probes() as counter:
            for user in users:
                local.add(user)
            local._new_batches()
        return counter[0]

    assert probes_for(500) == 1000
    assert probes_for(1000) == 2000
    assert probes_for(2000) == 4000


async def test_count_runs_one_aggregate_and_returns_the_scalar(
    registry: Registry, database: FakeDatabase, session: Session
) -> None:
    database.connection.script("COUNT(*)", [(7,)])
    total = await session.count(User.select().where(User.name == "A"))
    assert total == 7
    sql, args = database.connection.calls[-1]
    assert sql.startswith("SELECT COUNT(*) FROM ")
    assert "LIMIT" not in sql and "ORDER BY" not in sql
    assert args == ("A",)


async def test_count_of_no_rows_is_zero_not_none(
    registry: Registry, database: FakeDatabase, session: Session
) -> None:
    # fetchval yields None for an empty result; count must normalize to 0.
    total = await session.count(User.select())
    assert total == 0


async def test_count_on_a_closed_session_is_rejected(registry: Registry, session: Session) -> None:
    await session.close()
    with pytest.raises(SessionClosedError):
        await session.count(User.select())


def _persistent_post(session: Session, pk: int = 5) -> Post:
    """A `Post` in the state a fetch would have left it in.

    Built by hand rather than fetched because the refusals under test are about
    an object's *state*, and the fake connection's script would otherwise decide
    how many statements each of these tests runs.
    """
    post = Post._orm_new()
    post._orm_set_loaded(0, pk)
    post._orm_set_loaded(1, 1)
    post._orm_set_loaded(2, "t")
    post._orm_state = PERSISTENT
    post._orm_owner = session
    session._identity[(session._registry.spec_for(Post), (pk,))] = post
    return post


async def test_adding_an_object_already_scheduled_for_deletion_is_refused(
    session: Session,
) -> None:
    doomed = _persistent_post(session)
    session.delete(doomed)
    with pytest.raises(SessionError, match="scheduled for deletion and cannot be added"):
        session.add(doomed)


async def test_deleting_through_a_closed_session_is_rejected(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "write")
    doomed = _persistent_post(session)
    await session.close()
    with pytest.raises(SessionClosedError):
        session.delete(doomed)
    assert session._deleted == []


async def test_adding_through_a_closed_session_is_rejected(
    registry: Registry,
) -> None:
    session = Session(registry, "write")
    await session.close()
    with pytest.raises(SessionClosedError):
        session.add(User(email="a@b.c", name="A"))


async def test_native_fetch_hands_back_a_result_with_no_repeats_untouched(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    rows = [object(), object(), object()]

    class NativeConnection:
        async def _fetch_into(self, sql, args, destination):
            return rows

    monkeypatch.setattr(Session, "_hydrate_plan", lambda *_args: object())

    objects = await session._fetch_objects(
        NativeConnection(), cast(Any, object()), "SELECT distinct identities", ()
    )

    assert objects is rows
