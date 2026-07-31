# Observability bridges

Wreath's Native Flight Recorder already collects the metrics, traces, and events your app produces. Getting them *out* is a matter of picking a bridge — each reads the same recorder snapshot, so no two exports can disagree, and none pulls in a vendor SDK.

## User story: metrics on Lambda with zero infrastructure

> *As an API author, I run on Lambda where I can't stand up a metrics agent or a scrape target. I still want per-route metrics in CloudWatch — without pulling in `boto3` or running a sidecar.*

```python
from wreath import telemetry

telemetry.activate_cloudwatch_emf(projector, namespace="Trailhead")
```

CloudWatch's Embedded Metric Format is just structured JSON written to stdout, which the Lambda/ECS platform parses into metrics automatically — no agent, no `boto3`, no network call from your process. Like every bridge here it reads the same recorder snapshot, so switching later to a Prometheus scrape or a StatsD push never changes what the numbers *mean*, only where they land.

## Metrics: pull and push

Prometheus (scrape) mounts a `/metrics` endpoint straight from the app:

```python
projector = ...   # the app's metrics snapshot source
app.metrics(projector, path="/metrics")
```

Or drive the exporters directly from `wreath.telemetry`, all reading the same source:

```python
from wreath import telemetry

telemetry.activate_prometheus(projector)            # scrape (text 0.0.4)
telemetry.activate_openmetrics(projector)           # OpenMetrics 1.0.0 + exemplars
telemetry.activate_statsd(projector, dogstatsd=True)  # UDP push, DogStatsD tags
telemetry.activate_cloudwatch_emf(projector, namespace="Trailhead")  # EMF JSON to stdout
```

- **Prometheus / OpenMetrics** — counters, gauges, and per-route histograms in the exposition format; OpenMetrics adds the terminating `# EOF` and the richer content type.
- **StatsD / DogStatsD** — `flush()` sends UDP lines (counters as deltas, gauges absolute); DogStatsD mode emits `|#k:v` tags, plain StatsD folds labels into the metric name. `run_periodic(interval)` drives it from a supervised task.
- **CloudWatch EMF** — structured-JSON metric blobs to stdout that CloudWatch parses automatically. Zero infrastructure on ECS or Lambda — no agent, no `boto3`.

## Traces

`telemetry.activate_otel(...)` bridges wreath's spans to OpenTelemetry (OTLP), which in turn reaches Jaeger, Tempo, Honeycomb, and most vendors.

Because every bridge reads the recorder's own metric definitions rather than a parallel set, switching or doubling up exporters never changes what the numbers *mean* — only where they land.

## Trace context on outbound calls

A trace that stops at the request boundary is a trace of one hop. When a request calls
another service through `wreath.http_client` or `ServiceClient`, wreath puts the calling
request's context on the wire as W3C `traceparent`, so the two services' spans join
without an instrumentation package at either end.

The parent is the request's **own server span**, not the remote parent it inherited: work
a request causes is a child of *that request*. On the native path that span id is the
recorder's real one; on the pure and bare-ASGI paths `server_span()` falls back to the
incoming parent, which keeps the trace joined one level coarser.

Three properties worth knowing:

- **A header you wrote wins.** An explicit `traceparent` in `headers=` is a decision, and
  the framework does not overrule it.
- **Opt out per client, not globally.** An origin outside your trust boundary should not
  receive your trace ids: `HTTPClient(..., trace=TracePolicy(propagate=False))`. This is
  the same shape as `DestinationPolicy`, and for the same reason.
- **`tracestate` rides only when asked** — `TracePolicy(tracestate=True)`. It crosses
  trust boundaries carrying whatever an upstream put in it, so it is off by default.

Nothing is bound until something exists that could send it: constructing an `HTTPClient`
arms propagation, and an application with no outbound client pays one module-attribute
read per request and never touches a `ContextVar`. An application that *does* have one
pays a single `ContextVar` set per request — including for untraced requests, which bind
`None` rather than skipping the bind, so a context reused across keep-alive requests can
never carry the previous request's parent. A trace pointing at the wrong cause is worse
than no trace.
