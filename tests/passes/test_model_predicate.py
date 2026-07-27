"""``where=`` given a model predicate, which is the branch `ty` caught and pytest did not.

``_render_filter`` has three arms. ``Sql`` and ``str`` both return before the
lazy ``from .orm.compiler import ...`` at the bottom of the function, so a green
suite that only ever exercised those two said nothing at all about the third --
and when the compiler's seam was renamed underneath it, the import broke and
every test still passed. A type checker found it; a test should have.

So this drives a real model predicate all the way through to the SQL, which
means the import is exercised, the names are the ones the compiler exports, and
a future rename fails here rather than in production.
"""

from __future__ import annotations

import datetime
import re

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text, Timestamp
from wreath.passes import (
    ChunkedPass,
    DutyCycle,
    Purge,
    Rows,
    Sealed,
    Sql,
    Table,
)

from .fakes import FakeDatabase, World

NOW = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)


class Replay(Model, table="replays"):
    """A model over the same table the other pass tests walk by hand."""

    key: Mapped[str] = column(Text, primary_key=True)
    expires: Mapped[object] = column(Timestamp, index=True)
    tries: Mapped[int] = column(Int64)


async def _nap(_seconds):
    return None


@pytest.fixture
def world():
    return World(
        "replays",
        [
            {
                "key": f"k{index:03d}",
                "expires": NOW - datetime.timedelta(hours=index + 1),
                "tries": index % 3,
            }
            for index in range(9)
        ],
    )


@pytest.fixture
def database(world):
    return FakeDatabase(world)


def model_pass(**overrides):
    options = {
        "over": Replay,
        "units": Rows(key=(Replay.expires, Replay.key), limit=3, within="2s"),
        "frontier": Sealed(),
        "work": Purge(where=Replay.tries == 0),
        "pace": DutyCycle(1.0),
    }
    options.update(overrides)
    return ChunkedPass("purge_replays", **options)


async def test_a_model_predicate_reaches_the_sql_through_the_compiler(database, world):
    walk = model_pass()

    await walk.run(database, sleep=_nap)

    deletes = world.sql_of("DELETE FROM")
    assert deletes
    # The predicate was rendered by the ORM's own compiler, not re-implemented
    # here: same `render_predicate`, same placeholder numbering, spliced onto
    # the end of the keyset range rather than replacing it.
    # Qualified and quoted, because that is what `render_predicate` emits --
    # the point being that it was the compiler that emitted it.
    assert any('"tries" =' in sql for sql in deletes)
    assert all("(expires, key) <=" in sql for sql in deletes)


async def test_a_model_predicate_actually_filters_the_rows(database, world):
    walk = model_pass()

    await walk.run(database, sleep=_nap)

    # Only `tries = 0` rows were purged; the rest survive despite being inside
    # every chunk's range.
    assert sorted(row["tries"] for row in world.rows) == [1, 1, 1, 2, 2, 2]


async def test_the_model_predicates_binds_continue_the_keysets_numbering(
    database, world
):
    walk = model_pass()

    await walk.run(database, sleep=_nap)

    delete = world.sql_of("DELETE FROM")[0]
    # A fragment that restarted at $1 would silently bind the cursor's value to
    # the filter. The fake evaluates real binds, so a clash would change which
    # rows died -- but assert the shape too, because that is the invariant.
    placeholders = sorted({int(number) for number in re.findall(r"\$(\d+)", delete)})
    assert placeholders == list(range(1, len(placeholders) + 1))
    # ...and the model predicate's bind is the last one, not the first.
    assert placeholders[-1] == max(placeholders)
    assert delete.index("$4") > delete.index("(expires, key)")


async def test_a_model_predicate_over_a_bare_table_is_refused_with_the_fix(
    database, world
):
    # A `Table` carries no model, so there is nothing to resolve the columns
    # against. The refusal has to name the way out rather than just say no.
    walk = ChunkedPass(
        "purge_replays",
        over=Table("replays"),
        units=Rows(key=(Replay.expires, Replay.key), limit=3, within="2s"),
        frontier=Sealed(),
        work=Purge(where=Replay.tries == 0),
        pace=DutyCycle(1.0),
    )

    result = await walk.run(database, sleep=_nap)

    assert result.stopped == "blocked"
    assert "over=<Model>" in result.error
    assert "Sql(" in result.error


async def test_a_sql_fragment_still_works_beside_the_model_form(database, world):
    walk = model_pass(work=Purge(where=Sql("tries = ?", [0])))

    await walk.run(database, sleep=_nap)

    assert sorted(row["tries"] for row in world.rows) == [1, 1, 1, 2, 2, 2]
