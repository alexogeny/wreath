"""CloudWatch Embedded Metric Format (EMF) bridge for the Native Flight Recorder.

Renders the same ``Projector.snapshot()`` aggregates the Prometheus/OTLP bridges
read as EMF structured-JSON log lines. Written to stdout (or a sink), CloudWatch
Logs parses them into metrics with **zero infrastructure** — ideal for ECS/Lambda.

EMF carries metric *values* at the JSON root alongside dimension values, and one
blob is one dimension-value combination, so per-route metrics are emitted as one
blob per route plus a global blob. Counters are emitted as **deltas** since the
previous emit (CloudWatch SUMs over the period) unless ``cumulative=True``; gauges
are absolute. Zero-dependency: stdlib ``json`` only (no ``boto3``).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from ._metricdelta import DeltaTracker
from ._prometheus import (
    _PROJECTOR_LOSS_FIELDS,
    RouteLabels,
    _loss_reason_name,
    _resolve_route_labels,
)

__all__ = ["EmfBridge", "activate_cloudwatch_emf"]

#: CloudWatch caps metric definitions per EMF blob at 100.
_MAX_METRICS_PER_BLOB = 100


class EmfBridge:
    """Renders projector metrics as CloudWatch EMF blobs.

    ``source`` exposes ``snapshot()`` (and optionally ``recorder_loss()``).
    ``dimensions`` is a static dimension mapping (e.g. ``{"Service": "trailhead"}``)
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
        self._deltas = DeltaTracker()

    def _counter(self, key: tuple, value: float) -> float:
        if self._cumulative:
            return value
        return self._deltas.delta(key, value)

    def _blob(self, timestamp_ms: int, dims: dict[str, str],
              metrics: list[tuple[str, str, float]]) -> dict:
        body: dict[str, Any] = dict(dims)
        definitions = []
        for name, unit, value in metrics[:_MAX_METRICS_PER_BLOB]:
            body[name] = value
            definitions.append({"Name": name, "Unit": unit})
        body["_aws"] = {
            "Timestamp": int(timestamp_ms),
            "CloudWatchMetrics": [{
                "Namespace": self._namespace,
                "Dimensions": [list(dims.keys())],
                "Metrics": definitions,
            }],
        }
        return body

    def blobs(self, snapshot: Any, *, timestamp_ms: int,
              recorder_loss: dict | None = None) -> list[dict]:
        """The list of EMF blobs for one snapshot (pure; testable)."""
        out: list[dict] = []
        for r in tuple(getattr(snapshot, "routes", ()) or ()):
            lbl = _resolve_route_labels(self._route_labels, r.route_id)
            dims = {**self._dims, **{k: str(v) for k, v in lbl.items()}}
            out.append(self._blob(timestamp_ms, dims, [
                ("Requests", "Count", self._counter(("req", r.route_id), int(r.count))),
                ("Errors", "Count", self._counter(("err", r.route_id), int(r.errors))),
                ("DurationSum", "Milliseconds",
                 self._counter(("dsum", r.route_id), r.duration_us_sum / 1000.0)),
                ("DurationMax", "Milliseconds", r.duration_us_max / 1000.0),
            ]))

        gmetrics: list[tuple[str, str, float]] = [
            ("TracesAssembled", "Count",
             self._counter(("assembled",), int(getattr(snapshot, "assembled", 0)))),
            ("Pending", "Count", int(getattr(snapshot, "pending", 0))),
        ]
        loss = getattr(snapshot, "loss", None)
        for field in _PROJECTOR_LOSS_FIELDS:
            gmetrics.append((f"ProjectorLoss_{field}", "Count",
                             self._counter(("ploss", field), int(getattr(loss, field, 0)))))
        if recorder_loss:
            for reason, count in recorder_loss.items():
                name = _loss_reason_name(reason)
                gmetrics.append((f"RecorderLoss_{name}", "Count",
                                 self._counter(("rloss", name), int(count))))
        out.append(self._blob(timestamp_ms, dict(self._dims), gmetrics))
        return out

    def render(self, *, timestamp_ms: int | None = None) -> str:
        """Newline-separated EMF JSON blobs for the current snapshot."""
        ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
        snapshot = self._source.snapshot()
        recorder_loss = None
        getter = getattr(self._source, "recorder_loss", None)
        if callable(getter):
            recorder_loss = getter()
        blobs = self.blobs(snapshot, timestamp_ms=ts, recorder_loss=recorder_loss)
        return "\n".join(json.dumps(b, separators=(",", ":")) for b in blobs)

    def emit(self, *, timestamp_ms: int | None = None, sink: Any = None) -> None:
        """Write the EMF blobs to ``sink`` (default stdout), newline-terminated."""
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
