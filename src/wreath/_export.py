"""OTLP export pipeline for the Native Flight Recorder (Stage 4, slice 4c).

This is the threaded, network-facing half of OTLP support: the projector hands
each finished `ProjectedTrace` to
`ExportPipeline.on_trace`, which enqueues it on a bounded queue; a
dedicated exporter thread drains the queue on an interval, batches it, maps it to
OTLP through the pure `wreath._otlp` builders, and pushes it over a
transport. Metrics are exported on the same tick from a snapshot provider.

Two isolation guarantees hold the plan's line that exporter behavior never
touches a request stack and its failures never stall anything:

- `on_trace` only enqueues (dropping and counting when the queue is full), so
  the projector thread never blocks on the network.
- Every transport call is wrapped; a raising/​slow exporter increments an error
  counter and the pipeline keeps draining. Backpressure shows up as visible
  queue drops, never as growth or a stalled projector.

The default `OtlpHttpExporter` speaks OTLP/HTTP over the standard library
(`urllib`) with wreath's own protobuf codec, so enabling export pulls in **no**
third-party dependency -- no `protobuf`, no `opentelemetry-*`. It sends
`application/x-protobuf` by default because that is what SDK and collector
exporters send and therefore the path a receiver actually exercises;
`encoding="json"` selects OTLP/HTTP+JSON, which stays fully supported. Any
object with `export_traces`/`export_metrics` methods can stand in, and one that
also has `export_logs` gets the logs signal too.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Final
from typing import Protocol as _Protocol

from ._drainthread import DrainThread
from ._logsink import BoundedLogQueue
from ._otlp import (
    BoundedExportQueue,
    build_logs_request,
    build_metrics_request,
    build_trace_request,
)
from ._projector import ProjectedLog, ProjectedTrace, ProjectorSnapshot
from .http_client import DestinationRejected

__all__ = [
    "TraceMetricTransport",
    "OtlpHttpExporter",
    "ExportPipeline",
]

_DEFAULT_INTERVAL: Final = 1.0
_DEFAULT_QUEUE: Final = 4096
_DEFAULT_BATCH: Final = 512


class TraceMetricTransport(_Protocol):
    """The transport the pipeline pushes OTLP request dicts through.

    `export_logs` is optional: a transport written before the logs signal still
    satisfies this protocol, and the pipeline counts rather than crashes when it
    is missing.
    """

    def export_traces(self, request: dict[str, Any]) -> None: ...
    def export_metrics(self, request: dict[str, Any]) -> None: ...


def _json_body(request: dict[str, Any], _signal: str) -> bytes:
    return json.dumps(request).encode("utf-8")


def _protobuf_body(request: dict[str, Any], signal: str) -> bytes:
    # Imported here rather than at module scope: the declarations compile a
    # wire plan per message at import, and an application exporting nothing
    # should not pay for them.
    from . import _otlp_proto

    return getattr(_otlp_proto, f"encode_{signal}")(request)


#: media type and body encoder per supported OTLP/HTTP encoding.
_ENCODINGS: dict[str, tuple[str, Any]] = {
    "protobuf": ("application/x-protobuf", _protobuf_body),
    "json": ("application/json", _json_body),
}


type _Origin = tuple[str, str, int]


def _otlp_origin(url: str) -> _Origin:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("OTLP endpoint must be an absolute HTTP(S) URL")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep collector-directed redirects on the configured OTLP origin."""

    def __init__(self, origin: _Origin) -> None:
        self._origin = origin

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            redirected_origin = _otlp_origin(newurl)
        except ValueError as error:
            raise DestinationRejected(
                "cross-origin OTLP redirect was rejected"
            ) from error
        if redirected_origin != self._origin:
            raise DestinationRejected("cross-origin OTLP redirect was rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class OtlpHttpExporter:
    """A minimal OTLP/HTTP exporter over `urllib` (no third-party dep).

    Posts to `{endpoint}/v1/traces`, `/v1/logs` and `/v1/metrics` with a bounded
    timeout. Any transport-level failure raises, which the pipeline isolates and
    counts -- this class deliberately holds no retry/backoff policy of its own so
    that the single "drop and count" backpressure story stays in one place.

    **`protobuf` is the default encoding.** OTLP specifies both, and every
    receiver accepts JSON -- but protobuf is what SDK and collector exporters
    actually send, so it is the encoding a receiver's protobuf path is exercised
    by. JSON stays fully supported and selectable with `encoding="json"`, which
    is worth having: it is readable in a proxy log, and it is the fallback if a
    receiver's protobuf handling turns out to be the broken one.

    The request is built as an OTLP/JSON dict either way -- `_otlp.py` has one
    set of builders and `_otlp_proto.py` converts -- so the two encodings cannot
    describe different telemetry.

    Redirects may move within the configured collector origin. A redirect to a
    different scheme, host, or port raises `DestinationRejected`; a collector
    response therefore cannot turn telemetry export into a request to another
    service.

    Args:
        encoding: `"protobuf"` (default) or `"json"`.

    Raises:
        ValueError: `endpoint` is not an absolute HTTP(S) URL, or `encoding` is unknown.
        DestinationRejected: A collector response redirects to another origin.
    """

    __slots__ = (
        "_encode",
        "_headers",
        "_logs_url",
        "_metrics_url",
        "_opener",
        "_timeout",
        "_traces_url",
    )

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
        encoding: str = "protobuf",
    ) -> None:
        try:
            media_type, encode = _ENCODINGS[encoding]
        except KeyError:
            known = ", ".join(sorted(_ENCODINGS))
            raise ValueError(
                f"unknown OTLP encoding {encoding!r}; expected one of {known}"
            ) from None
        origin = _otlp_origin(endpoint)
        base = endpoint.rstrip("/")
        self._traces_url = f"{base}/v1/traces"
        self._metrics_url = f"{base}/v1/metrics"
        self._logs_url = f"{base}/v1/logs"
        self._timeout = timeout
        self._encode = encode
        self._headers = {"content-type": media_type, **(headers or {})}
        self._opener = urllib.request.build_opener(
            _SameOriginRedirectHandler(origin)
        )

    def _post(self, url: str, request: dict[str, Any], signal: str) -> None:
        body = self._encode(request, signal)
        req = urllib.request.Request(url, data=body, headers=self._headers, method="POST")  # noqa: S310 (configured OTLP endpoint)
        with self._opener.open(req, timeout=self._timeout) as response:
            # Drain and discard: the OTLP response body is not needed, but leaving
            # it unread can wedge keep-alive connections.
            response.read()

    def export_traces(self, request: dict[str, Any]) -> None:
        if request.get("resourceSpans"):
            self._post(self._traces_url, request, "traces")

    def export_logs(self, request: dict[str, Any]) -> None:
        if request.get("resourceLogs"):
            self._post(self._logs_url, request, "logs")

    def export_metrics(self, request: dict[str, Any]) -> None:
        if request.get("resourceMetrics"):
            self._post(self._metrics_url, request, "metrics")


