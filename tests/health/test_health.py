from __future__ import annotations

import asyncio
import time

import pytest
from _pgfidelity import check_for

from wreath.health import (
    callable_check,
    evaluate,
    health_router,
    postgres_check,
    readiness_status,
)


async def _ok():
    return {"latency_ms": 1}


async def _bad():
    raise RuntimeError("down")


@pytest.mark.asyncio
async def test_evaluate_all_pass():
    healthy, detail = await evaluate([callable_check("a", _ok), callable_check("b", _ok)])
    assert healthy is True
    assert detail["a"]["status"] == "pass" and detail["a"]["latency_ms"] == 1


@pytest.mark.asyncio
async def test_evaluate_one_fail():
    healthy, detail = await evaluate([callable_check("a", _ok), callable_check("db", _bad)])
    assert healthy is False
    assert detail["db"]["status"] == "fail" and "down" in detail["db"]["error"]


def test_health_router_registers_both_endpoints():
    router = health_router([callable_check("db", _ok)])
    paths = {getattr(route, "path", None) for route in router.routes}
    assert {"/health", "/ready"} <= paths, paths


def test_health_router_mounts_alerts_only_when_asked():
    without = {getattr(r, "path", None) for r in health_router([]).routes}
    withal = {
        getattr(r, "path", None)
        for r in health_router([], alerts=[callable_check("passes", _ok)]).routes
    }
    assert "/health/alerts" in withal
    assert "/health/alerts" in without  # the path exists either way...
    assert withal == without  # ...only the probe list differs


async def _hang() -> None:
    await asyncio.sleep(10)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
def test_health_check_timeout_must_be_finite(timeout: float):
    with pytest.raises(ValueError, match="finite"):
        callable_check("db", _hang, timeout=timeout)


@pytest.mark.asyncio
async def test_a_failed_non_critical_check_degrades_without_dropping_traffic():
    serving, detail = await evaluate(
        [callable_check("db", _ok), callable_check("cache", _bad, critical=False)]
    )
    assert serving is True  # still take traffic
    assert readiness_status(detail) == "degraded"
    assert detail["cache"]["status"] == "fail"
    assert detail["cache"]["critical"] is False


@pytest.mark.asyncio
async def test_a_failed_critical_check_is_unready():
    serving, detail = await evaluate(
        [callable_check("db", _bad), callable_check("cache", _ok, critical=False)]
    )
    assert serving is False
    assert readiness_status(detail) == "unready"


@pytest.mark.asyncio
async def test_all_passing_is_ready():
    serving, detail = await evaluate([callable_check("a", _ok)])
    assert serving is True
    assert readiness_status(detail) == "ready"


@pytest.mark.asyncio
async def test_a_hung_probe_times_out_instead_of_hanging_the_endpoint():
    started = time.perf_counter()
    serving, detail = await evaluate([callable_check("db", _hang, timeout=0.05)])
    elapsed = time.perf_counter() - started

    assert serving is False
    assert detail["db"]["status"] == "timeout"
    assert detail["db"]["timeout_s"] == 0.05
    assert elapsed < 1.0, elapsed  # nowhere near the probe's 10s sleep


@pytest.mark.asyncio
async def test_a_hung_non_critical_probe_only_degrades():
    serving, detail = await evaluate(
        [callable_check("metrics", _hang, critical=False, timeout=0.05)]
    )
    assert serving is True
    assert readiness_status(detail) == "degraded"


@pytest.mark.asyncio
async def test_every_check_reports_its_duration():
    _serving, detail = await evaluate([callable_check("a", _ok), callable_check("b", _bad)])
    for body in detail.values():
        assert isinstance(body["duration_ms"], float)
        assert body["duration_ms"] >= 0.0


@pytest.mark.asyncio
async def test_checks_run_concurrently_not_serially():
    async def slow() -> None:
        await asyncio.sleep(0.05)

    started = time.perf_counter()
    await evaluate([callable_check(f"c{i}", slow, timeout=2.0) for i in range(8)])
    elapsed = time.perf_counter() - started
    # Serial would be ~0.4s; concurrent is ~0.05s. Generous bound for CI.
    assert elapsed < 0.25, elapsed


@pytest.mark.asyncio
async def test_no_checks_is_ready():
    serving, detail = await evaluate([])
    assert serving is True
    assert readiness_status(detail) == "ready"


@pytest.mark.asyncio
async def test_a_probe_may_return_extra_detail():
    async def with_detail() -> dict:
        return {"lag_ms": 12}

    _serving, detail = await evaluate([callable_check("bus", with_detail)])
    assert detail["bus"]["lag_ms"] == 12
    assert detail["bus"]["status"] == "pass"


class FakeConnection:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def fetchval(self, sql: str) -> int:
        check_for(self, sql, ())
        if self.fail:
            raise ConnectionRefusedError("no route to host")
        return 1


class FakeDatabase:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, workload: str):
        self.acquired.append(workload)
        return FakeConnection(self.fail)

    async def release(self, workload: str, connection) -> None:
        self.released.append(workload)


@pytest.mark.asyncio
async def test_postgres_check_probes_the_reserved_pool_not_an_app_pool():
    database = FakeDatabase()
    serving, detail = await evaluate([postgres_check(database)])

    assert serving is True
    assert database.acquired == ["security_read"]
    assert detail["postgres"]["status"] == "pass"
    assert "round_trip_ms" in detail["postgres"]


@pytest.mark.asyncio
async def test_postgres_check_returns_the_connection_even_when_the_probe_fails():
    database = FakeDatabase(fail=True)
    serving, detail = await evaluate([postgres_check(database)])

    assert serving is False
    assert detail["postgres"]["status"] == "fail"
    assert database.released == ["security_read"]  # never leaked


@pytest.mark.asyncio
async def test_postgres_check_can_be_non_critical():
    database = FakeDatabase(fail=True)
    serving, detail = await evaluate([postgres_check(database, name="replica", critical=False)])
    assert serving is True
    assert readiness_status(detail) == "degraded"
