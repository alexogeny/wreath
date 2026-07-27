"""Pagination: page math + query-param parsing (pure) and query shaping (ORM)."""

from __future__ import annotations

import pytest

from wreath.pagination import (
    DEFAULT_SIZE,
    Page,
    PageParams,
    page_params,
    parse_sort,
)


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
    """Out of range clamps; unparseable falls back. Neither raises.

    A hand-edited URL should degrade to a page that exists, not 422 -- and
    `MAX_PAGE` is a real ceiling because `LIMIT/OFFSET` walks and discards every
    row before the offset.
    """
    from wreath.pagination import MAX_PAGE, MAX_SIZE

    clamped = page_params(_Q(f"page={MAX_PAGE + 1}&size={MAX_SIZE + 1}"))
    assert clamped.page == MAX_PAGE and clamped.size == MAX_SIZE
    assert page_params(_Q("page=0")).page == 1
    junk = page_params(_Q("page=abc&size="))
    assert junk.page == 1 and junk.size == DEFAULT_SIZE


# --- ORM query shaping (needs the built package for the model layout) --------

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
