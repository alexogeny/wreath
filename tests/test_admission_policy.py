from __future__ import annotations

import asyncio
import time

import pytest

from wreath import Wreath
from wreath.policy import (
    ConcurrencyPolicy,
    DeadlinePolicy,
    HttpPolicy,
    MaintenancePolicy,
)
from wreath.testing import TestClient


@pytest.mark.asyncio
async def test_concurrency_policy_refuses_instead_of_building_a_wait_queue() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    policy = ConcurrencyPolicy(1, retry_after=1)
    app = Wreath(http_policy=HttpPolicy(concurrency=policy))

    @app.get("/")
    async def work(request):
        entered.set()
        await release.wait()
        return "ok"

    async with TestClient(app) as client:
        first = asyncio.create_task(client.get("/"))
        await entered.wait()
        refused = await client.get("/")
        release.set()
        admitted = await first

    assert admitted.status == 200
    assert refused.status == 503
    assert refused.header("retry-after") == "1"
    assert refused.json()["detail"] == "Request concurrency limit reached"
    assert policy.stats().active == 0
    assert policy.stats().refused == 1


@pytest.mark.asyncio
async def test_concurrency_permit_is_released_when_a_handler_raises() -> None:
    calls = 0
    policy = ConcurrencyPolicy(1)
    app = Wreath(http_policy=HttpPolicy(concurrency=policy))

    @app.get("/")
    async def work(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return "recovered"

    async with TestClient(app) as client:
        failed = await client.get("/")
        recovered = await client.get("/")

    assert failed.status == 500
    assert recovered.status == 200
    assert policy.stats().active == 0


@pytest.mark.asyncio
async def test_deadline_policy_cancels_an_async_handler_and_answers_504() -> None:
    cancelled = asyncio.Event()
    app = Wreath(http_policy=HttpPolicy(deadline=DeadlinePolicy(0.01)))

    @app.get("/")
    async def slow(request):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    async with TestClient(app) as client:
        response = await client.get("/")

    assert response.status == 504
    assert response.json()["detail"] == "Request handler exceeded its deadline"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_a_handlers_own_timeout_error_is_not_mislabeled_as_the_deadline() -> None:
    app = Wreath(http_policy=HttpPolicy(deadline=DeadlinePolicy(1)))

    @app.get("/")
    async def timeout(request):
        raise TimeoutError("application timeout")

    async with TestClient(app) as client:
        response = await client.get("/")

    assert response.status == 500
    assert response.json()["detail"] != "Request handler exceeded its deadline"


@pytest.mark.asyncio
async def test_deadline_policy_measures_a_synchronous_handler_after_it_returns() -> None:
    app = Wreath(http_policy=HttpPolicy(deadline=DeadlinePolicy(0.005)))

    @app.get("/")
    def slow(request):
        time.sleep(0.02)
        return "too late"

    async with TestClient(app) as client:
        response = await client.get("/")

    assert response.status == 504
    assert response.json()["detail"] == "Request handler exceeded its deadline"


@pytest.mark.asyncio
async def test_maintenance_switch_is_dynamic_exact_and_counted() -> None:
    policy = MaintenancePolicy(exempt_paths=("/ready",), retry_after=30)
    app = Wreath(http_policy=HttpPolicy(maintenance=policy))

    @app.get("/")
    async def home(request):
        return "home"

    @app.get("/ready")
    async def ready(request):
        return "ready"

    @app.get("/ready/delete")
    async def not_exempt(request):
        return "must close"

    async with TestClient(app) as client:
        assert (await client.get("/")).status == 200
        policy.enable()
        refused = await client.get("/")
        exact = await client.get("/ready")
        prefix = await client.get("/ready/delete")
        policy.disable()
        reopened = await client.get("/")

    assert refused.status == 503
    assert refused.header("retry-after") == "30"
    assert exact.status == 200
    assert prefix.status == 503
    assert reopened.status == 200
    assert policy.refused == 2


def test_maintenance_policy_keeps_the_native_policy_program_available() -> None:
    policy = HttpPolicy(
        maintenance=MaintenancePolicy(active=True, exempt_paths=("/ready",))
    )
    descriptor = policy._native_descriptor
    assert descriptor is not None
    assert descriptor[0] == "wreath.http-policy.v4"
    assert descriptor[-1] is not None


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: ConcurrencyPolicy(0), "limit must be a positive integer"),
        (lambda: DeadlinePolicy(0), "seconds must be positive"),
        (lambda: DeadlinePolicy(float("nan")), "positive and finite"),
        (lambda: DeadlinePolicy(float("inf")), "positive and finite"),
        (lambda: MaintenancePolicy(active=1), "active must be a bool"),
        (
            lambda: MaintenancePolicy(exempt_paths=("relative",)),
            "absolute paths beginning with '/'",
        ),
    ],
)
def test_admission_configuration_refuses_at_construction(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()
