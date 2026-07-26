"""Liveness and readiness health checks.

Exposes ``/health`` (liveness -- is the process up) and ``/ready`` (readiness --
can it serve, i.e. its dependencies answer). Mount the router the app already
knows how to include::

    from wreath.health import health_router, callable_check, database_check

    checks = [database_check("postgres", lambda: db.pool("read").fetchval("SELECT 1"))]
    app.include_router(health_router(checks))

Liveness is always 200 unless ``is_live()`` says the process is draining.

Readiness runs every check concurrently, under a per-check timeout, and reports
one of three states. ``ready`` (200) is everything passing. ``degraded`` (200) is
a **non-critical** check failing -- an operator should look, but the instance
still serves. ``unready`` (503) is a critical check failing; take it out of the
load balancer::

    checks = [
        database_check("postgres", lambda: db.pool("read").fetchval("SELECT 1")),
        callable_check("analytics", ping_sink, critical=False, timeout=0.25),
    ]

Every check reports its own ``duration_ms``, so a readiness endpoint that got
slow says which dependency did it. A probe that overruns its ``timeout`` is
recorded as ``timeout`` rather than delaying the response -- an unreachable
dependency usually hangs instead of raising, and a readiness endpoint that hangs
is strictly worse than one that answers "no".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .request import Request
from .response import JSONResponse
from .router import Router

Probe = Callable[[], Awaitable[Any]]

__all__ = [
    "HealthCheck",
    "callable_check",
    "database_check",
    "evaluate",
    "health_router",
    "postgres_check",
    "readiness_status",
]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A named readiness probe. ``probe`` raises or returns a detail mapping.

    ``critical`` decides what a failure means. A critical check that fails makes
    the process unready (503) -- take it out of the load balancer. A
    non-critical one that fails reports ``degraded`` and still serves: a stale
    cache warmer or a lagging analytics sink is not a reason to drop traffic.

    ``timeout`` bounds the probe in seconds. A probe that overruns is recorded
    as ``timeout`` and treated as a failure, but it never delays the response --
    an unreachable dependency usually hangs rather than raising, and a readiness
    endpoint that hangs is worse than one that says "no".
    """

    name: str
    probe: Probe
    critical: bool = True
    timeout: float | None = 1.0


def callable_check(
    name: str, fn: Probe, *, critical: bool = True, timeout: float | None = 1.0
) -> HealthCheck:
    """Wrap any ``async`` callable as a readiness check (raise = unhealthy)."""
    return HealthCheck(name=name, probe=fn, critical=critical, timeout=timeout)


def database_check(name: str, ping: Probe) -> HealthCheck:
    """A DB readiness check. ``ping`` should run e.g. ``SELECT 1`` and return/raise.

    Supply the ping yourself when the connection is not a Wreath ``Database``;
    for one that is, :func:`postgres_check` wires it for you.
    """
    return HealthCheck(name=name, probe=ping)


def postgres_check(
    database: Any,
    *,
    name: str = "postgres",
    workload: str = "security_read",
    critical: bool = True,
    timeout: float = 1.0,
) -> HealthCheck:
    """Readiness for a Wreath ``Database``: one round trip, reporting latency.

    ``security_read`` by default, deliberately. Readiness answers "can this
    instance serve", and the app pools are exactly what saturates under load --
    probing one would report *unready* for an instance that is merely busy, and
    a load balancer would then remove the instance that is working hardest,
    making the incident worse. The security pool is small, separate, and
    reserved, so this measures reachability rather than saturation.
    """

    async def probe() -> dict[str, Any]:
        started = perf_counter()
        connection = await database.acquire(workload)
        try:
            await connection.fetchval("SELECT 1")
        finally:
            await database.release(workload, connection)
        return {"round_trip_ms": round((perf_counter() - started) * 1000.0, 3)}

    return HealthCheck(name=name, probe=probe, critical=critical, timeout=timeout)


async def _run_check(check: HealthCheck) -> tuple[bool, dict[str, Any]]:
    """Run one probe under its timeout, timing it. Never raises."""
    started = perf_counter()

    def elapsed() -> float:
        # Rounded at the boundary: this lands in a JSON body a human reads.
        return round((perf_counter() - started) * 1000.0, 3)

    try:
        if check.timeout is None:
            detail = await check.probe()
        else:
            async with asyncio.timeout(check.timeout):
                detail = await check.probe()
    except TimeoutError:
        # A hung dependency is the common failure, and the one a bare `await`
        # turns into a hung readiness endpoint.
        return False, {
            "status": "timeout",
            "critical": check.critical,
            "duration_ms": elapsed(),
            "timeout_s": check.timeout,
        }
    except Exception as exc:  # a failing probe is "unhealthy", never a 500
        return False, {
            "status": "fail",
            "critical": check.critical,
            "duration_ms": elapsed(),
            "error": str(exc),
        }
    extra = detail if isinstance(detail, dict) else {}
    return True, {
        "status": "pass",
        "critical": check.critical,
        "duration_ms": elapsed(),
        **extra,
    }


async def evaluate(checks: Iterable[HealthCheck]) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Run every check concurrently; return ``(serving, per_check_detail)``.

    ``serving`` is false only when a **critical** check failed. A failed
    non-critical check leaves it true and shows up as ``degraded`` in the
    readiness body -- see :func:`readiness_status`.
    """
    check_list = tuple(checks)
    results = await asyncio.gather(*(_run_check(check) for check in check_list))
    detail = {check.name: body for check, (_ok, body) in zip(check_list, results, strict=True)}
    serving = all(ok for (ok, _), check in zip(results, check_list, strict=True) if check.critical)
    return serving, detail


def readiness_status(detail: dict[str, dict[str, Any]]) -> str:
    """``ready`` | ``degraded`` | ``unready``, from an :func:`evaluate` detail."""
    failed = [body for body in detail.values() if body.get("status") != "pass"]
    if any(body.get("critical") for body in failed):
        return "unready"
    return "degraded" if failed else "ready"


def health_router(
    checks: Iterable[HealthCheck] = (),
    *,
    liveness_path: str = "/health",
    readiness_path: str = "/ready",
    is_live: Callable[[], bool] | None = None,
) -> Router:
    """Build a ``Router`` exposing liveness + readiness endpoints.

    TODO: ``app.health(...)`` convenience wiring (``app.py`` owned by a
    concurrent fork); until then include the returned router yourself.
    """
    router = Router()
    check_list = tuple(checks)

    @router.get(liveness_path)
    async def _liveness(request: Request) -> JSONResponse:
        if is_live is not None and not is_live():
            return JSONResponse({"status": "shutting_down"}, status=503)
        return JSONResponse({"status": "ok"})

    @router.get(readiness_path)
    async def _readiness(request: Request) -> JSONResponse:
        serving, detail = await evaluate(check_list)
        # `degraded` still serves: a non-critical dependency being down is
        # information for an operator, not a reason to drop the instance.
        return JSONResponse(
            {"status": readiness_status(detail), "checks": detail},
            status=200 if serving else 503,
        )

    return router
