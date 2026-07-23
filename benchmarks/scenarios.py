"""Shared benchmark scenario definitions and framework capability declarations."""

from __future__ import annotations

from dataclasses import dataclass, field

# Protocols are an orthogonal result dimension, independent of frameworks.
ALL_PROTOCOLS: frozenset[str] = frozenset({"http/1.1", "h2", "h3"})
HTTP1_ONLY: frozenset[str] = frozenset({"http/1.1"})

# Litestar is intentionally absent: its route registration is O(n^2) (it
# re-validates the whole routing trie on every add), so the suite's standard
# 10,000-route app takes ~30s to build and races the readiness deadline. See
# the note beside ROUTE_COUNT in apps.py.
FRAMEWORKS = (
    "wreath",
    "wreath-native", "wreath-metal",
    "starlette",
    "fastapi",
    "sanic",
    "blacksheep",
    "django",
    "flask",
)

_ALL = frozenset(FRAMEWORKS)
_WREATH_ONLY = frozenset({"wreath", "wreath-native", "wreath-metal"})
_REQUEST_FEATURE_FRAMEWORKS = frozenset(
    {
        "wreath", "wreath-native", "wreath-metal",
        "starlette", "fastapi", "sanic", "django", "flask",
    }
)
_STREAMING_FRAMEWORKS = frozenset(
    {"wreath", "wreath-native", "wreath-metal", "starlette", "fastapi"}
)
_BACKGROUND_FRAMEWORKS = frozenset(
    {"wreath", "wreath-native", "wreath-metal", "starlette"}
)
_WEBSOCKET_FRAMEWORKS = frozenset(
    {
        "wreath", "wreath-native", "wreath-metal",
        "starlette", "fastapi", "sanic", "blacksheep",
    }
)
# Template rendering and HTTP caching are expressible in every ASGI-tier
# framework (competitors use Jinja2 and a manual Cache-Control header); the
# webhook HMAC profile is a Wreath framework primitive, so only Wreath implements it.
_TEMPLATE_FRAMEWORKS = frozenset(
    {
        "wreath", "wreath-native", "wreath-metal",
        "starlette", "fastapi", "sanic", "blacksheep",
    }
)
_CACHE_FRAMEWORKS = _TEMPLATE_FRAMEWORKS
_WEBHOOK_FRAMEWORKS = _WREATH_ONLY

JSON_REQUEST_BODY = b'{"message":"hello","values":[1,2,3,4]}'
TYPED_REQUEST_BODY = (
    b'{"name":"benchmark-item","price":19.99,"tags":["a","b","c"],"active":true}'
)
SMALL_REQUEST_BODY = b"x" * 1_024
LARGE_RESPONSE_BODY = b"x" * 65_536
STREAM_CHUNKS = (b"a" * 256, b"b" * 256, b"c" * 256, b"d" * 256)

# Template scenario: render an HTML table whose cells need escaping.
TEMPLATE_ROW_COUNT = 20

# Webhook scenario: a signed inbound payload the verifier must accept. The
# headers carry Wreath's HMAC profile, signed once here; the benchmark app builds a
# verifier with an effectively unbounded age window so the fixed timestamp stays
# valid for the whole run.
WEBHOOK_SECRET = b"wreath-benchmark-secret-key"
WEBHOOK_KEY_ID = "bench"
WEBHOOK_BODY = b'{"event":"ping","id":42}'


def _webhook_headers() -> tuple[tuple[str, str], ...]:
    from datetime import UTC, datetime

    from wreath.webhooks import HMACWebhookSigner, WebhookEnvelope

    signer = HMACWebhookSigner({WEBHOOK_KEY_ID: WEBHOOK_SECRET}, key_id=WEBHOOK_KEY_ID)
    envelope = WebhookEnvelope(
        id="evt-benchmark",
        type="ping",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=WEBHOOK_BODY,
    )
    signed = signer.headers(envelope)
    return (
        ("Content-Type", "application/json"),
        *((key.decode("ascii"), value.decode("ascii")) for key, value in signed),
    )


WEBHOOK_HEADERS = _webhook_headers()


@dataclass(frozen=True, slots=True)
class Scenario:
    method: str
    path: str
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    frameworks: frozenset[str] = _ALL
    protocols: frozenset[str] = field(default_factory=lambda: ALL_PROTOCOLS)
    """Transport protocols this scenario can run over, independent of framework
    support. WebSocket scenarios are HTTP/1.1-only for now."""
    websocket: bool = False
    """When set, the load generator upgrades once per connection and each
    measured request is one masked text-frame echo roundtrip (body = payload)."""
    background: bool = False
    """When set, each measured request schedules one response-bound background
    task. The runner queries an unmeasured stats endpoint after load stops,
    waits for in-flight work to drain, and records started/completed/failed
    counts so a framework cannot look faster by dropping the work."""

    def supports(self, framework: str) -> bool:
        return framework in self.frameworks

    def supports_protocol(self, protocol: str) -> bool:
        return protocol in self.protocols


