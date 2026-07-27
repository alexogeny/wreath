# Operational observability plan

## Status

Future proposal; extends existing request ID and server-timing primitives.

## Objective

Provide low-overhead structured events, metrics, traces, and health snapshots across requests, persistent connections, clients, services, jobs, PostgreSQL, and command delivery without adding mandatory exporter dependencies.

## Public model

Neo defines stable instruments and event hooks. Optional exporters adapt snapshots/events to Prometheus, OpenTelemetry, JSON logging, or other systems. Applications may register custom bounded instruments at startup; request-time dynamic instrument creation is rejected.

Core contexts carry correlation, trace, service, job, and command identifiers without hidden mutable globals. Context propagation uses explicit context objects or scoped context variables with documented task inheritance.

## Native data plane

The optional native backend may provide:

- fixed-name counters and gauges;
- fixed-bucket histograms;
- bounded label dictionaries compiled at startup;
- a bounded event ring with overflow accounting;
- monotonic timestamps;
- strict W3C trace-context parsing and formatting;
- snapshot operations that minimize interference with hot paths.

The pure twin uses Python arrays, dictionaries with the same configured bounds, locks where required, and bounded queues. Snapshot values and overflow behavior match, allowing harmless timing differences.

Exporters and network I/O remain Python-side. C never imports or embeds an exporter SDK.

## Cardinality and privacy rules

- User, tenant, device, request, command, and job IDs are trace/log attributes, not metric labels by default.
- Label names and allowed values are configured at startup.
- Unknown labels fail or map to an explicit bounded `other` value.
- Payload bodies, credentials, cookies, authorization headers, and database parameters are excluded by default.
- Export backpressure never blocks request or control execution indefinitely.
- Dropped events and failed exports are themselves counted.

## Built-in coverage

Instrument request phases, response status, exception class, authentication decisions, active WebSockets, queue depth, disconnect reason, outbound pool wait, DNS/connect/TLS/request duration, retries, service state/restarts, job claims/runtime/outcomes, command state/ack latency, PostgreSQL pool saturation, and event-loop lag.

## Phases

1. Define event, instrument, context, redaction, and exporter protocols.
2. Implement pure registry, snapshots, bounded event queue, and test exporter.
3. Instrument supervisor and existing HTTP/PostgreSQL paths.
4. Implement native counters, histograms, trace parsing, and ring buffer.
5. Publish optional Prometheus/OpenTelemetry adapters.
6. Add overhead, cardinality, and exporter-failure benchmarks.

## Verification

Test bounded cardinality, ring overflow, exporter failure, cancellation, snapshot consistency, trace propagation, redaction, free-threaded updates, and pure/native parity. Measure disabled, enabled-unexported, and actively exported overhead separately.

## Completion criteria

Operators can explain service readiness, connection pressure, pool saturation, retries, job lag, command backlog, and error rates without enabling debug payload capture. Instrumentation has finite memory behavior and documented overhead.

## Risks

A generic event API can become an unstable logging framework. Keep a small semantic core, version built-in event schemas, and leave presentation/export policy to optional adapters.
