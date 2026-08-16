"""Registration: what the admin refuses to be, before it renders anything."""

from __future__ import annotations

import pytest

from tests.admin._doubles import FakeSession, routes
from wreath._admin import registry as admin_registry
from wreath.admin import Admin, AdminError, FieldAccess
from wreath.crud import Access

pytestmark = pytest.mark.asyncio


def _admin(**kwargs: object) -> Admin:
    kwargs.setdefault("authorize", Access.roles("staff"))
    kwargs.setdefault("csrf", lambda request: True)
    return Admin(lambda request: FakeSession(), **kwargs)  # type: ignore[arg-type]


async def test_a_public_admin_is_refused(account_model: type) -> None:
    with pytest.raises(AdminError) as caught:
        Admin(lambda request: FakeSession(), authorize=Access.public())
    assert "may not be public" in str(caught.value)


async def test_a_public_group_is_refused_even_when_writes_are_gated() -> None:
    """`{"read": public}` is still a public admin, and the group must be read."""
    with pytest.raises(AdminError) as caught:
        Admin(
            lambda request: FakeSession(),
            authorize={"read": Access.public(), "write": Access.roles("staff")},
        )
    message = str(caught.value)
    assert "list, retrieve" in message


async def test_router_refuses_when_nothing_is_registered() -> None:
    with pytest.raises(AdminError) as caught:
        _admin().router()
    assert "no models are registered" in str(caught.value)


async def test_write_operations_refuse_without_a_csrf_verifier(
    account_model: type,
) -> None:
    """The gap this phase found, made visible in the API rather than papered over."""
    admin = Admin(lambda request: FakeSession(), authorize=Access.roles("staff"))
    admin.register(account_model)
    with pytest.raises(AdminError) as caught:
        admin.router()
    message = str(caught.value)
    assert "csrf" in message and "header" in message


async def test_a_read_only_admin_needs_no_csrf_verifier(account_model: type) -> None:
    admin = Admin(lambda request: FakeSession(), authorize=Access.roles("staff"))
    admin.register(account_model, operations=("list", "retrieve"))
    paths = {path for _method, path in routes(admin.router())}
    assert "/admin/account/new" not in paths
    assert "/admin/account/{pk}" in paths


async def test_sensitive_columns_are_absent_from_every_view(
    account_model: type,
) -> None:
    """Withholding is `wreath.crud`'s decision, not a second one made here."""
    entry = _admin().register(account_model)
    assert "password_hash" not in entry.columns
    assert "password_hash" not in entry.editable
    assert "password_hash" not in entry.list_columns


async def test_expose_opts_a_withheld_column_back_in(account_model: type) -> None:
    entry = _admin().register(account_model, expose=("password_hash",))
    assert "password_hash" in entry.columns
    # Readable is not writable: `expose` widens what may be seen, and a
    # sensitive column is never settable from a generated form.
    assert "password_hash" not in entry.editable


async def test_registration_scans_sensitive_names_once(
    account_model: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = admin_registry.sensitive_fields
    calls = 0

    def counted(model: type) -> frozenset[str]:
        nonlocal calls
        calls += 1
        return original(model)

    monkeypatch.setattr(admin_registry, "sensitive_fields", counted)

    _admin().register(account_model)

    assert calls == 1


async def test_the_primary_key_is_never_editable(account_model: type) -> None:
    entry = _admin().register(account_model)
    assert "id" not in entry.editable
    assert "id" in entry.columns


async def test_readonly_columns_are_shown_and_not_editable(account_model: type) -> None:
    entry = _admin().register(account_model, readonly=("email",))
    assert "email" in entry.columns and "email" not in entry.editable


@pytest.mark.parametrize(
    "kwargs",
    [
        {"list_columns": ("nope",)},
        {"readonly": ("nope",)},
        {"exclude": ("nope",)},
        {"expose": ("nope",)},
        {"field_access": {"nope": FieldAccess(read="x")}},
    ],
)
async def test_naming_a_column_that_does_not_exist_is_refused(
    account_model: type, kwargs: dict
) -> None:
    """Each of these would otherwise read as protection that protects nothing."""
    with pytest.raises(AdminError):
        _admin().register(account_model, **kwargs)


async def test_listing_a_withheld_column_is_refused(account_model: type) -> None:
    with pytest.raises(AdminError) as caught:
        _admin().register(account_model, list_columns=("password_hash",))
    assert "expose" in str(caught.value)


async def test_a_duplicate_slug_is_refused(account_model: type) -> None:
    admin = _admin()
    admin.register(account_model)
    with pytest.raises(AdminError):
        admin.register(account_model)


async def test_an_unknown_operation_is_refused(account_model: type) -> None:
    with pytest.raises(AdminError) as caught:
        _admin().register(account_model, operations=("list", "purge"))
    assert "purge" in str(caught.value)


async def test_a_composite_primary_key_is_refused() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    class Pair(Model, table="admin_pairs"):
        left: Mapped[int] = column(Int64, primary_key=True)
        right: Mapped[str] = column(Text, primary_key=True)

    with pytest.raises(AdminError) as caught:
        _admin().register(Pair)
    assert "composite primary key" in str(caught.value)


async def test_every_generated_route_carries_the_access_rule(
    account_model: type,
) -> None:
    """A route the pipeline does not gate is the whole surface, unprotected."""
    from wreath._auth.requirements import requirement_for

    admin = _admin(authorize=Access.roles("staff"))
    admin.register(account_model)
    for (_method, path), endpoint in routes(admin.router()).items():
        requirement = requirement_for(endpoint)
        assert requirement.authenticated, path
        assert requirement.role_checks, path


async def test_step_up_reaches_the_write_routes_only(account_model: type) -> None:
    from wreath._auth.requirements import requirement_for

    admin = _admin(
        authorize={
            "read": Access.roles("staff"),
            "write": Access.roles("staff").within(300),
        }
    )
    admin.register(account_model)
    windows = {
        path: requirement_for(endpoint).second_factor
        for (_method, path), endpoint in routes(admin.router()).items()
    }
    assert windows["/admin/account/{pk}/delete"] == 300
    assert windows["/admin/account/{pk}"] is None
