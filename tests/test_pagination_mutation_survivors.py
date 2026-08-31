from __future__ import annotations

from typing import Any

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.expressions import BinaryExpr, BooleanExpr
from wreath.orm.types import Int64, Text
from wreath.pagination import (
    CursorParams,
    FilterField,
    FilterSet,
    InvalidPagination,
    Listing,
    PageParams,
    _decode_cursor,
    _encode_cursor,
    apply_sort,
    cursor_params,
    paginate,
    paginate_cursor,
)


class Widget(Model, table="pagination_mutant_widget"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)


class OtherWidget(Model, table="pagination_mutant_other_widget"):
    id: Mapped[int] = column(Int64, primary_key=True)


class QueryRequest:
    def __init__(self, query: bytes) -> None:
        self.query_string = query


class Session:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.queries = []

    async def fetch(self, query):
        self.queries.append(query)
        return self.items[: query.limit_]


def test_filter_set_refuses_a_query_for_another_model() -> None:
    filters = FilterSet(Widget, {"id": ("eq",)})
    with pytest.raises(TypeError, match="cannot apply"):
        filters.apply(OtherWidget.select(), {"id": 1})


def test_filter_set_refuses_an_unknown_public_name() -> None:
    filters = FilterSet(Widget, {"id": ("eq",)})
    with pytest.raises(InvalidPagination, match="filter 'name' is not allowed"):
        filters.apply(Widget.select(), {"name": "a"})


def test_filter_set_converts_a_scalar_to_equality_and_keeps_empty_queries() -> None:
    filters = FilterSet(Widget, {"id": ("eq",)})
    query = Widget.select()
    assert filters.apply(query, {}) is query
    filtered = filters.apply(query, {"id": 7})
    assert [predicate.operator for predicate in filtered.predicates] == ["="]


def test_listing_refuses_a_non_filter_set() -> None:
    with pytest.raises(TypeError, match="must be a FilterSet"):
        Listing(object())


def test_listing_refuses_unknown_allowed_and_default_sort_fields() -> None:
    filters = FilterSet(Widget, {"id": ("eq",)})
    with pytest.raises(ValueError, match="not a column"):
        Listing(filters, sort=("missing",))
    with pytest.raises(ValueError, match="use one of none"):
        Listing(filters, default_sort=("name",))
    with pytest.raises(ValueError, match="use one of id"):
        Listing(filters, sort=("id",), default_sort=("-name",))


def test_listing_accepts_descending_defaults_and_distinguishes_empty_sort() -> None:
    listing = Listing(
        FilterSet(Widget, {"id": ("eq",)}),
        sort=("id", "name"),
        default_sort=("-id",),
    )
    defaulted = listing.apply(Widget.select(), filters=None, sort=None)
    explicit_empty = listing.apply(Widget.select(), filters={}, sort=())
    assert [ordering.direction for ordering in defaulted.orderings] == ["DESC"]
    assert explicit_empty.orderings == ()

    ascending = Listing(
        FilterSet(Widget, {"id": ("eq",)}),
        sort=("id",),
        default_sort=("id",),
    ).apply(Widget.select())
    assert [ordering.direction for ordering in ascending.orderings] == ["ASC"]


def test_filter_set_refuses_a_declaration_for_an_unknown_column() -> None:
    with pytest.raises(ValueError, match="names 'missing'"):
        FilterSet(Widget, {"public": FilterField("missing")})


def test_cursor_params_requires_an_after_assignment_and_uses_the_first_one() -> None:
    params = cursor_params(QueryRequest(b"after&other=value&after=first&after=second"))
    assert params.after == "first"


def test_cursor_params_rejects_long_values_and_normalizes_an_empty_value() -> None:
    with pytest.raises(InvalidPagination, match="longer than 4096"):
        cursor_params(QueryRequest(b"after=" + b"a" * 4097))
    assert cursor_params(QueryRequest(b"after=")).after is None


def test_empty_sort_allowlist_names_none_and_nonempty_lists_names() -> None:
    with pytest.raises(InvalidPagination, match=r"Use one of: \(none\)"):
        apply_sort(Widget.select(), ("name",), allow=())
    with pytest.raises(InvalidPagination, match="Use one of: id"):
        apply_sort(Widget.select(), ("name",), allow=("id",))


@pytest.mark.asyncio
async def test_paginate_applies_sort_only_when_requested() -> None:
    class UntouchedAllowSort:
        def __iter__(self):
            raise AssertionError("allow_sort must stay cold when no sort was requested")

    plain_session = Session([{"id": 1, "name": "a"}])
    await paginate(
        plain_session,
        Widget.select(),
        PageParams(page=1, size=1),
        allow_sort=UntouchedAllowSort(),
        total=1,
    )
    assert plain_session.queries[0].orderings == ()

    sorted_session = Session([{"id": 1, "name": "a"}])
    await paginate(
        sorted_session,
        Widget.select(),
        PageParams(page=1, size=1, sort=("-name",)),
        allow_sort=("name",),
        total=1,
    )
    assert [ordering.direction for ordering in sorted_session.queries[0].orderings] == ["DESC"]


def test_cursor_decode_rejects_the_right_order_with_the_wrong_value_count() -> None:
    token = _encode_cursor(("name", "id"), ("a",))
    with pytest.raises(InvalidPagination, match="invalid pagination cursor"):
        _decode_cursor(token, ("name", "id"))


