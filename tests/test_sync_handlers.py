from __future__ import annotations

import inspect
from typing import Any

import pytest

from wreath import Response, Wreath
from wreath.binding import Depends
from wreath.testing import TestClient


def _sync_dependency(request: Any) -> int:
    return 7


def _app() -> Wreath:
    app = Wreath()

    @app.get("/plain")
    def plain(request: Any) -> Any:
        return {"ok": True}

    @app.get("/annotated")
    def annotated(request: Any) -> dict[str, Any]:
        return {"ok": True}

    @app.get("/created", status_code=201)
    def created(request: Any) -> Any:
        return {"created": True}

    @app.get("/response")
    def response(request: Any) -> Any:
        return Response(b"raw", media_type=b"text/plain")

    @app.get("/typed/{item_id}")
    def typed(request: Any, item_id: int) -> dict[str, int]:
        return {"doubled": item_id * 2}

    @app.get("/depends")
    def depends(request: Any, value: int = Depends(_sync_dependency)) -> dict[str, int]:
        return {"value": value}

    @app.get("/raises")
    def raises(request: Any) -> Any:
        raise ValueError("from a synchronous handler")

    @app.get("/async-twin")
    async def async_twin(request: Any) -> dict[str, Any]:
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_a_synchronous_handler_is_served() -> None:
    async with TestClient(_app()) as client:
        response = await client.get("/plain")
    assert response.status == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_a_return_annotation_still_validates_a_synchronous_handler() -> None:
    async with TestClient(_app()) as client:
        response = await client.get("/annotated")
    assert response.status == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_a_declared_status_survives_a_synchronous_handler() -> None:
    async with TestClient(_app()) as client:
        response = await client.get("/created")
    assert response.status == 201
    assert response.json() == {"created": True}


@pytest.mark.asyncio
async def test_a_synchronous_handler_may_return_a_response_object() -> None:
    async with TestClient(_app()) as client:
        response = await client.get("/response")
    assert response.status == 200
    assert response.text == "raw"


@pytest.mark.asyncio
async def test_parameters_bind_into_a_synchronous_handler() -> None:
    async with TestClient(_app()) as client:
        response = await client.get("/typed/21")
    assert response.status == 200
    assert response.json() == {"doubled": 42}


@pytest.mark.asyncio
async def test_dependencies_resolve_for_a_synchronous_handler() -> None:
    async with TestClient(_app()) as client:
        response = await client.get("/depends")
    assert response.status == 200
    assert response.json() == {"value": 7}


@pytest.mark.asyncio
async def test_an_exception_from_a_synchronous_handler_reaches_the_error_path() -> None:
    async with TestClient(_app()) as client:
        response = await client.get("/raises")
    assert response.status == 500


@pytest.mark.asyncio
async def test_a_synchronous_handler_answers_exactly_as_its_async_twin() -> None:
    async with TestClient(_app()) as client:
        sync = await client.get("/annotated")
        asynchronous = await client.get("/async-twin")
    assert sync.status == asynchronous.status
    assert sync.json() == asynchronous.json()
    assert dict(sync.headers).get(b"content-type") == dict(asynchronous.headers).get(
        b"content-type"
    )


def test_the_compiled_synchronous_handler_is_not_a_coroutine_function() -> None:
    app = _app()
    app._compile_routes()
    handler, _params = app._route_match("GET", "/annotated", 0)
    assert not inspect.iscoroutinefunction(handler)
    twin, _params = app._route_match("GET", "/async-twin", 0)
    assert inspect.iscoroutinefunction(twin)
