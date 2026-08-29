from __future__ import annotations

import pytest

from wreath.pagination import (
    DEFAULT_SIZE,
    CursorParams,
    Page,
    PageParams,
    page_params,
    parse_sort,
)


def test_numeric_rank_workspace_returns_only_the_requested_indices() -> None:
    from wreath.pagination import _rank_indices

    scores = (0.5, 2.0, 2.0, -1.0, 1.0)
    assert _rank_indices(scores, page=1, size=3, descending=False) == (3, 0, 4)
    # Reversing a stable ascending order puts the later equal-score item first,
    # matching sorted(...).reverse(), which this kernel replaces.
    assert _rank_indices(scores, page=1, size=3, descending=True) == (2, 1, 4)
    assert _rank_indices(scores, page=2, size=3, descending=True) == (0, 3)


def test_numeric_rank_can_bound_and_transform_the_owned_source() -> None:
    from wreath.pagination import _rank_indices

    assert _rank_indices(
        (-9.0, 2.0, -4.0, 100.0),
        page=1,
        size=2,
        descending=True,
        candidates=3,
        absolute=True,
    ) == (0, 2)


def test_numeric_rank_partial_workspace_matches_python_sort() -> None:
    from wreath.pagination import _rank_indices

    for count in (0, 1, 7, 48, 129):
        scores = tuple(((index * 37) % 19 - 9) / 3 for index in range(count))
        for descending in (False, True):
            ordered = sorted(range(count), key=lambda index: scores[index])
            if descending:
                # The established contract is ascending order reversed, so
                # equal scores reverse their index order too.
                ordered.reverse()
            for page, size in ((1, 1), (1, 12), (2, 12), (5, 7), (20, 12)):
                start = (page - 1) * size
                assert _rank_indices(scores, page=page, size=size, descending=descending) == tuple(
                    ordered[start : start + size]
                )


def test_numeric_rank_empty_page_still_validates_every_score() -> None:
    from wreath.pagination import _rank_indices

    with pytest.raises(TypeError, match="rank score 1 must be int or float"):
        _rank_indices((1.0, object()), page=20, size=12, descending=False)
    with pytest.raises(ValueError, match="rank score 1 must be finite"):
        _rank_indices((1.0, float("nan")), page=1, size=0, descending=False)


def test_page_math():
    page = Page(items=[1, 2, 3], total=25, page=2, size=10)
    assert page.pages == 3
    assert page.has_prev and page.has_next
    first = Page(items=[], total=0, page=1, size=10)
    assert first.pages == 0 and not first.has_next and not first.has_prev
    exact = Page(items=[], total=20, page=2, size=10)
    assert exact.pages == 2 and not exact.has_next


def test_page_as_dict():
    d = Page(items=[{"a": 1}], total=1, page=1, size=DEFAULT_SIZE).as_dict()
    assert d["total"] == 1 and d["pages"] == 1 and d["items"] == [{"a": 1}]


def test_parse_sort():
    assert parse_sort("") == ()
    assert parse_sort("name,-created_at, id ") == ("name", "-created_at", "id")


class _Q:
    """The one member `page_params` reads. It is a `Depends`, so it takes the
    request: a dependency's own parameters are never bound from the request, and
    the previous signature (`page_params(page, size, sort)` carrying `Query()`
    markers) received the request object *as* the page number."""

    def __init__(self, query: str = "") -> None:
        self.query_string = query.encode()


def test_page_params_defaults():
    assert page_params(_Q()) == PageParams(page=1, size=DEFAULT_SIZE, sort=())
    bound = page_params(_Q("page=3&size=5&sort=name,-id"))
    assert bound.page == 3 and bound.size == 5 and bound.sort == ("name", "-id")


def test_page_params_clamps_and_falls_back():
    from wreath.pagination import MAX_PAGE, MAX_SIZE

    clamped = page_params(_Q(f"page={MAX_PAGE + 1}&size={MAX_SIZE + 1}"))
    assert clamped.page == MAX_PAGE and clamped.size == MAX_SIZE
    assert page_params(_Q("page=0")).page == 1
    junk = page_params(_Q("page=abc&size="))
    assert junk.page == 1 and junk.size == DEFAULT_SIZE


def test_cursor_params_keeps_the_opaque_position_and_native_bounds():
    from wreath.pagination import cursor_params

    assert cursor_params(_Q("after=abc-_&size=9999&sort=-name")) == CursorParams(
        after="abc-_", size=100, sort=("-name",)
    )


def _model():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Widget(Model, table="widget_pag_test"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)

    return Widget


def test_apply_sort_allowlist_and_order():
    from wreath.pagination import apply_sort

    Widget = _model()
    q = apply_sort(Widget.select(), ("-name",), allow=("name",))
    assert len(q.orderings) == 1
    with pytest.raises(ValueError):
        apply_sort(Widget.select(), ("secret",), allow=("name",))