@pytest.mark.asyncio
async def test_cursor_defaults_to_the_primary_key_in_ascending_order() -> None:
    session = Session([{"id": 1, "name": "a"}])
    page = await paginate_cursor(
        session,
        Widget.select(),
        CursorParams(size=1),
    )
    assert page.next is None
    names = [ordering.expression.column.python_name for ordering in session.queries[0].orderings]
    assert names == ["id"]
    assert [ordering.direction for ordering in session.queries[0].orderings] == ["ASC"]


@pytest.mark.asyncio
async def test_cursor_respects_an_explicitly_narrow_sort_allowlist() -> None:
    with pytest.raises(InvalidPagination, match="allow-list"):
        await paginate_cursor(
            Session([]),
            Widget.select(),
            CursorParams(size=1, sort=("name",)),
            allow_sort=(),
        )


@pytest.mark.asyncio
async def test_cursor_appends_a_primary_key_once_and_inherits_descending_order() -> None:
    session = Session(
        [
            {"id": 2, "name": "b"},
            {"id": 1, "name": "a"},
        ]
    )
    page = await paginate_cursor(
        session,
        Widget.select(),
        CursorParams(size=1, sort=("-name",)),
        allow_sort=("name",),
    )
    assert page.next is not None
    orderings = session.queries[0].orderings
    assert [ordering.expression.column.python_name for ordering in orderings] == ["name", "id"]
    assert [ordering.direction for ordering in orderings] == ["DESC", "DESC"]

    explicit = Session([{"id": 1, "name": "a"}])
    await paginate_cursor(
        explicit,
        Widget.select(),
        CursorParams(size=1, sort=("name", "id")),
        allow_sort=("name", "id"),
    )
    assert len(explicit.queries[0].orderings) == 2


def _predicate_shape(predicate: BinaryExpr | BooleanExpr) -> Any:
    if isinstance(predicate, BinaryExpr):
        return predicate.operator
    return (predicate.operator, tuple(_predicate_shape(item) for item in predicate.operands))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort", "expected_comparison"),
    [(("name",), ">"), (("-name",), "<")],
)
async def test_cursor_builds_the_full_lexicographic_after_predicate(
    sort: tuple[str, ...],
    expected_comparison: str,
) -> None:
    first_session = Session(
        [
            {"id": 1, "name": "a"},
            {"id": 2, "name": "b"},
        ]
    )
    first = await paginate_cursor(
        first_session,
        Widget.select(),
        CursorParams(size=1, sort=sort),
        allow_sort=("name",),
    )
    second_session = Session([])
    await paginate_cursor(
        second_session,
        Widget.select(),
        CursorParams(after=first.next, size=1, sort=sort),
        allow_sort=("name",),
    )
    (predicate,) = second_session.queries[0].predicates
    assert _predicate_shape(predicate) == (
        "OR",
        (
            expected_comparison,
            ("AND", ("=", expected_comparison)),
        ),
    )


class ThreeColumnWidget(Model, table="pagination_mutant_three_column_widget"):
    id: Mapped[int] = column(Int64, primary_key=True)
    group: Mapped[str] = column(Text)
    name: Mapped[str] = column(Text)


@pytest.mark.asyncio
async def test_cursor_carries_every_prior_equality_into_the_third_comparison() -> None:
    first_session = Session(
        [
            {"id": 1, "group": "a", "name": "b"},
            {"id": 2, "group": "c", "name": "d"},
        ]
    )
    first = await paginate_cursor(
        first_session,
        ThreeColumnWidget.select(),
        CursorParams(size=1, sort=("group", "name")),
        allow_sort=("group", "name"),
    )
    second_session = Session([])
    await paginate_cursor(
        second_session,
        ThreeColumnWidget.select(),
        CursorParams(after=first.next, size=1, sort=("group", "name")),
        allow_sort=("group", "name"),
    )
    (predicate,) = second_session.queries[0].predicates
    assert _predicate_shape(predicate) == (
        "OR",
        (
            ("OR", (">", ("AND", ("=", ">")))),
            ("AND", (("AND", ("=", "=")), ">")),
        ),
    )


class WideWidget(Model, table="pagination_mutant_wide_widget"):
    id: Mapped[int] = column(Int64, primary_key=True)
    one: Mapped[int] = column(Int64)
    two: Mapped[int] = column(Int64)
    three: Mapped[int] = column(Int64)
    four: Mapped[int] = column(Int64)
    five: Mapped[int] = column(Int64)
    six: Mapped[int] = column(Int64)
    seven: Mapped[int] = column(Int64)
    eight: Mapped[int] = column(Int64)


@pytest.mark.asyncio
async def test_cursor_refuses_more_than_eight_normalized_columns() -> None:
    sort = ("one", "two", "three", "four", "five", "six", "seven", "eight")
    with pytest.raises(InvalidPagination, match="at most 8 order columns"):
        await paginate_cursor(
            Session([]),
            WideWidget.select(),
            CursorParams(size=1, sort=sort),
            allow_sort=sort,
        )


@pytest.mark.asyncio
async def test_cursor_does_not_issue_a_next_token_for_an_empty_page() -> None:
    page = await paginate_cursor(
        Session([]),
        Widget.select(),
        CursorParams(size=0),
    )
    assert page.items == []
    assert page.next is None

    truncated = await paginate_cursor(
        Session([{"id": 1, "name": "a"}]),
        Widget.select(),
        CursorParams(size=0),
    )
    assert truncated.items == []
    assert truncated.next is None
