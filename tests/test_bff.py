from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from wreath import Wreath
from wreath.bff import (
    BFFResource,
    _csrf_header,
    _query_suffix,
    _target,
    _validate_access_token,
    _validate_target_prefix,
    bff_access_token,
    bff_router,
    bff_session_policy,
    set_bff_tokens,
)
from wreath.exceptions import BadRequest
from wreath.policy import HttpPolicy
from wreath.request import Request
from wreath.testing import TestClient


def _request(*, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "headers": headers or []}, receive)


class MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def load(self, sid: str) -> dict[str, Any] | None:
        return self.rows.get(sid)

    async def save(self, sid: str, data: dict[str, Any], max_age: int) -> None:
        self.rows[sid] = dict(data)

    async def delete(self, sid: str) -> None:
        self.rows.pop(sid, None)


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


class RecordingClient:
    origin = "https://api.example"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[tuple[bytes, bytes], ...], bytes]] = []

    async def request(
        self,
        method: str,
        target: str,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        body: bytes = b"",
    ) -> UpstreamResponse:
        self.calls.append((method, target, headers, body))
        return UpstreamResponse(
            201,
            (
                (b"content-type", b"application/json"),
                (b"etag", b'"upstream"'),
                (b"set-cookie", b"upstream=secret"),
                (b"connection", b"close"),
                (b"x-private", b"not-for-the-browser"),
            ),
            b'{"created":true}',
        )


def _cookie(response: Any) -> str:
    raw = response.header("set-cookie")
    assert raw is not None
    return raw.split(";", 1)[0]


def _app(client: RecordingClient) -> tuple[Wreath, MemoryStore]:
    store = MemoryStore()
    app = Wreath(http_policy=HttpPolicy(session=bff_session_policy("s" * 32, store=store)))

    @app.post("/oauth/callback")
    async def callback(request: Any) -> dict[str, bool]:
        set_bff_tokens(
            request,
            access_token="access-token",
            refresh_token="refresh-token",
            expires_at=4_102_444_800,
        )
        return {"ok": True}

    app.include_router(
        bff_router({"catalog": BFFResource(client, target_prefix="/v2", methods={"GET", "POST"})})
    )
    return app, store


def test_bff_session_requires_server_side_storage() -> None:
    with pytest.raises(ValueError, match="server-side SessionStore"):
        bff_session_policy("s" * 32, store=None)


def test_bff_session_cookie_has_the_rfc_10017_security_attributes() -> None:
    policy = bff_session_policy("s" * 32, store=MemoryStore())

    assert policy._cookie == "__Host-Http-wreath_bff"
    assert policy._secure is True
    assert policy._http_only is True
    assert policy._same_site == "strict"
    assert policy._store is not None


