"""Models and a fake driver for the calculated-view tests.

The fake connection is the ORM suite's, so these exercise the real compiler,
the real predicate machinery, and the real envelope assembly without a
database. What only a live PostgreSQL can settle -- that Python's bucket
arithmetic agrees with ``date_trunc``, and that the spine steps a calendar day
across a DST change -- lives in ``tests/postgres/test_series_integration.py``.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from tests.orm.conftest import FakeDatabase
from wreath.orm import Mapped, Model, column, relationship
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.orm.types import Float64, Int64, Text, TimestampTz


class Paddock(Model, table="paddocks"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)


class Herd(Model, table="herds"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    started_at: Mapped[object] = column(TimestampTz)
    #: A to-many relation, so the refusal to group through one has something
    #: real to refuse.
    treks = relationship("Trek", foreign_key="herd_id", load="raise")


class Trek(Model, table="treks"):
    id: Mapped[int] = column(Int64, primary_key=True)
    herd_id: Mapped[int] = column(Int64, references=Herd.id)
    paddock_id: Mapped[int] = column(Int64, nullable=True)
    distance_km: Mapped[float] = column(Float64)
    grade: Mapped[str] = column(Text)
    started_at: Mapped[object] = column(TimestampTz)
    herd = relationship(Herd, foreign_key=herd_id, load="raise")


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def registry(database: FakeDatabase) -> Registry:
    return Registry(database, [Trek, Herd, Paddock], validate_schema="off")


@pytest.fixture
def session(registry: Registry) -> Session:
    return Session(registry, "read")


def utc(year: int, month: int, day: int, hour: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.UTC)


def last_statement(database: FakeDatabase) -> tuple[str, tuple[Any, ...]]:
    return database.connection.calls[-1]
