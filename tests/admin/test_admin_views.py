"""The four views, and the bounds they inherit from `wreath.pagination`."""

from __future__ import annotations

from typing import Any

import pytest

from tests.admin._doubles import FakeSession, Request, routes
from wreath.admin import Admin
from wreath.crud import Access
from wreath.pagination import MAX_SIZE

pytestmark = pytest.mark.asyncio


class SortingSession(FakeSession):
    """Records the query it was handed, so the sort allow-list is observable."""

    def __init__(self, rows: dict[Any, Any] | None = None) -> None:
        super().__init__(rows)
        self.queries: list[Any] = []

    async def fetch(self, query: Any) -> list[Any]:
        self.queries.append(query)
        return list(self.rows.values())


def _handlers(model: type, session: FakeSession, **register: Any) -> dict:
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    admin.register(model, **register)
    return routes(admin.router())


def _rows(model: type, count: int) -> dict:
    return {
        n: model(id=n, name=f"Person {n}", email=f"p{n}@x.test", note=None, active=True)
        for n in range(1, count + 1)
    }


async def test_the_list_view_shows_the_registered_columns(account_model: type) -> None:
    session = FakeSession(_rows(account_model, 3))
    handlers = _handlers(account_model, session, list_columns=("name", "email"))

    body = (await handlers[("GET", "/admin/account/")](Request())).body.decode()

    assert "Person 1" in body and "p1@x.test" in body
    assert ">Note<" not in body           # not a registered list column


async def test_an_empty_list_says_so_rather_than_drawing_an_empty_table(
    account_model: type,
) -> None:
    handlers = _handlers(account_model, FakeSession())
    body = (await handlers[("GET", "/admin/account/")](Request())).body.decode()
    assert "No rows to show." in body
    assert "<table>" not in body


async def test_the_detail_view_renders_every_shown_column(account_model: type) -> None:
    session = FakeSession(_rows(account_model, 1))
    handlers = _handlers(account_model, session)

    body = (
        await handlers[("GET", "/admin/account/{pk}")](Request(path_params={"pk": "1"}))
    ).body.decode()

    assert "Person 1" in body and "p1@x.test" in body
    assert "password_hash" not in body


async def test_a_missing_row_is_a_404_page(account_model: type) -> None:
    handlers = _handlers(account_model, FakeSession())
    response = await handlers[("GET", "/admin/account/{pk}")](
        Request(path_params={"pk": "9"})
    )
    assert response.status == 404
    assert "not found" in response.body.decode()


async def test_a_non_numeric_key_on_an_integer_column_is_a_404_not_a_500(
    account_model: type,
) -> None:
    handlers = _handlers(account_model, FakeSession(_rows(account_model, 1)))
    response = await handlers[("GET", "/admin/account/{pk}")](
        Request(path_params={"pk": "not-a-number"})
    )
    assert response.status == 404


async def test_the_page_size_is_bounded_by_pagination_s_ceiling(
    account_model: type,
) -> None:
    """`?size=` is request-controlled, so the admin inherits `MAX_SIZE` rather
    than deciding a second bound that could drift from it."""
    session = SortingSession(_rows(account_model, 3))
    handlers = _handlers(account_model, session)

    await handlers[("GET", "/admin/account/")](Request(query=b"size=99999"))

    assert session.queries[-1].limit_ == MAX_SIZE


async def test_an_unsortable_column_in_the_query_string_is_ignored(
    account_model: type,
) -> None:
    """The allow-list is what stops `?sort=` becoming a scan a caller can ask
    for; an unknown token is dropped rather than reaching `apply_sort`."""
    session = SortingSession(_rows(account_model, 2))
    handlers = _handlers(account_model, session)

    await handlers[("GET", "/admin/account/")](Request(query=b"sort=not_a_column"))

    assert session.queries[-1].orderings == ()


async def test_a_sortable_column_reaches_the_query(account_model: type) -> None:
    session = SortingSession(_rows(account_model, 2))
    handlers = _handlers(account_model, session)

    await handlers[("GET", "/admin/account/")](Request(query=b"sort=-name"))

    assert session.queries[-1].orderings != ()


async def test_an_excluded_column_is_not_sortable(account_model: type) -> None:
    session = SortingSession(_rows(account_model, 2))
    handlers = _handlers(account_model, session, exclude=("email",))

    await handlers[("GET", "/admin/account/")](Request(query=b"sort=email"))

    assert session.queries[-1].orderings == ()


async def test_the_create_form_omits_the_primary_key(account_model: type) -> None:
    handlers = _handlers(account_model, FakeSession())
    body = (await handlers[("GET", "/admin/account/new")](Request())).body.decode()

    assert 'name="name"' in body
    assert 'name="id"' not in body
    assert 'name="password_hash"' not in body


async def test_a_nullable_column_is_not_marked_required(account_model: type) -> None:
    handlers = _handlers(account_model, FakeSession())
    body = (await handlers[("GET", "/admin/account/new")](Request())).body.decode()

    note = body[body.index('id="account-note"'):]
    assert " required" not in note[: note.index(">")]
    name = body[body.index('id="account-name"'):]
    assert " required" in name[: name.index(">")]


async def test_a_boolean_column_becomes_a_checkbox(account_model: type) -> None:
    handlers = _handlers(account_model, FakeSession())
    body = (await handlers[("GET", "/admin/account/new")](Request())).body.decode()
    assert 'type="checkbox" id="account-active"' in body


async def test_an_unchecked_checkbox_is_false_rather_than_absent(
    account_model: type,
) -> None:
    """A checkbox a browser omits means `false`, and storing nothing would leave
    the previous `true` standing -- the one form control that fails silently."""
    row = account_model(id=1, name="A", email="e", note=None, active=True)
    session = FakeSession({1: row})
    handlers = _handlers(account_model, session)

    await handlers[("POST", "/admin/account/{pk}/edit")](
        Request(path_params={"pk": "1"}, form={"name": "A", "email": "e"})
    )

    assert row.active is False


async def test_clearing_a_nullable_text_field_stores_null_not_empty(
    account_model: type,
) -> None:
    row = account_model(id=1, name="A", email="e", note="something", active=False)
    session = FakeSession({1: row})
    handlers = _handlers(account_model, session)

    await handlers[("POST", "/admin/account/{pk}/edit")](
        Request(path_params={"pk": "1"}, form={"name": "A", "email": "e", "note": ""})
    )

    assert row.note is None


async def test_the_delete_confirmation_is_a_page_before_the_post(
    account_model: type,
) -> None:
    session = FakeSession(_rows(account_model, 1))
    handlers = _handlers(account_model, session)

    response = await handlers[("GET", "/admin/account/{pk}/delete")](
        Request(path_params={"pk": "1"})
    )

    assert response.status == 200
    assert session.deleted == []
    assert "permanently deletes" in response.body.decode()


async def test_the_overview_lists_every_registration(account_model: type) -> None:
    admin = Admin(
        lambda request: FakeSession(),
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    admin.register(account_model, label="People", slug="people")
    handlers = routes(admin.router())

    body = (await handlers[("GET", "/admin")](Request())).body.decode()

    assert "People" in body and "/admin/people/" in body
    assert "admin_accounts" in body
