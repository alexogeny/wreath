"""Shared benchmark scenario definitions and framework capability declarations."""

from __future__ import annotations

from dataclasses import dataclass, field

# Protocols are an orthogonal result dimension, independent of frameworks.
ALL_PROTOCOLS: frozenset[str] = frozenset({"http/1.1", "h2", "h3"})
HTTP1_ONLY: frozenset[str] = frozenset({"http/1.1"})

# This 10,000-route table is what the routing-* scenarios measure against. It
# lives here rather than in apps.py because two consumers need it and only one of
# them wants an app: every Python arm registers it at import, and run.py hands
# the same table to the Rust arm as JSON (see _axum_route_table). Importing
# apps.py to read it would build a whole framework app as a side effect.
ROUTE_COUNT = 10_000
ROUTE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _route_spec(index: int) -> tuple[str, str]:
    method = ROUTE_METHODS[index % len(ROUTE_METHODS)]
    match index % 5:
        case 0:
            path = f"/status/leaf-{index}"
        case 1:
            path = f"/api/v1/items/category-{index % 97}/leaf-{index}"
        case 2:
            path = f"/api/v2/groups/group-{index}/members/"
        case 3:
            path = f"/tenants/{{tenant_id}}/collections/collection-{index}/items/{{item_id}}"
        case _:
            path = f"/api/internal/v3/regions/region-{index}/zones/primary/nodes/current/status"
    return method, path


def route_path(path: str, style: str) -> str:
    """Rewrite the shared `{param}` syntax into one framework's dialect.

    Axum 0.8 takes `{param}` unchanged, which is why the Rust arm can consume
    ROUTE_SPECS as written.
    """
    if style == "sanic":
        return path.replace("{tenant_id}", "<tenant_id:str>").replace(
            "{item_id}", "<item_id:str>"
        )
    if style == "django":
        return path.replace("{tenant_id}", "<str:tenant_id>").replace(
            "{item_id}", "<str:item_id>"
        )
    if style == "flask":
        return path.replace("{tenant_id}", "<tenant_id>").replace("{item_id}", "<item_id>")
    return path


ROUTE_SPECS = tuple(_route_spec(index) for index in range(ROUTE_COUNT))

# Litestar is intentionally absent: its route registration is O(n^2) --
# construct_routing_trie/validate_node re-walks the entire trie on every add
# (~n^2/2 validate_node calls; measured 8.0M for 4,000 routes) -- so building the
# table above takes ~30s and flakily loses the race with the 30s readiness probe.
# It is a Litestar-internal cost we cannot fix from here, and bumping the timeout
# would just mask a boot cost that is part of what the routing benchmark
# compares. Every other framework builds this table in well under a second. Cap
# ROUTE_COUNT far lower only if you also re-add a framework that cannot take it.

#: Three arms in here are not frameworks in the sense the others are, and reading
#: a result without knowing which is which will mislead you:
#:
#: * `blacksheep-granian` is the *same BlackSheep app* as `blacksheep`, served by
#:   Granian instead of Uvicorn. The matrix otherwise holds the server fixed so
#:   the framework is the only variable; this pair inverts that, and is only
#:   meaningful read against `blacksheep` -- alone it says nothing.
#: * `granian-rsgi` is no framework at all -- a raw RSGI handler on the same
#:   server as `blacksheep-granian` (benchmarks/rsgi_app.py). It is the floor a
#:   Python framework's cost is measured *from*.
#: * `axum` is Rust. It is a ceiling, not a competitor: the same scenarios with
#:   no interpreter in the request path, so a Python row can be read as a
#:   fraction of what the hardware will do at all. See
#:   benchmarks/rust_arms/axum_server/.
#:
#: Candidates that were considered and rejected, with reasons, are recorded in
#: benchmarks/README.md -- add to that list rather than deleting a name here.
FRAMEWORKS = (
    "wreath",
    "wreath-native", "wreath-metal",
    "starlette",
    "fastapi",
    "sanic",
    "blacksheep",
    "blacksheep-granian",
    "granian-rsgi",
    "panther",
    "axum",
    "django",
    "flask",
)

#: Arms that `--framework` accepts but a bare run does not select, because their
#: dependencies cannot be resolved alongside the rest of the `benchmark` group.
#: Naming one explicitly still works, once it is installed.
#:
#: `panther` pins `httptools~=0.7.1` where `uvicorn[standard]>=0.51` needs
#: `>=0.8`. Resolving both downgrades the HTTP parser under every Uvicorn-hosted
#: arm, which is the shared baseline -- so it is installed on its own
#: (`uv pip install --no-deps panther`, see the `benchmark-panther` group) and
#: left out of the default sweep rather than allowed to move that baseline.
OPT_IN_FRAMEWORKS = frozenset({"panther"})