def _retrieval_model():
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text, TsVector, Vector

    class Doc(Model, table="doc_pag_test"):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text)
        embedding: Mapped[list] = column(Vector(3))
        search: Mapped[bytes] = column(TsVector("english", sources=("title",)), index="gin")

    return Doc


def test_the_default_allowlist_holds_no_retrieval_column():
    from wreath.pagination import apply_filters, apply_sort, sortable_fields

    Doc = _retrieval_model()
    assert sortable_fields(Doc) == ("id", "title")
    for name in ("embedding", "search", "-embedding"):
        with pytest.raises(ValueError, match="allow-list"):
            apply_sort(Doc.select(), (name,))
    with pytest.raises(ValueError, match="allow-list"):
        apply_filters(Doc.select(), {"embedding": [1.0, 0.0, 0.0]})
    # Ordinary columns are unaffected, and an explicit allow-list still wins:
    # this is the default, not a prohibition.
    assert len(apply_sort(Doc.select(), ("-title",)).orderings) == 1
    assert len(apply_sort(Doc.select(), ("embedding",), allow=("embedding",)).orderings) == 1


class _FakeSession:
    def __init__(self, items, total):
        self._items = items
        self._total = total
        self.calls = []

    async def fetch(self, query):
        self.calls.append((query.limit_, query.offset_, len(query.projection)))
        # a projection of 1 == the count query; else the page query
        return list(range(self._total)) if len(query.projection) == 1 else self._items


@pytest.mark.asyncio
async def test_paginate_shapes_and_counts():
    from wreath.pagination import paginate

    Widget = _model()
    session = _FakeSession(items=[{"id": 1}], total=42)
    page = await paginate(session, Widget.select(), PageParams(page=2, size=10))
    assert page.total == 42 and page.page == 2 and page.size == 10 and page.pages == 5
    # last fetch was the page query: LIMIT 10 OFFSET 10
    assert (10, 10) == session.calls[-1][:2]


class _CountingSession(_FakeSession):
    """A session that exposes an efficient ``count`` (like the real one)."""

    def __init__(self, items, total):
        super().__init__(items, total)
        self.count_queries = []

    async def count(self, query):
        self.count_queries.append(query)
        return self._total


@pytest.mark.asyncio
async def test_paginate_prefers_the_efficient_count_when_available():
    from wreath.pagination import paginate

    Widget = _model()
    session = _CountingSession(items=[{"id": 1}], total=42)
    page = await paginate(session, Widget.select(), PageParams(page=1, size=10))
    assert page.total == 42
    # The total came from session.count, not from materializing PK rows: every
    # fetch call was the page query (projection wider than the lone PK).
    assert len(session.count_queries) == 1
    assert all(projection != 1 for _, _, projection in session.calls)


class _CursorSession:
    def __init__(self, items):
        self.items = items
        self.queries = []

    async def fetch(self, query):
        self.queries.append(query)
        return self.items[: query.limit_]


@pytest.mark.asyncio
async def test_cursor_pagination_adds_a_primary_key_tie_breaker_and_round_trips():
    from wreath.pagination import paginate_cursor

    Widget = _model()
    first_session = _CursorSession(
        [
            {"id": 1, "name": "same"},
            {"id": 2, "name": "same"},
            {"id": 3, "name": "later"},
        ]
    )
    first = await paginate_cursor(
        first_session,
        Widget.select(),
        CursorParams(size=2, sort=("name",)),
        allow_sort=("name",),
    )
    assert first.has_next and first.next
    assert [
        ordering.expression.column.python_name for ordering in first_session.queries[0].orderings
    ] == [
        "name",
        "id",
    ]

    second_session = _CursorSession([{"id": 3, "name": "later"}])
    second = await paginate_cursor(
        second_session,
        Widget.select(),
        CursorParams(after=first.next, size=2, sort=("name",)),
        allow_sort=("name",),
    )
    assert not second.has_next
    assert len(second_session.queries[0].predicates) == 1


