"""Attacker-shaped regressions at HTTP, session, and WebSocket boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wreath import Wreath
from wreath._userkit import InMemoryUserStore
from wreath.auth import (
    BearerTokenBackend,
    Identity,
    SessionIdentityBackend,
    authenticated,
)
from wreath.middleware.compression import CompressionMiddleware
from wreath.middleware.security import TrustedHostMiddleware
from wreath.middleware.sessions import SessionMiddleware
from wreath.testing import TestClient
from wreath.users import user_router


class MemorySessions:
    """Small server-side session-store twin; copies like a database boundary."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    async def load(self, sid: str) -> dict[str, Any] | None:
        row = self.rows.get(sid)
        return None if row is None else dict(row)

    async def save(self, sid: str, data: dict[str, Any], _max_age: int) -> None:
        self.rows[sid] = dict(data)

    async def delete(self, sid: str) -> None:
        self.deleted.append(sid)
        self.rows.pop(sid, None)


async def _asgi_http(
    app: Wreath,
    path: str,
    *,
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "GET",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode(),
        "query_string": b"",
        "headers": headers or [(b"host", b"app.example")],
        "server": ("app.example", 443),
        "client": ("203.0.113.10", 40000),
        "root_path": "",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _cookie(response: Any) -> str:
    value = response.header("set-cookie")
    assert value is not None
    return value.split(";", 1)[0]


async def test_builtin_login_rotates_a_server_side_session(monkeypatch) -> None:
    """A planted anonymous SID must not become the victim's authenticated SID."""
    import wreath.users as users

    sessions = MemorySessions()
    app = Wreath()
    app.add_global_middleware(
        SessionMiddleware(secret="s" * 32, store=sessions, secure=False)
    )
    app.include_router(user_router(InMemoryUserStore(), secret="u" * 32))

    @app.get("/seed")
    async def seed(request: Any) -> dict[str, bool]:
        request.state.session["attacker_marker"] = True
        return {"ok": True}

    @app.get("/principal")
    async def principal(request: Any) -> dict[str, Any]:
        return request.state.session

    async def authenticate(_store: Any, email: str, _password: str) -> Any:
        return SimpleNamespace(
            id="victim-id", email=email, is_verified=True, is_active=True
        )

    monkeypatch.setattr(users._userkit, "authenticate", authenticate)
    async with TestClient(app) as client:
        planted = _cookie(await client.get("/seed"))
        login = await client.post(
            "/users/login",
            json={"email": "victim@example.test", "password": "irrelevant"},
            headers={"cookie": planted},
        )
        assert login.status == 200
        rotated = _cookie(login)
        assert rotated != planted
        replay = await client.get("/principal", headers={"cookie": planted})
        assert "principal" not in replay.json()
        victim = await client.get("/principal", headers={"cookie": rotated})
        assert victim.json()["principal"]["sub"] == "victim-id"
    assert sessions.deleted


async def test_websocket_honours_trusted_host_middleware() -> None:
    app = Wreath()
    app.add_middleware(TrustedHostMiddleware(["good.example"]))

    @app.websocket("/ws")
    async def socket(websocket: Any) -> None:
        await websocket.accept()

    async with TestClient(app) as client:
        with pytest.raises(ConnectionError):
            async with client.websocket(
                "/ws", headers={"host": "evil.example", "origin": "https://evil.example"}
            ):
                pass


async def test_cookie_authenticated_websocket_requires_an_allowed_origin() -> None:
    from wreath.middleware.security import WebSocketOriginMiddleware

    app = Wreath()
    app.add_middleware(WebSocketOriginMiddleware(["https://app.example"]))

    @app.websocket("/ws")
    async def socket(websocket: Any) -> None:
        await websocket.accept()

    async with TestClient(app) as client:
        with pytest.raises(ConnectionError):
            async with client.websocket(
                "/ws", headers={"host": "app.example", "origin": "https://evil.example"}
            ):
                pass
        async with client.websocket(
            "/ws", headers={"host": "app.example", "origin": "https://app.example"}
        ):
            pass


async def test_websocket_authentication_can_load_the_global_session() -> None:
    sessions = MemorySessions()
    app = Wreath()
    app.add_global_middleware(
        SessionMiddleware(secret="s" * 32, store=sessions, secure=False)
    )
    app.configure_auth(SessionIdentityBackend())

    @app.get("/login")
    async def login(request: Any) -> dict[str, bool]:
        request.state.session["principal"] = {
            "sub": "u1",
            "type": "User",
            "roles": [],
        }
        return {"ok": True}

    @app.websocket("/ws")
    @authenticated()
    async def socket(websocket: Any) -> None:
        await websocket.accept()
        await websocket.send_text(websocket.identity.id)

    async with TestClient(app) as client:
        cookie = _cookie(await client.get("/login"))
        async with client.websocket(
            "/ws",
            headers={
                "host": "app.example",
                "origin": "https://app.example",
                "cookie": cookie,
            },
        ) as websocket:
            assert await websocket.receive_text() == "u1"


async def test_duplicate_host_headers_are_rejected_by_trusted_host() -> None:
    app = Wreath()
    app.add_middleware(TrustedHostMiddleware(["app.example"]))

    @app.get("/private")
    async def private(request: Any) -> dict[str, bool]:
        return {"ok": True}

    sent = await _asgi_http(
        app,
        "/private",
        headers=[(b"host", b"app.example"), (b"host", b"evil.example")],
    )
    assert sent[0]["status"] == 400


async def test_split_cookie_headers_are_combined_without_losing_values() -> None:
    app = Wreath()

    @app.get("/cookies")
    async def cookies(request: Any) -> dict[str, str]:
        return request.cookies

    sent = await _asgi_http(
        app,
        "/cookies",
        headers=[
            (b"host", b"app.example"),
            (b"cookie", b"session=one"),
            (b"cookie", b"csrf=two"),
        ],
    )
    body = sent[1]["body"]
    assert b'"session":"one"' in body
    assert b'"csrf":"two"' in body


async def test_duplicate_authorization_headers_are_rejected() -> None:
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(
            lambda token: Identity(id="u1") if token == "good" else None
        )
    )

    @app.get("/private")
    @authenticated()
    async def private(request: Any) -> dict[str, bool]:
        return {"ok": True}

    sent = await _asgi_http(
        app,
        "/private",
        headers=[
            (b"host", b"app.example"),
            (b"authorization", b"Bearer good"),
            (b"authorization", b"Bearer bad"),
        ],
    )
    assert sent[0]["status"] == 401


