from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.middleware.base import HeaderSpec, MiddlewareContract, MiddlewareHooks
from wreath.openapi import ResponseSpec, compare_openapi, generate_openapi
from wreath.policy import (
    CorsPolicy,
    CsrfPolicy,
    HttpPolicy,
    RateLimitPolicy,
    RequestIdPolicy,
    SecurityHeadersPolicy,
    ServerTimingPolicy,
    TrustedHostPolicy,
)
from wreath.policy.base import PolicyContract
from wreath.testing import TestClient


def _app_with(*middleware: Any, routes: tuple[str, ...] = ("/widgets",)) -> Wreath:
    app = Wreath()
    for item in middleware:
        app.add_middleware(item)
    for path in routes:
        _register(app, path)
    return app


def _register(app: Wreath, path: str) -> None:
    @app.get(path, name=path.strip("/").replace("/", "_") or "root")
    async def handler(request: Any) -> dict[str, str]:  # pragma: no cover - shape only
        return {"ok": "yes"}


class _Described:
    """A route-scoped middleware that declares a contract."""

    def __init__(self, contract: MiddlewareContract, applies_to: Any = None) -> None:
        self._contract = contract
        if applies_to is not None:
            self.applies_to = applies_to

    async def before(self, request: Any) -> None:
        return None

    def describe(self) -> MiddlewareContract:
        return self._contract


class _Silent:
    """A middleware that declares nothing -- a user's own, most likely."""

    async def before(self, request: Any) -> None:
        return None


def test_a_middleware_that_declares_nothing_contributes_nothing() -> None:
    bare = generate_openapi(_app_with())
    withsilent = generate_openapi(_app_with(_Silent()))
    assert withsilent == bare


def test_a_non_callable_describe_attribute_is_ignored_rather_than_called() -> None:

    class Mislabelled:
        describe = "a string, not a method"

        async def before(self, request: Any) -> None:
            return None

    bare = generate_openapi(_app_with())
    assert generate_openapi(_app_with(Mislabelled())) == bare


def test_a_describe_returning_none_contributes_nothing() -> None:
    hooks = MiddlewareHooks(before=None, after=None)
    assert hooks.describe() is None

    class Wrapping:
        async def before(self, request: Any) -> None:
            return None

        def describe(self) -> Any:
            return None

    bare = generate_openapi(_app_with())
    assert generate_openapi(_app_with(Wrapping())) == bare


def test_a_declared_request_header_becomes_a_header_parameter() -> None:
    contract = MiddlewareContract(
        request_headers=(HeaderSpec("Idempotency-Key", description="Replay key"),)
    )
    document = generate_openapi(_app_with(_Described(contract)))
    parameters = document["paths"]["/widgets"]["get"]["parameters"]
    header = [p for p in parameters if p["in"] == "header"]
    assert [p["name"] for p in header] == ["Idempotency-Key"]
    assert header[0]["description"] == "Replay key"


def test_a_declared_response_reaches_the_operation() -> None:
    contract = MiddlewareContract(
        responses=((429, ResponseSpec(description="Too Many Requests")),),
        response_headers=((429, HeaderSpec("Retry-After")),),
    )
    document = generate_openapi(_app_with(_Described(contract)))
    responses = document["paths"]["/widgets"]["get"]["responses"]
    assert "429" in responses
    assert responses["429"]["description"] == "Too Many Requests"
    assert "Retry-After" in responses["429"]["headers"]


def test_a_route_response_shadows_a_middleware_model_without_rendering_it() -> None:
    contract = MiddlewareContract(
        responses=((409, ResponseSpec(complex, description="shadowed")),)
    )
    app = _app_with(_Described(contract), routes=())

    @app.get("/widgets", responses={409: ResponseSpec(description="Route conflict")})
    async def widgets(request: Any) -> dict[str, str]:
        return {"ok": "yes"}

    document = generate_openapi(app)

    assert document["paths"]["/widgets"]["get"]["responses"]["409"] == {
        "description": "Route conflict"
    }


