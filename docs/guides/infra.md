# What does this service actually need?

Ask that of a Django or a FastAPI application and there is no answer in the
code. It could need Redis, or Celery and RabbitMQ, or Kafka, or Mongo, or a
cache and a broker and a second datastore, wired any way at all. The framework
does not know, so nothing in the framework can tell you. The answer lives in a
Terraform directory in another repository, maintained by hand, and drifts.

Ask it of a wreath application and the answer is already written down. Not
because of anything clever in this module — because wreath owns those answers
already. The queue is PostgreSQL. So are the message bus, sessions, rate limits,
idempotency, workflows, chunked passes, progress and rooms. Blobs are
[`wreath.objects`](objects.md). Locks are advisory locks. Caches are this
process. There is no broker to discover, because there is no broker.

The rule that made that true was adopted for other reasons — keep
`src/wreath` free of mandatory runtime dependencies — and it turns out to make
the infrastructure small enough to *infer*.

```console
$ wreath infra infer camera_trap.app:app --settings camera_trap.config:Settings=CAMERA_TRAP --environ
```

## It is a derivation, not an analysis

`wreath infra infer` imports the application and reads the objects its own
declarations built. `app.postgres("main", dsn=...)` constructed a
[`Database`](../reference/postgres.md) that still holds its DSN and its pool
sizes. `app.objects("cards", backend="s3", ...)` constructed a store that still
holds its bucket and its region. `app.http_client("forage", base_url=...)`
constructed a client pinned to an origin, carrying the
[`DestinationPolicy`](http-client.md) that says where it may ever connect —
which is an egress rule, already written, in the application.

So there is no source parsing, no pattern matching, and nothing to be subtly
wrong about. There is also no cloud SDK and **no network call**: inference walks
objects that are already in memory, which is why it is safe to run from a
laptop against an application whose database is not reachable from there.
`tests/test_infra_infer.py` asserts both of those directly rather than
promising them.

## The camera-trap example, end to end

The [camera-trap example](../example/index.md) is the application in this
repository with enough surface to be a real test: a database, nine ORM models,
a durable queue with progress, an object store, Cedar authorization, and thirty-
three routes. Run from the repository root, with the example package on the
import path:

```console
$ CAMERA_TRAP_DSN=postgresql://camera_trap@db.internal:5432/camera_trap \
  CAMERA_TRAP_MAX_WINDOW_DAYS=90 \
  CAMERA_TRAP_SPECIES_CACHE_TTL=300 \
  CAMERA_TRAP_MEDIA_ROOT=/tmp/camera-trap/media \
  PYTHONPATH=example \
  wreath infra infer camera_trap.app:app \
    --settings camera_trap.config:Settings=CAMERA_TRAP --environ
```

That DSN is never connected to. It is read, split, and printed back without its
password, which is the whole of what a plan needs from it.

