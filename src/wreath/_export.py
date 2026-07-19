"""OTLP export pipeline for the Native Flight Recorder (Stage 4, slice 4c).

This is the threaded, network-facing half of OTLP support: the projector hands
each finished :class:`~wreath._projector.ProjectedTrace` to
:meth:`ExportPipeline.on_trace`, which enqueues it on a bounded queue; a
dedicated exporter thread drains the queue on an interval, batches it, maps it to
OTLP through the pure :mod:`wreath._otlp` builders, and pushes it over a
transport. Metrics are exported on the same tick from a snapshot provider.

Two isolation guarantees hold the plan's line that exporter behavior never
touches a request stack and its failures never stall anything:

- ``on_trace`` only enqueues (dropping and counting when the queue is full), so
  the projector thread never blocks on the network.
- Every transport call is wrapped; a raising/​slow exporter increments an error
  counter and the pipeline keeps draining. Backpressure shows up as visible
  queue drops, never as growth or a stalled projector.

The default :class:`OtlpHttpExporter` speaks OTLP/HTTP+JSON over the standard
library (`urllib`), so enabling export pulls in **no** third-party dependency;
any object with ``export_traces``/``export_metrics`` methods can stand in.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any, Final
from typing import Protocol as _Protocol

from ._otlp import (
    BoundedExportQueue,
    build_metrics_request,
    build_trace_request,
)
from ._projector import ProjectedTrace, ProjectorSnapshot

__all__ = [
    "TraceMetricTransport",
    "OtlpHttpExporter",
    "ExportPipeline",
]

_DEFAULT_INTERVAL: Final = 1.0
_DEFAULT_QUEUE: Final = 4096
_DEFAULT_BATCH: Final = 512


class TraceMetricTransport(_Protocol):
    """The transport the pipeline pushes OTLP request dicts through."""

    def export_traces(self, request: dict[str, Any]) -> None: ...
    def export_metrics(self, request: dict[str, Any]) -> None: ...


class OtlpHttpExporter:
    """A minimal OTLP/HTTP+JSON exporter over ``urllib`` (no third-party dep).

    Posts to ``{endpoint}/v1/traces`` and ``{endpoint}/v1/metrics`` with a bounded
    timeout. Any transport-level failure raises, which the pipeline isolates and
    counts -- this class deliberately holds no retry/backoff policy of its own so
    that the single "drop and count" backpressure story stays in one place.
    """

    __slots__ = ("_traces_url", "_metrics_url", "_timeout", "_headers")

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        base = endpoint.rstrip("/")
        self._traces_url = f"{base}/v1/traces"
        self._metrics_url = f"{base}/v1/metrics"
        self._timeout = timeout
        self._headers = {"content-type": "application/json", **(headers or {})}

    def _post(self, url: str, request: dict[str, Any]) -> None:
        body = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._headers, method="POST")
        with urllib.request.urlopen(req, timeout=self._timeout) as response:  # noqa: S310
            # Drain and discard: the OTLP response body is not needed, but leaving
            # it unread can wedge keep-alive connections.
            response.read()

    def export_traces(self, request: dict[str, Any]) -> None:
        if request.get("resourceSpans"):
            self._post(self._traces_url, request)

    def export_metrics(self, request: dict[str, Any]) -> None:
        if request.get("resourceMetrics"):
            self._post(self._metrics_url, request)


class ExportPipeline:
    """Owns the export queue and the exporter thread.

    ``snapshot_provider`` is called on the exporter thread each tick to obtain a
    fresh :class:`ProjectorSnapshot` for metrics; pass ``None`` to export traces
    only. ``image`` supplies low-cardinality route names/attributes to the OTLP
    mapping.
    """

    __slots__ = (
        "_transport",
        "_image",
        "_resource",
        "_queue",
        "_batch_size",
        "_interval",
        "_snapshot_provider",
        "_metrics_start_ns",
        "_lock",
        "_trace_errors",
        "_metric_errors",
        "_exported_traces",
        "_thread",
        "_stop",
    )

    def __init__(
        self,
        transport: TraceMetricTransport,
        *,
        image: Any = None,
        resource_attributes: dict[str, str] | None = None,
        queue_capacity: int = _DEFAULT_QUEUE,
        batch_size: int = _DEFAULT_BATCH,
        interval: float = _DEFAULT_INTERVAL,
        snapshot_provider: Callable[[], ProjectorSnapshot | None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._transport = transport
        self._image = image
        self._resource = resource_attributes
        self._queue = BoundedExportQueue(queue_capacity)
        self._batch_size = batch_size
        self._interval = interval
        self._snapshot_provider = snapshot_provider
        self._metrics_start_ns = 0
        self._lock = threading.Lock()
        self._trace_errors = 0
        self._metric_errors = 0
        self._exported_traces = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- hook and lifecycle -------------------------------------------------

    def on_trace(self, trace: ProjectedTrace) -> None:
        """The projector's export hook: enqueue, dropping if the queue is full."""
        self._queue.offer(trace)

    def set_snapshot_provider(
        self, provider: Callable[[], ProjectorSnapshot | None]
    ) -> None:
        """Attach the metrics source. The projector is built after the pipeline
        (it needs the pipeline's ``on_trace``), so this wires the back-reference."""
        self._snapshot_provider = provider

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._metrics_start_ns = time.time_ns()
        self._stop.clear()
        thread = threading.Thread(
            target=self._run, name="wreath-flight-export", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            self._thread = None
        # Final flush: drain whatever traces remain and emit one last metric read.
        self._export_traces()
        self._export_metrics()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self._interval)

    def _tick(self) -> None:
        self._export_traces()
        self._export_metrics()

    # -- export steps -------------------------------------------------------

    def _export_traces(self) -> None:
        pending = self._queue.drain()
        if not pending:
            return
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            request = build_trace_request(
                batch, image=self._image, resource_attributes=self._resource
            )
            try:
                self._transport.export_traces(request)
            except Exception:  # noqa: BLE001 -- isolate exporter failure to a counter
                with self._lock:
                    self._trace_errors += 1
            else:
                with self._lock:
                    self._exported_traces += len(batch)

    def _export_metrics(self) -> None:
        provider = self._snapshot_provider
        if provider is None:
            return
        snapshot = provider()
        if snapshot is None:
            return
        request = build_metrics_request(
            snapshot,
            image=self._image,
            start_unix_nano=self._metrics_start_ns,
            now_unix_nano=time.time_ns(),
            resource_attributes=self._resource,
        )
        try:
            self._transport.export_metrics(request)
        except Exception:  # noqa: BLE001
            with self._lock:
                self._metric_errors += 1

    # -- introspection ------------------------------------------------------

    @property
    def queue(self) -> BoundedExportQueue:
        return self._queue

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "exported_traces": self._exported_traces,
                "trace_errors": self._trace_errors,
                "metric_errors": self._metric_errors,
                "dropped": self._queue.dropped,
                "queued": len(self._queue),
            }
