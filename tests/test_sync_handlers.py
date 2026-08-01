"""A `def` route handler is served, and keeps its call convention throughout.

Before this, a synchronous handler was neither supported nor refused: it was
`await`ed like every other endpoint, `await {"ok": True}` raised `TypeError`
inside dispatch, and the caller got a 500 naming nothing. That is the worst of
the three available answers, so the behaviour is pinned here rather than left to
whichever wrapper happens to run.

The wrappers are the interesting part. A route acquires a different chain
depending on what it declares -- a return annotation adds
`compile_response_validator`, a non-200 status adds `_ensure_response`,
middleware adds `compile_middleware` -- and each of those used to force the
result back into a coroutine. Every combination below therefore has to answer
the same as its `async def` twin.
"""

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
    def depends(
        request: Any, value: int = Depends(_sync_dependency)
    ) -> dict[str, int]:
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
    """`compile_response_validator` wraps it, and must not force a coroutine."""
    async with TestClient(_app()) as client:
        response = await client.get("/annotated")
    assert response.status == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_a_declared_status_survives_a_synchronous_handler() -> None:
    """`_ensure_response` is the wrapper on this path."""
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
    """Not swallowed, and not escaped: the same 500 an `async def` would raise."""
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
    """The point of the change: no wrapper reintroduced the coroutine.

    Asserted on the compiled endpoint rather than on timing, because a timing
    assertion for ~300ns is a flaky test and this is the property that produces
    it.
    """
    app = _app()
    app._compile_routes()
    handler, _params = app._route_match("GET", "/annotated", 0)
    assert not inspect.iscoroutinefunction(handler)
    twin, _params = app._route_match("GET", "/async-twin", 0)
    assert inspect.iscoroutinefunction(twin)
