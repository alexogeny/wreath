"""Liveness and readiness health checks.

Exposes ``/health`` (liveness -- is the process up) and ``/ready`` (readiness --
can it serve, i.e. its dependencies answer). Mount the router the app already
knows how to include::

    from wreath.health import health_router, callable_check, database_check

    checks = [database_check("postgres", lambda: db.pool("read").fetchval("SELECT 1"))]
    app.include_router(health_router(checks))

Liveness is always 200 unless ``is_live()`` says the process is draining;
readiness runs every check concurrently and is 200 only if all pass, else 503.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .request import Request
from .response import JSONResponse
from .router import Router

Probe = Callable[[], Awaitable[Any]]

__all__ = [
    "HealthCheck",
    "callable_check",
    "database_check",
    "health_router",
]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A named readiness probe. ``probe`` raises or returns a detail mapping."""

    name: str
    probe: Probe


def callable_check(name: str, fn: Probe) -> HealthCheck:
    """Wrap any ``async`` callable as a readiness check (raise = unhealthy)."""
    return HealthCheck(name=name, probe=fn)


def database_check(name: str, ping: Probe) -> HealthCheck:
    """A DB readiness check. ``ping`` should run e.g. ``SELECT 1`` and return/raise.

    Supply the ping from your own ``Database``/pool so this module stays
    decoupled from the driver. TODO: a first-class ``postgres_check(database)``
    once the ``Database`` ping surface is pinned (out of this module's scope).
    """
    return HealthCheck(name=name, probe=ping)


async def _run_check(check: HealthCheck) -> tuple[bool, dict[str, Any]]:
    try:
        detail = await check.probe()
    except Exception as exc:  # a failing probe is "unhealthy", never a 500
        return False, {"status": "fail", "error": str(exc)}
    extra = detail if isinstance(detail, dict) else {}
    return True, {"status": "pass", **extra}


async def evaluate(checks: Iterable[HealthCheck]) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Run every check concurrently; return ``(all_healthy, per_check_detail)``."""
    check_list = tuple(checks)
    results = await asyncio.gather(*(_run_check(check) for check in check_list))
    detail = {check.name: body for check, (_ok, body) in zip(check_list, results, strict=True)}
    healthy = all(ok for ok, _ in results)
    return healthy, detail


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
        healthy, detail = await evaluate(check_list)
        return JSONResponse(
            {"status": "ready" if healthy else "unready", "checks": detail},
            status=200 if healthy else 503,
        )

    return router
