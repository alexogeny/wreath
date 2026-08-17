from __future__ import annotations

from typing import Any

import pytest

import wreath.app as app_module
from wreath import JSONResponse, Response, Wreath
from wreath.app import _StaticMatcher
from wreath.binding import Depends
from wreath.policy import HttpPolicy, RequestIdPolicy
from wreath.response import PreparedResponse


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
    raw_path: bytes | None = None,
    scope_extra: dict[str, Any] | None = None,
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
        "raw_path": path.encode() if raw_path is None else raw_path,
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 123),
        "root_path": "",
    }
    if scope_extra is not None:
        scope.update(scope_extra)
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
async def test_frozen_route_replays_a_prepared_response_on_portable_asgi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    response = PreparedResponse.text("ready", headers=((b"x-image", b"fixed"),))

    assert app.frozen("/ready", response) is response
    app._compile_routes()

    def unexpected_request(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unambiguous frozen ASGI route constructed Request")

    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    monkeypatch.setattr(app_module, "Request", unexpected_request)

    sent = await invoke(app, "/ready")
    assert sent == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": list(response.headers),
        },
        {"type": "http.response.body", "body": b"ready"},
    ]
    assert app._dispatch_http == app._handle_http_frozen


@pytest.mark.asyncio
async def test_frozen_get_answers_head_without_a_body() -> None:
    app = Wreath()
    response = PreparedResponse.text("ready")
    app.frozen("/ready", response)

    sent = await invoke(app, "/ready", method="HEAD")

    assert sent[0]["status"] == 200
    assert (b"content-length", b"5") in sent[0]["headers"]
    assert sent[1] == {"type": "http.response.body", "body": b""}


def test_frozen_route_requires_an_exact_immutable_response() -> None:
    app = Wreath()

    with pytest.raises(TypeError, match="exact PreparedResponse"):
        app.frozen("/mutable", Response(b"mutable"))
    with pytest.raises(TypeError, match="always response_only"):
        app.frozen("/duplicate", PreparedResponse(b"fixed"), response_only=True)
    with pytest.raises(TypeError, match="owns its status_code"):
        app.frozen("/status", PreparedResponse(b"fixed"), status_code=201)


@pytest.mark.asyncio
async def test_frozen_route_with_a_dependency_keeps_the_request_lifecycle() -> None:
    app = Wreath()
    ran: list[str] = []

    def audit(request: Any) -> None:
        ran.append(request.path)

    app.frozen(
        "/ready",
        PreparedResponse.text("ready"),
        dependencies=(Depends(audit),),
    )

    sent = await invoke(app, "/ready")

    assert ran == ["/ready"]
    assert sent[1]["body"] == b"ready"
    assert app._dispatch_http != app._handle_http_frozen


@pytest.mark.asyncio
async def test_frozen_route_with_route_middleware_keeps_the_lifecycle() -> None:
    app = Wreath()
    entered: list[str] = []

    async def around(request: Any, call_next: Any) -> Any:
        entered.append("before")
        response = await call_next(request)
        entered.append("after")
        return response

    app.frozen(
        "/ready",
        PreparedResponse.text("ready"),
        middleware=(around,),
    )

    sent = await invoke(app, "/ready")

    assert entered == ["before", "after"]
    assert sent[1]["body"] == b"ready"
    assert app._dispatch_http != app._handle_http_frozen


def test_frozen_route_preserves_its_control_plane_name() -> None:
    app = Wreath()
    app.frozen("/default", PreparedResponse())
    app.frozen("/named", PreparedResponse(), name="readiness")

    assert app._routes[0].endpoint.__name__ == "frozen_response"
    assert app._routes[1].endpoint.__name__ == "readiness"


@pytest.mark.asyncio
async def test_frozen_route_miss_uses_the_general_404_lifecycle() -> None:
    app = Wreath()
    app.frozen("/ready", PreparedResponse.text("ready"))

    sent = await invoke(app, "/missing")

    assert sent[0]["status"] == 404


@pytest.mark.asyncio
async def test_frozen_route_ambiguous_raw_path_uses_the_general_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen("/ready", PreparedResponse.text("ready"))
    app._compile_routes()
    original = app_module._request_new
    constructed = 0

    def observed_request(*args: Any, **kwargs: Any) -> Any:
        nonlocal constructed
        constructed += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    monkeypatch.setattr(app_module, "_request_new", observed_request)
    sent = await invoke(app, "/ready", raw_path=b"%2fready")

    assert sent[0]["status"] == 400
    assert constructed == 1


@pytest.mark.asyncio
async def test_frozen_route_active_trace_uses_the_general_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen("/ready", PreparedResponse.text("ready"))
    app._compile_routes()
    original = app_module._request_new
    constructed = 0

    def observed_request(*args: Any, **kwargs: Any) -> Any:
        nonlocal constructed
        constructed += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", True)
    monkeypatch.setattr(app_module, "_request_new", observed_request)
    sent = await invoke(app, "/ready")

    assert sent[1]["body"] == b"ready"
    assert constructed == 1


@pytest.mark.asyncio
async def test_frozen_route_attached_flight_uses_the_general_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen("/ready", PreparedResponse.text("ready"))
    app._compile_routes()
    original = app_module._request_new
    constructed = 0

    def observed_request(*args: Any, **kwargs: Any) -> Any:
        nonlocal constructed
        constructed += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    monkeypatch.setattr(app_module, "_request_new", observed_request)
    sent = await invoke(app, "/ready", scope_extra={"_wreath_flight": (1, 1)})

    assert sent[1]["body"] == b"ready"
    assert constructed == 1


