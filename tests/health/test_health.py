"""Health checks: probe aggregation and the readiness router."""

from __future__ import annotations

import pytest

from wreath.health import callable_check, evaluate, health_router


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


def test_health_router_builds():
    router = health_router([callable_check("db", _ok)])
    # a Router with the two endpoints registered
    assert router is not None
