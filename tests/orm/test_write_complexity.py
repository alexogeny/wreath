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
    return User(
        id=identifier,
        email=f"u{identifier}@e.x",
        name=f"n{identifier}",
        created_at=_CREATED,
    )


def _partial_user(identifier: int) -> User:
    return User(id=identifier, email=f"u{identifier}@e.x", name=f"n{identifier}")


async def _flush(session: Session) -> None:
    async with session.begin():
        await session.flush()


@pytest.mark.parametrize("count", [1, 2, 8, 64])
async def test_complete_insert_compiles_and_submits_once_per_shape(
    session: Session, database: FakeDatabase, count: int
) -> None:
    for index in range(count):
        session.add(_user(index))

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 1
    assert sum("INSERT INTO" in sql for sql in database.connection.statements) == 1


async def test_insert_compiles_once_per_distinct_column_set(
    session: Session, database: FakeDatabase
) -> None:
    database.connection.script("RETURNING", [[_CREATED] for _ in range(8)])
    for index in range(8):
        session.add(_user(index))
    for index in range(100, 108):
        session.add(_partial_user(index))

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 2


async def test_insert_compiles_once_per_model(session: Session, database: FakeDatabase) -> None:
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
    session = Session(registry, "write")
    users = []
    for index in range(16):
        user = _user(index)
        session.add(user)
        users.append(user)
    await _flush(session)

    for user in users:
        user.name = "renamed"
    database.connection.script("UPDATE", [[user.id] for user in users])

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
    database.connection.script('SET "email"', [[user.id] for user in users[::2]])
    database.connection.script('SET "name"', [[user.id] for user in users[1::2]])

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 2


async def test_delete_compiles_once_per_model(registry: Registry, database: FakeDatabase) -> None:
    session = Session(registry, "write")
    users = []
    for index in range(16):
        user = _user(index)
        session.add(user)
        users.append(user)
    await _flush(session)

    for user in users:
        session.delete(user)
    database.connection.script("DELETE", [[user.id] for user in users])

    with _count_write_sql_builds() as builds:
        await _flush(session)

    assert builds[0] == 1


async def test_compiles_do_not_grow_with_row_count() -> None:
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
    instance = _user(identifier)
    instance._orm_state = PERSISTENT
    instance._orm_owner = session
    instance._orm_clear_dirty()
    spec = session._registry.spec_for(User)
    session._identity[(spec, (identifier,))] = instance
    return instance


def _count_change_checks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    original = User._orm_has_changes

    def counting(self: User) -> bool:
        calls[0] += 1
        return original(self)

    monkeypatch.setattr(User, "_orm_has_changes", counting)
    return calls


async def test_first_changed_field_registers_the_dirty_object_once(registry: Registry) -> None:
    session = Session(registry, "write")
    instance = _loaded_user(session, 1)
    instance.name = "changed"
    instance.email = "changed@example.com"
    assert session._dirty_items == [instance]


@pytest.mark.parametrize("size", [16, 64, 256])
async def test_one_flush_checks_only_the_dirty_identity(
    registry: Registry,
    database: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
) -> None:
    session = Session(registry, "write")
    for index in range(size):
        _loaded_user(session, index)
    session._identity[(registry.spec_for(User), (size - 1,))].name = "changed"
    database.connection.script("UPDATE", [[size - 1]])

    calls = _count_change_checks(monkeypatch)
    await _flush(session)

    assert calls[0] == 2


async def test_a_clean_session_still_flushes_nothing(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "write")
    for index in range(8):
        _loaded_user(session, index)

    await _flush(session)

    assert not [sql for sql in database.connection.statements if "UPDATE" in sql]


async def test_a_write_is_still_found_when_it_is_the_last_object(
    registry: Registry, database: FakeDatabase
) -> None:
    session = Session(registry, "write")
    for index in range(8):
        _loaded_user(session, index)
    session._identity[(registry.spec_for(User), (7,))].name = "changed"
    database.connection.script("UPDATE", [[7]])

    await _flush(session)

    assert sum("UPDATE" in sql for sql in database.connection.statements) == 1
