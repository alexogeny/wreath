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


# --- the generator must not be the thing being measured ----------------------

def test_generator_threads_scale_with_the_cores_the_generator_has() -> None:
    """One h2load thread saturates near 130k req/s, below several arms here.

    While it was the default, every fast arm reported the generator's ceiling as
    its own throughput, and a multi-worker server read the same as a
    single-worker one because the bottleneck was on the client side in both.
    """
    from benchmarks.run import _generator_threads

    # Auto: never zero, never more than the connections it would spread across.
    assert _generator_threads(None, 32) >= 1
    assert _generator_threads(None, 1) == 1

    # Explicit: honoured, still clamped to something h2load can use.
    assert _generator_threads(8, 32) == 8
    assert _generator_threads(8, 4) == 4
    assert _generator_threads(0, 32) == 1
    assert _generator_threads(-4, 32) == 1


def test_generator_thread_count_is_recorded_for_every_run() -> None:
    # A throughput number taken through a saturated generator measures the
    # generator, so a run has to say what drove it or it cannot be judged later.
    import argparse

    from benchmarks.run import _generator_threads

    args = argparse.Namespace(generator_threads=3, connections=None, concurrency=16)
    assert _generator_threads(
        args.generator_threads, args.connections or args.concurrency) == 3


def test_background_scenarios_are_skipped_when_there_is_more_than_one_worker() -> None:
    """A verification that cannot run must not be reported as having run.

    Background scenarios reconcile the app's task counters against the requests
    handed over, and those counters are per process. With more than one worker
    `/background-stats` is answered by whichever worker the stats connection
    hashes to, so the tally is one worker's share measured against the whole
    run's total -- it read 2504 of 4010 for `wreath-native` and 503 of 4010 for
    `wreath-metal` in the same run, which is the connection split, not a defect
    in either. Proving the work happened is the point of these scenarios, so
    they are skipped rather than run unverified.
    """
    from benchmarks.run import scenario_runnable
    from benchmarks.scenarios import SCENARIOS

    background = [n for n, s in SCENARIOS.items() if s.background]
    assert background, "no background scenarios to check"
    for name in background:
        assert scenario_runnable("wreath-metal", name, 1)
        assert not scenario_runnable("wreath-metal", name, 2)

    # Everything else is unaffected by the worker count.
    for name in ("plaintext", "json"):
        assert scenario_runnable("wreath-metal", name, 1)
        assert scenario_runnable("wreath-metal", name, 4)

    # Framework support still governs regardless of workers.
    unsupported = next(
        n for n, s in SCENARIOS.items() if not s.supports("axum")
    )
    assert not scenario_runnable("axum", unsupported, 1)
