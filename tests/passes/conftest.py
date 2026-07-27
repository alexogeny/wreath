"""Shared fixtures for the chunked-pass tests."""

from __future__ import annotations

import datetime

import pytest

from wreath.jobs import JobRunner

from .fakes import FakeDatabase, World

NOW = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def jobs_runner():
    """A runner with the default thirty-second lease, for the shift refusal."""
    return JobRunner(FakeDatabase(World("replays", [])), name="work")


def expired_rows(count: int, *, live: int = 0) -> list[dict]:
    """*count* rows already past the clock, plus *live* rows that are not."""
    rows = [
        {"key": f"k{index:03d}", "expires": NOW - datetime.timedelta(seconds=count - index)}
        for index in range(count)
    ]
    rows += [
        {"key": f"live{index}", "expires": NOW + datetime.timedelta(seconds=600)}
        for index in range(live)
    ]
    return rows


@pytest.fixture
def world():
    """Ten expired rows and three that are not yet due."""
    return World("replays", expired_rows(10, live=3))


@pytest.fixture
def database(world):
    return FakeDatabase(world)