def test_an_applies_to_predicate_scopes_the_contract() -> None:
    contract = MiddlewareContract(responses=((429, ResponseSpec(description="Too Many Requests")),))
    scoped = _Described(contract, applies_to=lambda route: route.path == "/widgets")
    app = _app_with(scoped, routes=("/widgets", "/gadgets"))
    document = generate_openapi(app)

    assert "429" in document["paths"]["/widgets"]["get"]["responses"]
    assert "429" not in document["paths"]["/gadgets"]["get"]["responses"]


def test_router_scoped_middleware_does_not_decorate_operations_outside_it() -> None:
    from wreath import Router

    contract = MiddlewareContract(responses=((429, ResponseSpec(description="Too Many Requests")),))
    inner = Router()
    _register(inner, "/limited")

    app = Wreath()
    _register(app, "/open")
    app.include_router(inner, middleware=(_Described(contract),))

    document = generate_openapi(app)
    assert "429" in document["paths"]["/limited"]["get"]["responses"]
    assert "429" not in document["paths"]["/open"]["get"]["responses"]


def test_a_contract_can_restrict_itself_to_some_methods() -> None:
    contract = MiddlewareContract(
        request_headers=(HeaderSpec("Idempotency-Key"),),
        methods=frozenset({"POST"}),
    )
    app = Wreath()

    @app.get("/widgets")
    async def read(request: Any) -> dict[str, str]:  # pragma: no cover - shape only
        return {"ok": "yes"}

    @app.post("/widgets")
    async def write(request: Any) -> dict[str, str]:  # pragma: no cover - shape only
        return {"ok": "yes"}

    app.add_middleware(_Described(contract))
    document = generate_openapi(app)

    get_params = document["paths"]["/widgets"]["get"].get("parameters", [])
    post_params = document["paths"]["/widgets"]["post"].get("parameters", [])
    assert [p["name"] for p in get_params if p["in"] == "header"] == []
    assert [p["name"] for p in post_params if p["in"] == "header"] == ["Idempotency-Key"]


def test_a_global_middleware_that_also_scopes_itself_is_refused() -> None:

    class GlobalScoped:
        global_scope = True

        async def before(self, request: Any) -> None:
            return None

        def applies_to(self, route: Any) -> bool:  # pragma: no cover - refused
            return route.path == "/widgets"

        def describe(self) -> MiddlewareContract:
            return MiddlewareContract(
                responses=((429, ResponseSpec(description="Too Many Requests")),)
            )

    app = _app_with(GlobalScoped())
    with pytest.raises(ValueError, match="applies_to"):
        generate_openapi(app)


def test_only_one_global_preflight_handler_can_be_registered() -> None:
    class Preflight:
        async def before(self, request: Any) -> None:
            return None

        async def handle_preflight(self, request: Any) -> None:
            return None

    app = Wreath()
    app.add_global_middleware(Preflight())

    with pytest.raises(ValueError, match="only one global CORS preflight handler"):
        app.add_global_middleware(Preflight())


def test_a_routes_own_response_wins_over_a_middlewares() -> None:
    contract = MiddlewareContract(
        responses=((429, ResponseSpec(description="From the middleware")),)
    )
    app = Wreath()

    @app.get("/widgets", responses={429: ResponseSpec(description="From the route")})
    async def handler(request: Any) -> dict[str, str]:  # pragma: no cover - shape only
        return {"ok": "yes"}

    app.add_middleware(_Described(contract))
    document = generate_openapi(app)
    assert document["paths"]["/widgets"]["get"]["responses"]["429"]["description"] == (
        "From the route"
    )


def test_a_middlewares_response_lands_when_the_route_declares_none() -> None:
    contract = MiddlewareContract(
        responses=((429, ResponseSpec(description="From the middleware")),)
    )
    document = generate_openapi(_app_with(_Described(contract)))
    assert document["paths"]["/widgets"]["get"]["responses"]["429"]["description"] == (
        "From the middleware"
    )


