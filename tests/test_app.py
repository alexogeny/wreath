from __future__ import annotations

from typing import Any

import pytest

from wreath import JSONResponse, Wreath
from wreath.app import _StaticMatcher


def test_static_matcher_preserves_first_registration_precedence() -> None:
    async def first(request):
        return None

    async def second(request):
        return None

    matcher = _StaticMatcher()
    matcher.add("/assets/", first)
    matcher.add("/assets/private/", second)

    assert matcher.match("/other/file.txt") is None
    assert matcher.match("/assets/site.css") == (first, {"path": "site.css"})
    assert matcher.match("/assets/private/key.txt") == (first, {"path": "private/key.txt"})


def test_static_matcher_precedence_is_registration_order_not_prefix_length() -> None:
    """The narrower mount registered first must still shadow the wider one.

    Registration order, not longest-prefix: a scan that stops at the first hit
    is only equivalent to the old trie because of this rule.
    """

    async def deep(request):
        return None

    async def shallow(request):
        return None

    matcher = _StaticMatcher()
    matcher.add("/assets/private/", deep)
    matcher.add("/assets/", shallow)

    # Registered first wins even though the other prefix also matches.
    assert matcher.match("/assets/private/key.txt") == (deep, {"path": "key.txt"})
    assert matcher.match("/assets/site.css") == (shallow, {"path": "site.css"})


def test_static_matcher_ignores_a_repeated_prefix() -> None:
    async def first(request):
        return None

    async def second(request):
        return None

    matcher = _StaticMatcher()
    matcher.add("/assets/", first)
    matcher.add("/assets/", second)

    assert matcher.match("/assets/x") == (first, {"path": "x"})


def test_static_matcher_without_mounts_matches_nothing() -> None:
    assert _StaticMatcher().match("/anything/at/all") is None


def test_static_matcher_requires_the_whole_prefix() -> None:
    """A path that merely shares a leading substring is not a mount hit."""

    async def handler(request):
        return None

    matcher = _StaticMatcher()
    matcher.add("/assets/", handler)

    assert matcher.match("/asset") is None
    assert matcher.match("/assetsx/y") is None
    assert matcher.match("/assets/") == (handler, {"path": ""})


async def invoke(
    app: Wreath,
    path: str = "/",
    *,
    method: str = "GET",
    body: bytes = b"",
) -> list[dict[str, Any]]:
    messages = iter([{"type": "http.request", "body": body, "more_body": False}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 123),
        "root_path": "",
    }
    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_static_json_route() -> None:
    app = Wreath()

    @app.get("/json")
    async def endpoint(request):
        return {"message": "hello"}

    sent = await invoke(app, "/json")

    assert sent[0]["status"] == 200
    assert (b"content-type", b"application/json") in sent[0]["headers"]
    assert sent[1]["body"] == b'{"message":"hello"}'


@pytest.mark.asyncio
async def test_dynamic_route_exposes_path_parameters() -> None:
    app = Wreath()

    @app.get("/users/{user_id}")
    async def endpoint(request):
        return JSONResponse({"user_id": request.path_params["user_id"]})

    sent = await invoke(app, "/users/42")

    assert sent[1]["body"] == b'{"user_id":"42"}'


@pytest.mark.asyncio
async def test_request_body_is_cached() -> None:
    app = Wreath()

    @app.post("/echo")
    async def endpoint(request):
        first = await request.body()
        second = await request.body()
        assert first is second
        return first

    sent = await invoke(app, "/echo", method="POST", body=b"payload")
    assert sent[1]["body"] == b"payload"


@pytest.mark.asyncio
async def test_missing_route_is_404() -> None:
    sent = await invoke(Wreath(), "/missing")
    assert sent[0]["status"] == 404


@pytest.mark.asyncio
async def test_head_uses_get_headers_without_body() -> None:
    app = Wreath()

    @app.get("/")
    async def endpoint(request):
        return "hello"

    sent = await invoke(app, method="HEAD")
    assert (b"content-length", b"5") in sent[0]["headers"]
    assert sent[1]["body"] == b""


def test_rejects_duplicate_static_routes() -> None:
    app = Wreath()

    @app.get("/")
    async def first(request):
        return "first"

    with pytest.raises(ValueError, match="duplicate route"):

        @app.get("/")
        async def second(request):
            return "second"