@pytest.mark.parametrize("max_age", [True, False, 0, -1, 1.5, "1"])
def test_bff_session_refuses_each_invalid_max_age(max_age: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        bff_session_policy("s" * 32, store=MemoryStore(), max_age=cast(Any, max_age))


@pytest.mark.parametrize("value", [None, 0, b"token", "", "a token", "token!"])
def test_access_token_validation_refuses_each_invalid_shape(value: object) -> None:
    with pytest.raises(ValueError, match="non-empty RFC 6750 bearer token"):
        _validate_access_token(value, source="candidate")


def test_access_token_validation_preserves_valid_token() -> None:
    assert _validate_access_token("abc-._~+/=", source="candidate") == "abc-._~+/="


@pytest.mark.parametrize("refresh_token", ["", 0, b"refresh"])
def test_set_bff_tokens_refuses_each_invalid_refresh_token(refresh_token: object) -> None:
    request = _request()
    request.state.session = {}
    request.state._session_server_side = True

    with pytest.raises(ValueError, match="non-empty string or None"):
        set_bff_tokens(
            request,
            access_token="access",
            refresh_token=cast(Any, refresh_token),
        )


@pytest.mark.parametrize("expires_at", [True, False, "1", b"1", object()])
def test_set_bff_tokens_refuses_each_invalid_expiry_type(expires_at: object) -> None:
    request = _request()
    request.state.session = {}
    request.state._session_server_side = True

    with pytest.raises(TypeError, match="int, float, or None"):
        set_bff_tokens(request, access_token="access", expires_at=cast(Any, expires_at))


def test_set_bff_tokens_omits_absent_optional_values_and_rotates() -> None:
    request = _request()
    request.state.session = {}
    request.state._session_server_side = True

    set_bff_tokens(request, access_token="access")

    assert request.state.session == {"_wreath_bff": {"access_token": "access"}}
    assert request.state._session_rotate is True


def test_set_bff_tokens_preserves_finite_float_expiry() -> None:
    request = _request()
    request.state.session = {}
    request.state._session_server_side = True

    set_bff_tokens(request, access_token="access", expires_at=4_102_444_800.5)

    assert request.state.session["_wreath_bff"]["expires_at"] == 4_102_444_800.5


@pytest.mark.parametrize(
    "state",
    [
        {"_session_server_side": True, "session": None},
        {"_session_server_side": True, "session": []},
        {"_session_server_side": True, "session": {}},
        {"_session_server_side": True, "session": {"_wreath_bff": None}},
        {"_session_server_side": True, "session": {"_wreath_bff": []}},
        {"_session_server_side": True, "session": {"_wreath_bff": {}}},
        {
            "_session_server_side": True,
            "session": {"_wreath_bff": {"access_token": 1}},
        },
        {
            "_session_server_side": True,
            "session": {"_wreath_bff": {"access_token": "bad token"}},
        },
        {
            "_session_server_side": True,
            "session": {"_wreath_bff": {"access_token": "access", "expires_at": True}},
        },
        {
            "_session_server_side": True,
            "session": {"_wreath_bff": {"access_token": "access", "expires_at": "soon"}},
        },
        {
            "_session_server_side": True,
            "session": {"_wreath_bff": {"access_token": "access", "expires_at": float("inf")}},
        },
        {
            "_session_server_side": True,
            "session": {"_wreath_bff": {"access_token": "access", "expires_at": 1}},
        },
    ],
)
def test_bff_access_token_refuses_each_invalid_session_shape(state: dict[str, object]) -> None:
    request = _request()
    for name, value in state.items():
        setattr(request.state, name, value)

    assert bff_access_token(request) is None


@pytest.mark.parametrize("expires_at", [None, 4_102_444_800, 4_102_444_800.5])
def test_bff_access_token_accepts_each_unexpired_expiry_shape(
    expires_at: int | float | None,
) -> None:
    request = _request()
    token_set: dict[str, object] = {"access_token": "access"}
    if expires_at is not None:
        token_set["expires_at"] = expires_at
    request.state._session_server_side = True
    request.state.session = {"_wreath_bff": token_set}

    assert bff_access_token(request) == "access"


@pytest.mark.parametrize(
    "value",
    [
        1,
        "",
        "relative",
        "//api.example/v1",
        "https://api.example/v1",
        "/v1?admin=1",
        "/v1#admin",
        "/café",
        "/v1/%",
        "/v1/%2",
        "/v1/%GG",
        "/v1/%2e/admin",
        "/v1/%2E%2E/admin",
        "/v1/%5cadmin",
    ],
)
def test_target_prefix_refuses_each_invalid_shape(value: object) -> None:
    error = TypeError if not isinstance(value, str) else ValueError
    with pytest.raises(error):
        _validate_target_prefix(cast(Any, value))


@pytest.mark.parametrize("value", ["/", "/v1", "/v1/", "/v1/%20space"])
def test_target_prefix_normalizes_each_valid_shape(value: str) -> None:
    assert _validate_target_prefix(value) == value.rstrip("/")


@pytest.mark.parametrize(
    ("origin", "message"),
    [
        (None, "origin as text"),
        (1, "origin as text"),
        ("https://", "origin"),
        ("https://user@api.example", "origin"),
        ("https://user:password@api.example", "origin"),
        ("https://api.example?admin=1", "origin"),
        ("https://api.example#admin", "origin"),
        ("https://api.example:bad", "origin"),
        ("https://api.example:0", "origin"),
        (" https://api.example", "origin"),
        ("https://api.exa\tmple", "origin"),
        ("https://api.exa\x80mple", "origin"),
        ("https://api.example\\other", "origin"),
        (f"https://{'a' * 64}.example", "origin"),
    ],
)
def test_resource_configuration_refuses_each_invalid_origin(
    origin: object,
    message: str,
) -> None:
    client = type("Client", (), {"origin": origin, "request": RecordingClient.request})()
    error = TypeError if not isinstance(origin, str) else ValueError

    with pytest.raises(error, match=message):
        BFFResource(client)


@pytest.mark.parametrize("request_method", [None, 1, "request"])
def test_resource_configuration_requires_callable_request(request_method: object) -> None:
    client = type("Client", (), {"origin": "https://api.example", "request": request_method})()

    with pytest.raises(TypeError, match="async request method"):
        BFFResource(client)


@pytest.mark.parametrize("method", [None, 1, b"GET"])
def test_resource_configuration_refuses_non_text_method(method: object) -> None:
    with pytest.raises(TypeError, match="methods must be str"):
        BFFResource(RecordingClient(), methods=cast(Any, {method}))


def test_resource_configuration_refuses_a_string_as_the_method_collection() -> None:
    with pytest.raises(TypeError, match="methods must be a set of strings"):
        BFFResource(RecordingClient(), methods=cast(Any, "GET"))


@pytest.mark.parametrize("method", ["", "GET SPACE", "GET\n"])
def test_resource_configuration_refuses_invalid_method_token(method: str) -> None:
    with pytest.raises(ValueError, match="HTTP token"):
        BFFResource(RecordingClient(), methods={method})


@pytest.mark.parametrize(
    ("methods", "expected"),
    [
        ({"get", "HEAD"}, frozenset({"GET"})),
        ({"head"}, frozenset({"HEAD"})),
        ({"post"}, frozenset({"POST"})),
    ],
)
def test_resource_configuration_normalizes_methods(
    methods: set[str],
    expected: frozenset[str],
) -> None:
    assert BFFResource(RecordingClient(), methods=methods).methods == expected


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ([], TypeError),
        ((b"x-csrf",), TypeError),
        ((b"x-csrf", b"1", b"extra"), TypeError),
        (("x-csrf", b"1"), TypeError),
        ((b"x-csrf", "1"), TypeError),
        ((b"x-\xff", b"1"), ValueError),
        ((b"x csrf", b"1"), ValueError),
        ((b"x-csrf", b""), ValueError),
        ((b"x-csrf", b"one\rtwo"), ValueError),
        ((b"x-csrf", b"one\ntwo"), ValueError),
        ((b"content-type", b"1"), ValueError),
    ],
)
def test_csrf_header_refuses_each_invalid_shape(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        _csrf_header(cast(Any, value))


def test_csrf_header_refuses_list_even_when_it_has_two_bytes_values() -> None:
    with pytest.raises(TypeError, match="one .* bytes tuple"):
        _csrf_header(cast(Any, [b"x-csrf", b"1"]))


def test_csrf_header_names_the_non_bytes_value() -> None:
    with pytest.raises(TypeError, match="name and value must be bytes"):
        _csrf_header(cast(Any, (b"x-csrf", "1")))


def test_csrf_header_normalizes_valid_name() -> None:
    assert _csrf_header((b"X-BFF-CSRF", b"yes")) == (b"x-bff-csrf", b"yes")


def test_empty_query_has_no_suffix() -> None:
    assert _query_suffix(b"") == ""


@pytest.mark.parametrize("path", ["/other.example/admin", "//other.example/admin"])
def test_root_resource_target_refuses_a_path_that_would_become_scheme_relative(path: str) -> None:
    resource = BFFResource(RecordingClient(), target_prefix="/")

    with pytest.raises(BadRequest, match="must not begin with a slash"):
        _target(resource, path, b"")


@pytest.mark.parametrize("resources", [None, [], {}, "catalog"])
def test_bff_router_requires_nonempty_resource_mapping(resources: object) -> None:
    with pytest.raises(ValueError, match="map at least one"):
        bff_router(cast(Any, resources))


def test_bff_router_refuses_non_resource_value() -> None:
    with pytest.raises(TypeError, match=r"resources\['catalog'\] must be BFFResource"):
        bff_router({"catalog": cast(Any, RecordingClient())})


@pytest.mark.parametrize("token", [None, 1, "token"])
def test_bff_router_requires_callable_token_resolver(token: object) -> None:
    with pytest.raises(TypeError, match="callable BFF token resolver"):
        bff_router({"catalog": BFFResource(RecordingClient())}, token=cast(Any, token))


async def test_bff_router_awaits_async_token_resolver() -> None:
    async def token(_request: Request) -> str:
        return "access"

    app = Wreath()
    app.include_router(bff_router({"catalog": BFFResource(RecordingClient())}, token=token))

    response = await TestClient(app).get("/bff/session")

    assert response.json() == {"active": True}


async def test_bff_router_awaits_value_from_sync_token_resolver() -> None:
    async def resolved() -> str:
        return "access"

    def token(_request: Request) -> Any:
        return resolved()

    app = Wreath()
    app.include_router(bff_router({"catalog": BFFResource(RecordingClient())}, token=token))

    response = await TestClient(app).get("/bff/session")

    assert response.json() == {"active": True}


async def test_logout_handler_clears_and_rotates_session_directly() -> None:
    router = bff_router({"catalog": BFFResource(RecordingClient())})
    logout = next(route.endpoint for route in router.routes if route.path == "/bff/logout")
    request = _request(headers=[(b"x-wreath-bff", b"1")])
    request.state.session = {"private": "value"}

    response = await logout(request)

    assert response.status == 204
    assert request.state.session == {}
    assert request.state._session_rotate is True


@pytest.mark.parametrize("name", (b"content-type", b"idempotency-key"))
async def test_duplicate_singleton_headers_never_reach_a_credentialed_backend(
    name: bytes,
) -> None:
    client = RecordingClient()
    router = bff_router(
        {"catalog": BFFResource(client, methods={"POST"})},
        token=lambda _request: "access-token",
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/bff/catalog")
    request = _request(
        headers=[
            (b"x-wreath-bff", b"1"),
            (name, b"first"),
            (name, b"second"),
        ]
    )
    request.scope["method"] = "POST"

    with pytest.raises(BadRequest, match="more than once"):
        await endpoint(request)

    assert client.calls == []


async def test_repeatable_accept_fields_are_forwarded_without_collapsing() -> None:
    client = RecordingClient()
    router = bff_router(
        {"catalog": BFFResource(client, methods={"GET"})},
        token=lambda _request: "access-token",
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/bff/catalog")
    request = _request(
        headers=[
            (b"x-wreath-bff", b"1"),
            (b"accept", b"application/json"),
            (b"accept", b"application/problem+json"),
        ]
    )
    request.scope["method"] = "GET"

    await endpoint(request)

    forwarded = client.calls[0][2]
    assert forwarded.count((b"accept", b"application/json")) == 1
    assert forwarded.count((b"accept", b"application/problem+json")) == 1


def test_tokens_cannot_be_put_in_or_read_from_a_client_side_session() -> None:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "headers": []}, receive)
    request.state.session = {}
    request.state._session_server_side = False

    with pytest.raises(RuntimeError, match="server-side BFF session"):
        set_bff_tokens(request, access_token="must-not-enter-a-cookie")
    request.state.session["_wreath_bff"] = {"access_token": "must-not-leave-a-cookie"}
    assert bff_access_token(request) is None


@pytest.mark.parametrize("expires_at", [float("inf"), float("nan")])
def test_token_expiry_must_be_a_finite_timestamp(expires_at: float) -> None:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "headers": []}, receive)
    request.state.session = {}
    request.state._session_server_side = True

    with pytest.raises(ValueError, match="finite timestamp"):
        set_bff_tokens(request, access_token="access-token", expires_at=expires_at)


