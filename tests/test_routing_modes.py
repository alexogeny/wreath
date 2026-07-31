"""The Wreath app must behave identically under both routing backends."""

from __future__ import annotations

from typing import Any

import pytest

from wreath import JSONResponse, Wreath
from wreath._routing import Router as CompiledRouter
from wreath._routing import RoutingMode


async def handler(*_args: Any, **_kwargs: Any) -> None:
    pass


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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["decision", "trie", "bitset"])
async def test_a_method_miss_is_405_under_every_backend(mode: RoutingMode) -> None:
    """The 404-vs-405 split is derived by re-classifying the path under each
    registered method, so it has to agree across all three backends -- including
    the trie, whose `classify` is a shim over `match`."""
    app = _build(mode)

    @app.route("/users/{uid}", methods=("DELETE",))
    async def delete_user(request):
        return None

    sent = await invoke(app, "/users/42", method="POST")
    assert sent[0]["status"] == 405
    allow = dict(sent[0]["headers"])[b"allow"]
    assert set(allow.split(b", ")) == {b"GET", b"HEAD", b"DELETE"}
    # A path no route claims is still a plain 404 with no Allow.
    missing = await invoke(app, "/nowhere", method="POST")
    assert missing[0]["status"] == 404
    assert b"allow" not in dict(missing[0]["headers"])


def test_default_routing_mode_is_bitset() -> None:
    app = Wreath()
    assert app._routing == "bitset"
    assert app.router._mode == "bitset"


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError, match="unknown routing mode"):
        Wreath(routing="quantum")  # type: ignore


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/users/{id", "path parameters must occupy an entire segment"),
        ("/users/id}", "path parameters must occupy an entire segment"),
        ("/users/{}", "empty path placeholder"),
        ("/users/{:path}", "empty path placeholder"),
        ("/users/{id:int}", "unknown path converter 'int'"),
        ("/users/{rest:path}/meta", "must be the final path segment"),
    ],
)
def test_malformed_path_placeholders_are_rejected(path: str, message: str) -> None:
    router = CompiledRouter()

    with pytest.raises(ValueError, match=message):
        router.add(path, "GET", handler)


def test_a_final_greedy_path_placeholder_is_accepted() -> None:
    router = CompiledRouter()

    router.add("/assets/{rest:path}", "GET", handler)


@pytest.mark.parametrize("mode", ["decision", "bitset"])
def test_classifying_backends_issue_and_resolve_protected_tickets(
    mode: RoutingMode,
) -> None:
    authenticated = 1
    router = CompiledRouter(mode)
    router.add("/private/{item}", "GET", handler, (authenticated,))

    classification, ticket = router.classify("GET", "/private/42")

    assert classification == 2
    assert router.resolve(ticket, 0) is None
    assert router.resolve(ticket, authenticated) == (
        handler,
        {"item": "42"},
    )
