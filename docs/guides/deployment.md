---
description: Install the correct Wreath wheel, choose a TLS and proxy topology, size workers, drain gracefully, generate deployment artifacts and diagnose failures.
keywords: guide production deployment TLS HTTP2 HTTP3 proxy edge workers graceful shutdown Podman systemd diagnostics
boost: 1.4
---

# Deploy Wreath

A production deployment has four separately reviewable decisions: which native
capabilities are installed, where TLS terminates, how many processes own listeners,
and which step is allowed to mutate shared state. Pin all four in deployment artifacts.

## Install the capability you intend to run

Pin one Wreath version in the lockfile or hashed requirements. The extras install
version-matched companion wheels; they are capabilities, not alternate frameworks.

```bash
uv add 'wreath==0.3.4'          # portable framework and server
uv add 'wreath[linux]==0.3.4'   # Linux io_uring metal loop + native TLS transport
uv add 'wreath[h3]==0.3.4'      # Linux HTTP/3; http3 is an alias
```

`wreath[linux,h3]==0.3.4` selects both Linux capabilities. Confirm the resolved
environment during the image build:

```bash
uv run wreath --version
uv run wreath doctor preflight app:app --settings app.config:Settings=APP --environ
uv run wreath schema check app:app
```

See the [supported wheel and platform matrix](../start/releases.md). A missing native
capability is a startup refusal; Wreath does not silently substitute a slower Python
server path.

## Choose one network topology

### TLS at a trusted load balancer

Run Wreath on a private listener and trust forwarding headers only from the actual
proxy network:

```python title="proxy_policy.py"
from wreath import Wreath
from wreath.policy import HttpPolicy
from wreath.policy.proxy import ProxyPolicy
from wreath.policy.security import TrustedHostPolicy

app = Wreath(
    http_policy=HttpPolicy(
        proxy=ProxyPolicy(
            trusted=("10.40.0.0/16",),
            trust_proto=True,
            trust_host=True,
        ),
        trusted_host=TrustedHostPolicy(("api.example.com",)),
    )
)
```

```bash
uv run wreath run proxy_policy:app \
  --host 0.0.0.0 \
  --port 8000 \
  --protocol http/1.1 \
  --loop metal \
  --workers 4 \
  --shutdown-timeout 25
```

Do not use `trusted=("0.0.0.0/0",)`. An untrusted peer that can supply forwarding
headers can otherwise choose the client address, scheme and possibly host seen by
policy. Keep the application listener unreachable from the public network.

### Wreath terminates TLS

HTTP/2 network serving requires TLS with ALPN. Supply the certificate and key together;
when the key is encrypted, read its password from a file rather than process arguments:

```bash
uv run wreath run app:app \
  --host 0.0.0.0 \
  --port 443 \
  --protocol http/1.1 \
  --protocol h2 \
  --loop metal \
  --workers 4 \
  --tls-cert /run/secrets/tls.crt \
  --tls-key /run/secrets/tls.key \
  --tls-password-file /run/secrets/tls-password \
  --shutdown-timeout 25
```

Mount certificate material read-only and rotate it by replacing workers with a new
generation. The CLI reads the password file during startup. It refuses a lone
certificate, lone key, or password file without both.

### Add HTTP/3

Install `wreath[h3]`, expose both TCP and UDP on the same port, and add `h3` to the
protocol set:

```bash
uv run wreath run app:app \
  --host 0.0.0.0 \
  --port 443 \
  --protocol http/1.1 \
  --protocol h2 \
  --protocol h3 \
  --tls-cert /run/secrets/tls.crt \
  --tls-key /run/secrets/tls.key
```

HTTP/3 without its companion wheel or without TLS fails at startup. A network policy
that exposes TCP 443 but not UDP 443 leaves clients on HTTP/1.1 or HTTP/2; verify both
paths rather than treating an `Alt-Svc` header as proof that QUIC is reachable.

### Native edge proxy

