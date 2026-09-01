from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from wreath import Wreath
from wreath._native import _core
from wreath.policy import (
    CsrfPolicy,
    HttpPolicy,
    ProxyPolicy,
    SecurityHeadersPolicy,
    TrustedHostPolicy,
)
from wreath.request import Request

_IMPLEMENTATIONS = [_core.TrustedNetworks]

_SECRET = "x" * 32


@pytest.mark.parametrize("whitespace", [b"\n", b"\v", b"\f", b"\r"])
def test_native_forwarded_values_preserve_bytes_strip_semantics(
    whitespace: bytes,
) -> None:
    assert _core.forwarded_proto(whitespace + b"HTTPS" + whitespace) == "https"
    assert (
        _core.forwarded_host(whitespace + b"front.example" + whitespace)
        == b"front.example"
    )


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
    middleware = ProxyPolicy(trusted=("10.0.0.0/8",))

    await middleware._ingress(request)

    assert context.scope_calls == 0
    assert context.client == ("203.0.113.7", None)
    assert context.scheme == "https"
    assert request.header("host") == "public.example"


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
        assert (
            implementation(("10.0.0.0/8", "2001:db8::/32", "192.168.1.1/32")).contains(address)
            is expected
        ), address
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


@pytest.mark.parametrize("implementation", _IMPLEMENTATIONS)
def test_the_client_is_the_rightmost_hop_no_trusted_proxy_claims(implementation: Any) -> None:
    networks = implementation(("10.0.0.0/8", "2001:db8::/32"))
    for header, expected in (
        # One trusted proxy in front: skip it, the client is what it forwarded.
        (b"203.0.113.9, 10.1.2.3", "203.0.113.9"),
        # A forged hop to the left of a real one changes nothing.
        (b"1.2.3.4, 203.0.113.9, 10.1.2.3", "203.0.113.9"),
        # Every hop trusted: no untrusted claim exists, so the chain's origin
        # is the best answer available rather than nothing at all.
        (b"2001:db8::1, 10.0.0.1", "2001:db8::1"),
        (b"10.0.0.1", "10.0.0.1"),
        # RFC 7239 permits an obfuscated or unknown node. It is not an address,
        # so there is no client -- not a fabricated one.
        (b"unknown", None),
        # Bracketed, ported, and IPv4-mapped: unwrapped so it can be matched
        # against an IPv4 network, which `::ffff:203.0.113.9` never would be.
        (b"[::ffff:203.0.113.9]:80, 10.0.0.1", "203.0.113.9"),
    ):
        assert networks.forwarded_client(header) == expected, header


def test_trusted_is_required() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ProxyPolicy(trusted=())


async def test_untrusted_peer_cannot_override_anything() -> None:
    app = Wreath(http_policy=HttpPolicy(proxy=ProxyPolicy(trusted=["10.0.0.0/8"])))
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
    app = Wreath(http_policy=HttpPolicy(proxy=ProxyPolicy(trusted=["10.0.0.0/8"])))
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
    app = Wreath(
        http_policy=HttpPolicy(
            proxy=ProxyPolicy(trusted=["10.0.0.0/8"], trust_proto=False, trust_host=False)
        )
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
    app = Wreath(http_policy=HttpPolicy(proxy=ProxyPolicy(trusted=["10.0.0.0/8"])))
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


async def test_hsts_is_silently_dropped_behind_a_proxy_until_proxy_headers_run() -> None:

    def build(with_proxy: bool) -> Wreath:
        app = Wreath(
            http_policy=HttpPolicy(
                proxy=ProxyPolicy(trusted=["10.0.0.0/8"]) if with_proxy else None,
                security_headers=SecurityHeadersPolicy(
                    hsts_max_age=31_536_000, hsts_include_subdomains=True
                ),
            )
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

    def build(with_proxy: bool) -> Wreath:
        app = Wreath(
            http_policy=HttpPolicy(
                proxy=ProxyPolicy(trusted=["10.0.0.0/8"]) if with_proxy else None,
                csrf=CsrfPolicy(_SECRET),
            )
        )

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
    app = Wreath(
        http_policy=HttpPolicy(
            proxy=ProxyPolicy(trusted=["10.0.0.0/8"]),
            trusted_host=TrustedHostPolicy(("allowed.example",)),
        )
    )

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


# `wreath mutant` survived all three. Each is a "leave it alone" branch, and a
# leave-it-alone branch that stops working is the dangerous kind: nothing
# errors, the request simply arrives carrying a value a client chose.


@pytest.mark.asyncio
async def test_a_request_with_no_peer_address_is_never_trusted() -> None:
    app = Wreath(http_policy=HttpPolicy(proxy=ProxyPolicy(trusted=["127.0.0.0/8"])))

    seen: dict[str, Any] = {}

    @app.get("/")
    async def index(request: Any) -> dict:
        seen["client"] = request.client
        seen["scheme"] = request.scheme
        return {"ok": True}

    for missing in ({"client": None}, {"client": ()}):
        seen.clear()
        status, _ = await _call(
            app,
            missing,
            headers={"x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "https"},
        )
        assert status == 200  # no crash ...
        assert seen["client"] in (None, ())  # ... and nothing was rewritten
        assert seen["scheme"] == "http"


@pytest.mark.asyncio
async def test_an_unresolvable_forwarded_for_leaves_the_client_alone() -> None:
    app = Wreath(http_policy=HttpPolicy(proxy=ProxyPolicy(trusted=["127.0.0.0/8"])))

    seen: dict[str, Any] = {}

    @app.get("/")
    async def index(request: Any) -> dict:
        seen["client"] = request.client
        return {"ok": True}

    status, _ = await _call(
        app,
        {"client": ("127.0.0.1", 5000)},
        headers={"x-forwarded-for": "unknown, 127.0.0.1"},
    )
    assert status == 200
    assert seen["client"] == ("127.0.0.1", 5000)  # the real peer, untouched


@pytest.mark.asyncio
async def test_an_empty_forwarded_host_does_not_blank_the_host_header() -> None:
    app = Wreath(
        http_policy=HttpPolicy(proxy=ProxyPolicy(trusted=["127.0.0.0/8"], trust_host=True))
    )

    seen: dict[str, Any] = {}

    @app.get("/")
    async def index(request: Any) -> dict:
        seen["host"] = request.header("host")
        return {"ok": True}

    status, _ = await _call(
        app,
        {"client": ("127.0.0.1", 5000)},
        headers={"host": "real.example", "x-forwarded-host": ""},
    )
    assert status == 200
    assert seen["host"] == "real.example"

    # And a non-empty one still wins, so this is not "the override stopped".
    status, _ = await _call(
        app,
        {"client": ("127.0.0.1", 5000)},
        headers={"host": "real.example", "x-forwarded-host": "front.example"},
    )
    assert seen["host"] == "front.example"
