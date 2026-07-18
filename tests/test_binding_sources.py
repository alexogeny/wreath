from __future__ import annotations

from typing import Annotated, Any

import pytest

from wreath import Wreath
from wreath.binding import Body, Cookie, Form, Header, Path, Query
from wreath.testing import TestClient


@pytest.mark.asyncio
async def test_explicit_binding_sources_and_aliases() -> None:
    app = Wreath()

    @app.post("/items/{item_id}")
    async def item(
        request: Any,
        item_id: Annotated[int, Path()],
        verbose: Annotated[bool, Query(alias="details")],
        token: Annotated[str, Header(alias="x-token")],
        session: Annotated[str, Cookie(alias="session-id")],
        payload: Annotated[dict[str, int], Body()],
    ) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "verbose": verbose,
            "token": token,
            "session": session,
            "payload": payload,
        }

    async with TestClient(app) as client:
        response = await client.post(
            "/items/42?details=true",
            headers={"x-token": "secret", "cookie": "session-id=abc"},
            json={"count": 3},
        )

    assert response.status == 200
    assert response.json() == {
        "item_id": 42,
        "verbose": True,
        "token": "secret",
        "session": "abc",
        "payload": {"count": 3},
    }


@pytest.mark.asyncio
async def test_form_source_and_missing_header_location() -> None:
    app = Wreath()

    @app.post("/form")
    async def form_handler(
        request: Any,
        name: Annotated[str, Form(alias="display-name")],
        token: Annotated[str, Header(alias="x-token")],
    ) -> str:
        return f"{name}:{token}"

    async with TestClient(app) as client:
        missing = await client.post(
            "/form",
            headers={"content-type": "application/x-www-form-urlencoded"},
            content=b"display-name=Wreath",
        )
        response = await client.post(
            "/form",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-token": "ok",
            },
            content=b"display-name=Wreath",
        )

    assert missing.status == 422
    assert missing.json()["errors"][0]["loc"] == ["header", "x-token"]
    assert response.status == 200
    assert response.text == "Wreath:ok"


def test_body_cannot_be_combined_with_form() -> None:
    app = Wreath()

    @app.post("/invalid")
    async def invalid(
        request: Any,
        payload: Annotated[dict[str, Any], Body()],
        name: Annotated[str, Form()],
    ) -> None:
        return None

    with pytest.raises(TypeError, match="body and form/file"):
        app._compile_routes()
