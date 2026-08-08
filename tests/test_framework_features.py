"""First-party framework feature tests: responses, cookies, background,
CORS, and static files — exercised through the ASGI interface."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from wreath import Response, Wreath
from wreath.policy import CorsPolicy, HttpPolicy
from wreath.response import FileResponse, HTMLResponse, RedirectResponse, TextResponse


def http_scope(path: str = "/", method: str = "GET", headers: list | None = None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
    }


async def call(app: Wreath, scope: dict) -> tuple[int, dict[bytes, list[bytes]], bytes]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = sent[0]["status"]
    headers: dict[bytes, list[bytes]] = {}
    for name, value in sent[0]["headers"]:
        headers.setdefault(name, []).append(value)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, headers, body


# --- response types -------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_response() -> None:
    app = Wreath()

    @app.get("/")
    async def home(request: Any) -> Any:
        return HTMLResponse("<h1>hi</h1>")

    status, headers, body = await call(app, http_scope())
    assert status == 200
    assert headers[b"content-type"] == [b"text/html; charset=utf-8"]
    assert body == b"<h1>hi</h1>"


@pytest.mark.asyncio
async def test_redirect_response() -> None:
    app = Wreath()

    @app.get("/old")
    async def old(request: Any) -> Any:
        return RedirectResponse("/new location", status=301)

    status, headers, _ = await call(app, http_scope("/old"))
    assert status == 301
    assert headers[b"location"] == [b"/new%20location"]


@pytest.mark.asyncio
async def test_file_response(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_bytes(b"file contents")
    app = Wreath()

    @app.get("/file")
    async def file(request: Any) -> Any:
        return FileResponse(target)

    status, headers, body = await call(app, http_scope("/file"))
    assert status == 200
    assert headers[b"content-type"] == [b"text/plain"]
    assert headers[b"content-length"] == [b"13"]
    assert body == b"file contents"


# --- cookies --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_cookies_and_set_cookie() -> None:
    app = Wreath()

    @app.get("/")
    async def home(request: Any) -> Any:
        response = TextResponse(request.cookies.get("session", "none"))
        response.set_cookie("seen", "1", max_age=60, httponly=True)
        return response

    status, headers, body = await call(
        app, http_scope(headers=[(b"cookie", b"session=abc; other=x")])
    )
    assert status == 200
    assert body == b"abc"
    cookie = headers[b"set-cookie"][0]
    assert cookie.startswith(b"seen=1; Max-Age=60; Path=/")
    assert b"HttpOnly" in cookie and b"SameSite=Lax" in cookie


@pytest.mark.asyncio
async def test_delete_cookie() -> None:
    response = Response(b"")
    response.delete_cookie("session")
    cookie = dict((k, v) for k, v in response.headers)[b"set-cookie"]
    assert b"Max-Age=0" in cookie and b"Expires=Thu, 01 Jan 1970" in cookie


def test_samesite_none_requires_secure() -> None:
    # RFC 6265bis 5.4.7: SameSite=None cookies are dropped by browsers unless Secure.
    with pytest.raises(ValueError, match="Secure"):
        Response(b"").set_cookie("sid", "1", samesite="none")
    ok = Response(b"")
    ok.set_cookie("sid", "1", samesite="none", secure=True)
    assert b"SameSite=None" in dict(ok.headers)[b"set-cookie"]


def test_samesite_value_is_validated() -> None:
    with pytest.raises(ValueError, match="samesite must be"):
        Response(b"").set_cookie("sid", "1", samesite="strictish")


def test_host_and_secure_cookie_prefixes_are_enforced() -> None:
    # RFC 6265bis 4.1.3.
    with pytest.raises(ValueError, match="__Secure-"):
        Response(b"").set_cookie("__Secure-sid", "1")  # secure defaults False
    with pytest.raises(ValueError, match="__Host-"):
        Response(b"").set_cookie("__Host-sid", "1", secure=True, domain="example.com")
    ok = Response(b"")
    ok.set_cookie("__Host-sid", "1", secure=True)  # Path=/ and no Domain by default
    assert dict(ok.headers)[b"set-cookie"].startswith(b"__Host-sid=1")
    # A prefixed cookie can still be deleted (delete carries Secure for it).
    deleted = Response(b"")
    deleted.delete_cookie("__Host-sid")
    assert b"Secure" in dict(deleted.headers)[b"set-cookie"]


def test_cookie_value_rejects_control_characters() -> None:
    # A CR/LF in a cookie value is header injection; caught at the call (RFC 6265).
    with pytest.raises(ValueError, match="control character"):
        Response(b"").set_cookie("sid", "abc\r\nSet-Cookie: evil=1")
    with pytest.raises(ValueError, match="control character"):
        Response(b"").set_cookie("sid\n", "ok")


def test_method_not_allowed_carries_allow_header() -> None:
    # RFC 9110 15.5.6: a 405 MUST carry Allow.
    from wreath.exceptions import MethodNotAllowed, TooManyRequests

    exc = MethodNotAllowed(allow=["GET", "POST"])
    assert exc.status == 405
    assert (b"allow", b"GET, POST") in exc.headers
    # RFC 9110 10.2.3: 429 may carry Retry-After.
    assert (b"retry-after", b"30") in TooManyRequests(retry_after=30).headers


# --- background tasks -----------------------------------------------------------


@pytest.mark.asyncio
async def test_background_runs_after_response() -> None:
    order: list[str] = []
    app = Wreath()

    @app.get("/")
    async def home(request: Any) -> Any:
        async def task() -> None:
            await asyncio.sleep(0)
            order.append("background")

        response = TextResponse("ok")
        response.background = task
        return response

    status, _, body = await call(app, http_scope())
    order.append("after-call")
    assert status == 200 and body == b"ok"
    assert order == ["background", "after-call"]


# --- CORS -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_preflight_short_circuits() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            cors=CorsPolicy(
                allow_origins=["https://app.example"],
                allow_methods=["GET", "POST"],
                allow_headers=["x-custom"],
            )
        )
    )

    @app.route("/data", methods=["OPTIONS", "GET"])
    async def data(request: Any) -> Any:
        return TextResponse("real handler")

    status, headers, _ = await call(
        app,
        http_scope(
            "/data",
            "OPTIONS",
            headers=[
                (b"origin", b"https://app.example"),
                (b"access-control-request-method", b"GET"),
            ],
        ),
    )
    assert status == 204
    assert headers[b"access-control-allow-origin"] == [b"https://app.example"]
    assert headers[b"access-control-allow-methods"] == [b"GET, POST"]
    assert headers[b"access-control-allow-headers"] == [b"x-custom"]


@pytest.mark.asyncio
async def test_cors_preflight_for_get_only_route() -> None:
    """Preflights target routes that declare no OPTIONS method; the app-level
    fallback must answer them before the 404 path."""
    app = Wreath(
        http_policy=HttpPolicy(cors=CorsPolicy(allow_origins=["https://app.example"]))
    )

    @app.get("/data")
    async def data(request: Any) -> Any:
        return TextResponse("payload")

    status, headers, _ = await call(
        app,
        http_scope(
            "/data",
            "OPTIONS",
            headers=[
                (b"origin", b"https://app.example"),
                (b"access-control-request-method", b"GET"),
            ],
        ),
    )
    assert status == 204
    assert headers[b"access-control-allow-origin"] == [b"https://app.example"]


@pytest.mark.asyncio
async def test_cors_simple_request_headers_appended() -> None:
    app = Wreath(http_policy=HttpPolicy(cors=CorsPolicy(allow_origins=["*"])))

    @app.get("/data")
    async def data(request: Any) -> Any:
        return TextResponse("payload")

    status, headers, body = await call(
        app, http_scope("/data", headers=[(b"origin", b"https://anywhere")])
    )
    assert status == 200 and body == b"payload"
    assert headers[b"access-control-allow-origin"] == [b"*"]


@pytest.mark.asyncio
async def test_cors_disallowed_preflight_origin() -> None:
    app = Wreath(
        http_policy=HttpPolicy(cors=CorsPolicy(allow_origins=["https://app.example"]))
    )

    @app.route("/data", methods=["OPTIONS"])
    async def data(request: Any) -> Any:
        return TextResponse("nope")

    status, _, _ = await call(
        app,
        http_scope(
            "/data",
            "OPTIONS",
            headers=[
                (b"origin", b"https://evil.example"),
                (b"access-control-request-method", b"GET"),
            ],
        ),
    )
    assert status == 403


# --- static files ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_serves_files(tmp_path: Path) -> None:
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "site.css").write_bytes(b"body{}")
    app = Wreath()
    app.static("/assets", str(tmp_path))

    status, headers, body = await call(app, http_scope("/assets/css/site.css"))
    assert status == 200
    assert headers[b"content-type"] == [b"text/css"]
    assert body == b"body{}"


@pytest.mark.asyncio
async def test_static_etag_conditional(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    app = Wreath()
    app.static("/assets", str(tmp_path))

    _, headers, _ = await call(app, http_scope("/assets/a.txt"))
    etag = headers[b"etag"][0]
    status, _, body = await call(
        app, http_scope("/assets/a.txt", headers=[(b"if-none-match", etag)])
    )
    assert status == 304
    assert body == b""


@pytest.mark.asyncio
async def test_static_traversal_blocked(tmp_path: Path) -> None:
    (tmp_path / "public").mkdir()
    (tmp_path / "secret.txt").write_bytes(b"secret")
    app = Wreath()
    app.static("/assets", str(tmp_path / "public"))

    status, _, body = await call(app, http_scope("/assets/../secret.txt"))
    assert status == 404
    assert b"secret" not in body


@pytest.mark.asyncio
async def test_static_index_html(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_bytes(b"<html>home</html>")
    app = Wreath()
    app.static("/site", str(tmp_path))

    status, headers, body = await call(app, http_scope("/site/"))
    assert status == 200
    assert headers[b"content-type"] == [b"text/html"]
    assert body == b"<html>home</html>"


@pytest.mark.asyncio
async def test_static_missing_is_404(tmp_path: Path) -> None:
    app = Wreath()
    app.static("/assets", str(tmp_path))
    status, _, _ = await call(app, http_scope("/assets/nope.js"))
    assert status == 404


# --- lifespan -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_handlers_run_in_order() -> None:
    order: list[str] = []
    app = Wreath()

    @app.on_startup
    async def first(application: Wreath) -> None:
        application.state.ready = True
        order.append("first")

    @app.on_startup
    async def second(application: Wreath) -> None:
        order.append("second")

    @app.on_shutdown
    async def bye(application: Wreath) -> None:
        order.append("bye")

    messages = iter(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert order == ["first", "second", "bye"]
    assert app.state.ready is True


@pytest.mark.asyncio
async def test_lifespan_startup_failure_reported() -> None:
    app = Wreath()

    @app.on_startup
    async def boom(application: Wreath) -> None:
        raise RuntimeError("no database")

    messages = iter([{"type": "lifespan.startup"}])
    sent: list[dict] = []

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    assert sent[0]["type"] == "lifespan.startup.failed"
    assert "no database" in sent[0]["message"]


@pytest.mark.asyncio
async def test_a_test_client_reports_a_startup_failure_rather_than_hanging() -> None:
    # The app replies startup.failed and TestClient turns it into an error.
    from wreath.testing import TestClient

    app = Wreath()

    @app.on_startup
    async def boom(application: Wreath) -> None:
        raise RuntimeError("no database")

    with pytest.raises(RuntimeError, match="lifespan startup failed"):
        async with TestClient(app):
            pass


@pytest.mark.asyncio
async def test_a_test_client_reports_an_app_that_raises_instead_of_replying() -> None:
    # The harder case, and the reason _reply exists: the app raises without
    # sending anything, so nothing is ever put on the queue. Waiting on the
    # queue alone deadlocks, which turns a one-line mistake -- a route
    # annotation that does not resolve, a missing table -- into a test run that
    # hangs with no output at all.
    from wreath.testing import TestClient

    class Exploding:
        async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
            raise RuntimeError("compiling routes went wrong")

    with pytest.raises(RuntimeError, match="raised during lifespan startup") as caught:
        async with TestClient(Exploding()):
            pass
    # The original failure is the cause, not just a mention in the message.
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "compiling routes went wrong" in str(caught.value.__cause__)


@pytest.mark.asyncio
async def test_a_test_client_reports_an_app_that_returns_without_replying() -> None:
    from wreath.testing import TestClient

    class Silent:
        async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
            return

    with pytest.raises(RuntimeError, match="ended without a startup reply"):
        async with TestClient(Silent()):
            pass


# --- adversarial static-file containment and executor bounds (#4, #5) --------

@pytest.mark.asyncio
async def test_static_symlink_inside_root_is_not_followed(tmp_path) -> None:
    """A symlink under the static root pointing outside must not be served."""
    from wreath import Wreath
    from wreath.testing import TestClient

    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "item.txt").write_bytes(b"public")
    (tmp_path / "secret.txt").write_bytes(b"SECRET")
    os.symlink(tmp_path / "secret.txt", tmp_path / "public" / "leak.txt")

    app = Wreath()
    app.static("/assets", str(tmp_path / "public"))
    async with TestClient(app) as client:
        ok = await client.get("/assets/item.txt")
        assert ok.status == 200 and ok.body == b"public"
        leak = await client.get("/assets/leak.txt")
        assert leak.status == 404
        assert b"SECRET" not in leak.body
        escape = await client.get("/assets/../secret.txt")
        assert escape.status == 404


@pytest.mark.asyncio
async def test_file_response_uses_bounded_executor_submissions(tmp_path, monkeypatch) -> None:
    """Executor submissions must be constant with file size, not per-chunk."""
    from wreath.response import FileResponse

    submissions = 0
    real_to_thread = asyncio.to_thread

    async def counting_to_thread(func, /, *args, **kwargs):
        nonlocal submissions
        submissions += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)

    async def collect(response) -> bytes:
        nonlocal submissions
        body = bytearray()

        async def send(message):
            if message["type"] == "http.response.body":
                body.extend(message.get("body", b""))

        submissions = 0
        await response(send)
        return bytes(body)

    for size in (256 * 1024, 5 * 1024 * 1024):
        path = tmp_path / f"f{size}.bin"
        path.write_bytes(b"z" * size)
        body = await collect(FileResponse(str(path)))
        assert len(body) == size
        # Open (+fstat) is one submission, the reader worker is one more: two,
        # regardless of how many 256 KiB chunks the file holds.
        assert submissions <= 2, (size, submissions)
