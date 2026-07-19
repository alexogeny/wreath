"""The Wreath app must behave identically under both routing backends."""

from __future__ import annotations

from typing import Any

import pytest

from wreath import JSONResponse, Wreath
from wreath._routing import RoutingMode


async def invoke(
    app: Wreath, path: str = "/", *, method: str = "GET"
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    await app(scope, receive, send)
    return sent


def _build(mode: RoutingMode) -> Wreath:
    app = Wreath(routing=mode)

    @app.get("/")
    async def home(request):
        return {"page": "home"}

    @app.get("/users/{uid}")
    async def user(request):
        return JSONResponse({"uid": request.path_params["uid"]})

    @app.get("/users/me")
    async def me(request):
        return {"page": "me"}

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["decision", "trie", "bitset"])
async def test_static_and_dynamic(mode: RoutingMode) -> None:
    app = _build(mode)
    assert (await invoke(app, "/"))[1]["body"] == b'{"page":"home"}'
    assert (await invoke(app, "/users/42"))[1]["body"] == b'{"uid":"42"}'
    assert (await invoke(app, "/users/me"))[1]["body"] == b'{"page":"me"}'
    assert (await invoke(app, "/missing"))[0]["status"] == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["decision", "trie", "bitset"])
async def test_head_uses_get(mode: RoutingMode) -> None:
    app = _build(mode)
    sent = await invoke(app, "/users/42", method="HEAD")
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b""


def test_default_routing_mode_is_bitset() -> None:
    app = Wreath()
    assert app._routing == "bitset"
    assert app.router._mode == "bitset"


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError, match="unknown routing mode"):
        Wreath(routing="quantum")  # type: ignore
