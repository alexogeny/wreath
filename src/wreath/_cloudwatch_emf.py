"""CloudWatch Embedded Metric Format (EMF) bridge for the Native Flight Recorder.

Renders the same `Projector.snapshot()` aggregates and canonical subsystem
counter rows as the other metrics bridges, as EMF structured-JSON log lines.
Written to stdout (or a sink), CloudWatch Logs parses them into metrics with
**zero infrastructure** — ideal for ECS/Lambda.

EMF carries metric *values* at the JSON root alongside dimension values, and one
blob is one dimension-value combination, so per-route metrics are emitted as one
blob per route plus a global blob. Counters are emitted as **deltas** since the
previous emit (CloudWatch SUMs over the period) unless `cumulative=True`; gauges
are absolute. Zero-dependency: stdlib `json` only (no `boto3`).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

from ._native import _core
from ._prometheus import RouteLabels

__all__ = ["EmfBridge", "activate_cloudwatch_emf"]

#: CloudWatch caps metric definitions per EMF blob at 100.
_MAX_METRICS_PER_BLOB = 100


class EmfBridge:
    """Renders projector metrics as CloudWatch EMF blobs.

    `source` exposes `snapshot()` (and optionally `recorder_loss()`). `app` and
    `counter_sources` use the same `metrics.collect` seam as Prometheus/StatsD.
    `dimensions` is a static dimension mapping (e.g. `{"Service": "trailhead"}`)
    applied to every blob; per-route blobs add the route labels as dimensions.
    """

    __slots__ = (
        "_app",
        "_counter_sources",
        "_cumulative",
        "_deltas",
        "_dims",
        "_namespace",
        "_route_labels",
        "_source",
    )

    def __init__(
        self,
        source: Any,
        *,
        namespace: str = "Wreath",
        dimensions: dict[str, str] | None = None,
        route_labels: RouteLabels = None,
        cumulative: bool = False,
        app: Any = None,
        counter_sources: Sequence[Any] = (),
    ) -> None:
        from .metrics import _counter_sources, _snapshot_source

        self._source = _snapshot_source(source, bridge="cloudwatch-emf")

        explicit_sources = _counter_sources(counter_sources, bridge="CloudWatch")
        self._app = app
        self._counter_sources = explicit_sources
        self._namespace = namespace
        self._dims = {k: str(v) for k, v in (dimensions or {}).items()}
        self._route_labels = route_labels
        self._cumulative = cumulative
        self._deltas = _core.metric_delta_state()

    def blobs(
        self, snapshot: Any, *, timestamp_ms: int, recorder_loss: dict | None = None
    ) -> list[dict]:
        """The list of EMF blobs for one snapshot."""
        from .metrics import collect

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
            collect(self._app, self._counter_sources),
        )
        return [json.loads(line) for line in rendered.splitlines()]

    def render(self, *, timestamp_ms: int | None = None) -> str:
        """Newline-separated EMF JSON blobs for the current snapshot."""
        ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
        from .metrics import _read_snapshot

        snapshot, recorder_loss = _read_snapshot(self._source)
        from .metrics import collect

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
            collect(self._app, self._counter_sources),
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
    app: Any = None,
    counter_sources: Sequence[Any] = (),
) -> EmfBridge:
    """Wrap a snapshot source in a CloudWatch EMF bridge (see module doc)."""
    return EmfBridge(
        source,
        namespace=namespace,
        dimensions=dimensions,
        route_labels=route_labels,
        cumulative=cumulative,
        app=app,
        counter_sources=counter_sources,
    )


def _counter_blobs(
    readings: Sequence[Any],
    *,
    timestamp_ms: int,
    namespace: str,
    dimensions: dict[str, str],
    cumulative: bool,
    deltas: dict[tuple[str, str, str], int],
    lock: threading.Lock,
) -> list[dict[str, Any]]:
    """Render canonical counter rows; EMF is only the envelope layer."""
    blobs: list[dict[str, Any]] = []
    for reading in readings:
        with lock:
            rendered: list[tuple[str, int]] = []
            for name, value in reading.values.items():
                current = int(value)
                key = (reading.subsystem, reading.instance, name)
                previous = deltas.get(key)
                deltas[key] = current
                if cumulative or name in reading.gauges or previous is None or current < previous:
                    emitted = current
                else:
                    emitted = current - previous
                rendered.append((name, emitted))
        items = tuple(rendered)
        for offset in range(0, len(items), _MAX_METRICS_PER_BLOB):
            batch = items[offset : offset + _MAX_METRICS_PER_BLOB]
            dims = {**dimensions, "Instance": reading.instance}
            definitions = [
                {"Name": f"{reading.subsystem}_{name}", "Unit": "None"} for name, _value in batch
            ]
            blob: dict[str, Any] = {
                **dims,
                **{f"{reading.subsystem}_{name}": value for name, value in batch},
                "_aws": {
                    "Timestamp": timestamp_ms,
                    "CloudWatchMetrics": [
                        {
                            "Namespace": namespace,
                            "Dimensions": [list(dims)],
                            "Metrics": definitions,
                        }
                    ],
                },
            }
            blobs.append(blob)
    return blobs