#: What `--framework` defaults to: everything installable in one environment.
DEFAULT_FRAMEWORKS = tuple(f for f in FRAMEWORKS if f not in OPT_IN_FRAMEWORKS)

_ALL = frozenset(FRAMEWORKS)
_WREATH_ONLY = frozenset({"wreath", "wreath-native", "wreath-metal"})
#: Everything with a real router. The raw RSGI arm dispatches on an exact-path
#: dict plus one prefix test -- no parameter extraction, no method resolution, no
#: ordering -- so putting it in the 10,000-route scenarios would post the best
#: number in the table for work every other arm has to do.
_ROUTED_FRAMEWORKS = _ALL - {"granian-rsgi"}
_REQUEST_FEATURE_FRAMEWORKS = frozenset(
    {
        "wreath", "wreath-native", "wreath-metal",
        "starlette", "fastapi", "sanic", "django", "flask", "panther",
        # Rust: implemented in benchmarks/rust_arms/axum_server/, response for response
        # against the Starlette arm. BlackSheep is absent here (both arms of it),
        # so the granian pair stays comparable to the uvicorn one.
        "axum",
        # Raw RSGI: these endpoints are exact-path, so the no-router arm can
        # serve them honestly. Only the routing scenarios are beyond it.
        "granian-rsgi",
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
        "starlette", "fastapi", "sanic", "blacksheep", "blacksheep-granian",
    }
)
# Template rendering and HTTP caching are expressible in every ASGI-tier
# framework (competitors use Jinja2 and a manual Cache-Control header); the
# webhook HMAC profile is a Wreath framework primitive, so only Wreath implements it.
_TEMPLATE_FRAMEWORKS = frozenset(
    {
        "wreath", "wreath-native", "wreath-metal",
        "starlette", "fastapi", "sanic", "blacksheep", "blacksheep-granian",
        "panther",
    }
)
#: No longer an alias for the template set. The Rust arm sets one response header
#: on a fixed body, which is the whole cache-control scenario, but it renders no
#: templates -- pulling in a Jinja-equivalent engine would make it a comparison
#: of template libraries rather than of frameworks.
_CACHE_FRAMEWORKS = _TEMPLATE_FRAMEWORKS | {"axum", "granian-rsgi"}
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

#: Must equal `_JWT_SECRET` in `apps.py`. Not a secret in any sense that
#: matters: it signs one token, for one benchmark, against an app that exists
#: only while the benchmark runs.
JWT_SECRET = b"wreath-benchmark-hs256-secret-0123456789"


def _jwt_headers() -> tuple[tuple[str, str], ...]:
    """One HS256 bearer token, minted with the stdlib.

    Minted here rather than by `wreath` so the load generator stays a load
    generator: a token the framework under test also produced would let a defect
    in signing cancel itself out against the same defect in verifying.

    Long-dated because the runner mints once at import and then drives passes
    for as long as the battery takes; a token that expires mid-battery turns
    every later request into a 401 and reports it as throughput.
    """
    import base64
    import hmac
    import json as _json
    import time as _time

    def seg(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = seg(_json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = seg(
        _json.dumps(
            {
                "sub": "user",
                "iss": "https://bench.wreath.invalid",
                "aud": "wreath-bench",
                "exp": int(_time.time()) + 86_400,
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = hmac.new(JWT_SECRET, signing_input, "sha256").digest()
    return (("Authorization", f"Bearer {header}.{claims}.{seg(signature)}"),)


JWT_HEADERS = _jwt_headers()


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
    "auth-jwt": Scenario(
        # The same route and the same middleware as `auth-authenticated`, so the
        # difference between the two rows is exactly one HS256 verify: a compact
        # token split, two vectorised base64url segment decodes, the HMAC, and
        # the registered-claim checks. Nothing else in the suite reaches that
        # code, which is why a row that isolates it is worth more than a faster
        # one that blends it into the request.
        "GET",
        "/auth/profile",
        headers=JWT_HEADERS,
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
    "routing-shallow-get": Scenario(
        "GET", "/status/leaf-9995", frameworks=_ROUTED_FRAMEWORKS
    ),
    "routing-versioned-post": Scenario(
        "POST", "/api/v1/items/category-5/leaf-9996", frameworks=_ROUTED_FRAMEWORKS
    ),
    "routing-trailing-put": Scenario(
        "PUT", "/api/v2/groups/group-9997/members/", frameworks=_ROUTED_FRAMEWORKS
    ),
    "routing-params-patch": Scenario(
        "PATCH",
        "/tenants/acme/collections/collection-9998/items/42",
        frameworks=_ROUTED_FRAMEWORKS,
    ),
    "routing-deep-delete": Scenario(
        "DELETE",
        "/api/internal/v3/regions/region-9999/zones/primary/nodes/current/status",
        frameworks=_ROUTED_FRAMEWORKS,
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