Use `wreath.edge.serve()` when Wreath itself is the reverse-proxy tier. Its request
path is native and accepts no ASGI application; unsupported upstream features are
refused during configuration instead of falling back to `ReverseProxy`. Configure
upstream pools and destination policy in Python, then supervise that edge process as a
separate service. See the [edge API](../reference/operations.md#wreath.edge.serve).

## Size workers from assigned cores

`--workers` is available only with `--loop metal`, on its POSIX process model, and every
worker owns an `SO_REUSEPORT` listener. `wreath dev` stays single-worker.

Start with one worker per **assigned physical core**, leaving capacity for kernel
network work and colocated job/database activity, then measure the real request mix.
Do not multiply workers by logical CPU count and database pool size independently: four
workers with a write pool of eight can open thirty-two write connections before jobs
or migrations are counted.

Keep durable job concurrency, outbound client limits and PostgreSQL pools explicit per
process. Scale the application tier only after verifying the database and load
generator are not the bottleneck.

## Bound hostile and slow peers

The server flags are budgets, not decoration. Review at least:

- request line, header count and header bytes;
- body bytes and body chunk count;
- WebSocket fragments;
- request and keep-alive timeouts;
- HTTP/2 and HTTP/3 concurrent streams and header-list bytes;
- read and response high/low watermarks.

Defaults are conservative and validated together. Change them from observed traffic
and keep the values in service configuration, not a one-off shell history. The full
surface is in [`ServerConfig`](../reference/operations.md#serverconfig).

## Make shutdown fit the supervisor

Wreath handles `SIGINT` and `SIGTERM` gracefully. Shutdown stops the Inspector first,
then the listener, asks active protocols to finish current requests, drains until
`--shutdown-timeout`, closes remaining transports, joins telemetry, and runs ASGI
lifespan shutdown last.

Configure the outer supervisor's grace period longer than Wreath's drain:

```ini
[Service]
WorkingDirectory=/opt/wreath/app
EnvironmentFile=/etc/wreath/app.env
ExecStart=/opt/wreath/app/.venv/bin/wreath run app:app --host 0.0.0.0 --port 8000 --loop metal --workers 4 --shutdown-timeout 25
KillSignal=SIGTERM
TimeoutStopSec=35
Restart=on-failure
```

Before signalling a worker generation, remove it from readiness and allow the load
balancer's propagation delay. A supervisor that sends `SIGKILL` at 20 seconds cannot
honour a 25-second application drain.

## Build an inspectable container bundle

First ask the application what infrastructure its declarations require:

```bash
uv run wreath infra infer app:app \
  --settings app.config:Settings=APP \
  --env deploy/app.env
```

Resolve every reported gap. Then combine the plan with an immutable OCI image digest:

```bash
uv run wreath infra bundle app:app \
  --settings app.config:Settings=APP \
  --env deploy/app.env \
  --image registry.example/wreath/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --output deploy/bundle

podman compose -f deploy/bundle/compose.yaml config
podman compose -f deploy/bundle/compose.yaml up -d
```

The bundle contains `compose.yaml`, the inferred infrastructure plan, a deployment
contract and `SHA256SUMS`. It refuses a mutable image tag, unresolved gap, ambiguous
persistent-volume owner or overwrite of a reviewed bundle unless `--force` is explicit.
`infra bundle` renders files; it never contacts or mutates a provider.

Run migrations as a one-shot job from the same image before updating the application
service. Follow [migrations from detect to rollback](migration-workflow.md).

## Diagnose concrete failures

| Symptom | First command | What it separates |
|---|---|---|
| image will not start | `wreath doctor preflight app:app --environ` | missing config, inferred infrastructure gaps, hardening findings, undeclared access |
| support tables missing | `wreath schema check app:app` | Wreath-owned component versions and absent relations |
| models and database disagree | `wreath migrations status app:app migrations/*/migration.bin` | code, artifact chain, history and live catalog |
| route changed unexpectedly | `wreath doctor routes app:app --check routes.json` | deterministic method/path/wire/security manifest drift |
| server is saturated | `wreath inspect /run/wreath/inspector.sock summary` | workers, pressure and active-request facts |
| one request repeats a query | `wreath doctor n-plus-one /run/wreath/inspector.sock --strict` | recorded per-model query repetition |
| durable work disappeared | `wreath doctor trace TRACE_ID app:app --socket /run/wreath/inspector.sock` | request, job, message, workflow and pass carrying one trace |
| jobs are dead-lettered | `wreath jobs list app:app --state dead` | queue attempts and trace context |
| a backfill is barred | `wreath passes status app:app --holes` | phase, frontier and reproducing statements for dead chunks |
| process crashed | `wreath flight read crash.wfrr --limit 100` | bounded records left in the flight ring |

`inspect`, live `doctor` topics and capture require an Inspector configured in
`ServerConfig`; they are local Unix-socket control surfaces, not public HTTP admin
routes. Token-gate forensic capture, bound every arm by expiry and budget, and prefer
hashed or metadata-only fields. The [command-line guide](cli.md) gives the complete
capture and replay sequence.

## Deployment gate

A release candidate should not receive traffic until all of these are green:

```bash
uv run wreath doctor preflight app:app --settings app.config:Settings=APP --environ
uv run wreath doctor routes app:app --check routes.json
uv run wreath schema check app:app
uv run wreath migrations status app:app migrations/*/migration.bin
uv run wreath test
```

Then verify `/health`, `/ready`, TLS negotiation, the proxy/client address boundary and
a graceful `SIGTERM` in the actual topology. A green unit suite cannot prove any of
those deployment facts.

See [operations and deployment](operations.md), [policy and hardening](policy.md), and
the complete [operations API](../reference/operations.md).
