"""Shared models and a scriptable fake driver for ORM tests.

The fake connection speaks the same surface as ``wreath.postgres.Connection``
(execute/fetch/fetchrow/fetchval/close plus the ``_plans`` description cache),
so these tests exercise the real compiler, session, and hydrator without a
database. Behavior that depends on catalog fidelity belongs in the PostgreSQL
integration tests instead.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from wreath.orm import Mapped, Model, column, relationship
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Int64, Text, Timestamp


class User(Model, table="users"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    name: Mapped[str] = column(Text)
    created_at: Mapped[object] = column(Timestamp, nullable=True)
    posts = relationship("Post", foreign_key="author_id", load="raise")


class Post(Model, table="posts"):
    id: Mapped[int] = column(Int64, primary_key=True)
    author_id: Mapped[int] = column(Int64, references=User.id)
    title: Mapped[str] = column(Text)
    author = relationship(User, foreign_key=author_id, load="raise")


class Membership(Model, table="memberships"):
    """A composite primary key, for identity and batching coverage."""

    org_id: Mapped[int] = column(Int64, primary_key=True)
    user_id: Mapped[int] = column(Int64, primary_key=True)
    role: Mapped[str] = column(Text)


class FakePlan:
    __slots__ = ("result_names", "result_oids")

    def __init__(self, names: tuple[str, ...], oids: tuple[int, ...]) -> None:
        self.result_names = names
        self.result_oids = oids


class FakeConnection:
    """Records every statement and replays scripted results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self._plans: dict[str, FakePlan] = {}
        #: Matched against each statement in order; first hit wins.
        self.responses: list[tuple[str, Any]] = []
        self.fail_on: dict[str, Exception] = {}

    def script(self, fragment: str, rows: Any) -> None:
        self.responses.append((fragment, rows))

    def describe(self, sql: str, names: tuple[str, ...], oids: tuple[int, ...]) -> None:
        self._plans[sql] = FakePlan(names, oids)

    def _result(self, sql: str) -> Any:
        for fragment, rows in self.responses:
            if fragment in sql:
                return rows
        return []

    def _record(self, sql: str, args: tuple[Any, ...]) -> None:
        self.calls.append((sql, args))
        for fragment, error in self.fail_on.items():
            if fragment in sql:
                raise error

    async def execute(self, sql: str, *args: Any) -> str:
        self._record(sql, args)
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._record(sql, args)
        return list(self._result(sql))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._record(sql, args)
        rows = self._result(sql)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        return row[0] if row else None

    async def close(self) -> None:
        self.closed = True

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.calls]


class FakePool:
    def __init__(self, workload: str) -> None:
        self.workload = workload


class FakeDatabase:
    """One connection, handed out and returned like a real pool would."""

    def __init__(self, name: str = "main", workloads: tuple[str, ...] = ("read", "write")) -> None:
        self.name = name
        self.connection = FakeConnection()
        self.acquired = 0
        self.released = 0
        self.started = False
        self._workloads = workloads

    def pool(self, workload: str) -> FakePool:
        if workload not in self._workloads:
            raise KeyError(workload)
        return FakePool(workload)

    async def acquire(self, workload: str = "read") -> FakeConnection:
        self.acquired += 1
        return self.connection

    async def release(self, workload: str, connection: FakeConnection) -> None:
        self.released += 1

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def registry(database: FakeDatabase) -> Registry:
    return Registry(database, [User, Post, Membership], validate_schema="off")


@pytest.fixture
def session(registry: Registry) -> Session:
    return Session(registry, "write")


def user_row(
    identifier: int,
    email: str = "a@b.c",
    name: str = "A",
    created: Any = datetime.datetime(2024, 1, 1),
) -> list[Any]:
    """A full users row in declared column order."""
    return [identifier, email, name, created]


def post_row(identifier: int, author_id: int, title: str = "t") -> list[Any]:
    return [identifier, author_id, title]