@pytest.mark.parametrize(
    "raw",
    [
        b"filter=%2",
        b"filter=public#private",
        b"filter=public\nprivate",
        b"filter=public private",
        b"filter=public\x7fprivate",
    ],
)
async def test_query_suffix_refuses_each_unsafe_query_shape(raw: bytes) -> None:
    with pytest.raises(BadRequest) as caught:
        _query_suffix(raw)
    assert str(caught.value) == (
        "BFF query strings must use valid percent escapes and contain no controls"
    )


@pytest.mark.parametrize(
    ("client", "target_prefix", "methods", "message"),
    [
        (type("Client", (), {"origin": "http://api.example"})(), "/", {"GET"}, "HTTPS"),
        (
            type("Client", (), {"origin": "https://api.example/path"})(),
            "/",
            {"GET"},
            "origin",
        ),
        (RecordingClient(), "https://elsewhere.example/v1", {"GET"}, "origin-relative"),
        (RecordingClient(), "/v1/../admin", {"GET"}, "dot segments"),
        (RecordingClient(), "/v1", {"CONNECT"}, "CONNECT"),
        (RecordingClient(), "/v1", set(), "at least one"),
    ],
)
def test_resource_configuration_refuses_an_unsafe_proxy(
    client: Any,
    target_prefix: str,
    methods: set[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        BFFResource(client, target_prefix=target_prefix, methods=methods)


async def test_proxy_keeps_tokens_server_side_and_filters_both_header_directions() -> None:
    upstream = RecordingClient()
    app, store = _app(upstream)
    browser = TestClient(app)
    login = await browser.post("/oauth/callback")
    cookie = _cookie(login)

    assert login.header("set-cookie") == (
        f"{cookie}; Max-Age=1209600; Path=/; Secure; HttpOnly; SameSite=Strict"
    )
    assert b"access-token" not in login.body
    assert b"refresh-token" not in login.body
    assert list(store.rows.values()) == [
        {
            "_wreath_bff": {
                "access_token": "access-token",
                "expires_at": 4_102_444_800,
                "refresh_token": "refresh-token",
            }
        }
    ]

    response = await browser.post(
        "/bff/catalog/widgets/blue?expand=owner",
        headers={
            "cookie": cookie,
            "authorization": "Bearer browser-controlled",
            "x-wreath-bff": "1",
            "content-type": "application/json",
            "x-browser-private": "drop-me",
        },
        content=b'{"name":"blue"}',
    )

    assert response.status == 201
    assert response.body == b'{"created":true}'
    assert response.header("etag") == '"upstream"'
    assert response.header("set-cookie") is None
    assert response.header("connection") is None
    assert response.header("x-private") is None
    assert upstream.calls == [
        (
            "POST",
            "/v2/widgets/blue?expand=owner",
            (
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer access-token"),
            ),
            b'{"name":"blue"}',
        )
    ]


async def test_proxy_requires_the_non_simple_csrf_header_before_reading_the_session() -> None:
    upstream = RecordingClient()
    app, _store = _app(upstream)

    response = await TestClient(app).get("/bff/catalog/widgets")

    assert response.status == 403
    assert upstream.calls == []


async def test_proxy_requires_an_active_bff_session() -> None:
    upstream = RecordingClient()
    app, _store = _app(upstream)

    response = await TestClient(app).get("/bff/catalog/widgets", headers={"x-wreath-bff": "1"})

    assert response.status == 401
    assert response.header("www-authenticate") == "BFF"
    assert upstream.calls == []


async def test_static_resource_routes_make_unknown_resources_404_and_methods_405() -> None:
    upstream = RecordingClient()
    app, _store = _app(upstream)
    browser = TestClient(app)

    unknown = await browser.get("/bff/unknown/widgets", headers={"x-wreath-bff": "1"})
    disallowed = await browser.delete("/bff/catalog/widgets", headers={"x-wreath-bff": "1"})

    assert unknown.status == 404
    assert disallowed.status == 405
    assert disallowed.header("allow") == "GET, POST, HEAD"


async def test_proxy_refuses_path_traversal_before_the_outbound_call() -> None:
    upstream = RecordingClient()
    app, _store = _app(upstream)
    browser = TestClient(app)
    cookie = _cookie(await browser.post("/oauth/callback"))

    response = await browser.get(
        "/bff/catalog/../admin",
        headers={"cookie": cookie, "x-wreath-bff": "1"},
    )

    assert response.status == 400
    assert upstream.calls == []


async def test_session_status_and_logout_never_return_tokens_and_revoke_the_session() -> None:
    upstream = RecordingClient()
    app, store = _app(upstream)
    browser = TestClient(app)
    cookie = _cookie(await browser.post("/oauth/callback"))

    status = await browser.get("/bff/session", headers={"cookie": cookie})
    logout = await browser.post(
        "/bff/logout",
        headers={"cookie": cookie, "x-wreath-bff": "1"},
    )

    assert status.json() == {"active": True}
    assert b"token" not in status.body
    assert logout.status == 204
    assert "Max-Age=0" in (logout.header("set-cookie") or "")
    assert store.rows == {}
