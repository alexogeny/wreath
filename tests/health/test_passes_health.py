"""The `passes` check: alerting-visible, and deliberately not on the traffic path.

The instinct runs the other way, so the rule is worth restating. A blocked
backfill is a data problem and the application is still serving correctly.
Failing readiness for it converts that data problem into an outage -- and
removes the very workers that would have resumed the pass. What a stuck pass
needs is a person.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.health import (
    callable_check,
    evaluate,
    health_router,
    passes_check,
    readiness_status,
)

NOW = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)


class _Row:
    """Just enough of a `PassStatus` for the probe to read."""

    def __init__(self, name, state, *, gate_barred=False):
        self.name = name
        self._state = state
        self.gate_barred = gate_barred

    @property
    def state(self):
        return self._state


class _Database:
    def __init__(self, rows):
        self.rows = rows

    async def acquire(self, workload):
        return self

    async def release(self, workload, connection):
        return None


@pytest.fixture
def statuses(monkeypatch):
    """Steer `read_status` without needing a ledger."""
    holder = {"rows": []}

    async def fake_read_status(database, *, schema="wreath", name=None, workload="write"):
        return holder["rows"]

    import wreath.passes

    monkeypatch.setattr(wreath.passes, "read_status", fake_read_status)
    return holder


@pytest.mark.asyncio
async def test_a_walking_pass_is_reported_in_flight_and_passes(statuses):
    statuses["rows"] = [_Row("normalize_grades", "walking"), _Row("purge", "slow")]

    healthy, detail = await evaluate([passes_check(_Database([]))])

    # An orchestrator can see the deploy's data work is unfinished even though
    # the pods are serving, which was the whole reason to report it at all.
    assert healthy is True
    assert detail["passes"]["status"] == "pass"
    assert detail["passes"]["in_flight"] == ["normalize_grades", "purge"]


@pytest.mark.asyncio
async def test_a_blocked_pass_fails_the_check_and_names_itself(statuses):
    statuses["rows"] = [_Row("normalize_grades", "blocked")]

    _healthy, detail = await evaluate([passes_check(_Database([]))])

    assert detail["passes"]["status"] == "fail"
    assert "normalize_grades" in detail["passes"]["error"]


@pytest.mark.asyncio
async def test_a_stalled_pass_fails_the_check_too(statuses):
    statuses["rows"] = [_Row("rollup", "stalled")]

    _healthy, detail = await evaluate([passes_check(_Database([]))])

    assert detail["passes"]["status"] == "fail"
    assert "stalled: rollup" in detail["passes"]["error"]


@pytest.mark.asyncio
async def test_a_blocked_pass_never_makes_the_instance_unready(statuses):
    statuses["rows"] = [_Row("normalize_grades", "blocked")]

    serving, detail = await evaluate(
        [callable_check("postgres", _ok), passes_check(_Database([]))]
    )

    # This is the load-bearing assertion of the whole check. `serving` stays
    # true, so the load balancer keeps the instance -- and keeps the worker that
    # would resume the pass.
    assert serving is True
    assert readiness_status(detail) == "degraded"


@pytest.mark.asyncio
async def test_the_check_is_not_critical_so_it_cannot_be_wired_into_a_503(statuses):
    # Even if somebody puts it in `checks=` instead of `alerts=`, it must not be
    # able to take traffic away. Non-critical is the belt to the alerts path's
    # braces.
    assert passes_check(_Database([])).critical is False


@pytest.mark.asyncio
async def test_the_alerts_path_is_separate_from_readiness(statuses):
    statuses["rows"] = [_Row("normalize_grades", "blocked")]
    router = health_router(
        [callable_check("postgres", _ok)], alerts=[passes_check(_Database([]))]
    )

    paths = {route.path for route in router.routes}

    assert "/ready" in paths
    assert "/health/alerts" in paths


@pytest.mark.asyncio
async def test_a_barred_gate_is_reported_even_while_the_pass_walks_on(statuses):
    statuses["rows"] = [_Row("rollup", "walking", gate_barred=True)]

    healthy, detail = await evaluate([passes_check(_Database([]))])

    # Skipping bought throughput, so the pass is healthy and moving. The barred
    # gate is a separate fact and it still has to be visible.
    assert healthy is True
    assert detail["passes"]["gate_barred"] == ["rollup"]


async def _ok():
    return {"latency_ms": 1}