SCENARIOS = {
    "plaintext": Scenario("GET", "/"),
    "e2e": Scenario(
        # The whole stack orchestrated in one request: bearer authentication,
        # a wreath.postgres round trip, and a wreath.http_client fetch against
        # in-process upstreams (benchmarks/e2e_upstream.py), composed into one
        # JSON response. Self-contained: no external database or service.
        "GET",
        "/e2e",
        headers=(("Authorization", "Bearer user"),),
        frameworks=_WREATH_ONLY,
    ),
    "json": Scenario("GET", "/json"),
    "parameter": Scenario("GET", "/users/42"),
    "middleware-noop": Scenario("GET", "/middleware/noop", frameworks=_WREATH_ONLY),
    "missing": Scenario("GET", "/definitely-missing", frameworks=_WREATH_ONLY),
    "auth-missing": Scenario("GET", "/auth/profile", frameworks=_WREATH_ONLY),
    "auth-authenticated": Scenario(
        "GET",
        "/auth/profile",
        headers=(("Authorization", "Bearer user"),),
        frameworks=_WREATH_ONLY,
    ),
    "auth-rbac-allow": Scenario(
        "GET",
        "/auth/admin",
        headers=(("Authorization", "Bearer admin"),),
        frameworks=_WREATH_ONLY,
    ),
    "auth-rbac-deny": Scenario(
        "GET",
        "/auth/admin",
        headers=(("Authorization", "Bearer user"),),
        frameworks=_WREATH_ONLY,
    ),
    "header-lookup": Scenario(
        "GET",
        "/headers",
        headers=(("X-Benchmark", "wreath-benchmark-value"),),
        frameworks=_REQUEST_FEATURE_FRAMEWORKS,
    ),
    "body-1k": Scenario(
        "POST",
        "/body",
        body=SMALL_REQUEST_BODY,
        headers=(("Content-Type", "application/octet-stream"),),
        frameworks=_REQUEST_FEATURE_FRAMEWORKS,
    ),
    "json-body": Scenario(
        "POST",
        "/json-body",
        body=JSON_REQUEST_BODY,
        headers=(("Content-Type", "application/json"),),
        frameworks=_REQUEST_FEATURE_FRAMEWORKS,
    ),
    "response-64k": Scenario(
        "GET", "/response-64k", frameworks=_REQUEST_FEATURE_FRAMEWORKS
    ),
    "stream-4x256": Scenario(
        "GET", "/stream-4x256", frameworks=_STREAMING_FRAMEWORKS
    ),
    "background-noop": Scenario(
        "GET",
        "/background-noop",
        frameworks=_BACKGROUND_FRAMEWORKS,
        protocols=HTTP1_ONLY,
        background=True,
    ),
    "background-yield": Scenario(
        "GET",
        "/background-yield",
        frameworks=_BACKGROUND_FRAMEWORKS,
        protocols=HTTP1_ONLY,
        background=True,
    ),
    "routing-shallow-get": Scenario("GET", "/status/leaf-9995"),
    "routing-versioned-post": Scenario("POST", "/api/v1/items/category-5/leaf-9996"),
    "routing-trailing-put": Scenario("PUT", "/api/v2/groups/group-9997/members/"),
    "routing-params-patch": Scenario(
        "PATCH", "/tenants/acme/collections/collection-9998/items/42"
    ),
    "routing-deep-delete": Scenario(
        "DELETE", "/api/internal/v3/regions/region-9999/zones/primary/nodes/current/status"
    ),
    "validated-body": Scenario(
        "POST",
        "/typed-items/42?verbose=true",
        body=TYPED_REQUEST_BODY,
        headers=(("Content-Type", "application/json"),),
        frameworks=frozenset({"wreath", "wreath-native", "wreath-metal", "fastapi"}),
    ),
    "ws-echo": Scenario(
        "GET",
        "/ws-echo",
        body=b"x" * 125,
        frameworks=_WEBSOCKET_FRAMEWORKS,
        protocols=HTTP1_ONLY,
        websocket=True,
    ),
    "template": Scenario("GET", "/template", frameworks=_TEMPLATE_FRAMEWORKS),
    "cache-control": Scenario("GET", "/cached", frameworks=_CACHE_FRAMEWORKS),
    "webhook": Scenario(
        "POST",
        "/webhook",
        body=WEBHOOK_BODY,
        headers=WEBHOOK_HEADERS,
        frameworks=_WEBHOOK_FRAMEWORKS,
    ),
}
