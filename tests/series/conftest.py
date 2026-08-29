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


class Deploy(Model, table="deploys"):
    """A second, unrelated model — the annotation layer's source.

    Deliberately not joined to ``Trek``: markers come from somewhere else in the
    application, which is exactly why the range and the zone have to be shared
    by construction rather than by both queries being written carefully.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    version: Mapped[str] = column(Text)
    environment: Mapped[str] = column(Text)
    happened_at: Mapped[object] = column(TimestampTz)


class Sighting(Model, table="sightings"):
    """Something observed at a place and a time.

    Carries a place *and* a clock, because the spatial axis is only interesting
    where it composes with the temporal one — a heatmap of everything ever is a
    much easier query than "sightings per week per 10 km cell".
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    species: Mapped[str] = column(Text)
    lat: Mapped[float] = column(Float64)
    lon: Mapped[float] = column(Float64)
    weight_kg: Mapped[float] = column(Float64, nullable=True)
    seen_at: Mapped[object] = column(TimestampTz)


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def registry(database: FakeDatabase) -> Registry:
    return Registry(database, [Trek, Herd, Paddock, Deploy, Sighting], validate_schema="off")


@pytest.fixture
def session(registry: Registry) -> Session:
    return Session(registry, "read")


def utc(year: int, month: int, day: int, hour: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.UTC)


def last_statement(database: FakeDatabase) -> tuple[str, tuple[Any, ...]]:
    return database.connection.calls[-1]
