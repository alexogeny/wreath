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


# --- FastAPI-style marker defaults -------------------------------------------
#
# `limit: int = Query(20)` is the single most common porting mistake from
# FastAPI. Wreath accepted it and did the wrong thing three times over: the
# constraints were ignored, nothing bound, and the marker *object* was handed to
# the handler as the value. It is refused when routes compile, which is before
# the first request under any server.


def test_query_marker_as_a_default_is_refused() -> None:
    app = Wreath()

    @app.get("/items")
    async def items(request: Any, limit: int = Query(20)) -> Any:
        return limit

    with pytest.raises(TypeError, match="Annotated"):
        app._compile_routes()


def test_the_refusal_names_the_parameter_and_the_correct_form() -> None:
    app = Wreath()

    @app.get("/items")
    async def items(request: Any, limit: int = Query(minimum=1)) -> Any:
        return limit

    with pytest.raises(TypeError) as caught:
        app._compile_routes()
    message = str(caught.value)
    assert "limit" in message
    assert "Annotated[int, Query(...)] = <default>" in message


@pytest.mark.parametrize(
    ("marker", "argument"),
    [
        (Path, "item_id"),
        (Query, None),
        (Header, "x-token"),
        (Cookie, "session"),
        (Body, None),
        (Form, None),
    ],
)
def test_every_source_marker_is_refused_as_a_default(marker, argument) -> None:
    app = Wreath()
    default = marker(argument) if argument is not None else marker()

    @app.post("/items/{item_id}")
    async def items(request: Any, value: str = default) -> Any:
        return value

    with pytest.raises(TypeError, match="Annotated"):
        app._compile_routes()


def test_a_marker_without_an_annotation_is_refused_too() -> None:
    app = Wreath()

    @app.get("/items")
    async def items(request: Any, limit=Query(20)) -> Any:  # noqa: B008
        return limit

    with pytest.raises(TypeError, match="Annotated"):
        app._compile_routes()


@pytest.mark.asyncio
async def test_the_correct_annotated_form_still_binds_and_constrains() -> None:
    app = Wreath()

    @app.get("/items")
    async def items(
        request: Any, limit: Annotated[int, Query(minimum=1, maximum=100)] = 20
    ) -> dict[str, int]:
        return {"limit": limit}

    async with TestClient(app) as client:
        default = await client.get("/items")
        given = await client.get("/items?limit=50")
        over = await client.get("/items?limit=500")

    assert default.json() == {"limit": 20}
    assert given.json() == {"limit": 50}
    assert over.status == 422


@pytest.mark.asyncio
async def test_depends_is_still_written_as_a_default() -> None:
    """`Depends` is the one marker that *is* a default; the refusal must not
    catch it."""
    from wreath.binding import Depends

    app = Wreath()

    async def provide(request: Any) -> str:
        return "provided"

    @app.get("/items")
    async def items(request: Any, value: str = Depends(provide)) -> dict[str, str]:
        return {"value": value}

    async with TestClient(app) as client:
        response = await client.get("/items")

    assert response.json() == {"value": "provided"}


@pytest.mark.asyncio
async def test_a_repeated_query_parameter_binds_the_first_occurrence() -> None:
    """`?page=1&page=9` binds 1, and nothing else pinned that.

    The binder folds the parsed pairs into a mapping to answer each declared
    parameter once, and which occurrence survives that fold is observable to
    every caller -- a client that appends rather than replaces a parameter gets
    a different answer if it ever flips. It was `setdefault` in a Python loop
    and is now the same fold in one C call; this is what makes the two the same
    fold rather than two spellings that happen to agree today.
    """
    app = Wreath()

    @app.get("/search")
    async def search(request: Any, page: Annotated[int, Query()] = 0) -> dict[str, int]:
        return {"page": page}

    async with TestClient(app) as client:
        response = await client.get("/search?page=1&page=9")

    assert response.status == 200
    assert response.json() == {"page": 1}
