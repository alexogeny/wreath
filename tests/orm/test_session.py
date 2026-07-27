"""Sessions: identity, loading, raw SQL, transactions, and writes."""

from __future__ import annotations

import asyncio

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.compiler import MAX_SELECTIN_KEYS
from wreath.orm.errors import (
    DetachedInstanceError,
    MappingError,
    MultipleResultsError,
    SessionClosedError,
    SessionError,
    UnloadedRelationshipError,
)
from wreath.orm.model import DETACHED, PERSISTENT
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text, Timestamp

from .conftest import FakeDatabase, Membership, Post, User, post_row, user_row

pytestmark = pytest.mark.asyncio


# -- lifecycle -----------------------------------------------------------------


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


# -- identity ------------------------------------------------------------------


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


# -- reads ---------------------------------------------------------------------


async def test_get_compiles_a_primary_key_query(
    database: FakeDatabase, session: Session
) -> None:
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


async def test_fetch_one_keeps_a_stricter_limit(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1)])
    await session.fetch_one(User.select().limit(1))
    assert database.connection.calls[0][1] == (1,)


# -- relationship loading ------------------------------------------------------


async def test_selectin_batches_a_collection_without_n_plus_one(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1), user_row(2), user_row(3)])
    database.connection.script(
        "posts", [post_row(10, 1), post_row(11, 1), post_row(12, 3)]
    )
    users = await session.fetch(User.select().include(User.posts.selectin()))
    assert len(database.connection.calls) == 2
    assert [len(item.posts) for item in users] == [2, 0, 1]
    assert users[0].posts[0].id == 10


async def test_selectin_deduplicates_keys(
    database: FakeDatabase, session: Session
) -> None:
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


async def test_load_accepts_a_single_instance(
    database: FakeDatabase, session: Session
) -> None:
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
    users = await session.fetch(
        User.select().include(User.posts.selectin(Post.author.selectin()))
    )
    assert users[0].posts[0].author is users[0]


# -- raw SQL -------------------------------------------------------------------


