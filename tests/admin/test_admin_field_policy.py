from __future__ import annotations

from typing import Any

import pytest

from tests.admin._doubles import Authorizer, FakeSession, Request, routes
from wreath.admin import WITHHELD_MARKER, Admin, FieldAccess
from wreath.crud import Access

pytestmark = pytest.mark.asyncio

_SECRET = "ada@secret.example"


class Capture:
    """A template double that records the context it was handed."""

    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []

    def render_bytes(self, context: dict[str, Any]) -> bytes:
        self.contexts.append(context)
        return (
            b'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            b"<title>captured</title></head><body><main><h1>captured</h1>"
            b"</main></body></html>"
        )

    def values(self) -> list[str]:
        """Every string anywhere in every captured context."""
        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, str):
                found.append(node)
            elif isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    walk(value)

        for context in self.contexts:
            walk(context)
        return found


def _admin(model: type, session: FakeSession, **register: Any) -> Any:
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    admin.register(model, **register)
    return routes(admin.router())


def _account(model: type) -> Any:
    return model(id=1, name="Ada", email=_SECRET, note="hi", active=True)


async def test_a_withheld_field_never_reaches_the_detail_context(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = Capture()
    monkeypatch.setattr("wreath._admin.registry.DETAIL_TEMPLATE", capture)
    session = FakeSession({1: _account(account_model)})
    handlers = _admin(
        account_model,
        session,
        field_access={"email": FieldAccess(read="read_contact")},
    )

    request = Request(path_params={"pk": "1"}, authorizer=Authorizer())  # allows nothing
    response = await handlers[("GET", "/admin/account/{pk}")](request)

    assert response.status == 200
    assert _SECRET not in capture.values()
    assert WITHHELD_MARKER in capture.values()


async def test_a_permitted_field_does_reach_the_detail_context(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = Capture()
    monkeypatch.setattr("wreath._admin.registry.DETAIL_TEMPLATE", capture)
    session = FakeSession({1: _account(account_model)})
    handlers = _admin(
        account_model,
        session,
        field_access={"email": FieldAccess(read="read_contact")},
    )

    request = Request(path_params={"pk": "1"}, authorizer=Authorizer("read_contact"))
    await handlers[("GET", "/admin/account/{pk}")](request)

    assert _SECRET in capture.values()


async def test_a_withheld_field_never_reaches_the_list_context(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = Capture()
    monkeypatch.setattr("wreath._admin.registry.LIST_TEMPLATE", capture)
    session = FakeSession({1: _account(account_model)})
    handlers = _admin(
        account_model,
        session,
        field_access={"email": FieldAccess(read="read_contact")},
    )

    await handlers[("GET", "/admin/account/")](Request(authorizer=Authorizer()))

    assert _SECRET not in capture.values()


async def test_an_unreadable_field_is_absent_from_the_edit_form(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = Capture()
    monkeypatch.setattr("wreath._admin.registry.FORM_TEMPLATE", capture)
    session = FakeSession({1: _account(account_model)})
    handlers = _admin(
        account_model,
        session,
        field_access={"email": FieldAccess(read="read_contact", write="edit_contact")},
    )

    request = Request(path_params={"pk": "1"}, authorizer=Authorizer("edit_contact"))
    await handlers[("GET", "/admin/account/{pk}/edit")](request)

    names = {field["name"] for field in capture.contexts[0]["fields"]}
    assert "email" not in names
    assert _SECRET not in capture.values()
    assert "name" in names  # ungated columns are unaffected


async def test_a_readable_but_unwritable_field_is_shown_and_not_editable(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail, form = Capture(), Capture()
    monkeypatch.setattr("wreath._admin.registry.DETAIL_TEMPLATE", detail)
    monkeypatch.setattr("wreath._admin.registry.FORM_TEMPLATE", form)
    session = FakeSession({1: _account(account_model)})
    handlers = _admin(
        account_model,
        session,
        field_access={"email": FieldAccess(write="edit_contact")},
    )
    authorizer = Authorizer()

    await handlers[("GET", "/admin/account/{pk}")](
        Request(path_params={"pk": "1"}, authorizer=authorizer)
    )
    await handlers[("GET", "/admin/account/{pk}/edit")](
        Request(path_params={"pk": "1"}, authorizer=authorizer)
    )

    assert _SECRET in detail.values()
    assert "email" not in {field["name"] for field in form.contexts[0]["fields"]}


async def test_an_unwritable_field_submitted_anyway_is_dropped(account_model: type) -> None:
    account = _account(account_model)
    session = FakeSession({1: account})
    handlers = _admin(
        account_model,
        session,
        field_access={"email": FieldAccess(write="edit_contact")},
    )

    request = Request(
        path_params={"pk": "1"},
        form={"name": "Grace", "email": "attacker@example.com"},
        authorizer=Authorizer(),
    )
    response = await handlers[("POST", "/admin/account/{pk}/edit")](request)

    assert response.status == 303
    assert account.name == "Grace"
    assert account.email == _SECRET


async def test_no_authorizer_withholds_every_declared_field(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = Capture()
    monkeypatch.setattr("wreath._admin.registry.DETAIL_TEMPLATE", capture)
    session = FakeSession({1: _account(account_model)})
    handlers = _admin(
        account_model,
        session,
        field_access={"email": FieldAccess(read="read_contact")},
    )

    request = Request(path_params={"pk": "1"}, authorizer=None)
    await handlers[("GET", "/admin/account/{pk}")](request)

    assert _SECRET not in capture.values()
    assert "Ada" in capture.values()  # ungated columns still render


async def test_the_readable_and_writable_caches_do_not_share_a_slot(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wreath._admin.fields import resolve_readable, resolve_writable

    session = FakeSession({1: _account(account_model)})
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    entry = admin.register(
        account_model,
        field_access={"email": FieldAccess(read="read_contact")},
    )
    request = Request(authorizer=Authorizer("read_contact"))

    writable = await resolve_writable(
        request,
        Authorizer("read_contact"),
        entry.field_access,
        entry.editable,
        entry.resource,
        id(entry),
    )
    readable_authorizer = Authorizer("read_contact")
    readable = await resolve_readable(
        request,
        readable_authorizer,
        entry.field_access,
        entry.columns,
        entry.resource,
        id(entry),
    )
    cached = await resolve_readable(
        request,
        readable_authorizer,
        entry.field_access,
        entry.columns,
        entry.resource,
        id(entry),
    )

    # `id` is shown and not editable, so the two sets must differ. Sharing a
    # slot would make the second call return the first's answer.
    assert "id" in readable
    assert cached is readable
    assert readable_authorizer.calls == ["read_contact"]
    assert "id" not in writable
    assert readable >= writable


async def test_one_authorization_call_per_action_per_request(account_model: type) -> None:
    rows = {
        n: account_model(id=n, name=f"n{n}", email=f"e{n}", note=None, active=True)
        for n in range(1, 11)
    }
    session = FakeSession(rows)
    authorizer = Authorizer("read_contact")
    handlers = _admin(
        account_model,
        session,
        field_access={
            "email": FieldAccess(read="read_contact"),
            "note": FieldAccess(read="read_contact"),
        },
    )

    await handlers[("GET", "/admin/account/")](Request(authorizer=authorizer))

    assert authorizer.calls == ["read_contact"]
