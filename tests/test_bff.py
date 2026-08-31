from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wreath import Wreath
from wreath.bff import (
    BFFResource,
    bff_access_token,
    bff_router,
    bff_session_policy,
    set_bff_tokens,
)
from wreath.policy import HttpPolicy
from wreath.request import Request
from wreath.testing import TestClient

pytestmark = pytest.mark.asyncio


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
