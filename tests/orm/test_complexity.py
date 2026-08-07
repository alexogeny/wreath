"""Deterministic regressions for the Python-complexity audit (QPY-*)."""

from __future__ import annotations

import pytest

from wreath.orm.introspection import _normalize_default
from wreath.orm.registry import Registry
from wreath.orm.session import Session, _count_key_map_builds

from .conftest import FakeDatabase, Membership, Post, User, post_row, user_row


class _TrapRelationship:
    @property
    def name(self) -> str:
        raise AssertionError("relationship lookup scanned the relationship tuple")


def test_relationship_lookup_uses_compiled_name_index(registry: Registry) -> None:
    spec = registry.spec_for(User)
    expected = spec.relationships[0]
    object.__setattr__(spec, "relationships", (_TrapRelationship(), *spec.relationships))

    assert spec.relationship(expected.name) is expected


def _old_normalize(value: str) -> str:
    # The previous O(N^2) implementation, kept as the parity oracle.
    text = " ".join(value.split()).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text.lower()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "nextval('s'::regclass)",
        "(0)",
        "((0))",
        "( ( 0 ) )",
        "(())",
        "(x",
        "x)",
        "((0)",  # unbalanced
        "(0))",  # unbalanced
        "  (  'a b'  )  ",
        "(NULL)",
        "CURRENT_TIMESTAMP",
    ],
)
def test_normalize_default_matches_previous_semantics(value: str) -> None:
    assert _normalize_default(value) == _old_normalize(value)


def test_normalize_default_deep_nesting_matches_and_is_bounded() -> None:
    for n in (1, 2, 64, 4096):
        text = "(" * n + "0" + ")" * n
        assert _normalize_default(text) == "0" == _old_normalize(text)


@pytest.mark.performance
def test_normalize_default_scaling_is_linear() -> None:
    import statistics
    import time

    def median_ns(n: int) -> float:
        text = "(" * n + "0" + ")" * n
        samples = []
        for _ in range(15):
            start = time.perf_counter_ns()
            _normalize_default(text)
            samples.append(time.perf_counter_ns() - start)
        return statistics.median(samples)

    median_ns(1000)  # warm up
    ratio = median_ns(16384) / median_ns(8192)
    assert ratio < 2.6, ratio


# -- hydration: key offsets are a function of shape, not of row count ---------


def _fresh_registry(database: FakeDatabase) -> Registry:
    """A registry whose shape cache has never been filled.

    The plans below are cached per *shape*, so a second query of the same shape
    resolves nothing at all. Measuring "does this scale with rows" therefore
    needs a cold cache per row count, or every size after the first reads zero
    and the test passes without ever exercising the mapping.
    """
    return Registry(database, [User, Post, Membership], validate_schema="off")


@pytest.mark.asyncio
async def test_hydration_resolves_key_offsets_once_per_query(
    database: FakeDatabase,
) -> None:
    """Doubling the rows must not double the key-mapping work.

    `_hydrate` used to rebuild a `{python_name: index}` dict for every row,
    which is O(rows x columns) of pure repetition on a mapping fixed by the
    compiled projection. One resolution per query was the contract; it is now
    one per shape, and the test below pins that separately.
    """

    async def builds_for(rows: int) -> int:
        local = Session(_fresh_registry(database), "read")
        database.connection.responses.clear()
        database.connection.script("users", [user_row(i) for i in range(1, rows + 1)])
        with _count_key_map_builds() as counter:
            fetched = await local.fetch(User.select())
        assert len(fetched) == rows
        return counter[0]

    assert await builds_for(50) == 1
    assert await builds_for(100) == 1
    assert await builds_for(200) == 1


@pytest.mark.asyncio
async def test_hydration_resolves_key_offsets_once_per_shape(
    database: FakeDatabase,
) -> None:
    """A second query of the same shape must resolve nothing.

    Rebuilding per query is not free, and on the commonest read there is -- a
    `fetch_one`, one row -- it cost more than the per-row work it saves:
    measured 0.85-0.90x against not hoisting at all below five rows, against
    1.37-1.42x above fifty. A shape's plan cannot change between queries, so it
    is built once rather than tuned to a row count.
    """
    registry = _fresh_registry(database)

    async def builds() -> int:
        local = Session(registry, "read")
        database.connection.responses.clear()
        database.connection.script("users", [user_row(1), user_row(2)])
        with _count_key_map_builds() as counter:
            assert len(await local.fetch(User.select())) == 2
        return counter[0]

    assert await builds() == 1, "the first query of a shape resolves it"
    assert await builds() == 0, "every later query reuses the cached plan"
    assert await builds() == 0


