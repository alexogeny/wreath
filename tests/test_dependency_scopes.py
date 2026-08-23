"""`Depends(..., scope="app")`: values that outlive one request.

Request scope is the default and unchanged. App scope resolves once, on first
use, and is torn down by the owning application's lifespan shutdown -- so an
expensive stateless thing (a client, a compiled ruleset, a warmed table) is
built once without a module global.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.binding import AppScope, Depends, compile_binder
from wreath.request import Request
from wreath.testing import TestClient


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []},
        _receive,
    )


# --- scope validation --------------------------------------------------------


def test_an_unknown_scope_is_rejected_at_declaration() -> None:
    with pytest.raises(ValueError, match="scope must be"):
        Depends(lambda request: None, scope="session")


def test_the_default_scope_is_request() -> None:
    assert Depends(lambda request: None).scope == "request"


# --- app scope resolves once -------------------------------------------------


@pytest.mark.asyncio
async def test_an_app_scoped_dependency_is_constructed_once_across_requests() -> None:
    calls = 0

    async def expensive(request: Request) -> str:
        nonlocal calls
        calls += 1
        return f"built-{calls}"

    async def handler(request: Request, value=Depends(expensive, scope="app")) -> dict:
        return {"value": value}

    scope = AppScope()
    bound = compile_binder(handler, "/", app_scope=scope)

    assert await bound(_request()) == {"value": "built-1"}
    assert await bound(_request()) == {"value": "built-1"}
    assert await bound(_request()) == {"value": "built-1"}
    assert calls == 1


@pytest.mark.asyncio
async def test_a_request_scoped_dependency_is_constructed_every_request() -> None:
    calls = 0

    async def per_request(request: Request) -> int:
        nonlocal calls
        calls += 1
        return calls

    async def handler(request: Request, value=Depends(per_request)) -> dict:
        return {"value": value}

    bound = compile_binder(handler, "/", app_scope=AppScope())

    assert await bound(_request()) == {"value": 1}
    assert await bound(_request()) == {"value": 2}
    assert calls == 2


@pytest.mark.asyncio
async def test_concurrent_first_requests_construct_exactly_once() -> None:
    """The burst at startup is the case a naive check-then-set gets wrong."""
    import asyncio

    calls = 0

    async def expensive(request: Request) -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)      # a real factory awaits something
        return calls

    async def handler(request: Request, value=Depends(expensive, scope="app")) -> dict:
        return {"value": value}

    bound = compile_binder(handler, "/", app_scope=AppScope())
    results = await asyncio.gather(*(bound(_request()) for _ in range(20)))

    assert calls == 1
    assert all(result == {"value": 1} for result in results)


@pytest.mark.asyncio
async def test_cancelling_one_app_scope_waiter_does_not_cancel_the_others() -> None:
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def expensive(request: Request) -> str:
        started.set()
        await release.wait()
        return "shared"

    async def handler(request: Request, value=Depends(expensive, scope="app")) -> dict:
        return {"value": value}

    bound = compile_binder(handler, "/", app_scope=AppScope())
    creator = asyncio.create_task(bound(_request()))
    await started.wait()
    cancelled = asyncio.create_task(bound(_request()))
    survivor = asyncio.create_task(bound(_request()))
    await asyncio.sleep(0)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()

    assert await creator == {"value": "shared"}
    assert await survivor == {"value": "shared"}
    assert await bound(_request()) == {"value": "shared"}


# --- lifetime inversion is a compile-time error ------------------------------


def test_an_app_scoped_dependency_cannot_depend_on_a_request_scoped_one() -> None:
    """The value would capture whichever request happened to build it."""

    async def per_request(request: Request) -> str:
        return "leaky"

    async def singleton(request: Request, inner=Depends(per_request)) -> str:
        return inner

    async def handler(request: Request, value=Depends(singleton, scope="app")) -> dict:
        return {"value": value}

    with pytest.raises(TypeError, match="outlive the request"):
        compile_binder(handler, "/", app_scope=AppScope())


def test_a_request_scoped_dependency_may_depend_on_an_app_scoped_one() -> None:
    """The safe direction: the inner value simply lives longer."""

    async def singleton(request: Request) -> str:
        return "shared"

    async def per_request(request: Request, inner=Depends(singleton, scope="app")) -> str:
        return inner

    async def handler(request: Request, value=Depends(per_request)) -> dict:
        return {"value": value}

    compile_binder(handler, "/", app_scope=AppScope())     # compiles fine


@pytest.mark.asyncio
async def test_an_app_scoped_chain_shares_one_instance_of_each_level() -> None:
    inner_calls = outer_calls = 0

    async def inner(request: Request) -> str:
        nonlocal inner_calls
        inner_calls += 1
        return "inner"

    async def outer(request: Request, dep=Depends(inner, scope="app")) -> str:
        nonlocal outer_calls
        outer_calls += 1
        return f"outer({dep})"

    async def handler(request: Request, value=Depends(outer, scope="app")) -> dict:
        return {"value": value}

    bound = compile_binder(handler, "/", app_scope=AppScope())
    for _ in range(3):
        assert await bound(_request()) == {"value": "outer(inner)"}
    assert (inner_calls, outer_calls) == (1, 1)


def test_app_scope_without_a_container_is_a_clear_error() -> None:
    async def singleton(request: Request) -> str:
        return "x"

    async def handler(request: Request, value=Depends(singleton, scope="app")) -> dict:
        return {"value": value}

    with pytest.raises(TypeError, match="without an application scope"):
        compile_binder(handler, "/")


# --- the same factory at both scopes -----------------------------------------


@pytest.mark.asyncio
async def test_one_factory_used_at_both_scopes_yields_two_distinct_values() -> None:
    """The per-request cache is keyed by (callable, scope), not callable."""
    calls = 0

    async def factory(request: Request) -> int:
        nonlocal calls
        calls += 1
        return calls

    async def handler(
        request: Request,
        shared=Depends(factory, scope="app"),
        fresh=Depends(factory),
    ) -> dict:
        return {"shared": shared, "fresh": fresh}

    bound = compile_binder(handler, "/", app_scope=AppScope())
    first = await bound(_request())
    second = await bound(_request())

    assert first["shared"] == second["shared"]      # app scope pinned
    assert first["fresh"] != second["fresh"]        # request scope moved on


# --- generator cleanup runs at shutdown, not after the request ---------------


@pytest.mark.asyncio
async def test_an_app_scoped_generator_is_not_cleaned_up_after_a_request() -> None:
    events: list[str] = []

    async def resource(request: Request):
        events.append("open")
        try:
            yield "handle"
        finally:
            events.append("close")

    async def handler(request: Request, value=Depends(resource, scope="app")) -> dict:
        return {"value": value}

    scope = AppScope()
    bound = compile_binder(handler, "/", app_scope=scope)

    await bound(_request())
    await bound(_request())
    assert events == ["open"]          # still open across requests

    await scope.aclose()
    assert events == ["open", "close"]


@pytest.mark.asyncio
async def test_app_scope_teardown_runs_every_leg_even_when_one_fails() -> None:
    events: list[str] = []

    async def bad(request: Request):
        try:
            yield "bad"
        finally:
            events.append("bad")
            raise RuntimeError("teardown exploded")

    async def good(request: Request):
        try:
            yield "good"
        finally:
            events.append("good")

    async def handler(
        request: Request,
        a=Depends(bad, scope="app"),
        b=Depends(good, scope="app"),
    ) -> dict:
        return {"a": a, "b": b}

    scope = AppScope()
    bound = compile_binder(handler, "/", app_scope=scope)
    await bound(_request())

    with pytest.raises(RuntimeError, match="teardown exploded"):
        await scope.aclose()
    # The failure did not strand the other generator.
    assert set(events) == {"bad", "good"}


# --- end to end through an application ---------------------------------------


@pytest.mark.asyncio
async def test_app_scope_is_wired_through_the_application_lifespan() -> None:
    events: list[str] = []

    async def client(request: Request):
        events.append("open")
        try:
            yield {"id": len(events)}
        finally:
            events.append("close")

    app = Wreath()

    @app.get("/use")
    async def use(request: Request, dep=Depends(client, scope="app")) -> dict:
        return dep

    async with TestClient(app) as http:
        first = await http.get("/use")
        second = await http.get("/use")
        assert first.json() == second.json()
        assert events == ["open"]

    # Leaving the lifespan context tore the app scope down.
    assert events == ["open", "close"]


@pytest.mark.asyncio
async def test_two_applications_do_not_share_app_scoped_values() -> None:
    """Explicit ownership: the container hangs off the app, not the module."""
    built: list[str] = []

    async def singleton(request: Request) -> int:
        built.append("x")
        return len(built)

    def make_app() -> Wreath:
        app = Wreath()

        @app.get("/n")
        async def n(request: Request, value=Depends(singleton, scope="app")) -> dict:
            return {"n": value}

        return app

    async with TestClient(make_app()) as first, TestClient(make_app()) as second:
        assert (await first.get("/n")).json() == {"n": 1}
        assert (await second.get("/n")).json() == {"n": 2}
    assert built == ["x", "x"]
