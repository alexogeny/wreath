from __future__ import annotations

import ast
import pickle
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import pytest

from wreath import Wreath
from wreath._auth.decorators import roles
from wreath._userkit import InMemoryUserStore
from wreath.auth import (
    BearerTokenBackend,
    Identity,
    SessionIdentityBackend,
    authenticated,
)
from wreath.binding import File, Form
from wreath.policy import HttpPolicy, TrustedHostPolicy, WebSocketOriginPolicy
from wreath.policy.compression import CompressionPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.templates import Template, TemplateSyntaxError
from wreath.testing import TestClient
from wreath.users import user_router


def _write_pickle_canary(path: str) -> None:
    Path(path).write_text("executed", encoding="utf-8")


class _PickleGadget:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __reduce__(self) -> tuple[Any, tuple[str]]:
        return _write_pickle_canary, (str(self.path),)


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

    async def delete_for(self, _subject: str) -> int:
        return 0


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


def test_a_validly_signed_pickle_gadget_is_never_deserialized(tmp_path: Path) -> None:
    canary = tmp_path / "pickle-rce"
    payload = pickle.dumps(_PickleGadget(canary))
    middleware = SessionPolicy(secret="s" * 32, secure=False)
    cookie = middleware._sign(payload, int(time.time()))

    assert middleware._load(cookie) is None
    assert not canary.exists()


def test_template_source_cannot_call_or_construct_python(tmp_path: Path) -> None:
    canary = tmp_path / "template-rce"
    sources = (
        "{{ __import__('pathlib').Path(CANARY).write_text('executed') }}".replace(
            "CANARY", repr(str(canary))
        ),
        "{{ gadget.run() }}",
        "{{ ().__class__.__bases__ }}",
        "{% if __import__('os') %}executed{% endif %}",
    )
    for source in sources:
        with pytest.raises(TemplateSyntaxError):
            Template.from_string(source)
    assert not canary.exists()


def test_runtime_modules_introduce_no_unsafe_deserializer_or_shell_sink() -> None:
    source_root = Path(__file__).parents[2] / "src" / "wreath"
    # Paths, not basenames: `cli.py` now appears in five packages, and matching
    # on the name alone would excuse every one of them. Each entry names the one
    # file it means, relative to `src/wreath`.
    tooling = {Path("_cli.py"), Path("_devserver.py"), Path("infra/cli.py")}
    excluded_directories = {"_devtools", "_docs", "_mutant", "_port", "typegen"}
    dangerous_imports = {"dill", "marshal", "pickle", "shelve"}
    # Every finding below names one of these in source, so a file containing
    # none of them cannot produce one. Reading the tree costs 22 ms and parsing
    # it 1.9 s, so the cheap half answers first for the files it can settle --
    # and it settles most of them. Sound rather than heuristic: an `ast.Import`
    # of `pickle`, a call to `os.popen`, or a `shell=True` keyword each require
    # their own spelling to be present, so nothing skipped here could have been
    # a finding. Over-eager the harmless way: the word in a comment buys one
    # needless parse, never a missed sink.
    tokens = (*dangerous_imports, "popen", "system", "shell")
    findings: list[str] = []

    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root)
        if relative in tooling or any(part in excluded_directories for part in relative.parts):
            continue
        source = path.read_text(encoding="utf-8")
        if not any(token in source for token in tokens):
            continue
        tree = ast.parse(source, filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in dangerous_imports:
                        findings.append(f"{relative}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".", 1)[0]
                if module in dangerous_imports:
                    findings.append(f"{relative}:{node.lineno}: from {node.module} import")
            elif isinstance(node, ast.Call):
                function = node.func
                if (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "os"
                    and function.attr in {"popen", "system"}
                ):
                    findings.append(f"{relative}:{node.lineno}: os.{function.attr}")
                for keyword in node.keywords:
                    if (
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        findings.append(f"{relative}:{node.lineno}: shell=True")

    assert findings == []


async def test_builtin_login_rotates_a_server_side_session(monkeypatch) -> None:
    import wreath.users as users

    sessions = MemorySessions()
    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(session=SessionPolicy(secret="s" * 32, store=sessions, secure=False))
    )
    app.include_router(
        user_router(InMemoryUserStore(), sessions=sessions, secret="u" * 32)
    )

    @app.get("/seed")
    async def seed(request: Any) -> dict[str, bool]:
        request.state.session["attacker_marker"] = True
        return {"ok": True}

    @app.get("/principal")
    async def principal(request: Any) -> dict[str, Any]:
        return request.state.session

    async def authenticate(_store: Any, email: str, _password: str) -> Any:
        return SimpleNamespace(id="victim-id", email=email, is_verified=True, is_active=True)

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
    app = Wreath(http_policy=HttpPolicy(trusted_host=TrustedHostPolicy(["good.example"])))

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
    app = Wreath(
        http_policy=HttpPolicy(websocket_origin=WebSocketOriginPolicy(["https://app.example"]))
    )

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
    app.configure_http_policy(
        HttpPolicy(session=SessionPolicy(secret="s" * 32, store=sessions, secure=False))
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
        cookie = _cookie(await client.get("/login", headers={"host": "app.example"}))
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
    app = Wreath(http_policy=HttpPolicy(trusted_host=TrustedHostPolicy(["app.example"])))

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
        BearerTokenBackend(lambda token: Identity(id="u1") if token == "good" else None)
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
    app.configure_http_policy(HttpPolicy(compression=CompressionPolicy(minimum_size=0)))

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
    sessions = MemorySessions()
    app.include_router(
        user_router(
            InMemoryUserStore(),
            sessions=sessions,
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


async def test_encoded_nul_in_a_static_path_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    app = Wreath()
    app.static("/assets", str(root))

    for path in ("/assets/\x00", "/assets/a\x00.txt", "/assets/\x00/a.txt"):
        sent = await _asgi_http(app, path)
        assert sent[0]["status"] == 404, path


async def test_malformed_multipart_body_is_refused_with_400(tmp_path: Path) -> None:
    app = Wreath()

    @app.post("/upload")
    async def upload(
        request: Any,
        caption: Annotated[str, Form()],
        photo: Annotated[Any, File()],
    ) -> dict[str, str]:
        return {"caption": caption}

    bodies = [
        ("multipart/form-data", b"--x\r\n\r\nv\r\n--x--\r\n"),
        ("multipart/form-data; boundary=x", b""),
        ("multipart/form-data; boundary=x", b"--x\r\nno-colon\r\n\r\nv\r\n--x--\r\n"),
        (
            "multipart/form-data; boundary=x",
            b"--x\r\ncontent-disposition: form-data\r\n\r\nv\r\n",
        ),
    ]
    async with TestClient(app) as client:
        for content_type, body in bodies:
            response = await client.post(
                "/upload", content=body, headers={"content-type": content_type}
            )
            assert response.status == 400, (content_type, body)


async def test_role_check_accepts_any_collection_of_role_names() -> None:
    app = Wreath()
    app.configure_auth(
        backend=BearerTokenBackend(
            lambda token: (
                Identity(id="1", roles=("admin", "user"))  # type: ignore[arg-type]
                if token == "admin"
                else None
            )
        )
    )

    @app.get("/all")
    @roles("admin")
    async def all_mode(request: Any) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/any")
    @roles("admin", "other", mode="any")
    async def any_mode(request: Any) -> dict[str, bool]:
        return {"ok": True}

    async with TestClient(app) as client:
        headers = {"authorization": "Bearer admin"}
        assert (await client.get("/any", headers=headers)).status == 200
        assert (await client.get("/all", headers=headers)).status == 200