async def test_the_documented_ratelimit_policy_equals_what_the_runtime_sends() -> None:
    app = Wreath(http_policy=HttpPolicy(rate_limit=RateLimitPolicy(limit=60, window=60.0, burst=1)))

    @app.get("/widgets")
    async def handler(request: Any) -> dict[str, str]:
        return {"ok": "yes"}

    document = generate_openapi(app)
    headers = document["paths"]["/widgets"]["get"]["responses"]["429"]["headers"]
    documented = headers["RateLimit-Policy"]["schema"]["const"]

    async with TestClient(app) as client:
        first = await client.get("/widgets")
        limited = await client.get("/widgets")

    assert first.status == 200
    assert limited.status == 429
    assert limited.header("ratelimit-policy") == documented


def test_behaviours_are_emitted_under_the_vendor_extension() -> None:
    contract = MiddlewareContract(behaviours=frozenset({"retry-after"}))
    document = generate_openapi(_app_with(_Described(contract)))
    assert document["paths"]["/widgets"]["get"]["x-wreath-behaviours"] == ["retry-after"]


def test_an_unknown_behaviour_is_refused_at_generation() -> None:
    contract = MiddlewareContract(behaviours=frozenset({"teleport"}))
    with pytest.raises(ValueError, match="teleport"):
        generate_openapi(_app_with(_Described(contract)))


def test_removing_a_behaviour_is_a_breaking_change() -> None:
    contract = MiddlewareContract(behaviours=frozenset({"idempotency-key"}))
    old = generate_openapi(_app_with(_Described(contract)))
    new = generate_openapi(_app_with())
    changes = compare_openapi(old, new)
    assert any(change.kind == "behaviour-removed" for change in changes), changes


def test_adding_a_behaviour_is_not_breaking() -> None:
    contract = MiddlewareContract(behaviours=frozenset({"idempotency-key"}))
    old = generate_openapi(_app_with())
    new = generate_openapi(_app_with(_Described(contract)))
    changes = compare_openapi(old, new)
    assert not any(change.kind == "behaviour-removed" for change in changes), changes


def test_every_shipped_middleware_that_has_a_contract_declares_one() -> None:
    from wreath.cache_control import CacheControl
    from wreath.policy import (
        CachePolicy,
        CompressionPolicy,
        IdempotencyPolicy,
        SessionPolicy,
    )

    described = [
        RateLimitPolicy(limit=60, window=60.0),
        IdempotencyPolicy(),
        CsrfPolicy(secret="x" * 32),
        RequestIdPolicy(),
        CachePolicy(default=CacheControl(max_age=60)),
        CompressionPolicy(),
        CorsPolicy(allow_origins=["https://example.test"]),
        SecurityHeadersPolicy(),
        ServerTimingPolicy(),
        SessionPolicy(secret="x" * 32),
        TrustedHostPolicy(allowed_hosts=["example.test"]),
    ]
    for item in described:
        contract = item.describe()
        assert isinstance(contract, (MiddlewareContract, PolicyContract))
        # A contract that declares nothing at all is a describe() nobody needed.
        assert (
            contract.request_headers
            or contract.response_headers
            or contract.responses
            or contract.behaviours
        ), f"{type(item).__name__}.describe() declares nothing"


def test_a_middleware_configured_to_emit_nothing_declares_nothing() -> None:
    from wreath.policy import CachePolicy, ServerTimingPolicy

    assert ServerTimingPolicy(emit_header=False).describe().response_headers == ()
    assert CachePolicy().describe().response_headers == ()


def test_the_hooks_container_accepts_a_contract() -> None:
    contract = MiddlewareContract(behaviours=frozenset({"etag"}))
    hooks = MiddlewareHooks(before=None, contract=contract)
    assert hooks.describe() is contract