```text
Infrastructure inferred from camera_trap.app:app
================================================

PostgreSQL (1)
--------------
  main  db.internal:5432/camera_trap as camera_trap
    pool read           1..10
    pool write          0..10
    held write          1 of 10 for the life of the process (jobs runner 'ingest' LISTEN doorbell); 9 left for requests
    schemas             camera_trap, wreath
    extensions          none
    application tables  9 ORM model(s); their DDL comes from wreath migrations
    wreath tables       camera_trap.jobs  (jobs, from app.jobs('ingest'))
                        wreath.series_buckets, wreath.series_corrections  (series, from app.series(database='main'))

Object storage (1)
------------------
  media  local disk, root /tmp/camera-trap/media
      requires  a writable directory that survives a restart -- a volume, not a container layer

Egress (0)
----------
  none: this application pins no outbound HTTP client.
  A ServiceClient built over a client the application did not register is
  invisible here; see the notes.

Listener
--------
  http  33 route(s), 0 websocket route(s)
      methods  DELETE, GET, PATCH, POST, PUT
      the port, the TLS termination and the load balancer are
      deployment decisions; the application does not declare them

What would be a separate service somewhere else
-----------------------------------------------
  Every row is PostgreSQL, the local disk, or this process. There is no
  broker, no cache server and no second datastore anywhere in this plan,
  and that is a property of wreath rather than of this application.

                subsystem             lives in                   instead of
  declared      durable jobs          PostgreSQL                 Celery, RQ, Redis, RabbitMQ
  absent        message bus           PostgreSQL                 Kafka, RabbitMQ, SNS + SQS
  absent        webhook inbox/outbox  PostgreSQL                 SQS, Celery
  absent        sessions              PostgreSQL                 Redis, Memcached
  absent        rate limits           PostgreSQL                 Redis
  absent        idempotency           PostgreSQL                 Redis
  unobservable  workflows             PostgreSQL                 Temporal, Airflow
  absent        chunked passes        PostgreSQL                 Celery, a bespoke backfill script
  declared      progress              PostgreSQL LISTEN/NOTIFY   Redis pub/sub
  unobservable  rooms                 PostgreSQL LISTEN/NOTIFY   Redis pub/sub
  unobservable  calculated views      PostgreSQL                 a warehouse, dbt
  unobservable  distributed locks     PostgreSQL advisory locks  Redis, etcd, ZooKeeper
  unobservable  application cache     in-process memory          Redis, Memcached
  unobservable  response cache        in-process memory          Varnish, Redis

  durable jobs: runner 'ingest' on database 'main'
  workflows: a Workflow is constructed with a store and handed to a handler; nothing registers it on the application
  progress: watched queue 'ingest'
  rooms: a RoomRegistry is constructed over a message bus and held by the application author, not by the application
  calculated views: a Series is a declared class queried through a session; it registers nothing on the application
  distributed locks: advisory locks are taken per call site, so there is no declaration to read
  application cache: @cached decorates a function; the application never sees it
  response cache: response caching is a route decoration, not an application registration

Settings contract
-----------------
  camera_trap.config:Settings, prefix CAMERA_TRAP
  keys as wreath.config.Environment.bind resolves them, in declaration order
    key                            field              type        supplied by
    CAMERA_TRAP_DSN                dsn                str | None  process
    CAMERA_TRAP_MAX_WINDOW_DAYS    max_window_days    int         process
    CAMERA_TRAP_SPECIES_CACHE_TTL  species_cache_ttl  float       process
    CAMERA_TRAP_SESSION_KEY        session_key        str | None  -- nothing supplies this
    CAMERA_TRAP_SESSION_SECURE     session_secure     bool        -- nothing supplies this
    CAMERA_TRAP_MEDIA_ROOT         media_root         Path        process
    CAMERA_TRAP_MEDIA_KEY          media_key          str | None  default

Gaps (2)
--------
  [missing] CAMERA_TRAP_SESSION_KEY
      camera_trap.config:Settings requires session_key (str | None) and
      nothing supplies CAMERA_TRAP_SESSION_KEY
  [missing] CAMERA_TRAP_SESSION_SECURE
      camera_trap.config:Settings requires session_secure (bool) and nothing
      supplies CAMERA_TRAP_SESSION_SECURE

Notes
-----
  - Telemetry sinks are not derivable from an application: TelemetryConfig
    is a field of wreath.server.ServerConfig, not of Wreath, so a sink is a
    property of how the application is served rather than of the
    application.
```

`tests/test_infra_docs_example.py` runs that command and compares it with this
page, so the block above cannot quietly become a description of an older
version of the example.

## The three things in that output worth stopping on

### One connection is held for the life of the process, and the pool has to allow for it

```text
    held write          1 of 10 for the life of the process (jobs runner 'ingest' LISTEN doorbell); 9 left for requests
```

A [job runner](jobs.md) and a [message bus](../reference/messaging.md) each hold
one `LISTEN` connection so a `NOTIFY` wakes them without waiting out a poll —
and that connection comes *out of a workload pool* and is never given back. With
a ten-connection write pool that is invisible. With `PoolConfig(max_size=1)` it
is a process that cannot serve a write at all, and the symptom is an acquire
timeout with no obvious cause. So the plan does the subtraction, and a pool with
nothing left over is reported as a gap rather than as a row.

One thing the plan will not do is guess which database a table belongs in. Most
subsystems say — `app.jobs(database="main")` names one — but a
[webhook](http-client.md) inbox is handed a session per call and never sees a
`Database` at all. With one database registered there is nothing to decide; with
two, the plan reports it as a gap naming the component and the command that
emits its DDL, rather than creating the tables beside a subsystem that reads the
other one. Wreath's own startup refuses in the same place, for the same reason.

### The absence of a broker is stated, not implied

