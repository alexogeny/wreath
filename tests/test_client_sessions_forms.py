from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._json import dumps as _json_dumps
from wreath.policy import HttpPolicy
from wreath.policy.sessions import SessionPolicy
from wreath.response import TextResponse
from wreath.testing import TestClient
from wreath.websocket import WebSocket


@pytest.mark.asyncio
async def test_client_get_json_and_params() -> None:
    app = Wreath()

    @app.get("/items/{item_id}")
    async def get_item(request: Any, item_id: int, q: str = "") -> Any:
        return {"id": item_id, "q": q}

    client = TestClient(app)
    response = await client.get("/items/5", params={"q": "bolt"})
    assert response.status == 200
    assert response.header("content-type") == "application/json"
    assert response.json() == {"id": 5, "q": "bolt"}


@pytest.mark.asyncio
async def test_client_post_json_body() -> None:
    app = Wreath()

    @app.post("/echo")
    async def echo(request: Any) -> Any:
        return await request.json()

    client = TestClient(app)
    response = await client.post("/echo", json={"a": [1, 2]})
    assert response.json() == {"a": [1, 2]}


@pytest.mark.asyncio
async def test_client_lifespan_context() -> None:
    app = Wreath()
    events: list[str] = []

    @app.on_startup
    async def up(application: Wreath) -> None:
        events.append("up")

    @app.on_shutdown
    async def down(application: Wreath) -> None:
        events.append("down")

    @app.get("/")
    async def home(request: Any) -> Any:
        return TextResponse("ok")

    async with TestClient(app) as client:
        assert (await client.get("/")).text == "ok"
        assert events == ["up"]
    assert events == ["up", "down"]


@pytest.mark.asyncio
async def test_client_websocket_session() -> None:
    app = Wreath()

    @app.websocket("/ws")
    async def echo_ws(ws: WebSocket) -> None:
        await ws.accept()
        async for message in ws:
            await ws.send(message)

    client = TestClient(app)
    async with client.websocket("/ws") as ws:
        await ws.send_text("hello")
        assert await ws.receive_text() == "hello"
        await ws.send_bytes(b"\x01\x02")
        assert await ws.receive_bytes() == b"\x01\x02"


@pytest.mark.asyncio
async def test_client_websocket_rejection() -> None:
    app = Wreath()  # no websocket routes registered

    client = TestClient(app)
    with pytest.raises(ConnectionError):
        async with client.websocket("/nope"):
            pass


@pytest.mark.asyncio
async def test_session_roundtrip_and_tamper_resistance() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="test-secret" * 4)))

    @app.get("/visit")
    async def visit(request: Any) -> Any:
        count = request.state.session.get("count", 0) + 1
        request.state.session["count"] = count
        return {"visits": count}

    client = TestClient(app)
    first = await client.get("/visit")
    assert first.json() == {"visits": 1}
    cookie = first.header("set-cookie")
    assert cookie is not None and cookie.startswith("wreath_session=")
    token = cookie.split(";")[0].split("=", 1)[1]

    second = await client.get("/visit", headers={"cookie": f"wreath_session={token}"})
    assert second.json() == {"visits": 2}

    # Tampered signature: treated as a fresh session.
    tampered = token[:-4] + "0000"
    third = await client.get("/visit", headers={"cookie": f"wreath_session={tampered}"})
    assert third.json() == {"visits": 1}


@pytest.mark.asyncio
async def test_session_unchanged_sets_no_cookie() -> None:
    app = Wreath()
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32)))

    @app.get("/read")
    async def read(request: Any) -> Any:
        return {"count": request.state.session.get("count", 0)}

    client = TestClient(app)
    response = await client.get("/read")
    assert response.header("set-cookie") is None


@pytest.mark.asyncio
async def test_session_cleared_deletes_cookie() -> None:
    app = Wreath()
    middleware = SessionPolicy(secret="s" * 32)
    app.configure_http_policy(HttpPolicy(session=middleware))

    @app.get("/logout")
    async def logout(request: Any) -> Any:
        request.state.session.clear()
        return {"ok": True}

    token = middleware._sign(b'{"user":"a"}', 2_000_000_000)

    client = TestClient(app)
    response = await client.get("/logout", headers={"cookie": f"wreath_session={token}"})
    cookie = response.header("set-cookie")
    assert cookie is not None and "Max-Age=0" in cookie


