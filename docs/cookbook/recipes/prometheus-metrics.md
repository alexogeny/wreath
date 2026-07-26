# Expose a Prometheus /metrics endpoint

Wreath's Native Flight Recorder already aggregates per-route counts, errors, and
duration histograms off the request path. To let Prometheus scrape them, mount a
`/metrics` endpoint that renders the recorder's own snapshot:

```python
projector = ...   # the app's metrics snapshot source (anything with snapshot())

app.metrics(projector, path="/metrics")
```

`app.metrics(source, path="/metrics")` includes a router that, on each scrape,
reads one consistent `source.snapshot()` and renders Prometheus text exposition
format 0.0.4 — counters, gauges, and per-route histograms. Nothing runs on the
request path: a handler that never gets scraped pays nothing. The exposition is
hand-rolled to the spec, so there is no `prometheus_client` dependency.

By default rows are labelled by numeric `route_id`. Pass `route_labels=` to turn
those ids into meaningful scrape labels, and `namespace=` to prefix the metric
family names:

```python
app.metrics(projector, path="/metrics", namespace="llamacam",
            route_labels={1: {"method": "GET", "path": "/llamas"}})
```

The same snapshot feeds every other bridge, so nothing can disagree. If you'd
rather drive the exporter yourself — to gate the endpoint behind auth, or to run
OpenMetrics instead — reach for `wreath.telemetry` directly:

```python
from wreath import telemetry

bridge = telemetry.activate_prometheus(projector, namespace="llamacam")
# mount bridge.handler() on a route you control, or:
# telemetry.activate_openmetrics(projector)   # OpenMetrics 1.0.0 + # EOF
```

`activate_prometheus` returns a `PrometheusBridge`; `bridge.handler()` is a ready
async handler you can mount on any route, so exposure and auth stay your call.
