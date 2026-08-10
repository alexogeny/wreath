"""First-class server timing policy.

Nothing is timed unless this is mounted, because the duration it reports is
visible to anyone who can read the response:

```python
app.configure_http_policy(HttpPolicy(server_timing=ServerTimingPolicy()))
```
The elapsed time is recorded on request state whether or not the header is
emitted, so `elapsed(request)` is the single measurement an access log or a
tracing exporter reads later rather than each timing its own span.

The clock is `time.perf_counter`, which is monotonic: a wall-clock step
mid-request cannot produce a negative duration.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .._native import _core
from .._webpolicy import replace_server_timing
from ..request import Request

_format_server_timing: Any = _core.format_server_timing

_METRIC_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STATE_START = "_wreath_timing_start"
_STATE_ELAPSED = "_wreath_timing_elapsed"


def elapsed(request: Request) -> float:
    """Seconds spent on this request, as measured by `ServerTimingPolicy`.

    The span runs from that policy's `_ingress` stage to its `_egress` stage, so
    it covers routing, authentication, every stage mounted inside it, the
    handler, and response construction -- but not the write to the socket, which
    has not happened yet. It is recorded whether or not the header is emitted,
    so an access log or a tracing exporter reads this one measurement instead of
    timing a span of its own.

    Returns:
        Elapsed seconds as a float, from a monotonic clock.

    Raises:
        RuntimeError: The request was not timed.
    """
    context = request._context
    native = None if context is None else context.policy_elapsed
    if native is not None:
        return native
    value = request.state.get(_STATE_ELAPSED)
    if value is None:
        raise RuntimeError("ServerTimingPolicy has not timed this request")
    return value


class ServerTimingPolicy:
    """Record request duration and optionally report it as `Server-Timing`.

    Global policy. The duration is always recorded on request state for
    `elapsed(request)`; `emit_header` controls only whether it also goes on the
    wire. Emitting is opt-out rather than opt-in here, but the header is visible
    to anyone who can read the response, so turn it off where a timing signal is
    a side channel worth closing.

    The emitted header is one metric in the RFC-registered `Server-Timing`
    format -- `name;dur=milliseconds`, with the duration to three decimal places.
    Milliseconds is what the format specifies; `elapsed` returns seconds.

    The clock is `time.perf_counter`, which is monotonic, so a wall-clock step
    mid-request cannot produce a negative duration. Where this policy sits
    in the chain decides what the number covers -- a stage mounted outside it is
    not in the span.

    A request whose timer never started, because a `_ingress` stage ahead of this
    one short-circuited it, is left untimed rather than reported as zero.

    Args:
        metric: Metric name, 1-64 characters of A-Za-z0-9_- and validated once here.
        emit_header: Send the `Server-Timing` header as well as recording the duration.

    Raises:
        ValueError: `metric` is empty, over 64 characters, or has other characters.
    """

    __slots__ = ("_emit", "_metric")

    def __init__(self, *, metric: str = "total", emit_header: bool = True) -> None:
        # Validated once here so the per-request formatter never has to escape:
        # an unchecked name would be concatenated straight into a header.
        if not _METRIC_NAME.fullmatch(metric):
            raise ValueError("metric must be 1-64 characters of [A-Za-z0-9_-]")
        self._metric = metric.encode("ascii")
        self._emit = emit_header

    def describe(self) -> Any:
        """`Server-Timing`, and only when this instance was told to emit it.

        Built with `emit_header=False` the policy still times the request
        for `request.state`, but the client never sees anything -- so the
        contract is empty, not a header nobody sends.
        """
        from .base import HeaderSpec, PolicyContract

        if not self._emit:
            return PolicyContract()
        return PolicyContract(
            response_headers=(
                (
                    None,
                    HeaderSpec(
                        "Server-Timing",
                        description=(
                            f"Server-side duration as the `{self._metric.decode('ascii')}` "
                            "metric, in milliseconds."
                        ),
                    ),
                ),
            ),
        )

    def _ingress_sync(self, request: Request) -> None:
        """Start the timer for this request."""
        request.state.__setattr__(_STATE_START, time.perf_counter())
        return None

    async def _ingress(self, request: Request) -> None:
        """Reference executor wrapper; compiled policy uses `_ingress_sync`."""
        return self._ingress_sync(request)

    def _egress_inplace(self, request: Request, response: Any) -> None:
        """Record the elapsed time and, when configured, append `Server-Timing`.

        Returns the response unchanged when the timer never started.
        """
        start = request.state.get(_STATE_START)
        if start is None:
            # A short-circuiting _ingress-stage ahead of this one skipped the timer.
            return
        duration = time.perf_counter() - start
        request.state.__setattr__(_STATE_ELAPSED, duration)
        if self._emit:
            replace_server_timing(
                response.headers,
                self._metric,
                _format_server_timing(self._metric, duration),
            )

    def _egress_sync(self, request: Request, response: Any) -> Any:
        """Reference executor transformer; compiled policy mutates in place."""
        self._egress_inplace(request, response)
        return response

    async def _egress(self, request: Request, response: Any) -> Any:
        """Reference executor wrapper; compiled policy mutates in place."""
        return self._egress_sync(request, response)


__all__ = ["ServerTimingPolicy", "elapsed"]
