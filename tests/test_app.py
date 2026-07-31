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


@pytest.mark.asyncio
async def test_a_raising_exception_handler_still_answers_the_client() -> None:
    """A handler that raises used to escape `Wreath.__call__` entirely.

    The client got no response at all -- a hung request rather than a 500 --
    and the ASGI server saw an application that never started a response. The
    handler's failure is a bug in user code, so it has to surface loudly *and*
    the client still has to get an answer.
    """
    app = Wreath()

    @app.get("/boom")
    async def endpoint(request):
        raise RuntimeError("handler failed")

    @app.exception_handler(RuntimeError)
    async def broken(request, error):
        raise ValueError("the handler itself is broken")

    sent = await invoke(app, "/boom")

    assert sent[0]["status"] == 500
    assert (b"content-type", b"application/problem+json") in sent[0]["headers"]
    assert app.exception_handler_errors == 1


@pytest.mark.asyncio
async def test_a_raising_exception_handler_reports_its_own_error_in_debug() -> None:
    app = Wreath(debug=True)

    @app.get("/boom")
    async def endpoint(request):
        raise RuntimeError("handler failed")

    @app.exception_handler(RuntimeError)
    async def broken(request, error):
        raise ValueError("the handler itself is broken")

    sent = await invoke(app, "/boom")

    assert sent[0]["status"] == 500
    # The *handler's* failure is the actionable one; naming the original error
    # would send the reader looking at code that is working as intended.
    assert b"the handler itself is broken" in sent[1]["body"]


@pytest.mark.asyncio
async def test_a_raising_status_handler_still_answers_the_client() -> None:
    from wreath.exceptions import NotFound

    app = Wreath()

    @app.get("/boom")
    async def endpoint(request):
        raise NotFound()

    async def broken(request, error):
        raise ValueError("the status handler is broken")

    app.add_status_handler(404, broken)

    sent = await invoke(app, "/boom")

    assert sent[0]["status"] == 500
    assert app.exception_handler_errors == 1


@pytest.mark.asyncio
async def test_wrong_method_on_a_matching_path_is_405_with_allow() -> None:
    """`MethodNotAllowed` was defined and raised nowhere: the router answered a
    method miss with a 404, so a defined exception guarded nothing."""
    app = Wreath()

    @app.get("/items")
    async def read(request):
        return "read"

    @app.route("/items", methods=("DELETE",))
    async def remove(request):
        return "removed"

    @app.post("/other")
    async def other(request):
        return "other"

    sent = await invoke(app, "/items", method="POST")

    assert sent[0]["status"] == 405
    allow = dict(sent[0]["headers"])[b"allow"]
    assert set(allow.split(b", ")) == {b"GET", b"HEAD", b"DELETE"}


@pytest.mark.asyncio
async def test_an_unmatched_path_is_still_404() -> None:
    app = Wreath()

    @app.get("/items")
    async def read(request):
        return "read"

    sent = await invoke(app, "/nowhere", method="POST")
    assert sent[0]["status"] == 404


@pytest.mark.asyncio
async def test_a_405_is_shaped_by_a_registered_status_handler() -> None:
    app = Wreath()

    @app.get("/items")
    async def read(request):
        return "read"

    async def shaped(request, error):
        return JSONResponse(
            {"allow": [value.decode() for _name, value in error.headers]}, status=405
        )

    app.add_status_handler(405, shaped)

    sent = await invoke(app, "/items", method="PUT")
    assert sent[0]["status"] == 405
    assert sent[1]["body"] == b'{"allow":["GET, HEAD"]}'
    assert app.exception_handler_errors == 0


@pytest.mark.asyncio
async def test_a_method_miss_on_a_parameterised_path_is_405() -> None:
    app = Wreath()

    @app.get("/items/{item_id}")
    async def read(request):
        return "read"

    sent = await invoke(app, "/items/7", method="DELETE")
    assert sent[0]["status"] == 405
    assert dict(sent[0]["headers"])[b"allow"] == b"GET, HEAD"


def test_rejects_duplicate_static_routes() -> None:
    app = Wreath()

    @app.get("/")
    async def first(request):
        return "first"

    with pytest.raises(ValueError, match="duplicate route"):

        @app.get("/")
        async def second(request):
            return "second"


@pytest.mark.asyncio
async def test_greedy_path_converter_binds_slashes_and_reverses() -> None:
    from wreath.openapi import generate_openapi
    from wreath.testing import TestClient

    app = Wreath()

    @app.get("/assets/{asset_path:path}", name="asset")
    async def asset(request: Any, asset_path: str) -> dict[str, str]:
        return {
            "asset": asset_path,
            "path": request.url_path_for("asset", asset_path="css/site.css"),
            "url": request.url_for("asset", asset_path="css/site.css"),
        }

    async with TestClient(app) as client:
        response = await client.get(
            "/assets/images/logo.svg", headers={"host": "example.test"}
        )

    assert response.status == 200
    assert response.json() == {
        "asset": "images/logo.svg",
        "path": "/assets/css/site.css",
        "url": "http://example.test/assets/css/site.css",
    }
    assert app.url_path_for("asset", asset_path="docs/index.html") == (
        "/assets/docs/index.html"
    )
    assert "/assets/{asset_path}" in generate_openapi(app)["paths"]


@pytest.mark.asyncio
async def test_static_route_precedes_an_unscoped_greedy_fallback() -> None:
    from wreath.testing import TestClient

    app = Wreath()

    @app.get("/assets/{asset_path:path}")
    async def fallback(request: Any, asset_path: str) -> str:
        return f"fallback:{asset_path}"

    @app.get("/assets/manifest.json")
    async def manifest(request: Any) -> str:
        return "manifest"

    async with TestClient(app) as client:
        response = await client.get("/assets/manifest.json")
        head = await client.head("/assets/css/site.css")

    assert response.text == "manifest"
    assert head.status == 200
    assert dict(head.headers)[b"content-length"] == str(
        len("fallback:css/site.css")
    ).encode()
    assert head.body == b""


@pytest.mark.asyncio
async def test_host_route_precedes_the_host_agnostic_route() -> None:
    from wreath.testing import TestClient

    app = Wreath()

    @app.get("/", name="default")
    async def default(request: Any) -> str:
        return "default"

    @app.get("/", host="{tenant}.example.test", name="tenant-home")
    async def tenant(request: Any, tenant: str) -> str:
        return tenant

    async with TestClient(app) as client:
        tenant_response = await client.get("/", headers={"host": "acme.example.test"})
        default_response = await client.get("/", headers={"host": "elsewhere.test"})

    assert tenant_response.text == "acme"
    assert default_response.text == "default"


@pytest.mark.asyncio
async def test_mount_dispatches_a_child_asgi_application_with_root_path() -> None:
    from wreath.testing import TestClient

    observed: list[tuple[str, str]] = []

    async def child(scope: dict[str, Any], receive: Any, send: Any) -> None:
        observed.append((scope["path"], scope["root_path"]))
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": scope["path"].encode()})

    app = Wreath()

    class ParentHeaders:
        def after_inplace(self, request: Any, response: Any) -> None:
            response.headers.append((b"x-parent", b"wreath"))

    app.add_global_middleware(ParentHeaders())
    app.mount("/service", child, name="service")

    async with TestClient(app) as client:
        response = await client.get("/service/health")

    assert response.status == 201
    assert response.text == "/health"
    assert dict(response.headers)[b"x-parent"] == b"wreath"
    assert observed == [("/health", "/service")]
    assert app.url_path_for("service", path="health/live") == "/service/health/live"