def test_frozen_dispatch_is_not_selected_for_request_lifecycle_features() -> None:
    class Hook:
        global_scope = True

        def before_sync(self, request: Any) -> None:
            return None

    shapes = []

    policy = Wreath(http_policy=HttpPolicy(request_id=RequestIdPolicy()))
    policy.frozen("/ready", PreparedResponse())
    shapes.append(policy)

    hooked = Wreath()
    hooked.add_middleware(Hook())
    hooked.frozen("/ready", PreparedResponse())
    shapes.append(hooked)

    dynamic = Wreath()
    dynamic.frozen("/ready", PreparedResponse(), host="example.com")
    shapes.append(dynamic)

    protected = Wreath()
    protected.frozen("/ready", PreparedResponse(), permissions=("ready:read",))
    shapes.append(protected)

    mixed = Wreath()
    mixed.frozen("/ready", PreparedResponse())

    @mixed.get("/ordinary")
    def ordinary(request: Any) -> PreparedResponse:
        return PreparedResponse()

    shapes.append(mixed)

    for app in shapes:
        app._compile_routes()
        assert app._dispatch_http != app._handle_http_frozen


def test_application_without_frozen_routes_has_an_empty_frozen_response_image() -> None:
    app = Wreath()

    @app.get("/ordinary")
    async def ordinary(request: Any) -> PreparedResponse:
        return PreparedResponse()

    app._compile_routes()

    assert app._frozen_responses == {}
    assert app._dispatch_http != app._handle_http_frozen


@pytest.mark.asyncio
async def test_frozen_native_seam_uses_the_one_shot_without_portable_scope_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    response = PreparedResponse.text("ready")
    app.frozen("/ready", response)
    app._compile_routes()

    class NativeScope:
        flight = 0

    class NativeSend:
        def __init__(self) -> None:
            self.responses: list[tuple[int, tuple[tuple[bytes, bytes], ...], bytes]] = []

        async def send(self, message: dict[str, Any]) -> None:
            raise AssertionError(f"native frozen route emitted ASGI message: {message}")

        def _wreath_response(
            self,
            status: int,
            headers: tuple[tuple[bytes, bytes], ...],
            body: bytes,
        ) -> Any:
            self.responses.append((status, headers, body))

            async def completed() -> None:
                return None

            return completed()

    def unexpected_arm(*args: Any) -> None:
        raise AssertionError("undeclared native frozen route armed cancellation")

    def unexpected_general_finisher(*args: Any) -> None:
        raise AssertionError("native frozen seam entered the general response finisher")

    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    monkeypatch.setattr(app_module, "_arm_cancel_on_disconnect", unexpected_arm)
    monkeypatch.setattr(Wreath, "_finish_http_plain", unexpected_general_finisher)
    sender = NativeSend()
    await app._handle_http_frozen(
        NativeScope(), None, sender.send, "GET", "/ready", True
    )

    assert sender.responses == [(response.status, response.headers, response.body)]


@pytest.mark.asyncio
async def test_frozen_native_seam_falls_back_for_an_attached_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen("/ready", PreparedResponse.text("ready"))
    app._compile_routes()
    fell_back: list[str] = []

    class NativeScope:
        flight = 1

    class NativeSend:
        async def send(self, message: dict[str, Any]) -> None:
            return None

    def fallback(
        self: Wreath,
        scope: Any,
        receive: Any,
        send: Any,
        method: str,
        path: str,
        native_response: bool,
    ) -> Any:
        fell_back.append(path)

        async def completed() -> None:
            return None

        return completed()

    monkeypatch.setattr(Wreath, "_handle_http", fallback)
    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    await app._handle_http_frozen(
        NativeScope(), None, NativeSend().send, "GET", "/ready", True
    )

    assert fell_back == ["/ready"]


@pytest.mark.asyncio
async def test_frozen_native_seam_arms_only_the_declared_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    app.frozen(
        "/declared",
        PreparedResponse.text("declared"),
        cancel_on_disconnect=False,
    )
    app.frozen("/plain", PreparedResponse.text("plain"))
    app._compile_routes()
    armed: list[bool] = []

    class NativeScope:
        flight = 0

    class NativeSend:
        async def send(self, message: dict[str, Any]) -> None:
            return None

        def _wreath_response(self, status: int, headers: Any, body: bytes) -> Any:
            async def completed() -> None:
                return None

            return completed()

    monkeypatch.setattr(
        app_module,
        "_arm_cancel_on_disconnect",
        lambda send, enabled: armed.append(enabled),
    )
    monkeypatch.setattr(app_module._telemetry, "PROPAGATING", False)
    sender = NativeSend()
    await app._handle_http_frozen(
        NativeScope(), None, sender.send, "GET", "/plain", True
    )
    await app._handle_http_frozen(
        NativeScope(), None, sender.send, "GET", "/declared", True
    )

    assert armed == [False]


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
async def test_allow_does_not_repeat_an_explicit_head_route() -> None:
    app = Wreath()

    @app.get("/items")
    async def read(request):
        return "read"

    @app.route("/items", methods=("HEAD",))
    async def inspect_headers(request):
        return ""

    sent = await invoke(app, "/items", method="POST")

    assert sent[0]["status"] == 405
    assert dict(sent[0]["headers"])[b"allow"] == b"GET, HEAD"


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


def test_named_route_registration_does_not_rescan_prior_definitions() -> None:
    class NoIterationList(list):
        def __iter__(self):
            raise AssertionError("route registration rescanned prior definitions")

    app = Wreath()

    @app.get("/first", name="first")
    async def first(request):
        return "first"

    app._routes = NoIterationList(app._routes)

    @app.get("/second", name="second")
    async def second(request):
        return "second"

    with pytest.raises(ValueError, match="route name 'first' is already registered"):

        @app.get("/third", name="first")
        async def duplicate(request):
            return "duplicate"


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
