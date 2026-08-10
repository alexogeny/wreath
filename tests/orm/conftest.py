"""Shared models and a scriptable fake driver for ORM tests.

The fake connection speaks the same surface as `wreath.postgres.Connection`
(execute/fetch/fetchrow/fetchval/close plus the `_plans` description cache),
so these tests exercise the real compiler, session, and hydrator without a
database. Behavior that depends on catalog fidelity belongs in the PostgreSQL
integration tests instead.

**The fake refuses what the driver refuses and returns what the driver
returns.** Both come from the driver itself rather than from a restatement
here -- `check_statement` is the shipped refusal path and `ScriptedRecord` is
the row surface, so neither can drift from what a real connection does. This
matters more than it looks: thirteen introspection tests once passed against a
fake scripted with `str` and `int` rows, modelling a driver with catalog codecs
that does not exist, and `validate_schema="error"` -- the framework default --
had never once completed lifespan startup against a real PostgreSQL. See
the never-more-capable rule for doubles in `AGENTS.md`.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any

import pytest

# `tests/` is not a package, so the shared helpers are reached by path. Same
# mechanism the other suites use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _pgfidelity import (
    PreparedStatements,
    check_statement,
    driver_row_value,
    record,
)

from wreath._replay_adapters import ScriptedRecord
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


def _row(scripted: Any) -> Any:
    """A scripted row in the driver's own `Record` surface.

    A mapping keeps its column names; a positional sequence has none to keep,
    so it is wrapped with empty names -- positional access works, named access
    raises, and that is honest about what the fake was told. Anything already
    `Record`-shaped, or a scalar (`fetchval` results, counts), passes through.
    """
    if isinstance(scripted, ScriptedRecord):
        return scripted
    if isinstance(scripted, dict):
        return record(scripted)
    if isinstance(scripted, (list, tuple)):
        return ScriptedRecord((), tuple(scripted))
    return scripted


class FakePlan:
    __slots__ = ("checked", "result_names", "result_oids")

    def __init__(
        self, names: tuple[str, ...], oids: tuple[int, ...], checked: bool = True
    ) -> None:
        self.result_names = names
        self.result_oids = oids
        #: False when the plan deliberately disagrees with the scripted rows --
        #: the handful of tests whose subject *is* a plan/model mismatch. An
        #: opt-out that has to be written is a decision; a silent exemption is
        #: the hole this guard exists to close.
        self.checked = checked


class FakeConnection:
    """Records every statement and replays scripted results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self._plans: dict[str, FakePlan] = {}
        #: Matched against each statement in order; first hit wins.
        self.responses: list[tuple[str, Any]] = []
        self.fail_on: dict[str, Exception] = {}
        self.command_tags: list[tuple[str, str]] = []
        #: The prepared-statement cache, so a cast on a placeholder is fine on
        #: the first execution and fatal on the second, exactly as it is against
        #: a server. Without it the fake cannot reproduce the whole class.
        self._prepared = PreparedStatements()

    def script(self, fragment: str, rows: Any) -> None:
        """Script rows, shaped the way `fetch` actually hands them back.

        A positional list is wrapped rather than passed through: the driver
        yields `Record`s, and a bare `list` accepts `.append`, `.index` and
        slicing that a real row does not. Wrapping keeps positional access --
        which is all the hydrator uses -- and removes the surface that lets a
        test lean on something production never gets.
        """
        self.responses.append((fragment, [_row(r) for r in rows]))

    def script_command(self, fragment: str, tag: str) -> None:
        """Return one real PostgreSQL-shaped command tag for matching SQL."""
        self.command_tags.append((fragment, tag))

    def describe(
        self,
        sql: str,
        names: tuple[str, ...],
        oids: tuple[int, ...],
        *,
        checked: bool = True,
    ) -> None:
        """Declare the result shape. `checked=False` only when the plan is the lie.

        Pass `checked=False` when the test's subject is a plan that disagrees
        with the model or the rows -- proving `MappingError` fires, for
        instance. Everywhere else the declared OIDs are enforced against the
        scripted values, so a fake cannot claim a type the driver would not
        return.
        """
        self._plans[sql] = FakePlan(names, oids, checked)

    def _check_against_plan(self, sql: str, rows: list[Any]) -> None:
        """Refuse a scripted value the driver could not have produced.

        A `describe()` call declares the result OIDs, and from that moment the
        fake knows exactly what a real connection would hand back for each
        column: `driver_row_value` runs the driver's own `_decode_value`, so the
        answer comes from the shipped codec table rather than from a wish.

        Checked here rather than in `script()` because a test may script before
        it describes, and a rule that depends on call order is a rule that gets
        worked around. Untyped rows -- no `describe()` for this statement --
        stay positional-only and unchecked, which is honest: the fake was never
        told what they are.

        This is the guard that was missing when thirteen introspection tests
        scripted `str` for a `name` column and `validate_schema="error"`, the
        framework default, had never once worked against a real PostgreSQL.
        """
        plan = next((p for stmt, p in self._plans.items() if stmt in sql), None)
        if plan is None or not plan.checked or not plan.result_oids:
            return
        for index, row in enumerate(rows):
            if not isinstance(row, ScriptedRecord):
                continue
            for position, oid in enumerate(plan.result_oids):
                if position >= len(row):
                    break
                scripted = row[position]
                expected = driver_row_value(oid, scripted)
                if type(scripted) is not type(expected):
                    column_name = (
                        plan.result_names[position]
                        if position < len(plan.result_names)
                        else f"column {position}"
                    )
                    raise AssertionError(
                        f"row {index} scripts {scripted!r} ({type(scripted).__name__}) "
                        f"for {column_name!r}, but oid {oid} decodes to "
                        f"{expected!r} ({type(expected).__name__}). Script what the "
                        f"driver returns, not what the test wishes it returned -- "
                        f"a double is never more capable than the real thing"
                    )

    def _result(self, sql: str) -> Any:
        for fragment, rows in self.responses:
            if fragment in sql:
                self._check_against_plan(sql, rows)
                return rows
        return []

    def _record(self, sql: str, args: tuple[Any, ...]) -> None:
        # Derived from the driver, not restated: one unbindable argument, one
        # statement per command, and the second execution of a poisoned cast.
        check_statement(sql, args, self._prepared)
        self.calls.append((sql, args))
        for fragment, error in self.fail_on.items():
            if fragment in sql:
                raise error

    async def execute(self, sql: str, *args: Any) -> str:
        self._record(sql, args)
        for fragment, tag in self.command_tags:
            if fragment in sql:
                return tag
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
