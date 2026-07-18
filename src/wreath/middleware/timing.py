"""Server-Timing response header.

Opt-in, because the duration is visible to anyone who can read the response::

    app.add_middleware(ServerTimingMiddleware(), priority=-1)

The elapsed time is recorded on request state whether or not the header is
emitted, so ``elapsed(request)`` is the single measurement an access log or a
tracing exporter reads later rather than each timing its own span.

The clock is :func:`time.perf_counter`, which is monotonic: a wall-clock step
mid-request cannot produce a negative duration.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .._native import _core
from ..request import Request

if _core is not None and hasattr(_core, "format_server_timing"):
    _format_server_timing: Any = _core.format_server_timing
else:  # pragma: no cover - exercised by the WREATH_PURE test matrix
    from .._pure.observability import format_server_timing as _format_server_timing

_METRIC_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STATE_START = "_wreath_timing_start"
_STATE_ELAPSED = "_wreath_timing_elapsed"


def elapsed(request: Request) -> float:
    """Seconds spent on this request, as measured by ServerTimingMiddleware."""
    value = request.state.get(_STATE_ELAPSED)
    if value is None:
        raise RuntimeError("ServerTimingMiddleware has not timed this request")
    return value


class ServerTimingMiddleware:
    """Record request duration and optionally report it as ``Server-Timing``."""

    global_scope = True
    __slots__ = ("_emit", "_metric")

    def __init__(self, *, metric: str = "total", emit_header: bool = True) -> None:
        # Validated once here so the per-request formatter never has to escape:
        # an unchecked name would be concatenated straight into a header.
        if not _METRIC_NAME.fullmatch(metric):
            raise ValueError("metric must be 1-64 characters of [A-Za-z0-9_-]")
        self._metric = metric.encode("ascii")
        self._emit = emit_header

    async def before(self, request: Request) -> None:
        request.state.__setattr__(_STATE_START, time.perf_counter())
        return None

    async def after(self, request: Request, response: Any) -> Any:
        start = request.state.get(_STATE_START)
        if start is None:
            # A short-circuiting before-hook ahead of this one skipped the timer.
            return response
        duration = time.perf_counter() - start
        request.state.__setattr__(_STATE_ELAPSED, duration)
        if self._emit:
            response.headers.append(
                (b"server-timing", _format_server_timing(self._metric, duration))
            )
        return response


__all__ = ["ServerTimingMiddleware", "elapsed"]
