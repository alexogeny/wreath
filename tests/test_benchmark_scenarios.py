from benchmarks.load import _build_request
from benchmarks.scenarios import JSON_REQUEST_BODY, SCENARIOS


def test_build_request_includes_scenario_headers_and_body_length() -> None:
    request = _build_request(
        "127.0.0.1",
        8000,
        "/json-body",
        "POST",
        JSON_REQUEST_BODY,
        (("Content-Type", "application/json"),),
    )

    assert request.startswith(b"POST /json-body HTTP/1.1\r\n")
    assert b"Content-Type: application/json\r\n" in request
    assert f"Content-Length: {len(JSON_REQUEST_BODY)}\r\n".encode() in request
    assert request.endswith(b"\r\n\r\n" + JSON_REQUEST_BODY)


def test_scenario_capabilities_allow_incremental_framework_support() -> None:
    stream = SCENARIOS["stream-4x256"]

    assert stream.supports("wreath")
    assert stream.supports("starlette")
    assert not stream.supports("sanic")
    assert not stream.supports("litestar")

    middleware = SCENARIOS["middleware-noop"]
    assert middleware.supports("wreath")
    assert not middleware.supports("starlette")

    assert SCENARIOS["auth-rbac-allow"].supports("wreath")
    assert not SCENARIOS["auth-rbac-allow"].supports("fastapi")


def test_template_and_cache_scenarios_include_asgi_competitors() -> None:
    template = SCENARIOS["template"]
    cache = SCENARIOS["cache-control"]
    for framework in ("wreath", "wreath-native", "starlette", "fastapi", "sanic", "blacksheep"):
        assert template.supports(framework)
        assert cache.supports(framework)
    # Traditional-tier frameworks are not part of the ASGI comparison here.
    assert not template.supports("django")
    assert not cache.supports("flask")


def test_webhook_scenario_is_wreath_only_signed_payload() -> None:
    webhook = SCENARIOS["webhook"]
    assert webhook.supports("wreath")
    assert webhook.supports("wreath-native")
    # Competitors have no webhook primitive, so the scenario is Wreath-only.
    assert not webhook.supports("starlette")
    assert not webhook.supports("sanic")
    # The scenario carries a signed request the verifier accepts.
    header_names = {name.lower() for name, _ in webhook.headers}
    assert "wreath-webhook-signature" in header_names
    assert webhook.method == "POST"


def test_large_subrouter_pruning_benchmark_builds_protected_tree() -> None:
    from benchmarks.bench_router_pruning import build_application

    app, target = build_application(3, 4)
    capabilities = app._capabilities
    eligible = (
        capabilities["authenticated"]
        | capabilities["permission:control:access"]
        | capabilities["permission:tenant:2:read"]
    )
    assert app._match("GET", target, 0) is None
    assert app._match("GET", target, eligible) is not None
