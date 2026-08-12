"""CloudWatch Embedded Metric Format (EMF) bridge for the Native Flight Recorder.

Renders the same `Projector.snapshot()` aggregates the Prometheus/OTLP bridges
read as EMF structured-JSON log lines. Written to stdout (or a sink), CloudWatch
Logs parses them into metrics with **zero infrastructure** — ideal for ECS/Lambda.

EMF carries metric *values* at the JSON root alongside dimension values, and one
blob is one dimension-value combination, so per-route metrics are emitted as one
blob per route plus a global blob. Counters are emitted as **deltas** since the
previous emit (CloudWatch SUMs over the period) unless `cumulative=True`; gauges
are absolute. Zero-dependency: stdlib `json` only (no `boto3`).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from ._native import _core
from ._prometheus import RouteLabels

__all__ = ["EmfBridge", "activate_cloudwatch_emf"]

#: CloudWatch caps metric definitions per EMF blob at 100.
_MAX_METRICS_PER_BLOB = 100


class EmfBridge:
    """Renders projector metrics as CloudWatch EMF blobs.

    `source` exposes `snapshot()` (and optionally `recorder_loss()`).
    `dimensions` is a static dimension mapping (e.g. `{"Service": "trailhead"}`)
    applied to every blob; per-route blobs add the route labels as dimensions.
    """

    __slots__ = ("_source", "_namespace", "_dims", "_route_labels", "_cumulative", "_deltas")

    def __init__(
        self,
        source: Any,
        *,
        namespace: str = "Wreath",
        dimensions: dict[str, str] | None = None,
        route_labels: RouteLabels = None,
        cumulative: bool = False,
    ) -> None:
        if not hasattr(source, "snapshot"):
            raise TypeError("cloudwatch-emf source must expose snapshot()")
        self._source = source
        self._namespace = namespace
        self._dims = {k: str(v) for k, v in (dimensions or {}).items()}
        self._route_labels = route_labels
        self._cumulative = cumulative
        self._deltas = _core.metric_delta_state()

    def blobs(self, snapshot: Any, *, timestamp_ms: int,
              recorder_loss: dict | None = None) -> list[dict]:
        """The list of EMF blobs for one snapshot."""
        rendered = _core.emf_render(
            snapshot,
            timestamp_ms,
            recorder_loss,
            self._namespace,
            self._dims,
            self._route_labels,
            self._cumulative,
            self._deltas,
            _MAX_METRICS_PER_BLOB,
        )
        return [json.loads(line) for line in rendered.splitlines()]

    def render(self, *, timestamp_ms: int | None = None) -> str:
        """Newline-separated EMF JSON blobs for the current snapshot."""
        ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
        snapshot = self._source.snapshot()
        recorder_loss = None
        getter = getattr(self._source, "recorder_loss", None)
        if callable(getter):
            recorder_loss = getter()
        return _core.emf_render(
            snapshot,
            ts,
            recorder_loss,
            self._namespace,
            self._dims,
            self._route_labels,
            self._cumulative,
            self._deltas,
            _MAX_METRICS_PER_BLOB,
        )

    def emit(self, *, timestamp_ms: int | None = None, sink: Any = None) -> None:
        """Write the EMF blobs to `sink` (default stdout), newline-terminated."""
        (sink or sys.stdout).write(self.render(timestamp_ms=timestamp_ms) + "\n")


def activate_cloudwatch_emf(
    source: Any,
    *,
    namespace: str = "Wreath",
    dimensions: dict[str, str] | None = None,
    route_labels: RouteLabels = None,
    cumulative: bool = False,
) -> EmfBridge:
    """Wrap a snapshot source in a CloudWatch EMF bridge (see module doc)."""
    return EmfBridge(
        source, namespace=namespace, dimensions=dimensions,
        route_labels=route_labels, cumulative=cumulative,
    )
