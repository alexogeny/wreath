"""Prometheus text-exposition bridge for the Native Flight Recorder.

The recorder aggregates request metrics off-path in the projector
(`wreath._projector.Projector`): a running total of assembled traces, a
pending gauge, categorized loss counters, and per-route counts/errors/duration
(a base-2 log-bucket histogram). `wreath.telemetry.activate_prometheus`
wraps a snapshot source in a `PrometheusBridge` that renders that state as
Prometheus text exposition format 0.0.4 on demand — the same snapshot the OTLP
metrics path reads, mapped to Prometheus instead of OTLP. Nothing here runs on
the request path; a scrape calls `PrometheusBridge.render` and reads a
consistent projector snapshot.

Zero-dependency: the exposition text is hand-rolled to the format spec; there is
no `prometheus_client` import. The renderer is duck-typed over the snapshot
(`assembled`/`pending`/`routes`/`loss` and per-route
`route_id`/`count`/`errors`/`duration_us_sum`/`duration_us_max`/`buckets`),
so it needs no native build to exercise.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ._native import _core

__all__ = [
    "render_exposition",
    "PrometheusBridge",
    "prometheus_handler",
    "openmetrics_handler",
    "metrics_router",
    "CONTENT_TYPE",
    "OPENMETRICS_CONTENT_TYPE",
    "HISTOGRAM_BUCKETS",
]

#: The exposition media type a scrape endpoint must advertise (format 0.0.4).
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: The OpenMetrics 1.0.0 exposition media type.
OPENMETRICS_CONTENT_TYPE = "application/openmetrics-text; version=1.0.0; charset=utf-8"

#: base-2 log buckets, matching `_flight_schema.HISTOGRAM_BUCKETS` /
#: `histogram_bucket`: bucket `i` counts durations in `[2**i, 2**(i+1))` us
#: (bucket 0 is `<= 1` us), the top bucket is the clamped overflow.
HISTOGRAM_BUCKETS = 64

_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_:]")
_NAME_LEAD = re.compile(r"^[^a-zA-Z_:]")
_LABEL_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_]")
_LABEL_NAME_LEAD = re.compile(r"^[^a-zA-Z_]")

# A route-label resolver maps a numeric route_id to a label mapping, or is a
# static per-id mapping; None labels rows by route_id only.
RouteLabels = Callable[[int], Mapping[str, str]] | Mapping[int, Mapping[str, str]] | None


def _sanitize_metric_name(name: str) -> str:
    name = _NAME_INVALID.sub("_", name)
    if _NAME_LEAD.match(name):
        name = "_" + name
    return name


def _sanitize_label_name(name: str) -> str:
    """The public-test oracle for the native route-label sanitizer."""
    name = _LABEL_NAME_INVALID.sub("_", name)
    if not name or _LABEL_NAME_LEAD.match(name):
        name = "_" + name
    return name


def render_exposition(
    snapshot: Any,
    *,
    recorder_loss: Mapping[Any, int] | None = None,
    route_labels: RouteLabels = None,
    namespace: str = "wreath",
    openmetrics: bool = False,
    counters: Sequence[Any] = (),
) -> str:
    """Render a projector snapshot as Prometheus text exposition (format 0.0.4).

    With `openmetrics=True` the output is OpenMetrics 1.0.0 instead: counter
    `# TYPE`/`# HELP` families drop the `_total` suffix (samples keep it),
    and the text is terminated by `# EOF`.

    `snapshot` is any object exposing `assembled` (int), `pending` (int),
    `loss` (with the projector loss fields), and `routes` (each with
    `route_id`/`count`/`errors`/`duration_us_sum`/`duration_us_max` and
    a 64-entry `buckets` list). `recorder_loss` maps a `LossReason` (or any
    named/int reason) to its ring-drop count. `route_labels` turns a numeric
    `route_id` into scrape labels (e.g. `{"method": "GET", "path": "/x"}`);
    without it, rows are labelled `route_id="<n>"`.
    """
    ns = _sanitize_metric_name(namespace).rstrip("_")

    def _cfam(name: str) -> str:
        # OpenMetrics declares the counter family without the `_total` suffix the
        # samples carry; Prometheus 0.0.4 uses the full name for both.
        return name[:-6] if openmetrics and name.endswith("_total") else name

    routes: Sequence[Any] = tuple(getattr(snapshot, "routes", ()) or ())
    requests = f"{ns}_http_requests_total"
    errors = f"{ns}_http_request_errors_total"
    hist = f"{ns}_http_request_duration_seconds"
    dmax = f"{ns}_http_request_duration_max_seconds"
    route_blocks = _core.prometheus_route_blocks(
        routes,
        (requests, errors, hist, dmax),
        route_labels,
        isinstance(route_labels, Mapping),
    )

    assembled = f"{ns}_flight_traces_assembled_total"
    pending = f"{ns}_flight_pending"
    proj_loss = f"{ns}_flight_projector_loss_total"
    rec_loss = f"{ns}_flight_recorder_loss_total"
    global_block = _core.prometheus_global_block(
        snapshot,
        recorder_loss,
        (
            _cfam(assembled),
            assembled,
            pending,
            _cfam(proj_loss),
            proj_loss,
            _cfam(rec_loss),
            rec_loss,
        ),
    )

    # Grouped by family before emitting, because the exposition format requires
    # every sample of a family to be contiguous and a scraper rejects the text
    # outright when they are not -- two queues would otherwise interleave into
    # two HELP blocks for one name.
    # `gauge`, not `counter`: canonical rows preserve their names and expose the
    # current reading. `Counters.gauges` lets delta-oriented push bridges avoid
    # subtracting live values, but changing only some Prometheus families to
    # counters would also add `_total` and break the existing public names.
    counter_block = _core.prometheus_counter_block(counters, ns)
    return _core.prometheus_document(
        route_blocks,
        global_block,
        counter_block,
        (_cfam(requests), _cfam(errors), hist, dmax),
        openmetrics,
    )


class PrometheusBridge:
    """Renders a snapshot source's metrics as Prometheus exposition on demand.

    `source` is anything with `snapshot()` (returning a projector snapshot)
    and, optionally, `recorder_loss()` — the `wreath._projector.Projector`
    satisfies both. Rendering reads a consistent snapshot; it never touches the
    request path.
    """

    __slots__ = (
        "_app",
        "_counter_sources",
        "_source",
        "_namespace",
        "_route_labels",
        "_openmetrics",
    )

    def __init__(
        self,
        source: Any,
        *,
        namespace: str = "wreath",
        route_labels: RouteLabels = None,
        openmetrics: bool = False,
        app: Any = None,
        counter_sources: Sequence[Any] = (),
    ) -> None:
        from .metrics import _counter_sources, _snapshot_source

        explicit_sources = _counter_sources(counter_sources, bridge="Prometheus")
        #: The application whose registered subsystems are asked for counters on
        #: every render. Optional and defaulted absent: the projector half works
        #: without one, and a bridge built for a test double should not have to
        #: invent an app to satisfy this.
        self._app = app
        self._counter_sources = explicit_sources
        self._source = _snapshot_source(source, bridge="prometheus")
        self._namespace = namespace
        self._route_labels = route_labels
        self._openmetrics = openmetrics

    @property
    def content_type(self) -> str:
        return OPENMETRICS_CONTENT_TYPE if self._openmetrics else CONTENT_TYPE

    def render(self) -> str:
        """A fresh exposition text for the current snapshot."""
        from .metrics import _read_snapshot, collect

        readings = collect(self._app, self._counter_sources)
        snapshot, recorder_loss = _read_snapshot(self._source)
        return render_exposition(
            snapshot,
            recorder_loss=recorder_loss,
            route_labels=self._route_labels,
            namespace=self._namespace,
            openmetrics=self._openmetrics,
            counters=readings,
        )

    def handler(self) -> Callable[[Any], Any]:
        """An async request handler that returns the exposition as a Response.

        Mount it yourself, e.g. `app.route("/metrics", methods=("GET",))(bridge.handler())`.
        Left unmounted by default so the endpoint's exposure and any auth gating
        stay the app's decision.
        """
        from .response import Response

        content_type = self.content_type.encode("ascii")

        async def metrics(_request: Any) -> Any:
            return Response(self.render().encode("utf-8"), media_type=content_type)

        return metrics


def prometheus_handler(
    source: Any,
    *,
    namespace: str = "wreath",
    route_labels: RouteLabels = None,
    counter_sources: Sequence[Any] = (),
    app: Any = None,
) -> Callable[[Any], Any]:
    """Convenience: a ready async `/metrics` handler for a snapshot source."""
    return PrometheusBridge(
        source,
        namespace=namespace,
        route_labels=route_labels,
        counter_sources=counter_sources,
        app=app,
    ).handler()


def openmetrics_handler(
    source: Any,
    *,
    namespace: str = "wreath",
    route_labels: RouteLabels = None,
    counter_sources: Sequence[Any] = (),
    app: Any = None,
) -> Callable[[Any], Any]:
    """Convenience: a ready async `/metrics` handler emitting OpenMetrics 1.0.0."""
    return PrometheusBridge(
        source,
        namespace=namespace,
        route_labels=route_labels,
        openmetrics=True,
        counter_sources=counter_sources,
        app=app,
    ).handler()


def metrics_router(
    source: Any,
    *,
    path: str = "/metrics",
    namespace: str = "wreath",
    route_labels: RouteLabels = None,
    counter_sources: Sequence[Any] = (),
    app: Any = None,
):
    """Convenience: a `wreath.router.Router` exposing `GET <path>`.

    The consumer `include_router`s it. `Wreath.metrics()` is the application-
    owned convenience and passes its app through this same collector seam.
    """
    from .router import Router

    router = Router()
    router.get(path)(
        prometheus_handler(
            source,
            namespace=namespace,
            route_labels=route_labels,
            counter_sources=counter_sources,
            app=app,
        )
    )
    return router
