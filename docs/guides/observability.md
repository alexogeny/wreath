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
app.metrics(
    projector,
    path="/metrics",
    counter_sources=(client_facts_provider,),
)
```

Or drive the exporters directly from `wreath.telemetry`, all reading the same source:

```python
from wreath import telemetry

telemetry.activate_prometheus(projector, app=app)            # scrape (text 0.0.4)
telemetry.activate_openmetrics(projector, app=app)           # OpenMetrics 1.0.0
telemetry.activate_statsd(projector, app=app, dogstatsd=True)  # UDP push
telemetry.activate_cloudwatch_emf(projector, app=app, namespace="Trailhead")  # EMF
```

An explicit `counter_sources=(provider,)` adds any operation-owned subsystem
whose `counters()` returns `wreath.metrics.Counters`. Every bridge delegates
discovery, deduplication, shape checks, and failure isolation to
`wreath.metrics.collect`; Prometheus, OpenMetrics, StatsD/DogStatsD and
CloudWatch EMF only encode those canonical rows. A
`ClientFactsProvider` uses this seam for fixed totals and ISO alpha-2
`country_xx` totals only, so Prometheus and DogStatsD never receive an
attacker-controlled browser, platform, User-Agent, country, or IP as a metric
label.
Values are monotonic counters by default. A source whose reading also contains
live gauges marks their names explicitly, for example
`Counters("health", "public", {"ready": 1}, gauges=frozenset({"ready"}))`, so
delta-oriented bridges keep the gauge absolute.
Providers declared with `app.client_facts()` are already application-owned and
are discovered automatically by `app.metrics(...)`; they need no duplicate
`counter_sources` wiring.
The default `AIScrapingPolicy` is discovered in the same walk and contributes
the bounded `ai_scraping_policy.refused` total. Native refusals also remain in
Flight and OTLP as the structured `ai_scraping` policy disposition even though
they occur before route activation. They remain successful policy decisions,
not unhandled errors, so error reporters such as Sentry, Rollbar, and Bugsnag
are not invoked for them.

- **Prometheus / OpenMetrics** — counters, gauges, and per-route histograms in the exposition format; OpenMetrics adds the terminating `# EOF` and the richer content type.
- **StatsD / DogStatsD** — `flush()` sends UDP lines (counters as deltas, gauges absolute); DogStatsD mode emits `|#k:v` tags, plain StatsD folds labels into the metric name. `run_periodic(interval)` drives it from a supervised task.
- **CloudWatch EMF** — structured-JSON metric blobs to stdout that CloudWatch parses automatically. Zero infrastructure on ECS or Lambda — no agent, no `boto3`.

## When a number is not enough

