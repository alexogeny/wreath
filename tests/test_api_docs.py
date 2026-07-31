"""Native API-docs subsystem: fail-closed gating, self-containment, escaping.

These are pure-Python (v1 has no native code of its own), so they hold under
both the default build and ``WREATH_PURE=1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wreath import Wreath
from wreath.testing import TestClient


@dataclass
class DocPayload:
    name: str


def _app(summary: str | None = None, **docs_kwargs: Any) -> Wreath:
    app = Wreath()

    @app.get("/thing", summary=summary, tags=("things",))
    async def thing(request: Any) -> str:
        return "ok"

    app.enable_api_docs(**docs_kwargs)
    return app


@pytest.mark.asyncio
async def test_env_gate_off_in_production() -> None:
    # environments listed but current env is production -> not registered.
    app = _app(environments=("dev", "staging"), env="production")
    async with TestClient(app) as client:
        assert (await client.get("/docs")).status == 404
        assert (await client.get("/openapi.json")).status == 404


@pytest.mark.asyncio
async def test_env_gate_defaults_to_production_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("WREATH_ENV", raising=False)
    app = Wreath()

    @app.get("/thing")
    async def thing(request: Any) -> str:
        return "ok"

    # env unset -> resolves to "production" -> withheld, returns False.
    registered = app.enable_api_docs(environments=("dev",))
    assert registered is False
    async with TestClient(app) as client:
        assert (await client.get("/docs")).status == 404


@pytest.mark.asyncio
async def test_present_in_listed_environment() -> None:
    app = _app(environments=("dev",), env="dev")
    async with TestClient(app) as client:
        docs = await client.get("/docs")
        assert docs.status == 200
        content_type = dict(docs.headers).get(b"content-type", b"")
        assert content_type.startswith(b"text/html")
        # self-contained: no external/CDN asset references.
        body = docs.body.decode("utf-8")
        assert "unpkg.com" not in body
        assert "swagger" not in body.lower()
        assert "<h2>things</h2>" in body
        assert "<table>" not in body
        assert "Schemas" not in body
        assert "Try it" not in body
        spec = await client.get("/openapi.json")
        assert spec.status == 200
        assert dict(spec.headers).get(b"content-type") == b"application/json"


@pytest.mark.asyncio
async def test_docs_sets_own_csp_with_nonce() -> None:
    app = _app(environments=("dev",), env="dev")
    async with TestClient(app) as client:
        docs = await client.get("/docs")
        csp = dict(docs.headers).get(b"content-security-policy", b"").decode()
        assert "default-src 'self'" in csp
        assert "style-src 'nonce-" in csp
        # the nonce in the CSP is also on the inline <style>.
        nonce = csp.split("style-src 'nonce-", 1)[1].split("'", 1)[0]
        assert f'<style nonce="{nonce}">' in docs.body.decode("utf-8")


@pytest.mark.asyncio
async def test_auth_gate_rejects_unauthenticated() -> None:
    app = _app(environments=("dev",), env="dev", authenticated=True)
    async with TestClient(app) as client:
        # no backend / identity -> fail closed.
        assert (await client.get("/docs")).status in (401, 403)


def _bearer_backend(good_token: str = "letmein") -> Any:
    from wreath._auth.backends import BearerTokenBackend
    from wreath._auth.models import Identity

    def verify(token: str) -> Any:
        return Identity("u1") if token == good_token else None

    return BearerTokenBackend(verify)


@pytest.mark.asyncio
async def test_scoped_backend_guards_without_global_configure() -> None:
    # auth= installs a docs-scoped backend; the app has NO global configure_auth.
    app = _app(environments=("dev",), env="dev", auth=_bearer_backend())
    async with TestClient(app) as client:
        assert (await client.request("GET", "/docs")).status == 401
        assert (await client.request("GET", "/openapi.json")).status == 401
        ok = {"authorization": "Bearer letmein"}
        assert (await client.request("GET", "/docs", headers=ok)).status == 200
        assert (await client.request("GET", "/openapi.json", headers=ok)).status == 200
        # a bad token is rejected too.
        bad = {"authorization": "Bearer nope"}
        assert (await client.request("GET", "/docs", headers=bad)).status == 401


@pytest.mark.asyncio
async def test_open_when_no_auth_args() -> None:
    # no auth/authorize/authenticated -> open within the allowed env (env gate only).
    app = _app(environments=("dev",), env="dev")
    async with TestClient(app) as client:
        assert (await client.get("/docs")).status == 200
        assert (await client.get("/openapi.json")).status == 200


@pytest.mark.asyncio
async def test_try_it_out_inherits_docs_gate() -> None:
    # try_it_out shares the docs gate: no separate/stricter auth.
    app = _app(
        environments=("dev",), env="dev", auth=_bearer_backend(), try_it_out=True
    )
    async with TestClient(app) as client:
        assert (await client.request("GET", "/docs")).status == 401
        ok = {"authorization": "Bearer letmein"}
        assert (await client.request("GET", "/docs", headers=ok)).status == 200


@pytest.mark.asyncio
async def test_docs_render_operation_parameters_body_models_and_try_script() -> None:
    app = Wreath()

    @app.post("/things", tags=("things",), summary="Create a thing")
    async def create(
        request: Any,
        payload: DocPayload,
        required: int,
        optional: int = 1,
    ) -> DocPayload:
        """Create one thing."""
        return payload

    app.enable_api_docs(
        environments=("dev",),
        env="dev",
        try_it_out=True,
    )
    async with TestClient(app) as client:
        body = (await client.get("/docs")).body.decode("utf-8")

    assert "<h2>things</h2>" in body
    assert "Create one thing." in body
    assert "<table>" in body
    assert "<td>yes</td>" in body
    assert "<td>no</td>" in body
    assert "Request body" in body
    assert "Try it" in body
    assert "Schemas" in body
    assert "<code>DocPayload</code>" in body
    assert "document.querySelectorAll" in body


@pytest.mark.asyncio
async def test_user_authored_strings_are_escaped() -> None:
    app = _app(summary="<script>alert(1)</script>", environments=("dev",), env="dev")
    async with TestClient(app) as client:
        body = (await client.get("/docs")).body.decode("utf-8")
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "<script>alert(1)</script>" not in body