@pytest.mark.asyncio
async def test_a_cursor_is_bound_to_its_sort_order():
    from wreath.pagination import paginate_cursor

    Widget = _model()
    session = _CursorSession([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    first = await paginate_cursor(session, Widget.select(), CursorParams(size=1, sort=("name",)))
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        await paginate_cursor(
            _CursorSession([]),
            Widget.select(),
            CursorParams(after=first.next, size=1, sort=("-name",)),
        )


def test_the_ceilings_are_the_documented_numbers() -> None:
    from wreath.pagination import MAX_PAGE, MAX_SIZE

    assert (MAX_SIZE, MAX_PAGE) == (100, 10_000)
    assert page_params(_Q("page=999999&size=999999")) == PageParams(
        page=10_000,
        size=100,
        sort=(),
    )


def test_parse_sort_drops_blanks_rather_than_producing_them() -> None:
    assert parse_sort("name,") == ("name",)
    assert parse_sort(",name") == ("name",)
    assert parse_sort("name,,id") == ("name", "id")
    assert parse_sort(", ,\t,") == ()
    assert parse_sort("  ") == ()


def test_a_blank_page_or_size_falls_back_rather_than_clamping_to_one() -> None:
    assert page_params(_Q("page=&size=")) == PageParams(
        page=1,
        size=DEFAULT_SIZE,
        sort=(),
    )
    assert page_params(_Q("page=%20&size=%20")).size == DEFAULT_SIZE
    # And `sort=` absent or blank is no sort at all, not a one-element tuple.
    assert page_params(_Q("sort=")).sort == ()
    assert page_params(_Q("")).sort == ()


def test_a_leading_minus_sorts_descending_and_its_absence_ascending() -> None:
    from wreath.pagination import apply_sort

    Widget = _model()
    ascending = apply_sort(Widget.select(), ("name",), allow=("name",)).orderings
    descending = apply_sort(Widget.select(), ("-name",), allow=("name",)).orderings
    assert [o.direction for o in ascending] == ["ASC"]
    assert [o.direction for o in descending] == ["DESC"]

    mixed = apply_sort(Widget.select(), ("name", "-id"), allow=("name", "id")).orderings
    assert [o.direction for o in mixed] == ["ASC", "DESC"]


def test_sorting_by_nothing_returns_the_query_unchanged() -> None:
    from wreath.pagination import apply_sort

    Widget = _model()
    query = Widget.select()
    assert apply_sort(query, (), allow=("name",)) is query
    assert apply_sort(query, ()).orderings == ()


def test_filtering_by_nothing_returns_the_query_unchanged() -> None:
    from wreath.pagination import apply_filters

    Widget = _model()
    query = Widget.select()
    assert apply_filters(query, {}) is query

    filtered = apply_filters(query, {"name": "a"}, allow=("name",))
    assert filtered is not query
    assert len(filtered.predicates) == 1
    with pytest.raises(ValueError):
        apply_filters(query, {"secret": "x"}, allow=("name",))


def test_filters_default_to_the_same_allow_list_as_sorting() -> None:
    from wreath.pagination import apply_filters

    Widget = _model()
    assert len(apply_filters(Widget.select(), {"name": "a"}).predicates) == 1


def test_filter_set_compiles_rich_operators_and_listing_sort_once() -> None:
    from wreath.pagination import FilterField, FilterSet, Listing

    Widget = _model()
    filters = FilterSet(
        Widget,
        {
            "identifier": FilterField("id", ("gte", "lt", "in")),
            "name": ("eq", "ilike"),
        },
        max_terms=3,
    )
    listing = Listing(filters, sort=("id", "name"), default_sort=("-id",))
    query = listing.apply(
        Widget.select(),
        filters={"identifier": {"gte": 2, "lt": 10}, "name": {"ilike": "A%"}},
    )
    assert [predicate.operator for predicate in query.predicates] == [">=", "<", "ILIKE"]
    assert [ordering.direction for ordering in query.orderings] == ["DESC"]


def test_filter_set_refuses_undeclared_operators_and_unbounded_terms() -> None:
    from wreath.pagination import FilterSet, InvalidPagination

    Widget = _model()
    filters = FilterSet(Widget, {"id": ("eq", "gte")}, max_terms=1)
    with pytest.raises(InvalidPagination, match="does not allow"):
        filters.apply(Widget.select(), {"id": {"lt": 3}})
    with pytest.raises(InvalidPagination, match="max_terms"):
        filters.apply(Widget.select(), {"id": {"eq": 3, "gte": 1}})


def test_filter_set_refuses_text_operators_on_non_text_columns_at_declaration() -> None:
    from wreath.pagination import FilterSet

    Widget = _model()
    with pytest.raises(ValueError, match="Text or Varchar"):
        FilterSet(Widget, {"id": ("ilike",)})


def test_a_filter_allow_list_narrows_below_the_default() -> None:
    from wreath.pagination import apply_filters, sortable_fields

    Widget = _model()
    assert "id" in sortable_fields(Widget)  # the default would allow it
    with pytest.raises(ValueError):
        apply_filters(Widget.select(), {"id": 1}, allow=("name",))
    assert len(apply_filters(Widget.select(), {"id": 1}).predicates) == 1


@pytest.mark.asyncio
async def test_paginate_applies_the_sort_and_honours_allow_sort() -> None:
    from wreath.pagination import paginate

    Widget = _model()
    session = _FakeSession(items=[{"id": 1}], total=1)

    sorted_page = await paginate(
        session,
        Widget.select(),
        PageParams(page=1, size=10, sort=("-name",)),
        allow_sort=("name",),
        total=1,
    )
    assert sorted_page.total == 1

    # A column the model has, that `allow_sort` withholds, is refused rather
    # than quietly sorted by -- which is what forwarding `allow=` is for.
    with pytest.raises(ValueError):
        await paginate(
            session,
            Widget.select(),
            PageParams(page=1, size=10, sort=("id",)),
            allow_sort=("name",),
            total=1,
        )

    # And with no sort the query is untouched, so the branch is pinned both ways.
    plain = await paginate(
        session,
        Widget.select(),
        PageParams(page=1, size=10),
        total=1,
    )
    assert plain.total == 1
