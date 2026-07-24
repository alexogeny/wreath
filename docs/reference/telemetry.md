# `wreath.telemetry`

Native metrics, tracing configuration, and OpenTelemetry integration — the
public surface of the Native Flight Recorder. Constructing a `TelemetryConfig`
validates it and can compute its exact fixed memory budget; passing it to
`wreath.server` creates a native recorder and starts the off-path projector
that drains its ring.

The OpenTelemetry bridge is lazy by design: the request path never constructs a
Python OTel object. `current_span` and `activate_otel` let user code opt in at
the call site, degrading to an immutable `SpanContextView` when no OTel
packages are installed.

::: wreath.telemetry
