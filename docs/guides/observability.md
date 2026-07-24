# Observability bridges

Wreath's Native Flight Recorder already collects the metrics, traces, and events your app produces. Getting them *out* is a matter of picking a bridge — each reads the same recorder snapshot, so no two exports can disagree, and none pulls in a vendor SDK.

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
telemetry.activate_cloudwatch_emf(projector, namespace="Llamacam")  # EMF JSON to stdout
```

- **Prometheus / OpenMetrics** — counters, gauges, and per-route histograms in the exposition format; OpenMetrics adds the terminating `# EOF` and the richer content type.
- **StatsD / DogStatsD** — `flush()` sends UDP lines (counters as deltas, gauges absolute); DogStatsD mode emits `|#k:v` tags, plain StatsD folds labels into the metric name. `run_periodic(interval)` drives it from a supervised task.
- **CloudWatch EMF** — structured-JSON metric blobs to stdout that CloudWatch parses automatically. Zero infrastructure on ECS or Lambda — no agent, no `boto3`.

## Traces

`telemetry.activate_otel(...)` bridges wreath's spans to OpenTelemetry (OTLP), which in turn reaches Jaeger, Tempo, Honeycomb, and most vendors.

Because every bridge reads the recorder's own metric definitions rather than a parallel set, switching or doubling up exporters never changes what the numbers *mean* — only where they land.