The subsystem table is printed in full whether or not the application uses any
of it. That is deliberate, and it is the part readers do not believe: an
application with a durable queue, a watchable ingest, background sweeps and
cross-worker fan-out provisions **one PostgreSQL database and a directory**.
Printing only the database and letting you infer the rest reads as an
oversight — as though the Redis line had been forgotten. Writing every row down,
with what a comparable stack would have provisioned beside it, is the claim
being made.

Three values appear in that first column, and the third is the honest one:

- **`declared`** — the application registered it. It needs nothing new.
- **`absent`** — the application registered none, and the application object
  would know if it had.
- **`unobservable`** — the framework never learns about this one. A
  `RoomRegistry` is constructed over a bus and handed to a handler; a `Workflow`
  is constructed with a store; `@cached` decorates a function. None of them
  registers anything, so reporting them as absent would be a false negative
  wearing a finding's clothes. They are also all PostgreSQL or this process, so
  the answer does not change — only the confidence does.

### A settings field with no supplier is a gap, by name

This is the row the whole command exists for.

```text
  [missing] CAMERA_TRAP_SESSION_KEY
      camera_trap.config:Settings requires session_key (str | None) and
      nothing supplies CAMERA_TRAP_SESSION_KEY
```

The failure it closes is worth naming precisely, because it is extremely common
and always looks like something else. An infrastructure definition carries a
hand-maintained list of environment variables. An application, in another
repository and often in another language, declares a settings type that reads
those names. They are two independent statements of one contract and nothing
connects them. Rename a field in the application and the stack still applies
perfectly cleanly; the container starts, fails to bind its configuration, and
dies — and the first thing anyone reads is an error about a null value, three
layers from the rename.

[`Environment.bind`](config-state.md) is the app-side half of that contract: it
turns `session_key` under prefix `CAMERA_TRAP` into `CAMERA_TRAP_SESSION_KEY`
by a fixed rule, honouring nested dataclasses (`__`) and
`Annotated[str, Env("EXACT_NAME")]` aliases. `wreath infra infer` derives the
keys that rule will read — from the same primitives the binder uses, not a
second copy of them — and checks each against whatever is supposed to supply it.

A key can be supplied three ways, and the report says which:

| `supplied by` | Meaning |
| --- | --- |
| a file path | a `--env` dotenv file sets it |
| `process` | `--environ` was passed and the process environment has it |
| `default` | the field carries its own default, so nothing needs to set it |
| `-- nothing supplies this` | the gap |

**The command exits 1 when there are gaps.** That is the point of it: run it in
CI against the environment a deployment will actually have, and a renamed field
fails the pipeline instead of the container.

The report goes the other way too. A key an authored dotenv supplies that no
field reads is reported as `[unread-key]` — a typo'd variable that has been
sitting in a deployment doing nothing, which is the same drift seen from the
other side.

## What it cannot see, and why that is written down rather than guessed

An inference that is subtly wrong is worse than no inference, because it looks
authoritative. So this command reports what it could not reach instead of
inventing it.

- **Telemetry sinks.** `TelemetryConfig` is a field of
  [`ServerConfig`](server.md), not of `Wreath`, so an exporter is a property of
  how an application is *served* rather than of the application. A note says so
  every time.
- **A `ServiceClient` over a client the application did not register.**
  [`ServiceClient`](service-client.md) wraps an `HTTPClient`; when that client
  came from `app.http_client(...)` the origin is derived, and when it was
  constructed inline it is invisible. Register the client on the application and
  it appears.
- **The listener's port, its TLS, its load balancer.** The application does not
  declare them, so the plan does not invent them.
- **The settings model.** Nothing records which settings type an application
  binds — `Environment.bind` is a free function whose result the author keeps —
  so `--settings module:Class[=PREFIX]` names it. Without it the contract is
  reported as unchecked rather than as satisfied.

## What this stage is not

This is stage one and it is read-only by design. There is no provider, no state
file, no diff, and no `apply`. It emits a plan for a person to read, because an
inference that is subtly wrong should be caught by a human before anything
touches an account rather than after.

It is also not a replacement for Terraform's coverage. It covers what wreath
applications need, which is a much smaller set — and where the answer to "does
it support X" is no, that is the documented position rather than a discovered
disappointment.

Reference: [`wreath.infra`](../reference/infra.md).
