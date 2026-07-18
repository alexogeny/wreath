"""ProxyHeadersMiddleware: trusted forwarding headers, and the bugs it fixes."""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from wreath import Wreath
from wreath._native import _core
from wreath._pure.proxy import TrustedNetworks as PureTrustedNetworks
from wreath.middleware import (
    CSRFMiddleware,
    ProxyHeadersMiddleware,
    SecurityHeadersMiddleware,
    TrustedHostMiddleware,
)
from wreath.request import Request

_IMPLEMENTATIONS = [PureTrustedNetworks]
if _core is not None and hasattr(_core, "TrustedNetworks"):
    _IMPLEMENTATIONS.append(_core.TrustedNetworks)

_SECRET = "x" * 32


async def _call(app: Any, scope_extra: dict[str, Any], **kwargs: Any) -> tuple[int, dict]:
    """Drive the app over ASGI directly: TestClient cannot set scope["client"]."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": kwargs.get("method", "GET"),
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in kwargs.get("headers", {}).items()
        ],
        "server": ("app", 80),
        "client": ("127.0.0.1", 5000),
        "root_path": "",
        **scope_extra,
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {name.decode(): value.decode() for name, value in start["headers"]}
    return start["status"], headers


@pytest.mark.asyncio
async def test_native_context_proxy_updates_do_not_materialize_scope() -> None:
    class NativeLikeContext:
        method = "GET"
        path = "/"
        query_string = b""
        headers = [
            (b"x-forwarded-for", b"203.0.113.7"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"public.example"),
        ]

        def __init__(self) -> None:
            self.client = ("10.0.0.5", 5000)
            self.scheme = "http"
            self.scope_calls = 0

        def _asgi_scope(self) -> dict[str, Any]:
            self.scope_calls += 1
            raise AssertionError("built-in proxy middleware materialized the ASGI scope")

        def _set_client(self, client: tuple[str, int | None]) -> None:
            self.client = client

        def _set_scheme(self, scheme: str) -> None:
            self.scheme = scheme

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    context = NativeLikeContext()
    request = Request(context, receive)
    middleware = ProxyHeadersMiddleware(trusted=("10.0.0.0/8",))

    await middleware.before(request)

    assert context.scope_calls == 0
    assert context.client == ("203.0.113.7", None)
    assert context.scheme == "https"
    assert request.header("host") == "public.example"


# --- matcher parity ---------------------------------------------------------


@pytest.mark.parametrize("implementation", _IMPLEMENTATIONS)
def test_networks_match_ipaddress_semantics(implementation: Any) -> None:
    networks = implementation(("10.0.0.0/8", "2001:db8::/32", "192.168.1.1/32"))
    for address, expected in (
        ("10.255.255.255", True),
        ("11.0.0.1", False),
        ("2001:db8::dead", True),
        ("2001:db9::1", False),
        ("192.168.1.1", True),
        ("192.168.1.2", False),
        ("::ffff:10.1.2.3", True),  # IPv4-mapped collapses to IPv4
        ("not-an-ip", False),
        ("fe80::1%eth0", False),  # zone identifiers are refused, unlike stdlib
    ):
        assert implementation(("10.0.0.0/8", "2001:db8::/32", "192.168.1.1/32")).contains(
            address
        ) is expected, address
    assert networks.count == 3


@pytest.mark.parametrize("implementation", _IMPLEMENTATIONS)
def test_networks_reject_ambiguous_and_malformed_configuration(implementation: Any) -> None:
    for bad in ("10.0.0.0/33", "010.0.0.1", "10.1.2.3/8", "10.0.0.0/", "", "10.0.0.0/x"):
        with pytest.raises(ValueError):
            implementation((bad,))
    with pytest.raises(TypeError):
        implementation((10,))


@pytest.mark.parametrize("implementation", _IMPLEMENTATIONS)
def test_forwarded_client_walks_from_the_right(implementation: Any) -> None:
    networks = implementation(("10.0.0.0/8", "192.168.0.0/16"))
    for value, expected in (
        (b"203.0.113.9", "203.0.113.9"),
        (b"203.0.113.9, 10.1.2.3", "203.0.113.9"),
        (b"203.0.113.9, 10.1.2.3, 192.168.5.5", "203.0.113.9"),
        # A client-forged leftmost hop must not win over the proxy's own record.
        (b"1.2.3.4, 203.0.113.9, 10.1.2.3", "203.0.113.9"),
        (b"10.1.2.3, 10.4.5.6", "10.1.2.3"),  # all trusted: origin is internal
        (b"203.0.113.9:443, 10.1.2.3", "203.0.113.9"),
        (b"[2001:db8::1]:443, 10.1.2.3", "2001:db8::1"),
        (b"2001:db8::1, 10.1.2.3", "2001:db8::1"),
        (b"  203.0.113.9  ,  10.1.2.3  ", "203.0.113.9"),
        (b"::ffff:203.0.113.9, 10.1.2.3", "203.0.113.9"),
        # Unparseable anywhere in the chain: the trusted boundary is unknowable.
        (b"unknown, 10.1.2.3", None),
        (b"203.0.113.9, , 10.1.2.3", None),
        (b"[2001:db8::1]junk, 10.1.2.3", None),
        (b"", None),
    ):
        assert networks.forwarded_client(value) == expected, value


@pytest.mark.parametrize("implementation", _IMPLEMENTATIONS)
def test_forwarded_client_renders_canonically(implementation: Any) -> None:
    networks = implementation(())
    for value in ("2001:0db8:0000:0000:0000:0000:0000:0001", "1:0:0:2:0:0:0:3", "::1"):
        assert networks.forwarded_client(value.encode()) == str(ipaddress.ip_address(value))


def test_native_matcher_agrees_with_pure_reference() -> None:
    if _core is None or not hasattr(_core, "TrustedNetworks"):
        pytest.skip("native core unavailable")
    nets = ("10.0.0.0/8", "2001:db8::/32")
    native, pure = _core.TrustedNetworks(nets), PureTrustedNetworks(nets)
    for value in (
        b"203.0.113.9, 10.1.2.3",
        b"1.2.3.4, 203.0.113.9, 10.1.2.3",
        b"2001:db8::1, 10.0.0.1",
        b"unknown",
        b"10.0.0.1",
        b"[::ffff:203.0.113.9]:80, 10.0.0.1",
    ):
        assert native.forwarded_client(value) == pure.forwarded_client(value), value


# --- middleware behavior ----------------------------------------------------


def test_trusted_is_required() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ProxyHeadersMiddleware(trusted=())


async def test_untrusted_peer_cannot_override_anything() -> None:
    app = Wreath()
    app.add_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]), priority=-10)
    seen: dict[str, Any] = {}

    @app.get("/")
    async def index(request: Any) -> str:
        seen["scheme"] = request.scope["scheme"]
        seen["client"] = request.scope["client"]
        seen["host"] = request.header("host")
        return "ok"

    status, _ = await _call(
        app,
        {"client": ("203.0.113.9", 5000)},  # not a configured proxy
        headers={
            "host": "real.example",
            "x-forwarded-for": "1.2.3.4",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "evil.example",
        },
    )
    assert status == 200
    assert seen == {
        "scheme": "http",
        "client": ("203.0.113.9", 5000),
        "host": "real.example",
    }


async def test_trusted_peer_applies_forwarded_values() -> None:
    app = Wreath()
    app.add_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]), priority=-10)
    seen: dict[str, Any] = {}

    @app.get("/")
    async def index(request: Any) -> str:
        seen["scheme"] = request.scope["scheme"]
        seen["client"] = request.scope["client"]
        seen["host"] = request.header("host")
        return "ok"

    status, _ = await _call(
        app,
        {"client": ("10.0.0.5", 5000)},
        headers={
            "host": "internal.local",
            "x-forwarded-for": "203.0.113.9, 10.0.0.5",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "public.example",
        },
    )
    assert status == 200
    assert seen == {
        "scheme": "https",
        "client": ("203.0.113.9", None),
        "host": "public.example",
    }


async def test_individual_overrides_can_be_disabled() -> None:
    app = Wreath()
    app.add_middleware(
        ProxyHeadersMiddleware(trusted=["10.0.0.0/8"], trust_proto=False, trust_host=False),
        priority=-10,
    )
    seen: dict[str, Any] = {}

    @app.get("/")
    async def index(request: Any) -> str:
        seen["scheme"] = request.scope["scheme"]
        seen["host"] = request.header("host")
        seen["client"] = request.scope["client"]
        return "ok"

    await _call(
        app,
        {"client": ("10.0.0.5", 5000)},
        headers={
            "host": "internal.local",
            "x-forwarded-for": "203.0.113.9",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "public.example",
        },
    )
    # X-Forwarded-For still applies; proto and host do not.
    assert seen == {"scheme": "http", "host": "internal.local", "client": ("203.0.113.9", None)}


async def test_garbage_forwarded_proto_is_ignored() -> None:
    app = Wreath()
    app.add_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]), priority=-10)
    seen: dict[str, Any] = {}

    @app.get("/")
    async def index(request: Any) -> str:
        seen["scheme"] = request.scope["scheme"]
        return "ok"

    await _call(
        app,
        {"client": ("10.0.0.5", 5000)},
        headers={"host": "h.example", "x-forwarded-proto": "javascript:alert(1)"},
    )
    assert seen["scheme"] == "http"


# --- the failures this middleware exists to fix -----------------------------


async def test_hsts_is_silently_dropped_behind_a_proxy_until_proxy_headers_run() -> None:
    """SecurityHeadersMiddleware gates HSTS on an HTTPS scheme."""

    def build(with_proxy: bool) -> Wreath:
        app = Wreath()
        if with_proxy:
            app.add_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]), priority=-10)
        app.add_middleware(
            SecurityHeadersMiddleware(hsts_max_age=31_536_000, hsts_include_subdomains=True)
        )

        @app.get("/")
        async def index(request: Any) -> str:
            return "ok"

        return app

    headers = {"host": "app.example", "x-forwarded-proto": "https"}
    _, without = await _call(build(False), {"client": ("10.0.0.5", 5000)}, headers=headers)
    _, with_proxy = await _call(build(True), {"client": ("10.0.0.5", 5000)}, headers=headers)

    assert "strict-transport-security" not in without
    assert with_proxy["strict-transport-security"] == "max-age=31536000; includeSubDomains"


async def test_csrf_rejects_every_unsafe_request_behind_a_proxy_until_proxy_headers_run() -> None:
    """CSRF compares the browser's Origin against scheme://host from the scope.

    Behind a TLS-terminating proxy the scope says http, the browser says https,
    and a legitimate same-origin form post is rejected.
    """

    def build(with_proxy: bool) -> Wreath:
        app = Wreath()
        if with_proxy:
            app.add_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]), priority=-10)
        app.add_middleware(CSRFMiddleware(_SECRET))

        @app.get("/")
        async def form(request: Any) -> str:
            return "form"

        @app.post("/")
        async def submit(request: Any) -> str:
            return "ok"

        return app

    proxy = {"client": ("10.0.0.5", 5000)}

    async def post_a_real_form(app: Wreath) -> int:
        # A browser first loads the form over HTTPS and is issued a token.
        _, issued = await _call(
            app, proxy, headers={"host": "app.example", "x-forwarded-proto": "https"}
        )
        token = issued["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        # Then it posts that token back, same-origin, over the same proxy.
        status, _ = await _call(
            app,
            proxy,
            method="POST",
            headers={
                "host": "app.example",
                "origin": "https://app.example",
                "x-forwarded-proto": "https",
                "cookie": f"wreath_csrf={token}",
                "x-csrf-token": token,
            },
        )
        return status

    assert await post_a_real_form(build(False)) == 403
    assert await post_a_real_form(build(True)) == 200


async def test_trusted_host_validates_the_forwarded_host() -> None:
    """ProxyHeaders overrides Host; TrustedHost still gets to reject it."""
    app = Wreath()
    app.add_middleware(ProxyHeadersMiddleware(trusted=["10.0.0.0/8"]), priority=-10)
    app.add_middleware(TrustedHostMiddleware(("allowed.example",)), priority=-5)

    @app.get("/")
    async def index(request: Any) -> str:
        return "ok"

    ok, _ = await _call(
        app,
        {"client": ("10.0.0.5", 5000)},
        headers={"host": "internal.local", "x-forwarded-host": "allowed.example"},
    )
    rejected, _ = await _call(
        app,
        {"client": ("10.0.0.5", 5000)},
        headers={"host": "allowed.example", "x-forwarded-host": "evil.example"},
    )
    assert ok == 200
    assert rejected == 400
