"""The write path compiles per shape, not per instance.

The read path has compiled once per query *shape* since the compiler existed:
`compile_select` keys a `_CachedPlan` by `shape_key` and the registry holds it.
The write path had no equivalent, so flushing N rows of one model rebuilt the
same INSERT text N times -- the column filter, the two `", ".join(...)`
generator expressions, and the f-string assembly, once per row.

These are deterministic probe counts, not timings: the assertion is that the
number of statements *compiled* is a function of the distinct write shapes in
the flush, and that doubling the instance count does not change it.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.orm.model import PERSISTENT
from wreath.orm.registry import Registry
from wreath.orm.session import Session, _count_write_sql_builds

from .conftest import FakeDatabase, Membership, Post, User

pytestmark = pytest.mark.asyncio

_CREATED = datetime.datetime(2024, 1, 1)


def _user(identifier: int) -> User:
    """Every column loaded, so the INSERT needs no RETURNING."""
    return User(
        id=identifier,
        email=f"u{identifier}@e.x",
        name=f"n{identifier}",
        created_at=_CREATED,
    )


def _partial_user(identifier: int) -> User:
    """`created_at` left unloaded, so it moves into RETURNING -- a second shape."""
    return User(id=identifier, email=f"u{identifier}@e.x", name=f"n{identifier}")


async def _flush(session: Session) -> None:
    async with session.begin():
        await session.flush()


@pytest.mark.parametrize("count", [1, 2, 8, 64])
async def test_insert_compiles_once_per_shape_not_per_row(
    session: Session, database: FakeDatabase, count: int
) -> None:
    """N rows of one model, all with the same loaded columns, is one compile."""
    for index in range(count):
        session.add(_user(index))

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 1
    # The statements still go out one per row: this is about compilation, not
    # about batching them into a multi-row INSERT (a round-trip question).
    assert sum("INSERT INTO" in sql for sql in database.connection.statements) == count


async def test_insert_compiles_once_per_distinct_column_set(
    session: Session, database: FakeDatabase
) -> None:
    """Two shapes of the same model are two compiles, however many rows each."""
    # Only the partial shape emits RETURNING, so this script cannot be hit by
    # the full one.
    database.connection.script("RETURNING", [[_CREATED]])
    for index in range(8):
        session.add(_user(index))
    for index in range(100, 108):
        session.add(_partial_user(index))

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 2


async def test_insert_compiles_once_per_model(
    session: Session, database: FakeDatabase
) -> None:
    for index in range(8):
        session.add(_user(index))
        session.add(Post(id=index, author_id=index, title="t"))
        session.add(Membership(org_id=index, user_id=index, role="r"))

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 3


async def test_update_compiles_once_per_dirty_column_set(
    registry: Registry, database: FakeDatabase
) -> None:
    """The dirty set selects the statement; equal sets share one compile."""
    session = Session(registry, "write")
    users = []
    for index in range(16):
        user = _user(index)
        session.add(user)
        users.append(user)
    await _flush(session)

    for user in users:
        user.name = "renamed"

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 1


async def test_update_distinct_dirty_sets_compile_separately(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "write")
    users = []
    for index in range(16):
        user = _user(index)
        session.add(user)
        users.append(user)
    await _flush(session)

    for position, user in enumerate(users):
        if position % 2:
            user.name = "renamed"
        else:
            user.email = f"changed{position}@e.x"

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 2


async def test_delete_compiles_once_per_model(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "write")
    users = []
    for index in range(16):
        user = _user(index)
        session.add(user)
        users.append(user)
    await _flush(session)

    for user in users:
        session.delete(user)

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 1


async def test_compiles_do_not_grow_with_row_count() -> None:
    """The doubling assertion, stated directly rather than via a ratio.

    A fresh registry per size, so this measures only "compiles do not scale with
    rows" and not the separate fact that the cache outlives a session.
    """
    counts = []
    for size in (32, 64, 128):
        registry = Registry(FakeDatabase(), [User, Post, Membership], validate_schema="off")
        session = Session(registry, "write")
        for index in range(size):
            session.add(_user(index))
        with _count_write_sql_builds() as builds:
            await _flush(session)
        counts.append(builds[0])

    assert counts == [1, 1, 1]


async def test_plan_cache_outlives_the_session(registry: Registry) -> None:
    """A second session on the same registry compiles nothing new."""
    first = Session(registry, "write")
    for index in range(4):
        first.add(_user(index))
    await _flush(first)

    second = Session(registry, "write")
    for index in range(100, 104):
        second.add(_user(index))
    with _count_write_sql_builds() as builds:
        await _flush(second)

    assert builds[0] == 0


def _loaded_user(session: Session, identifier: int) -> User:
    """A clean, persistent user in the session's identity map.

    The shape a big read leaves behind: loaded from the database, so it is
    `PERSISTENT` with no pending changes, and findable by identity.
    """
    instance = _user(identifier)
    instance._orm_state = PERSISTENT
    instance._orm_clear_dirty()
    spec = session._registry.spec_for(User)
    session._identity[(spec, (identifier,))] = instance
    return instance


def _count_change_checks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count `_orm_has_changes` calls, the per-object cost of finding the dirty."""
    calls = [0]
    original = User._orm_has_changes

    def counting(self: User) -> bool:
        calls[0] += 1
        return original(self)

    monkeypatch.setattr(User, "_orm_has_changes", counting)
    return calls


@pytest.mark.parametrize("size", [16, 64, 256])
async def test_one_flush_scans_the_identity_map_at_most_twice(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """Finding the one changed row must not cost three passes over the map.

    A request that loaded `size` rows and changed one used to pay `3 * size`
    change checks per flush -- `_has_pending`, `_collect_written`, and the
    update ordering each walked the whole identity map. The ceiling here is two:
    one short-circuiting existence check, and one scan that builds the list.
    """
    session = Session(registry, "write")
    for index in range(size):
        _loaded_user(session, index)
    # The last one, so the existence check cannot short-circuit early and this
    # measures the worst case rather than a lucky ordering.
    session._identity[(registry.spec_for(User), (size - 1,))].name = "changed"

    calls = _count_change_checks(monkeypatch)
    await _flush(session)

    assert calls[0] <= 2 * size


async def test_a_clean_session_still_flushes_nothing(
    registry: Registry, database: FakeDatabase
) -> None:
    """The short-circuiting existence check must not lose the early return."""
    session = Session(registry, "write")
    for index in range(8):
        _loaded_user(session, index)

    await _flush(session)

    assert not [sql for sql in database.connection.statements if "UPDATE" in sql]


async def test_a_write_is_still_found_when_it_is_the_last_object(
    registry: Registry, database: FakeDatabase
) -> None:
    """Short-circuiting on the *first* dirty object must not miss a later one."""
    session = Session(registry, "write")
    for index in range(8):
        _loaded_user(session, index)
    session._identity[(registry.spec_for(User), (7,))].name = "changed"

    await _flush(session)

    assert sum("UPDATE" in sql for sql in database.connection.statements) == 1
