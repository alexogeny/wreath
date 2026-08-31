from __future__ import annotations

from typing import Any, cast

import pytest

from wreath import JSONResponse, Wreath
from wreath._native import _core
from wreath._routing import Router as CompiledRouter
from wreath._routing import RoutingMode


async def handler(*_args: Any, **_kwargs: Any) -> None:
    pass


async def invoke(app: Wreath, path: str = "/", *, method: str = "GET") -> list[dict[str, Any]]:
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
@pytest.mark.parametrize("mode", ["policy"])
async def test_static_and_dynamic(mode: RoutingMode) -> None:
    app = _build(mode)
    assert (await invoke(app, "/"))[1]["body"] == b'{"page":"home"}'
    assert (await invoke(app, "/users/42"))[1]["body"] == b'{"uid":"42"}'
    assert (await invoke(app, "/users/me"))[1]["body"] == b'{"page":"me"}'
    assert (await invoke(app, "/missing"))[0]["status"] == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["policy"])
async def test_head_uses_get(mode: RoutingMode) -> None:
    app = _build(mode)
    sent = await invoke(app, "/users/42", method="HEAD")
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["policy"])
async def test_a_method_miss_is_405_under_every_backend(mode: RoutingMode) -> None:
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


def test_default_routing_mode_uses_the_policy_table() -> None:
    app = Wreath()
    assert app._routing == "policy"
    assert app.router._mode == "policy"
    assert type(app.router._table).__name__ == "PolicyRouteTable"


def test_native_module_exports_only_the_canonical_route_table() -> None:
    assert hasattr(_core, "PolicyRouteTable")
    assert not hasattr(_core, "BitsetRouteTable")
    assert not hasattr(_core, "DecisionRouteTable")
    assert not hasattr(_core, "RouteTable")


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError, match="unknown routing mode 'quantum'; use 'policy'"):
        Wreath(routing=cast(Any, "quantum"))


@pytest.mark.parametrize("mode", ["bitset", "decision", "trie"])
def test_removed_routing_mode_aliases_are_rejected(mode: str) -> None:
    with pytest.raises(ValueError, match=f"unknown routing mode '{mode}'; use 'policy'"):
        Wreath(routing=cast(Any, mode))


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


@pytest.mark.parametrize("mode", ["policy"])
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


def test_policy_table_orders_host_static_and_greedy_routes() -> None:
    router = CompiledRouter("policy")

    async def fallback(*_args: Any, **_kwargs: Any) -> None:
        pass

    async def manifest(*_args: Any, **_kwargs: Any) -> None:
        pass

    async def tenant(*_args: Any, **_kwargs: Any) -> None:
        pass

    router.add("/assets/{rest:path}", "GET", fallback)
    router.add("/assets/manifest.json", "GET", manifest)
    router.add("/assets/{name}", "GET", tenant, host="{account}.example.test")
    router.compile()

    assert router.classify_request("GET", "/assets/logo.svg", "acme.example.test") == (
        1,
        (tenant, {"name": "logo.svg", "account": "acme"}),
    )
    assert router.classify_request("GET", "/assets/manifest.json", "elsewhere.test") == (
        1,
        (manifest, None),
    )
    assert router.classify_request("GET", "/assets/css/site.css", "elsewhere.test") == (
        1,
        (fallback, {"rest": "css/site.css"}),
    )


def test_dynamic_dispatch_only_walks_routes_for_the_requested_method() -> None:
    router = CompiledRouter("policy")
    for index in range(64):
        router.add(f"/method-{index}/{{rest:path}}", f"METHOD-{index}", handler)
    router.add("/assets/{rest:path}", "GET", handler)
    router.compile()

    assert router.classify_request("GET", "/assets/site.css", "example.test")[0] == 1
    stats = router._table.probe_stats()
    assert stats["dynamic_candidates"] == 1


@pytest.mark.parametrize(
    ("path", "host", "request_path", "request_host", "params"),
    [
        (
            "/private/{rest:path}",
            None,
            "/private/reports/annual.pdf",
            "example.test",
            {"rest": "reports/annual.pdf"},
        ),
        (
            "/private/{item}",
            "{tenant}.example.test",
            "/private/42",
            "acme.example.test",
            {"item": "42", "tenant": "acme"},
        ),
    ],
)
def test_dynamic_protected_routes_resolve_the_native_continuation(
    path: str,
    host: str | None,
    request_path: str,
    request_host: str,
    params: dict[str, str],
) -> None:
    required = 4
    router = CompiledRouter("policy")
    router.add(path, "GET", handler, (required,), host=host)
    router.compile()

    classification, ticket = router.classify_request("GET", request_path, request_host)

    assert classification == 2
    assert router.resolve(ticket, 0) is None
    assert router.resolve(ticket, required) == (handler, params)