@pytest.mark.asyncio
async def test_joined_hydration_resolves_each_step_once_per_query(
    database: FakeDatabase,
) -> None:
    """A joined shape always takes this path, and pays per step per row."""

    async def builds_for(rows: int) -> int:
        local = Session(_fresh_registry(database), "read")
        database.connection.responses.clear()
        database.connection.script(
            "LEFT JOIN",
            [[10 + i, i, "t", *user_row(i, f"{i}@b.c")] for i in range(1, rows + 1)],
        )
        with _count_key_map_builds() as counter:
            posts = await local.fetch(Post.select().include(Post.author.joined()))
        assert len(posts) == rows
        return counter[0]

    # One for the root projection, one for the single joined step -- at every
    # row count. Before, this was 2 * rows.
    assert await builds_for(50) == 2
    assert await builds_for(200) == 2


@pytest.mark.asyncio
async def test_hydration_reads_column_metadata_once_per_query(
    database: FakeDatabase, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the key-offset hoist above, on the same mapping.

    `_pk_offsets` was lifted out of the per-row loop; the cell loop beside it
    kept reading `item.index` -- a `@property` whose whole body is
    `return self.column.index` -- twice per column per row, plus a
    `PgType.from_wire` call that usually returns its argument unchanged. Both
    are settled by the compiled projection, so both are O(rows x columns) of
    Python frames for values that cannot change between rows.

    Measured on 10,000 real rows: the Record path ran at 387,983 rows/s before
    this was hoisted and 530,053 after, against the native hydrator's 1,943,738.
    """
    from wreath.orm.schema import ColumnSpec

    reads = [0]
    original = ColumnSpec.index

    class _Counting:
        def __get__(self, obj: object, owner: type | None = None) -> object:
            if obj is None:
                return self
            reads[0] += 1
            return original.__get__(obj, owner)

    monkeypatch.setattr(ColumnSpec, "index", _Counting())

    async def reads_for(rows: int) -> int:
        local = Session(session._registry, "read")
        database.connection.responses.clear()
        database.connection.script("users", [user_row(i) for i in range(1, rows + 1)])
        reads[0] = 0
        fetched = await local.fetch(User.select())
        assert len(fetched) == rows
        return reads[0]

    at_50 = await reads_for(50)
    at_200 = await reads_for(200)

    assert at_50 == at_200, (
        f"column metadata is re-read per row: {at_50} reads for 50 rows, "
        f"{at_200} for 200. It is fixed by the compiled projection."
    )


@pytest.mark.asyncio
async def test_selectin_hydration_resolves_child_offsets_once(
    database: FakeDatabase, session: Session
) -> None:
    database.connection.script("users", [user_row(1), user_row(2)])
    database.connection.script(
        "posts", [post_row(10, 1), post_row(11, 1), post_row(12, 2)]
    )
    with _count_key_map_builds() as counter:
        users = await session.fetch(User.select().include(User.posts.selectin()))
    assert len(users[0].posts) == 2
    # One for the parent projection, one for the child projection -- not one
    # per child row, and not one per batch.
    assert counter[0] == 2


@pytest.mark.asyncio
async def test_an_empty_result_resolves_nothing_and_stays_out_of_the_way(
    database: FakeDatabase, session: Session
) -> None:
    """No rows means no resolution, and no new failure mode.

    The primary-key check lives inside the offset resolution, so hoisting it
    above the row loop unconditionally would let a projection missing its key
    raise MappingError for a query that simply matched nothing. Keeping the
    resolution behind the emptiness guard preserves the old behaviour exactly.
    """
    database.connection.script("users", [])
    with _count_key_map_builds() as counter:
        assert await session.fetch(User.select()) == []
    assert counter[0] == 0