class ExportPipeline:
    """Owns the export queue and the exporter thread.

    `snapshot_provider` is called on the exporter thread each tick to obtain a
    fresh `ProjectorSnapshot` for metrics; pass `None` to export traces
    only. `image` supplies low-cardinality route names/attributes to the OTLP
    mapping.
    """

    __slots__ = (
        "_transport",
        "_image",
        "_resource",
        "_queue",
        "_batch_size",
        "_snapshot_provider",
        "_metrics_start_ns",
        "_lock",
        "_trace_errors",
        "_metric_errors",
        "_log_errors",
        "_exported_traces",
        "_exported_logs",
        "_log_queue",
        "_log_registry",
        "_drain",
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
        log_registry: Any = None,
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
        self._snapshot_provider = snapshot_provider
        self._metrics_start_ns = 0
        self._lock = threading.Lock()
        self._trace_errors = 0
        self._metric_errors = 0
        self._log_errors = 0
        self._exported_traces = 0
        self._exported_logs = 0
        # Logs get their own bounded queue rather than sharing the trace one:
        # they arrive one to two orders of magnitude more often, so a shared
        # queue would let a log burst evict the traces an operator came for.
        self._log_queue: BoundedLogQueue = BoundedLogQueue(queue_capacity)
        self._log_registry = log_registry
        self._drain = DrainThread(
            "wreath-flight-export", interval, self._tick, self._flush
        )

    # -- hook and lifecycle -------------------------------------------------

    def on_trace(self, trace: ProjectedTrace) -> None:
        """The projector's export hook: enqueue, dropping if the queue is full."""
        self._queue.offer(trace)

    def on_log(self, record: ProjectedLog) -> None:
        """The projector's log hook: enqueue, dropping if the queue is full.

        Only enqueues, exactly like `on_trace`, so the projector thread never
        blocks on the network.
        """
        if self._log_registry is not None:
            self._log_queue.offer(record)

    def set_log_registry(self, registry: Any) -> None:
        """Attach the call-site registry the OTLP mapping renders against."""
        self._log_registry = registry

    def set_snapshot_provider(
        self, provider: Callable[[], ProjectorSnapshot | None]
    ) -> None:
        """Attach the metrics source. The projector is built after the pipeline
        (it needs the pipeline's `on_trace`), so this wires the back-reference."""
        self._snapshot_provider = provider

    def start(self) -> None:
        # The stamp is inside the guard, so a second `start` on a running
        # pipeline does not reset the window the metric deltas are read against.
        if self._drain.running:
            return
        self._metrics_start_ns = time.time_ns()
        self._drain.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._drain.stop(timeout)

    def _flush(self) -> None:
        """The last drain, after `stop` has joined the thread.

        Traces and metrics but deliberately *not* logs: a log record is already
        published to the ring by its writer, and re-exporting the tail here
        would double-count against `_exported_logs`.
        """
        self._export_traces()
        self._export_metrics()

    def _tick(self) -> None:
        self._export_traces()
        self._export_logs()
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

    def _export_logs(self) -> None:
        registry = self._log_registry
        if registry is None:
            return
        pending = self._log_queue.drain()
        if not pending:
            return
        exporter = getattr(self._transport, "export_logs", None)
        if exporter is None:
            # A transport predating the logs signal. Counted, not silent: an
            # operator who configured logs export and gets nothing needs a
            # rising number, not a mystery.
            with self._lock:
                self._log_errors += 1
            return
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            request = build_logs_request(
                batch, registry=registry, resource_attributes=self._resource
            )
            try:
                exporter(request)
            except Exception:  # noqa: BLE001 -- isolate exporter failure to a counter
                with self._lock:
                    self._log_errors += 1
            else:
                with self._lock:
                    self._exported_logs += len(batch)

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
                "exported_logs": self._exported_logs,
                "log_errors": self._log_errors,
                "log_dropped": self._log_queue.dropped,
                "trace_errors": self._trace_errors,
                "metric_errors": self._metric_errors,
                "dropped": self._queue.dropped,
                "queued": len(self._queue),
            }