A counter says a durable job failed; it does not say what it was doing. For the
work nobody is watching — a background job, a pass shift — Wreath can record
**one attempt** and hand it back as a runnable pytest: identity, the enqueuing
request's trace context, the boundaries it crossed in order, and one of four
outcomes. Deny-by-default, arguments never captured, and a replay that cannot
reach the live queue. See
[Durable jobs](jobs.md#recording-an-attempt-and-replaying-it-as-a-test) and
[`wreath.recording`](../reference/recording.md#recording-a-durable-job-attempt).

## Traces

`telemetry.activate_otel(...)` bridges wreath's spans to OpenTelemetry (OTLP), which in turn reaches Jaeger, Tempo, Honeycomb, and most vendors.

When a handler already resolved `ClientFacts`,
`telemetry.annotate_otel(recording_span, facts)` adds the standard
`geo.country.iso_code`, `user_agent.*`, `browser.mobile`, and
`user_agent.synthetic.type=bot` attributes. Wreath's projection omits the raw
IP and original User-Agent. It is explicit because `activate_otel` returns
parent context, not a recording span owned by Wreath.

With Wreath's native server and Flight Recorder, that same resolution also
emits one compact client-facts carrier behind the completion cell. The
projector enriches Wreath's exported server span automatically in both JSON and
protobuf OTLP. It exports only country, IP family/source, a stable bundled-UA
rule id, mobile/bot flags, and whether Web Bot Auth verified the agent; raw
network and header identifiers stay out of the ring.
An ingress refusal needs no client-facts carrier: its completion flags carry
the bounded `ai_scraping` disposition directly, and the OTLP server span emits
`wreath.policy.refused=true` without retaining the User-Agent.

Wreath also ships its own dependency-free `OtlpHttpExporter` for OTLP traces,
metrics, and logs. The OpenTelemetry bridge is for context/interoperability; it
is not the only way telemetry leaves the process.

## Unhandled errors and Sentry

Unhandled HTTP, WebSocket, exception-renderer, and background failures are
recorded as `wreath.error.unhandled` events on the same logging/OTLP spine. The
record contains the exception type and framework phase, never a request body,
headers, or exception message.

Add a Sentry sink without giving Wreath ownership of Sentry's global setup:

```python
import sentry_sdk
from wreath.errors import BugsnagErrorReporter, RollbarErrorReporter, SentryErrorReporter

sentry_sdk.init(dsn=settings.sentry_dsn)
app.add_error_reporter(SentryErrorReporter(client_facts=client_facts_provider))
```

Passing a client-facts provider is optional. When present, the adapter puts
browser and platform facts in non-indexed event context and only bounded
country/IP-family/source/mobile/bot values in Sentry tags. It never sets a
Sentry user, raw address, raw User-Agent, city, coordinates, or ASN
organization.

The same protocol has first-party `RollbarErrorReporter` and
`BugsnagErrorReporter` adapters. The application initializes whichever vendor
SDK it chose; Wreath only forwards the exception. Native OTLP logs remain the
vendor-neutral route and need none of those packages.

Reporter failures are counted and logged; they do not replace the application's
response. An explicit `OtlpErrorReporter` is available for jobs or other owners
outside app dispatch, while app-boundary errors are already recorded and should
not register it a second time.

Because every bridge reads the recorder's own metric definitions rather than a parallel set, switching or doubling up exporters never changes what the numbers *mean* — only where they land.

### The OTLP encoding

`OtlpHttpExporter` sends **`application/x-protobuf`** by default, over the
standard library and wreath's own [protobuf codec](protobuf.md) — so enabling
export still pulls in no third-party dependency, neither `protobuf` nor
`opentelemetry-*`.

OTLP specifies both protobuf and JSON, and every receiver accepts JSON. Protobuf
is the default because it is what SDK and collector exporters actually send,
which makes it the path a receiver's handling is genuinely exercised on; a
JSON-only exporter is the encoding least likely to have been tested against.

JSON stays fully supported and is worth keeping for two cases — it is readable
in a proxy log, and it is the fallback if a receiver's protobuf handling turns
out to be the broken one:

```python
OtlpHttpExporter("https://otlp.example.com", encoding="json")
```

Both encodings are built from one set of request builders and converted at the
edge, so they cannot describe different telemetry. An unknown `encoding=` is
refused at construction rather than at the first export.

The exporter pins redirects to the collector's configured scheme, host, and
port. Same-origin redirects still work; a cross-origin redirect is refused, so
a compromised collector response cannot turn an export into a request to an
internal service.

## Trace context on outbound calls

A trace that stops at the request boundary is a trace of one hop. When a request calls
another service through `wreath.http_client` or `ServiceClient`, wreath puts the calling
request's context on the wire as W3C `traceparent`, so the two services' spans join
without an instrumentation package at either end.

The parent is the request's **own server span**, not the remote parent it inherited: work
a request causes is a child of *that request*. On the native path that span id is the
recorder's real one; on the Python and bare-ASGI paths `server_span()` falls back to the
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

## Trace context across the queue

A durable job is caused by the request that enqueued it, and a job that fails at 03:00 is
the hardest thing in the system to attribute: the request succeeded hours ago, and
nothing linked them. `JobRunner.enqueue` and `launch` write the calling request's
`traceparent` onto the job row, and the runner rebinds it around the handler — so the
job's own outbound calls join the same trace, and `ctx.trace_context` names the cause for
a log line.

```python
@runner.task("rebuild_thumbnails")
async def rebuild(ctx, upload_id: int):
    logger.info("rebuilding", caused_by=ctx.trace_context)
```

Three properties, each of which is a decision rather than an accident:

- **The context rides the row, never the `NOTIFY`.** The doorbell payload is empty and
  stays empty — PostgreSQL caps it at 8000 bytes and correctness never depends on a
  notification arriving at all. Data on that channel would be a second, lossy transport.
- **`traceparent` only; `tracestate` is dropped.** `tracestate` is vendor routing for the
  *next hop of a live call*. A job resumes a trace rather than continuing a conversation,
  and a routing hint would sit in the queue going stale.
- **Absent stays absent.** A job enqueued outside a traced request stores SQL `NULL`, not
  an empty string, so `WHERE trace_context IS NOT NULL` means what it says.

Registering a `JobRunner` arms propagation the same way constructing an `HTTPClient`
does: the queue is a seam that carries context past this process, so an application with
jobs and no outbound client still binds one.

### Rolling deploys

The column arrives as version 2 of the `jobs` schema component, and both directions of a
mid-rollout fleet keep working:

- An **older build against the upgraded schema** never writes or reads the column;
  `bootstrap` warns that a newer wreath has already upgraded it and carries on.
- A **newer build against a version-1 schema** — a DBA who has applied step 1 by hand and
  not yet step 2 — enqueues and drains normally, without the context. The runner asks the
  catalog once whether the column is there rather than discovering it from a failed
  `INSERT`: turning an observability feature into a queue outage is the wrong trade.

## Trace context across sagas, passes and the durable bus

Four durable rows now carry the same string, written by four subsystems that share no
code, so one trace id names everything a request set in motion.

**Workflow instances.** The traceparent rides `<table>_instances`, and `run`, `resume`
*and the compensation chain* all execute under it. That is the case durable workflows
exist for: an instance resumed hours later in another process is the same trace as the
one that started it, and an undo is visibly part of the saga rather than an orphan
beside it.

**Chunked passes.** The traceparent rides the ledger row, and every shift rebinds it, so
a chunk that dead-letters on day three of a backfill still names the drive that started
the walk. Two decisions shape what that means:

- **Capture, never mint.** A pass driven only by `cron` has no originating request. It
  stores SQL `NULL` and its shifts run untraced, rather than being given a freshly
  invented trace id. Wreath propagates context; it does not generate spans, and it
  carries the sampling decision rather than re-deciding it — a minted traceparent would
  have to choose a sampled flag, and `-01` forces every backend in the path to retain a
  trace that may run for days while `-00` produces an id that is stored, printed, and
  collected by nothing.
- **The trace belongs to the cycle, not to the pass.** `seed` records the first drive
  that *has* a trace (`COALESCE`, so a later one does not re-attribute a walk already
  under way), and a recurring pass re-captures when a new cycle begins. That is the
  retention bound: a recurring pass runs for the life of the deployment, and carrying
  one drive's traceparent across every cycle would produce a trace that never ends.

A single finite backfill driven from a traced request is therefore one trace for as long
as the backfill runs. That is the operator's own choice at the moment they drive it, and
the alternative expressible in one `traceparent` column is no trace at all.

**Durable bus messages.** `publish(..., durable=True)` writes the calling request's
traceparent onto every subscriber group's row, and the consumer — routinely a different
service on a different day — runs its handler under it.

**Ephemeral fan-out carries nothing, deliberately.** The plan's rule for the queue
("context rides the row, never the `NOTIFY`") does not transfer to ephemeral publish,
because there is no row: `pg_notify($1, $2)` carries your payload *as* the message. The
only place a traceparent could go is inside that payload, which means wrapping every
ephemeral message in an envelope — a breaking change to a live wire format between
processes, needing a versioned envelope that a mid-rolling-deploy subscriber on the old
build can still read. The 8000-byte `NOTIFY` bound is not the obstacle; a traceparent is
55 bytes. It is deferred, with a row in [the roadmap](../reference/roadmap.md), and
`wreath doctor trace` names it as unsearched every time it runs.

## Following a trace afterwards

```bash
wreath jobs list myapp:app                     # dead letters, each with its trace id
wreath passes status myapp:app                 # a stopped pass prints its trace id
wreath doctor trace <trace-id> myapp:app --socket /run/wreath.sock
```

`wreath doctor trace` is the join. Given a trace id it reports every job, durable
message, workflow instance and chunked pass carrying it, and — with `--socket` — the
recorded request itself out of the Flight Recorder's ring.

It always prints a **`not searched`** section, and that is the part to read. A forensic
tool that quietly leaves a source out is worse than one that answers nothing, because
"no durable work carries this trace" then reads as "nothing does". Every source it could
not reach is named: a table that is not on this database, a schema still on the version
before propagation, an Inspector socket nobody gave it, a trace that has aged out of the
ring, and ephemeral bus messages, which carry no context at all.