async def test_encoded_path_separators_are_rejected_before_routing() -> None:
    app = Wreath()

    @app.get("/admin/panel")
    async def admin(request: Any) -> dict[str, bool]:
        return {"sensitive": True}

    sent = await _asgi_http(app, "/admin/panel", raw_path=b"/admin%2fpanel")
    assert sent[0]["status"] == 400


async def test_authenticated_responses_are_not_compressed_by_default() -> None:
    app = Wreath()
    app.configure_auth(BearerTokenBackend(lambda _token: Identity(id="u1")))
    app.add_middleware(CompressionMiddleware(minimum_size=0))

    @app.get("/secret")
    @authenticated()
    async def secret(request: Any) -> dict[str, str]:
        return {"secret": "token-123", "reflection": "A" * 100}

    async with TestClient(app) as client:
        response = await client.get(
            "/secret",
            headers={"authorization": "Bearer x", "accept-encoding": "gzip"},
        )
    assert response.header("content-encoding") is None


async def test_password_reset_email_issuance_is_bounded(monkeypatch) -> None:
    import wreath.users as users

    sent = 0

    async def start_reset(*_args: Any, **_kwargs: Any) -> None:
        nonlocal sent
        sent += 1

    monkeypatch.setattr(users._userkit, "start_password_reset", start_reset)
    app = Wreath()
    app.include_router(
        user_router(
            InMemoryUserStore(),
            secret="s" * 32,
            max_reset_requests=3,
            reset_window=60.0,
        )
    )
    async with TestClient(app) as client:
        for _ in range(5):
            response = await client.post(
                "/users/forgot-password", json={"email": "victim@example.test"}
            )
            assert response.status == 200
            assert response.json() == {"status": "reset_email_sent"}
    assert sent == 3
