---
description: Add bounded readiness, telemetry and explicit native-server deployment configuration.
keywords: guide operations health readiness telemetry native server deployment
---

# Operations and deployment

Liveness answers whether the process should be restarted. Readiness answers whether it
should receive traffic. A non-critical dependency can degrade without causing an outage.

```python title="app.py"
from wreath import Wreath
from wreath.health import callable_check, health_router

app = Wreath()


async def primary_database() -> dict:
    return {"round_trip_ms": 1.2}


async def analytics_sink() -> None:
    raise ConnectionError("analytics unavailable")


app.include_router(
    health_router(
        [
            callable_check("primary", primary_database, timeout=0.5),
            callable_check(
                "analytics",
                analytics_sink,
                critical=False,
                timeout=0.2,
            ),
        ]
    )
)
```

```python title="test_app.py"
from wreath.testing import TestClient

from app import app


async def test_non_critical_failure_reports_degraded_and_keeps_serving() -> None:
    async with TestClient(app) as client:
        live = await client.get("/health")
        ready = await client.get("/ready")

    assert live.json() == {"status": "ok"}
    assert ready.status == 200
    assert ready.json()["status"] == "degraded"
    assert ready.json()["checks"]["primary"]["status"] == "pass"
    assert ready.json()["checks"]["analytics"]["status"] == "fail"
```

Serve the same ASGI application through Wreath's native server:

```bash
uv run wreath run app:app \
  --host 0.0.0.0 \
  --port 8000 \
  --loop metal \
  --workers 4
```

## Wire one bounded observability path

The native server can publish request metrics, correlated structured logs and OTLP
traces from one fixed recorder. Forensic capture is a separate, stricter ceiling: a
runtime arm can reveal less than this policy, never more.

```python title="serve.py"
from dataclasses import dataclass

from app import app
from wreath.config import Environment, Secret, read_osenv
from wreath.inspector import InspectorConfig
from wreath.recording import BodyCapture, RecordingPolicy, RedactionPolicy
from wreath.server import ServerConfig, run
from wreath.telemetry import (
    HistogramConfig,
    LoggingConfig,
    Mode,
    OTLPConfig,
    PerRoutePolicy,
    SamplingPolicy,
    TelemetryConfig,
)


@dataclass(frozen=True)
class ObservabilitySettings:
    otlp_endpoint: str
    capture_token: Secret[str]


settings = Environment(read_osenv()).bind(ObservabilitySettings, prefix="OBS")

telemetry = TelemetryConfig(
    mode=Mode.FORENSIC,
    ring_records=16_384,
    active_requests=2_048,
    histograms=HistogramConfig(
        per_route=PerRoutePolicy.CAPPED,
        max_route_histograms=256,
    ),
    detailed=SamplingPolicy(rate=0.05),
    forensic=SamplingPolicy(rate=0.01),
    logging=LoggingConfig(writer_queue=8_192),
    otlp=OTLPConfig(
        enabled=True,
        endpoint=settings.otlp_endpoint,
        export_queue=4_096,
        batch_size=512,
        timeout_seconds=5.0,
    ),
    capture_slabs=64,
    slab_bytes=64 * 1_024,
    ring_path="/var/lib/wreath/flight.wfrr",
)

config = ServerConfig(
    host="0.0.0.0",
    port=8_000,
    telemetry=telemetry,
    inspector=InspectorConfig(
        path="/run/wreath/inspector.sock",
        capture_token=settings.capture_token.reveal(),
    ),
    recording=RecordingPolicy(
        capture_slabs=64,
        max_capture_bytes=4 * 1_024 * 1_024,
        redaction=RedactionPolicy(
            header_hash=frozenset({"x-request-id"}),
            body=BodyCapture.HASHED,
        ),
    ),
    recording_path="/var/lib/wreath/captures.wfr1",
)

print(telemetry.memory_budget(route_count=24))
run(app, config)
```

`OBS_OTLP_ENDPOINT` supplies the OTLP collector and `OBS_CAPTURE_TOKEN` gates
capture-control commands. Completion counters and bounded route histograms always stay
available through the recorder. The exporter sends traces, logs and metrics off the
request path through its bounded queue. The default log writer emits text to a terminal
and JSON lines otherwise; pass `ServerConfig(log_writer=...)` to own the sink.

The WFRR ring retains recent request state across a process crash. The WFR1 sink writes
nothing sensitive until an operator installs a bounded, expiring arm with
[`wreath capture`](cli.md#capture-replay-and-crash-evidence); its redaction and byte
budget must fit inside the startup policy above. Inspect the calculated memory budget
and route count during deployment rather than allowing telemetry cardinality to grow at
runtime.

TLS, protocol selection, limits and telemetry are `ServerConfig` values rather than
hidden command-line state. `TelemetryConfig.memory_budget()` calculates the fixed
recorder allocation before the server starts. Use `wreath infra` to infer database,
listener, object-store and egress requirements from compiled declarations.

Continue with the complete [production deployment runbook](deployment.md) for wheel
extras, TLS and proxy topologies, graceful shutdown, worker sizing, containers,
migration ordering and concrete failure diagnosis.

See [operations reference](../reference/operations.md) for server, health, telemetry,
recording, replay and edge APIs.