@pytest.mark.asyncio
async def test_legacy_unbound_session_is_loaded_and_reissued_for_this_host() -> None:
    app = Wreath()
    middleware = SessionPolicy(secret="s" * 32)
    app.configure_http_policy(HttpPolicy(session=middleware))

    @app.get("/read")
    async def read(request: Any) -> Any:
        return {"user": request.state.session.get("user")}

    token = middleware._sign(_json_dumps({"user": "ada", "n": 3}), 2_000_000_000)

    client = TestClient(app)
    response = await client.get("/read", headers={"cookie": f"wreath_session={token}"})
    assert response.json() == {"user": "ada"}
    assert (response.header("set-cookie") or "").startswith("wreath_session=")


@pytest.mark.asyncio
async def test_session_mutation_still_reissues_the_cookie() -> None:
    app = Wreath()
    middleware = SessionPolicy(secret="s" * 32)
    app.configure_http_policy(HttpPolicy(session=middleware))

    @app.get("/bump")
    async def bump(request: Any) -> Any:
        request.state.session["n"] = request.state.session.get("n", 0) + 1
        return {"n": request.state.session["n"]}

    token = middleware._sign(_json_dumps({"user": "ada", "n": 3}), 2_000_000_000)

    client = TestClient(app)
    response = await client.get("/bump", headers={"cookie": f"wreath_session={token}"})
    assert response.json() == {"n": 4}
    assert (response.header("set-cookie") or "").startswith("wreath_session=")


@pytest.mark.asyncio
async def test_session_payload_that_does_not_round_trip_is_reissued() -> None:
    app = Wreath()
    middleware = SessionPolicy(secret="s" * 32)
    app.configure_http_policy(HttpPolicy(session=middleware))

    @app.get("/read")
    async def read(request: Any) -> Any:
        return {"user": request.state.session.get("user")}

    token = middleware._sign(b'{"user": "ada"}', 2_000_000_000)  # note the space

    client = TestClient(app)
    response = await client.get("/read", headers={"cookie": f"wreath_session={token}"})
    # The session still reads correctly ...
    assert response.json() == {"user": "ada"}
    # ... and is rewritten in wreath's own encoding rather than silently kept.
    cookie = response.header("set-cookie")
    assert cookie is not None and cookie.startswith("wreath_session=")


@pytest.mark.asyncio
async def test_absent_and_rejected_sessions_both_write_nothing() -> None:
    app = Wreath()
    middleware = SessionPolicy(secret="s" * 32)
    app.configure_http_policy(HttpPolicy(session=middleware))

    @app.get("/read")
    async def read(request: Any) -> Any:
        return {"empty": not request.state.session}

    client = TestClient(app)
    absent = await client.get("/read")
    assert absent.json() == {"empty": True}
    assert absent.header("set-cookie") is None

    forged = await client.get("/read", headers={"cookie": "wreath_session=a.b.c"})
    assert forged.json() == {"empty": True}
    assert forged.header("set-cookie") is None


@pytest.mark.asyncio
async def test_urlencoded_form() -> None:
    app = Wreath()

    @app.post("/login")
    async def login(request: Any) -> Any:
        form = await request.form()
        return {"user": form["user"], "next": form.get("next", "/")}

    client = TestClient(app)
    response = await client.post(
        "/login",
        content=b"user=andie&password=pw",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.json() == {"user": "andie", "next": "/"}


@pytest.mark.asyncio
async def test_multipart_form_with_file() -> None:
    app = Wreath()

    @app.post("/upload")
    async def upload(request: Any) -> Any:
        form = await request.form()
        upload = form.files["avatar"]
        return {
            "field": form["title"],
            "filename": upload.filename,
            "size": len(upload.data),
            "content_type": upload.content_type,
        }

    body = (
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="title"\r\n\r\n'
        b"holiday photo\r\n"
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="avatar"; filename="me.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        b"PNGDATA\r\n"
        b"--boundary123--\r\n"
    )
    client = TestClient(app)
    response = await client.post(
        "/upload",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=boundary123"},
    )
    assert response.json() == {
        "field": "holiday photo",
        "filename": "me.png",
        "size": 7,
        "content_type": "image/png",
    }
