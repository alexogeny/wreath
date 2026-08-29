from __future__ import annotations

from typing import Any

import pytest

from tests.admin._doubles import FakeSession, Identity, Request, routes
from wreath.admin import Admin, AdminError
from wreath.audit_log import current_actor
from wreath.crud import Access

pytestmark = pytest.mark.asyncio


class RecordingSession(FakeSession):
    """A session that records the bound actor whenever it is asked to flush."""

    def __init__(self, rows: dict[Any, Any] | None = None) -> None:
        super().__init__(rows)
        self.actors: list[str | None] = []
        self.transactions = 0

    async def flush(self) -> None:
        self.actors.append(current_actor())

    def begin(self) -> Any:
        self.transactions += 1
        return super().begin()


def _handlers(model: type, session: FakeSession) -> dict:
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: True,
    )
    admin.register(model)
    return routes(admin.router())


def _account(model: type) -> Any:
    return model(id=1, name="Ada", email="ada@x.io", note=None, active=True)


async def test_a_create_is_attributed_to_the_authenticated_identity(
    account_model: type,
) -> None:
    session = RecordingSession()
    handlers = _handlers(account_model, session)

    response = await handlers[("POST", "/admin/account/new")](
        Request(form={"name": "Ada", "email": "ada@x.io"}, identity=Identity("41"))
    )

    assert response.status == 303
    assert session.actors == ["user:41"]


async def test_an_update_is_attributed(account_model: type) -> None:
    session = RecordingSession({1: _account(account_model)})
    handlers = _handlers(account_model, session)

    await handlers[("POST", "/admin/account/{pk}/edit")](
        Request(path_params={"pk": "1"}, form={"name": "Grace"}, identity=Identity("9"))
    )

    assert session.actors == ["user:9"]


async def test_a_delete_is_attributed(account_model: type) -> None:
    session = RecordingSession({1: _account(account_model)})
    handlers = _handlers(account_model, session)

    response = await handlers[("POST", "/admin/account/{pk}/delete")](
        Request(path_params={"pk": "1"}, identity=Identity("9"))
    )

    assert response.status == 303
    assert session.actors == ["user:9"]
    assert session.deleted


async def test_the_actor_does_not_leak_past_the_write(account_model: type) -> None:
    session = RecordingSession()
    handlers = _handlers(account_model, session)

    await handlers[("POST", "/admin/account/new")](
        Request(form={"name": "Ada", "email": "e"}, identity=Identity("41"))
    )

    assert current_actor() is None


async def test_a_write_with_no_identity_raises_rather_than_recording_one(
    account_model: type,
) -> None:
    session = RecordingSession()
    handlers = _handlers(account_model, session)

    with pytest.raises(AdminError) as caught:
        await handlers[("POST", "/admin/account/new")](
            Request(form={"name": "Ada", "email": "e"}, identity=None)
        )

    assert "no authenticated identity" in str(caught.value)
    assert session.actors == []


async def test_writes_use_the_ordinary_transaction_and_flush(
    account_model: type,
) -> None:
    session = RecordingSession({1: _account(account_model)})
    handlers = _handlers(account_model, session)

    await handlers[("POST", "/admin/account/{pk}/edit")](
        Request(path_params={"pk": "1"}, form={"name": "Grace"}, identity=Identity("9"))
    )

    assert session.transactions == 1
    assert session.closed == 1


async def test_a_refused_form_post_writes_nothing(account_model: type) -> None:
    session = RecordingSession()
    admin = Admin(
        lambda request: session,
        authorize=Access.roles("staff"),
        csrf=lambda request: False,
    )
    admin.register(account_model)
    handlers = routes(admin.router())

    response = await handlers[("POST", "/admin/account/new")](
        Request(form={"name": "Ada", "email": "e"}, identity=Identity("41"))
    )

    assert response.status == 403
    assert session.added == [] and session.actors == []


async def test_an_async_csrf_verifier_is_awaited(account_model: type) -> None:
    session = RecordingSession()

    async def verify(request: Any) -> bool:
        return False

    admin = Admin(lambda request: session, authorize=Access.roles("staff"), csrf=verify)
    admin.register(account_model)
    handlers = routes(admin.router())

    response = await handlers[("POST", "/admin/account/new")](
        Request(form={"name": "Ada", "email": "e"}, identity=Identity("41"))
    )

    assert response.status == 403
    assert session.added == []