def test_middleware_schema_components_are_collected() -> None:

    class Claim:
        def __init__(self, name: str) -> None:
            self.name = name
            self.relations = ("t",)

        def statements(self) -> tuple[str, ...]:
            return ("CREATE TABLE t ()",)

    class RouteScopedOwner:
        async def before(self, request: Any) -> None:
            return None

        def component(self) -> Any:
            return Claim("route_scoped_owner")

    class GlobalOwner:
        global_scope = True

        async def before(self, request: Any) -> None:
            return None

        def component(self) -> Any:
            return Claim("global_owner")

    app = Wreath()
    app.add_middleware(RouteScopedOwner())
    app.add_middleware(GlobalOwner())

    names = [claim.name for claim in app.schema_components()]
    assert "route_scoped_owner" in names
    assert "global_owner" in names


def _generated(app: Wreath) -> dict[str, str]:
    from wreath.typegen.inspect import build_api_model
    from wreath.typegen.targets.typescript import render_typescript

    return render_typescript(build_api_model(app))


def _one_route_app(*middleware: Any, method: str = "get") -> Wreath:
    from wreath.policy import IdempotencyPolicy

    rate_limit = next((item for item in middleware if type(item) is RateLimitPolicy), None)
    idempotency = next((item for item in middleware if type(item) is IdempotencyPolicy), None)
    app = Wreath(
        http_policy=HttpPolicy(
            rate_limit=rate_limit,
            idempotency=idempotency,
        )
        if rate_limit is not None or idempotency is not None
        else None
    )
    decorator = getattr(app, method)

    @decorator("/widgets")
    async def handler(request: Any) -> dict[str, str]:  # pragma: no cover - shape
        return {"ok": "yes"}

    for item in middleware:
        if type(item) not in (RateLimitPolicy, IdempotencyPolicy):
            app.add_middleware(item)
    return app


def test_an_app_with_no_declared_behaviour_ships_no_runtime() -> None:
    assert "behaviours.ts" not in _generated(_one_route_app())


def test_the_generated_runtime_carries_the_declared_behaviours() -> None:
    from wreath.policy import IdempotencyPolicy

    app = _one_route_app(IdempotencyPolicy(), method="post")
    files = _generated(app)
    assert "behaviours.ts" in files
    runtime = files["behaviours.ts"]

    # The operation is named beside the behaviour the server declared for it.
    assert '"idempotency-key"' in runtime
    # And the three runtimes the vocabulary needs are all present.
    assert "idempotency-key" in runtime
    assert "if-none-match" in runtime
    assert "retry-after" in runtime


def test_the_generated_retry_is_bounded_and_says_so() -> None:
    from wreath.typegen.targets.typescript import RETRY_CEILING

    app = _one_route_app(RateLimitPolicy(limit=60, window=60.0))
    runtime = _generated(app)["behaviours.ts"]

    assert f"export const RETRY_CEILING = {RETRY_CEILING};" in runtime
    assert "attempt >= RETRY_CEILING" in runtime
    assert RETRY_CEILING < 10, "a ceiling this high is not a ceiling"


def test_the_generated_runtime_imports_nothing() -> None:
    app = _one_route_app(RateLimitPolicy(limit=60, window=60.0))
    runtime = _generated(app)["behaviours.ts"]

    for line in runtime.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import "), f"generated runtime imports: {line}"
        assert "require(" not in stripped, f"generated runtime requires: {line}"


async def test_the_wire_facts_the_generated_retry_relies_on_are_real() -> None:
    app = _one_route_app(RateLimitPolicy(limit=60, window=60.0, burst=1))

    async with TestClient(app) as client:
        await client.get("/widgets")
        limited = await client.get("/widgets")

    assert limited.status == 429
    delay = limited.header("retry-after")
    # The runtime does `Number(raw)` and requires a finite, non-negative value.
    assert delay is not None
    assert float(delay) >= 1.0
