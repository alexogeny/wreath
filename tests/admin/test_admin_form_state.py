"""Controls `wreath mutant` found nobody was watching.

Each test here exists because the sweep removed a clause and the suite stayed
green. None of them was a security control -- those were killed -- but each is a
place the admin quietly does the wrong thing to an operator mid-edit, which is
how a generated admin loses trust.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.admin._doubles import FakeSession, Request, routes
from wreath.admin import Admin
from wreath.crud import Access

pytestmark = pytest.mark.asyncio


def _handlers(model: type, session: FakeSession, **register: Any) -> dict:
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    admin.register(model, **register)
    return routes(admin.router())


@pytest.fixture
def counted_model() -> type:
    """A model with a non-nullable integer, so a bad submission can be refused."""
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Widget(Model, table="admin_widgets"):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text)
        quantity: Mapped[int] = column(Int64)

    return Widget


async def test_a_star_rule_covers_every_operation(account_model: type) -> None:
    """`authorize={"*": ...}` is the whole-admin default, and nothing reached it."""
    from wreath._auth.requirements import requirement_for

    admin = Admin(
        lambda request: FakeSession(),
        authorize={"read": Access.roles("staff"), "*": Access.roles("owner")},
        csrf=lambda request: True,
    )
    admin.register(account_model)

    windows = {
        path: requirement_for(endpoint).role_checks
        for (_method, path), endpoint in routes(admin.router()).items()
    }
    read = windows["/admin/account/{pk}"][0].values
    write = windows["/admin/account/{pk}/delete"][0].values
    assert read == frozenset({"staff"})
    assert write == frozenset({"owner"})


async def test_an_empty_value_on_a_non_nullable_column_stays_empty(
    counted_model: type,
) -> None:
    """Only a *nullable* column turns a cleared control into NULL. On a
    non-nullable one the empty string must reach the ORM, so the refusal comes
    from the column's own type rather than from a NULL nobody asked for."""
    row = counted_model(id=1, name="a", quantity=3)
    session = FakeSession({1: row})
    handlers = _handlers(counted_model, session)

    response = await handlers[("POST", "/admin/widget/{pk}/edit")](
        Request(path_params={"pk": "1"}, form={"name": "", "quantity": "4"})
    )

    assert response.status == 303
    assert row.name == ""


async def test_every_column_type_a_form_can_carry_round_trips() -> None:
    """A form transports strings and the ORM stores typed values, and the ORM
    does not parse -- it refuses `'4'` for an `int8`. Without a conversion step
    every non-text column was uneditable and the admin answered 422 to a
    correctly filled form. One case per type the map claims to handle."""
    import datetime
    import decimal

    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Date, Float64, Int64, Numeric, Text, TimestampTz

    class Reading(Model, table="admin_readings"):
        id: Mapped[int] = column(Int64, primary_key=True)
        label: Mapped[str] = column(Text)
        count: Mapped[int] = column(Int64, nullable=True)
        ratio: Mapped[float] = column(Float64, nullable=True)
        amount: Mapped[decimal.Decimal] = column(Numeric, nullable=True)
        day: Mapped[datetime.date] = column(Date, nullable=True)
        at: Mapped[datetime.datetime] = column(TimestampTz, nullable=True)

    row = Reading(id=1, label="x")
    handlers = _handlers(Reading, FakeSession({1: row}))

    response = await handlers[("POST", "/admin/reading/{pk}/edit")](
        Request(path_params={"pk": "1"}, form={
            "label": "measured",
            "count": "42",
            "ratio": "1.5",
            "amount": "10.25",
            "day": "2026-08-03",
            "at": "2026-08-03T12:00:00+00:00",
        })
    )

    assert response.status == 303, response.body.decode()
    assert row.count == 42
    assert row.ratio == 1.5
    assert row.amount == decimal.Decimal("10.25")   # not a float: numeric is exact
    assert row.day == datetime.date(2026, 8, 3)
    assert row.at.year == 2026


async def test_an_unparseable_number_names_the_field_it_came_from() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Gauge(Model, table="admin_gauges"):
        id: Mapped[int] = column(Int64, primary_key=True)
        label: Mapped[str] = column(Text)
        reading: Mapped[int] = column(Int64, nullable=True)

    handlers = _handlers(Gauge, FakeSession())

    response = await handlers[("POST", "/admin/gauge/new")](
        Request(form={"label": "a", "reading": "twelve"})
    )

    body = response.body.decode()
    assert response.status == 422
    assert "Reading" in body and "not an integer" in body


