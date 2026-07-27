"""Prometheus text-exposition bridge for the Native Flight Recorder.

The recorder aggregates request metrics off-path in the projector
(:class:`wreath._projector.Projector`): a running total of assembled traces, a
pending gauge, categorized loss counters, and per-route counts/errors/duration
(a base-2 log-bucket histogram). :func:`wreath.telemetry.activate_prometheus`
wraps a snapshot source in a :class:`PrometheusBridge` that renders that state as
Prometheus text exposition format 0.0.4 on demand — the same snapshot the OTLP
metrics path reads, mapped to Prometheus instead of OTLP. Nothing here runs on
the request path; a scrape calls :meth:`PrometheusBridge.render` and reads a
consistent projector snapshot.

Zero-dependency: the exposition text is hand-rolled to the format spec; there is
no ``prometheus_client`` import. The renderer is duck-typed over the snapshot
(``assembled``/``pending``/``routes``/``loss`` and per-route
``route_id``/``count``/``errors``/``duration_us_sum``/``duration_us_max``/``buckets``),
so it needs no native build to exercise.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

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

#: base-2 log buckets, matching ``_flight_schema.HISTOGRAM_BUCKETS`` /
#: ``histogram_bucket``: bucket ``i`` counts durations in ``[2**i, 2**(i+1))`` us
#: (bucket 0 is ``<= 1`` us), the top bucket is the clamped overflow.
HISTOGRAM_BUCKETS = 64

#: Human-readable reason for each projector loss field.
_PROJECTOR_LOSS_FIELDS = (
    "orphan_phase",
    "orphan_correlation",
    "pending_evicted",
    "decode_error",
    "export_error",
    "recent_evicted",
)

_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_:]")
_NAME_LEAD = re.compile(r"^[^a-zA-Z_:]")
_LABEL_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_]")
_LABEL_NAME_LEAD = re.compile(r"^[^a-zA-Z_]")

# A route-label resolver maps a numeric route_id to a label mapping, or is a
# static per-id mapping; None labels rows by route_id only.
RouteLabels = Callable[[int], Mapping[str, str]] | Mapping[int, Mapping[str, str]] | None


# --- formatting -------------------------------------------------------------

def _sanitize_metric_name(name: str) -> str:
    name = _NAME_INVALID.sub("_", name)
    if _NAME_LEAD.match(name):
        name = "_" + name
    return name


def _sanitize_label_name(name: str) -> str:
    name = _LABEL_NAME_INVALID.sub("_", name)
    if not name or _LABEL_NAME_LEAD.match(name):
        name = "_" + name
    return name


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float | int) -> str:
    """A Prometheus-valid numeric literal (``+Inf`` for infinity, int-exact for
    integers, round-trippable float otherwise)."""
    if isinstance(value, bool):  # bool is an int subclass; render as 0/1
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _render_labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    parts = [
        f'{_sanitize_label_name(k)}="{_escape_label_value(str(v))}"'
        for k, v in labels.items()
    ]
    return "{" + ",".join(parts) + "}"


def _le_seconds(bucket_index: int) -> float:
    """Upper bound (inclusive ``le``) in seconds for cumulative-through-bucket
    ``bucket_index``: the exclusive upper edge ``2**(i+1)`` microseconds."""
    return (2 ** (bucket_index + 1)) / 1_000_000.0


# --- family builder ---------------------------------------------------------

class _Writer:
    __slots__ = ("_lines",)

    def __init__(self) -> None:
        self._lines: list[str] = []

    def family(self, name: str, kind: str, help_text: str) -> None:
        self._lines.append(f"# HELP {name} {_escape_help(help_text)}")
        self._lines.append(f"# TYPE {name} {kind}")

    def sample(self, name: str, labels: Mapping[str, str], value: float | int) -> None:
        self._lines.append(f"{name}{_render_labels(labels)} {_format_value(value)}")

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


def _resolve_route_labels(route_labels: RouteLabels, route_id: int) -> dict[str, str]:
    if route_labels is None:
        return {"route_id": str(route_id)}
    # Test for the Mapping arm, not `callable()`: a Mapping is not callable, so
    # this narrows both arms cleanly, where `callable()` leaves the mapping case
    # as a callable intersection that cannot be resolved.
    resolved: Mapping[str, str] | None
    if isinstance(route_labels, Mapping):
        # The two arms of RouteLabels are not separable by narrowing -- nothing
        # stops a Mapping subclass from defining __call__ -- so state the arm the
        # isinstance established. Testing Mapping rather than callable() also
        # keeps the else-branch a plain callable instead of an intersection.
        table = cast("Mapping[int, Mapping[str, str]]", route_labels)
        resolved = table.get(route_id)
    else:
        resolved = route_labels(route_id)
    if not resolved:
        return {"route_id": str(route_id)}
    return {k: str(v) for k, v in resolved.items()}


def _loss_reason_name(reason: Any) -> str:
    name = getattr(reason, "name", None)
    if name is None:
        name = str(reason)
    return name.lower()


def render_exposition(
    snapshot: Any,
    *,
    recorder_loss: Mapping[Any, int] | None = None,
    route_labels: RouteLabels = None,
    namespace: str = "wreath",
    openmetrics: bool = False,
) -> str:
    """Render a projector snapshot as Prometheus text exposition (format 0.0.4).

    With ``openmetrics=True`` the output is OpenMetrics 1.0.0 instead: counter
    ``# TYPE``/``# HELP`` families drop the ``_total`` suffix (samples keep it),
    and the text is terminated by ``# EOF``.

    ``snapshot`` is any object exposing ``assembled`` (int), ``pending`` (int),
    ``loss`` (with the projector loss fields), and ``routes`` (each with
    ``route_id``/``count``/``errors``/``duration_us_sum``/``duration_us_max`` and
    a 64-entry ``buckets`` list). ``recorder_loss`` maps a ``LossReason`` (or any
    named/int reason) to its ring-drop count. ``route_labels`` turns a numeric
    ``route_id`` into scrape labels (e.g. ``{"method": "GET", "path": "/x"}``);
    without it, rows are labelled ``route_id="<n>"``.
    """
    ns = _sanitize_metric_name(namespace).rstrip("_")
    w = _Writer()

    def _cfam(name: str) -> str:
        # OpenMetrics declares the counter family without the `_total` suffix the
        # samples carry; Prometheus 0.0.4 uses the full name for both.
        return name[:-6] if openmetrics and name.endswith("_total") else name

    routes: Sequence[Any] = tuple(getattr(snapshot, "routes", ()) or ())

    # --- per-route counters --------------------------------------------------
    requests = f"{ns}_http_requests_total"
    w.family(_cfam(requests), "counter", "Requests finalized by the flight projector, by route.")
    for route in routes:
        w.sample(requests, _resolve_route_labels(route_labels, route.route_id), route.count)

    errors = f"{ns}_http_request_errors_total"
    w.family(_cfam(errors), "counter",
             "Failed requests (non-OK terminal, 5xx, or promoted), by route.")
    for route in routes:
        w.sample(errors, _resolve_route_labels(route_labels, route.route_id), route.errors)

    # --- per-route duration histogram ---------------------------------------
    hist = f"{ns}_http_request_duration_seconds"
    w.family(hist, "histogram", "Request duration in seconds (base-2 log buckets), by route.")
    for route in routes:
        base = _resolve_route_labels(route_labels, route.route_id)
        buckets = list(route.buckets)
        total = 0
        cumulative = 0
        # Emit a cumulative le boundary only where a bucket saw observations, then
        # the mandatory +Inf bucket equal to the total count. Monotonic in le.
        for i in range(min(len(buckets), HISTOGRAM_BUCKETS)):
            cumulative += buckets[i]
            total += buckets[i]
            if buckets[i]:
                w.sample(hist + "_bucket",
                         {**base, "le": _format_value(_le_seconds(i))}, cumulative)
        w.sample(hist + "_bucket", {**base, "le": "+Inf"}, total)
        w.sample(hist + "_sum", base, route.duration_us_sum / 1_000_000.0)
        w.sample(hist + "_count", base, total)

    dmax = f"{ns}_http_request_duration_max_seconds"
    w.family(dmax, "gauge", "Maximum observed request duration in seconds, by route.")
    for route in routes:
        w.sample(dmax, _resolve_route_labels(route_labels, route.route_id),
                 route.duration_us_max / 1_000_000.0)

    # --- global projector state ---------------------------------------------
    assembled = f"{ns}_flight_traces_assembled_total"
    w.family(_cfam(assembled), "counter", "Total request traces the projector has finalized.")
    w.sample(assembled, {}, int(getattr(snapshot, "assembled", 0)))

    pending = f"{ns}_flight_pending"
    w.family(pending, "gauge", "Completions awaiting their trailing correlation/phase cells.")
    w.sample(pending, {}, int(getattr(snapshot, "pending", 0)))

    # --- loss counters -------------------------------------------------------
    proj_loss = f"{ns}_flight_projector_loss_total"
    w.family(_cfam(proj_loss), "counter", "Telemetry items the projector dropped, by reason.")
    loss = getattr(snapshot, "loss", None)
    for field_name in _PROJECTOR_LOSS_FIELDS:
        w.sample(proj_loss, {"reason": field_name}, int(getattr(loss, field_name, 0)))

    if recorder_loss is not None:
        rec_loss = f"{ns}_flight_recorder_loss_total"
        w.family(_cfam(rec_loss), "counter",
                 "Items the recorder dropped before the projector saw them, by reason.")
        for reason, count in recorder_loss.items():
            w.sample(rec_loss, {"reason": _loss_reason_name(reason)}, int(count))

    text = w.text()
    return text + "# EOF\n" if openmetrics else text


# --- bridge + mountable handler ---------------------------------------------

class PrometheusBridge:
    """Renders a snapshot source's metrics as Prometheus exposition on demand.

    ``source`` is anything with ``snapshot()`` (returning a projector snapshot)
    and, optionally, ``recorder_loss()`` — the :class:`wreath._projector.Projector`
    satisfies both. Rendering reads a consistent snapshot; it never touches the
    request path.
    """

    __slots__ = ("_source", "_namespace", "_route_labels", "_openmetrics")

    def __init__(
        self,
        source: Any,
        *,
        namespace: str = "wreath",
        route_labels: RouteLabels = None,
        openmetrics: bool = False,
    ) -> None:
        if not hasattr(source, "snapshot"):
            raise TypeError("prometheus source must expose snapshot()")
        self._source = source
        self._namespace = namespace
        self._route_labels = route_labels
        self._openmetrics = openmetrics

    @property
    def content_type(self) -> str:
        return OPENMETRICS_CONTENT_TYPE if self._openmetrics else CONTENT_TYPE

    def render(self) -> str:
        """A fresh exposition text for the current snapshot."""
        snapshot = self._source.snapshot()
        recorder_loss = None
        getter = getattr(self._source, "recorder_loss", None)
        if callable(getter):
            recorder_loss = getter()
        return render_exposition(
            snapshot,
            recorder_loss=recorder_loss,
            route_labels=self._route_labels,
            namespace=self._namespace,
            openmetrics=self._openmetrics,
        )

    def handler(self) -> Callable[[Any], Any]:
        """An async request handler that returns the exposition as a Response.

        Mount it yourself, e.g. ``app.route("/metrics", methods=("GET",))(bridge.handler())``.
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
) -> Callable[[Any], Any]:
    """Convenience: a ready async ``/metrics`` handler for a snapshot source."""
    return PrometheusBridge(source, namespace=namespace, route_labels=route_labels).handler()


def openmetrics_handler(
    source: Any,
    *,
    namespace: str = "wreath",
    route_labels: RouteLabels = None,
) -> Callable[[Any], Any]:
    """Convenience: a ready async ``/metrics`` handler emitting OpenMetrics 1.0.0."""
    return PrometheusBridge(
        source, namespace=namespace, route_labels=route_labels, openmetrics=True,
    ).handler()


def metrics_router(
    source: Any,
    *,
    path: str = "/metrics",
    namespace: str = "wreath",
    route_labels: RouteLabels = None,
):
    """Convenience: a :class:`wreath.router.Router` exposing ``GET <path>``.

    The consumer ``include_router``s it. Kept here (not wired into ``app.py``) so
    a future ``app.metrics()`` convenience is an additive follow-up.
    """
    # TODO(app-wiring): an `app.metrics(path=..., auth=...)` convenience on the
    #   application factory would mirror `app.http_client`/`app.objects`; deferred
    #   because app.py is owned by a concurrent workstream.
    from .router import Router

    router = Router()
    router.get(path)(prometheus_handler(source, namespace=namespace, route_labels=route_labels))
    return router