async def test_raw_returns_driver_records_untouched(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("custom", [["anything"]])
    rows = await session.raw("SELECT 1 AS custom").fetch()
    assert rows == [["anything"]]
    assert database.connection.calls[0] == ("SELECT 1 AS custom", ())


async def test_raw_passes_arguments_through(
    database: FakeDatabase, session: Session
) -> None:
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


async def test_raw_models_rejects_an_extra_column(
    database: FakeDatabase, session: Session
) -> None:
    sql = "SELECT id, email, name, created_at, extra FROM users"
    database.connection.script("users", [[1, "a@b.c", "A", None, "x"]])
    database.connection.describe(
        sql,
        ("id", "email", "name", "created_at", "extra"),
        (Int64.oid, Text.oid, Text.oid, Timestamp.oid, Text.oid),
    )
    with pytest.raises(MappingError, match="unexpected extra"):
        await session.raw(sql).models(User)


async def test_raw_models_rejects_a_type_mismatch(
    database: FakeDatabase, session: Session
) -> None:
    sql = "SELECT id, email, name, created_at FROM users"
    database.connection.script("users", [user_row(1)])
    database.connection.describe(
        sql, ("id", "email", "name", "created_at"), (Text.oid, Text.oid, Text.oid, Timestamp.oid)
    )
    with pytest.raises(MappingError, match="OID"):
        await session.raw(sql).models(User)


async def test_raw_models_rejects_duplicate_names(
    database: FakeDatabase, session: Session
) -> None:
    sql = "SELECT id, id, name, created_at FROM users"
    database.connection.script("users", [[1, 1, "A", None]])
    database.connection.describe(
        sql, ("id", "id", "name", "created_at"), (Int64.oid, Int64.oid, Text.oid, Timestamp.oid)
    )
    with pytest.raises(MappingError, match="more than once"):
        await session.raw(sql).models(User)


# -- transactions --------------------------------------------------------------


async def test_begin_commits_on_clean_exit(
    database: FakeDatabase, session: Session
) -> None:
    async with session.begin():
        await session.raw("SELECT 1").execute()
    assert database.connection.statements == ["BEGIN", "SELECT 1", "COMMIT"]


async def test_begin_rolls_back_on_error(
    database: FakeDatabase, session: Session
) -> None:
    with pytest.raises(RuntimeError):
        async with session.begin():
            raise RuntimeError("boom")
    assert database.connection.statements == ["BEGIN", "ROLLBACK"]


async def test_begin_rolls_back_on_cancellation(
    database: FakeDatabase, session: Session
) -> None:
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


# -- writes --------------------------------------------------------------------


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


async def test_an_inserted_object_enters_the_identity_map(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    user = User(email="a@b.c", name="A")
    session.add(user)
    await session.flush()
    database.connection.script("SELECT", [user_row(7, "a@b.c")])
    assert (await session.get(User, 7)) is user


async def test_update_writes_only_dirty_columns(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    user = (await session.fetch(User.select()))[0]
    user.name = "B"
    database.connection.calls.clear()
    await session.flush()
    sql, args = database.connection.calls[1]
    assert sql == 'UPDATE "public"."users" SET "name" = $1 WHERE "id" = $2'
    assert args == ("B", 1)


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
    database.connection.calls.clear()
    await session.flush()
    sql, args = database.connection.calls[1]
    assert sql == (
        'DELETE FROM "public"."memberships" WHERE "org_id" = $1 AND "user_id" = $2'
    )
    assert args == (1, 1)


async def test_flush_opens_and_commits_its_own_transaction(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    session.add(User(email="a@b.c", name="A"))
    await session.flush()
    assert database.connection.statements[0] == "BEGIN"
    assert database.connection.statements[-1] == "COMMIT"


async def test_flush_inside_a_transaction_does_not_nest_one(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("INSERT", [[7, None]])
    async with session.begin():
        session.add(User(email="a@b.c", name="A"))
        await session.flush()
    assert database.connection.statements.count("BEGIN") == 1
    assert "SAVEPOINT wreath_sp_1" not in database.connection.statements


async def test_flush_orders_inserts_then_updates_then_deletes(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("SELECT", [user_row(1, "a@b.c", "A")])
    existing = (await session.fetch(User.select()))[0]
    existing.name = "changed"
    database.connection.responses.clear()
    database.connection.script("INSERT", [[7, None]])
    database.connection.script("SELECT", [[2, 1, "x"]])
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


# -- unit-of-work bookkeeping --------------------------------------------------


async def test_interleaved_models_flush_in_model_then_insertion_order(
    database: FakeDatabase, session: Session
) -> None:
    # Registration order is User, Post, Membership: inserts must group by model
    # in that order, and preserve add() order within each model.
    for index in range(3):
        session.add(Post(id=index, author_id=1, title=f"p{index}"))
        session.add(
            User(id=index, email=f"{index}@b.c", name=f"u{index}", created_at=None)
        )
    await session.flush()
    inserts = [s for s in database.connection.statements if s.startswith("INSERT")]
    tables = ["users" if '"users"' in item else "posts" for item in inserts]
    assert tables == ["users"] * 3 + ["posts"] * 3
    # Insertion order is preserved within each model.
    titles = [args[2] for sql, args in database.connection.calls if '"posts"' in sql]
    assert titles == ["p0", "p1", "p2"]


async def test_adding_the_same_object_twice_schedules_it_once(session: Session) -> None:
    user = User(email="a@b.c", name="A")
    session.add(user)
    session.add(user)
    assert session._new.count(user) == 1
    assert len(session._new_ordinals) == 1


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
    assert session._new_ordinals == {}
    session.add(user)
    assert session._new == [user]
    assert len(session._new_ordinals) == 1


async def test_flush_and_close_leave_no_stale_bookkeeping(
    database: FakeDatabase, session: Session
) -> None:
    session.add(User(id=1, email="a@b.c", name="A", created_at=None))
    await session.flush()
    assert session._new == []
    assert session._new_ordinals == {}
    assert session._deleted == []
    assert session._deleted_ids == set()
    await session.close()
    assert session._new_ordinals == {}


async def test_a_failed_insert_preserves_pending_bookkeeping(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.fail_on["INSERT"] = RuntimeError("constraint")
    user = User(email="a@b.c", name="A")
    session.add(user)
    with pytest.raises(RuntimeError):
        await session.flush()
    # Retry semantics: the object stays scheduled, with its ordinal intact.
    assert session._new == [user]
    assert id(user) in session._new_ordinals


async def test_scheduling_and_ordering_work_stays_linear(session: Session) -> None:
    from wreath.orm.session import _count_probes

    def probes_for(count: int) -> int:
        local = Session(session._registry, "write")
        users = [User(id=i, email=f"{i}@b.c", name="u") for i in range(count)]
        with _count_probes() as counter:
            for user in users:
                local.add(user)
            sorted(
                local._new,
                key=lambda item: (local._order(type(item)), local._new_ordinals[id(item)]),
            )
        return counter[0]

    # One membership probe per add and one order lookup per sort key: doubling
    # the pending set exactly doubles the work. The old `_new.index()` key and
    # `in self._new` scan made this quadruple.
    assert probes_for(500) == 1000
    assert probes_for(1000) == 2000
    assert probes_for(2000) == 4000


# -- count ---------------------------------------------------------------------


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


async def test_count_on_a_closed_session_is_rejected(
    registry: Registry, session: Session
) -> None:
    await session.close()
    with pytest.raises(SessionClosedError):
        await session.count(User.select())