async def test_a_refused_submission_redraws_what_was_typed(
    counted_model: type,
) -> None:
    """The form repopulates from the submission, not from the stored row -- an
    operator who mistyped one field must not lose the other four."""
    row = counted_model(id=1, name="original", quantity=3)
    session = FakeSession({1: row})
    handlers = _handlers(counted_model, session)

    response = await handlers[("POST", "/admin/widget/{pk}/edit")](
        Request(
            path_params={"pk": "1"},
            form={"name": "edited", "quantity": "not-a-number"},
        )
    )

    body = response.body.decode()
    assert response.status == 422
    assert 'value="edited"' in body            # what they typed, kept
    assert 'value="not-a-number"' in body      # including the bad one
    assert "original" not in body              # not reverted to the stored row


async def test_a_refused_create_redraws_what_was_typed(counted_model: type) -> None:
    session = FakeSession()
    handlers = _handlers(counted_model, session)

    response = await handlers[("POST", "/admin/widget/new")](
        Request(form={"name": "fresh", "quantity": "nope"})
    )

    assert response.status == 422
    assert 'value="fresh"' in response.body.decode()
    assert session.added == []


async def test_a_create_form_starts_empty(counted_model: type) -> None:
    """The other side of the branch above: with no instance and no submission,
    every control is blank rather than carrying the last row's values."""
    handlers = _handlers(counted_model, FakeSession())
    body = (await handlers[("GET", "/admin/widget/new")](Request())).body.decode()
    assert 'value=""' in body


async def test_a_checked_box_is_redrawn_checked(account_model: type) -> None:
    row = account_model(id=1, name="a", email="e", note=None, active=True)
    handlers = _handlers(account_model, FakeSession({1: row}))

    body = (
        await handlers[("GET", "/admin/account/{pk}/edit")](
            Request(path_params={"pk": "1"})
        )
    ).body.decode()

    control = body[body.index('id="account-active"'):]
    assert " checked" in control[: control.index(">")]


async def test_an_unchecked_box_is_redrawn_unchecked(account_model: type) -> None:
    row = account_model(id=1, name="a", email="e", note=None, active=False)
    handlers = _handlers(account_model, FakeSession({1: row}))

    body = (
        await handlers[("GET", "/admin/account/{pk}/edit")](
            Request(path_params={"pk": "1"})
        )
    ).body.decode()

    control = body[body.index('id="account-active"'):]
    assert " checked" not in control[: control.index(">")]


async def test_the_sort_link_toggles_to_descending_when_already_ascending(
    account_model: type,
) -> None:
    row = account_model(id=1, name="a", email="e", note=None, active=True)
    handlers = _handlers(account_model, FakeSession({1: row}))

    ascending = (
        await handlers[("GET", "/admin/account/")](Request(query=b"sort=name"))
    ).body.decode()
    unsorted = (
        await handlers[("GET", "/admin/account/")](Request())
    ).body.decode()

    assert "sort=-name" in ascending      # already ascending -> offer descending
    assert "sort=-name" not in unsorted   # not sorted -> offer ascending
    assert "sort=name" in unsorted


async def test_a_required_hint_names_the_requirement(account_model: type) -> None:
    """The hint is a control's accessible description, so a wrong one is a
    wrong instruction rather than a cosmetic slip."""
    handlers = _handlers(account_model, FakeSession())
    body = (await handlers[("GET", "/admin/account/new")](Request())).body.decode()

    assert 'id="account-name-hint">text, required<' in body
    assert 'id="account-note-hint">text<' in body


async def test_a_multiline_column_gets_a_textarea() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Jsonb, Text

    class Doc(Model, table="admin_docs"):
        id: Mapped[int] = column(Int64, primary_key=True)
        title: Mapped[str] = column(Text)
        payload: Mapped[dict] = column(Jsonb, nullable=True)

    handlers = _handlers(Doc, FakeSession())
    body = (await handlers[("GET", "/admin/doc/new")](Request())).body.decode()

    assert '<textarea id="doc-payload"' in body
    assert '<input type="text" id="doc-title"' in body
