# Trace requests with OpenTelemetry

The recorder generates and carries each request's trace and span ids in native
code — the request path never constructs a Python OpenTelemetry object. When you
*do* want to emit your own spans under the request, opt in at the call site with
`telemetry.activate_otel(request)`:

```python
from wreath import telemetry
from opentelemetry import trace

tracer = trace.get_tracer("trailhead")

@app.get("/checkout")
async def checkout(request):
    context = telemetry.activate_otel(request)
    with tracer.start_as_current_span("charge-card", context=context):
        await charge(request)
    return {"ok": True}
```

`activate_otel` returns an OpenTelemetry `Context` holding the request's *owned
server span* — the span the recorder generated for this request and exports over
OTLP — so your spans are children of the same server span your backend sees, not
the incoming remote parent. An app that never calls it pays nothing: the SDK
object is created only here, never on the request path.

It degrades gracefully. With no `opentelemetry` packages installed, or on an
unpropagated request, it returns an immutable native `SpanContextView` instead of
raising — so instrumentation code stays the same whether or not the SDK is
present. If you only need to read the correlation ids (to log them, or to build a
`traceparent` for an outbound call), reach for the view directly:

```python
view = telemetry.current_span(request)   # the incoming remote context
if view.is_valid:
    log.info("trace", trace_id=view.trace_id_hex, parent=view.traceparent())
```

`current_span` reads only the incoming `traceparent` header; `server_span` gives
the request's owned server span with the same view type.
