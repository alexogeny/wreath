from __future__ import annotations

from typing import Any

import pytest

from tests.admin._doubles import FakeSession, Request, routes
from wreath._admin import registry
from wreath.admin import Admin, AdminError
from wreath.crud import Access


def _admin(model: type, session: FakeSession, **registration: Any) -> Admin:
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    admin.register(model, **registration)
    return admin


def test_generated_columns_exposed_for_reading_stay_uneditable() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text, TsVector

    class Article(Model, table="admin_articles"):
        id: Mapped[int] = column(Int64, primary_key=True)
        body: Mapped[str] = column(Text)
        search: Mapped[bytes] = column(TsVector("english", sources=("body",)))

    entry = _admin(Article, FakeSession(), expose=("search",)).models[0]

    assert "search" in entry.columns
    assert "search" not in entry.editable


def test_registration_defaults_the_label_to_the_model_name(account_model: type) -> None:
    assert _admin(account_model, FakeSession()).models[0].label == "Account"


@pytest.mark.parametrize("prefix, expected", [("/admin/", "/admin"), ("/", "/")])
@pytest.mark.asyncio
async def test_overview_home_link_is_valid_for_trimmed_and_root_prefixes(
    account_model: type, prefix: str, expected: str
) -> None:
    admin = _admin(account_model, FakeSession())
    index = routes(admin.router(prefix))[("GET", expected)]

    assert f'href="{expected}"' in (await index(Request())).body.decode()


def test_only_requested_operation_routes_are_mounted(account_model: type) -> None:
    mounted = set(routes(_admin(account_model, FakeSession(), operations=("create",)).router()))

    assert mounted == {
        ("GET", "/admin"),
        ("GET", "/admin/account/new"),
        ("POST", "/admin/account/new"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/admin/account/{pk}/edit"),
        ("POST", "/admin/account/{pk}/edit"),
        ("GET", "/admin/account/{pk}/delete"),
        ("POST", "/admin/account/{pk}/delete"),
    ],
)
async def test_missing_rows_are_404s_on_every_row_write_view(
    account_model: type, method: str, path: str
) -> None:
    handler = routes(_admin(account_model, FakeSession()).router())[(method, path)]

    response = await handler(Request(path_params={"pk": "404"}))

    assert response.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/account/{pk}/edit", "/admin/account/{pk}/delete"])
async def test_csrf_refusal_stops_existing_row_writes(account_model: type, path: str) -> None:
    row = account_model(id=1, name="a", email="e")
    session = FakeSession({1: row})
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: False,
    )
    admin.register(account_model)

    response = await routes(admin.router())[("POST", path)](
        Request(path_params={"pk": "1"}, form={"name": "changed"})
    )

    assert response.status == 403
    assert row.name == "a"
    assert session.deleted == []


def test_operation_rule_precedes_group_and_star_rules() -> None:
    exact = Access.roles("exact")
    assert (
        registry._rule_for(
            {
                "update": exact,
                "write": Access.roles("group"),
                "*": Access.roles("fallback"),
            },
            "update",
        )
        is exact
    )


def test_missing_operation_group_and_star_resolves_to_public() -> None:
    with pytest.raises(AdminError, match="may not be public"):
        Admin(
            lambda request: FakeSession(),
            authorize={"list": Access.roles("staff")},
        )


def test_text_primary_keys_are_not_coerced() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Text

    class Token(Model, table="admin_tokens"):
        id: Mapped[str] = column(Text, primary_key=True)

    entry = _admin(Token, FakeSession()).models[0]

    assert registry._coerce_pk(entry, "007") == "007"


def test_unsortable_columns_have_no_sort_link(account_model: type) -> None:
    entry = _admin(account_model, FakeSession(), exclude=("email",)).models[0]
    assert registry._sort_url("/admin/account", {}, "email", entry) == ""


def test_form_fields_keep_distinct_control_shapes(account_model: type) -> None:
    entry = _admin(account_model, FakeSession()).models[0]
    fields = {
        field["name"]: field
        for field in registry._form_fields(
            entry, None, frozenset(entry.editable), {"name": "typed"}
        )
    }

    assert fields["name"]["checked"] is False
    assert fields["name"]["required"] is True
    assert fields["name"]["plain"] is True
    assert fields["note"]["required"] is False
    assert fields["active"]["required"] is False


def test_non_nullable_boolean_is_not_marked_required() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Bool, Int64

    class Switch(Model, table="admin_switches"):
        id: Mapped[int] = column(Int64, primary_key=True)
        enabled: Mapped[bool] = column(Bool)

    entry = _admin(Switch, FakeSession()).models[0]
    field = registry._form_fields(entry, None, frozenset({"enabled"}))[0]

    assert field["required"] is False


def test_multiline_form_fields_are_not_plain() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Jsonb

    class Document(Model, table="admin_documents"):
        id: Mapped[int] = column(Int64, primary_key=True)
        payload: Mapped[dict] = column(Jsonb)

    entry = _admin(Document, FakeSession()).models[0]
    field = registry._form_fields(entry, None, frozenset({"payload"}))[0]

    assert field["multiline"] is True
    assert field["plain"] is False


def test_missing_submitted_field_uses_the_instance_value(account_model: type) -> None:
    entry = _admin(account_model, FakeSession()).models[0]
    row = account_model(id=1, name="stored", email="e")
    fields = registry._form_fields(entry, row, frozenset({"name"}), {})

    assert fields[0]["value"] == "stored"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "form, expected_value, expected_raw",
    [({}, False, ""), ({"active": "on"}, True, "true")],
)
async def test_boolean_submission_records_value_and_redraw_text(
    account_model: type,
    form: dict[str, str],
    expected_value: bool,
    expected_raw: str,
) -> None:
    entry = _admin(account_model, FakeSession()).models[0]

    submitted = await registry._submitted(Request(form=form), entry, frozenset({"active"}))

    assert submitted.values == {"active": expected_value}
    assert submitted.raw == {"active": expected_raw}
