"""The lazy ASGI scope must stay lazy on the native server path.

On `wreath.server` a request is a native `_RequestContext`, and `Request`
exposes `.method`/`.path`/`.scheme`/`.query_string`/`.headers`/`.client` as
direct member reads so the ASGI scope dict is never built. `_asgi_scope()`
allocates a 13-key dict and retains it for the request lifetime, so one
`request.scope[...]` in a global hook costs that on *every* request.

These tests pin the contract by handing each component a context whose
`_asgi_scope()` fails the test, which is deterministic -- no timing involved.
Components that legitimately need the whole scope (a user handler, the
`scope` property itself) are not covered here; the request-path components
below are the ones that only ever wanted one member.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from wreath.binding import compile_binder
from wreath.policy import CsrfPolicy, SecurityHeadersPolicy
from wreath.request import Request
from wreath.response import Response

_SECRET = "x" * 32


class StrictContext:
    """A `_RequestContext` stand-in that refuses to materialize its scope."""

    def __init__(
        self,
        *,
        method: str = "GET",
        path: str = "/",
        query_string: bytes = b"",
        scheme: str = "http",
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.query_string = query_string
        self.scheme = scheme
        self.headers = headers if headers is not None else [(b"host", b"example.test")]
        self.client = ("127.0.0.1", 5000)
        self.scope_calls = 0

    def _asgi_scope(self) -> dict[str, Any]:
        self.scope_calls += 1
        raise AssertionError(
            "a request-path component materialized the lazy ASGI scope; "
            "read the member off Request (.scheme/.query_string/.method) instead"
        )

    def _set_client(self, client: tuple[str, int | None]) -> None:
        self.client = client

    def _set_scheme(self, scheme: str) -> None:
        self.scheme = scheme


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(context: StrictContext, **kwargs: Any) -> Request:
    return Request(context, _receive, **kwargs)


# --- SecurityHeadersPolicy ----------------------------------------------
# `after` is a global hook, so this one ran on literally every response.


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.asyncio
async def test_security_headers_reads_scheme_without_building_the_scope(
    scheme: str,
) -> None:
    context = StrictContext(scheme=scheme)
    request = _request(context)
    middleware = SecurityHeadersPolicy(hsts_max_age=31_536_000)

    response = await middleware._egress(request, Response(b"x"))

    assert context.scope_calls == 0
    assert request._scope is None
    names = {name for name, _ in response.headers}
    # The scheme still selects the right header set: HSTS only over https.
    assert (b"strict-transport-security" in names) is (scheme == "https")


# --- CsrfPolicy ---------------------------------------------------------


@pytest.mark.asyncio
async def test_csrf_safe_method_does_not_build_the_scope() -> None:
    context = StrictContext(method="GET")
    request = _request(context)
    middleware = CsrfPolicy(_SECRET, secure=False)

    assert await middleware._ingress(request) is None
    assert context.scope_calls == 0
    assert request._scope is None


@pytest.mark.asyncio
async def test_csrf_unsafe_method_does_not_build_the_scope() -> None:
    """The origin check reads the scheme; it used to reach it via the scope."""
    middleware = CsrfPolicy(_SECRET, secure=False)
    token = middleware._new_token(int(time.time()))

    context = StrictContext(
        method="POST",
        headers=[
            (b"host", b"example.test"),
            (b"origin", b"http://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
            (b"x-csrf-token", token.encode()),
        ],
    )
    request = _request(context)

    # None means "allowed to proceed": the origin check passed, which is the
    # branch that reads the scheme.
    assert await middleware._ingress(request) is None
    assert context.scope_calls == 0
    assert request._scope is None


@pytest.mark.asyncio
async def test_csrf_rejection_path_does_not_build_the_scope() -> None:
    context = StrictContext(method="POST", headers=[(b"host", b"example.test")])
    request = _request(context)
    middleware = CsrfPolicy(_SECRET, secure=False)

    response = await middleware._ingress(request)

    assert response is not None and response.status == 403
    assert context.scope_calls == 0


# --- the compiled binder ----------------------------------------------------


@pytest.mark.asyncio
async def test_query_binding_does_not_build_the_scope() -> None:
    """Every handler with a typed query parameter went through the scope."""

    async def handler(request: Request, limit: int = 10, name: str = "") -> dict:
        return {"limit": limit, "name": name}

    bound = compile_binder(handler, "/items")
    context = StrictContext(path="/items", query_string=b"limit=25&name=ada")
    request = _request(context)

    assert await bound(request) == {"limit": 25, "name": "ada"}
    assert context.scope_calls == 0
    assert request._scope is None


@pytest.mark.asyncio
async def test_query_binding_defaults_without_a_query_string() -> None:
    async def handler(request: Request, limit: int = 10) -> dict:
        return {"limit": limit}

    bound = compile_binder(handler, "/items")
    context = StrictContext(path="/items")
    request = _request(context)

    assert await bound(request) == {"limit": 10}
    assert context.scope_calls == 0


# --- the dict-scope path still behaves identically ---------------------------


@pytest.mark.asyncio
async def test_dict_scope_backed_requests_are_unaffected() -> None:
    """Under a portable ASGI server the scope *is* the dict; same answers."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/items",
        "scheme": "https",
        "query_string": b"limit=7",
        "headers": [(b"host", b"example.test")],
        "client": ("127.0.0.1", 5000),
    }
    request = Request(scope, _receive)
    middleware = SecurityHeadersPolicy(hsts_max_age=31_536_000)
    response = await middleware._egress(request, Response(b"x"))
    assert b"strict-transport-security" in {name for name, _ in response.headers}

    async def handler(request: Request, limit: int = 10) -> dict:
        return {"limit": limit}

    assert await compile_binder(handler, "/items")(Request(scope, _receive)) == {
        "limit": 7
    }


@pytest.mark.asyncio
async def test_scheme_defaults_to_http_when_a_dict_scope_omits_it() -> None:
    """`Request.scheme` keeps the `scope.get("scheme", "http")` default."""
    request = Request({"type": "http", "method": "GET", "path": "/"}, _receive)
    assert request.scheme == "http"
