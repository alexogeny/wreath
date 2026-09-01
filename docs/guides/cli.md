---
description: Choose the Wreath command for scaffolding, development, migrations, deployment, diagnosis, replay and operations.
keywords: CLI commands new run dev test migrations doctor capture replay flight privacy audit infra passes jobs schema MCP
---

```hero
eyebrow: Guide · command-line tasks
title: Start with the job, not the command name.
lede: Wreath's CLI creates projects, proves deployments, operates durable work and turns production evidence into local reproductions. This map shows the shortest path for each task.
signal: scaffold
signal: prove
signal: diagnose
signal: replay
action: Preflight a deployment -> #before-a-deployment
action: Run a migration -> migration-workflow.md
```

All commands are available through the installed `wreath` executable or through
`uv run wreath` inside a project. Add `--help` at any level to see the exact
arguments supported by the installed version.

## Command map

| Command | Use it to |
|---|---|
| `new` | create a runnable service or modular monolith |
| `run` | serve an application in one foreground process |
| `dev` | serve locally and reload after source changes |
| `typegen` | generate TypeScript or Python contracts from typed routes |
| `migrations` | detect, baseline, generate, inspect, apply or reverse model changes |
| `infra` | infer requirements and render an inspectable deployment bundle |
| `privacy` | inspect erasure, access-request and retention declarations |
| `docs` | build or strictly check this dependency-free documentation format |
| `port` | report on or emit a FastAPI-to-Wreath port |
| `mutant` | remove declared controls and see whether tests object |
| `fuzz` | prove controls, then fuzz the tests that killed them |
| `test` | run tests with the Wreath runner or pytest compatibility |
| `audit` | inspect static HTML, live responses or source for defects |
| `inspect` | query a running server's read-only Inspector socket |
| `ci` | generate CI for a supported forge |
| `capabilities` | find the native Wreath owner for a familiar capability |
| `mcp` | relay a local JSON-RPC client to an application's MCP endpoint |
| `doctor` | run route, deployment, query and trace diagnostics |
| `capture` | arm bounded forensic capture on a running server |
| `replay` | re-drive a captured transport, request plan or generated test |
| `passes` | inspect or retry chunked backfills, rollups and purges |
| `jobs` | inspect durable queue rows and dead letters |
| `schema` | emit or verify Wreath's own PostgreSQL support tables |
| `flight` | decode or replay the recorder left behind by a crash |

## Create a project

Create a PostgreSQL-backed modular monolith with tenant isolation and GitHub CI:

```bash
wreath new dispatch \
  --directory services \
  --profile modular-monolith \
  --database postgres \
  --tenancy \
  --forge github
```

`new` refuses a non-empty destination and has no force flag. Generate CI later
with `wreath ci init --help`. Generate consumer contracts without changing the
file unless it drifted:

```bash
wreath typegen dispatch.app:app \
  --target typescript \
  --output web/src/api.ts \
  --react-query \
  --check
```

Use `--factory` when the target is a zero-argument application factory.

## Develop and test

```bash
wreath dev dispatch.app:app
wreath test
wreath test -k migration
```

`wreath test` is the routine project runner. `run` is the production foreground
server; TLS, protocols, loops, workers and shutdown behavior are covered by the
[deployment guide](deployment.md).

Before adding another package for a familiar concern, ask Wreath what owns it:

```bash
wreath capabilities celery
wreath capabilities rate-limit
wreath capabilities --json
```

## Database changes and support tables

Application model changes belong to `migrations`:

```bash
wreath migrations detect dispatch.app:app
wreath migrations generate dispatch.app:app --initial --output migrations/0001
wreath migrations status dispatch.app:app migrations/0001/migration.bin
wreath migrations apply dispatch.app:app migrations/0001/migration.bin
```

Follow the complete [detect-to-rollback migration workflow](migration-workflow.md) before
applying a production change.

`schema` is different: it owns the tables used by Wreath subsystems such as jobs,
rooms and workflow state.

```bash
wreath schema sql dispatch.app:app > wreath-support.sql
wreath schema check dispatch.app:app
```

## Before a deployment

Run every startup refusal Wreath already knows, then inspect the deterministic
route and security manifest:

```bash
wreath doctor preflight dispatch.app:app
wreath doctor routes dispatch.app:app --write route-manifest.json
```

Infer the infrastructure implied by the application declarations:

```bash
wreath infra infer dispatch.app:app --format json > infrastructure-plan.json
wreath infra bundle dispatch.app:app \
  --image registry.example/dispatch@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --output deploy
podman compose -f deploy/compose.yaml config
```

The bundle is reviewable input to deployment, not an instruction to mutate a
cluster. It includes the inferred plan, deployment metadata, Compose definition
and checksums. See [deploy Wreath](deployment.md) for topology and rollout order.

Check privacy declarations and user-facing output as separate gates:

```bash
wreath privacy retention dispatch.privacy:privacy
wreath privacy plan dispatch.privacy:privacy --subject user_123
wreath audit code dispatch
wreath audit static dispatch.app:app --static public --strict
```

Use `wreath privacy access --help` and `wreath audit runtime --help` for the
subject and live-server arguments required by those modes.

## Inspect a running server

The Inspector is a local Unix-domain control surface configured on the server.
It is read-only through `inspect`:

```bash
wreath inspect /run/wreath/dispatch.sock summary
wreath inspect /run/wreath/dispatch.sock active
wreath inspect /run/wreath/dispatch.sock explain-route --method POST --path /jobs
wreath inspect /run/wreath/dispatch.sock failures --json
```

If one request repeatedly reads the same model, reproduce it under the query
diagnostic:

```bash
wreath doctor n-plus-one /run/wreath/dispatch.sock --threshold 10 --strict
```

Trace one durable operation across jobs, messages, workflows and passes:

```bash
wreath doctor trace 0123456789abcdef0123456789abcdef dispatch.app:app \
  --socket /run/wreath/dispatch.sock
```

## Capture, replay and crash evidence

Capture is bounded, expiring and capability-protected. It is not a permanent
traffic tap:

```bash
wreath capture /run/wreath/dispatch.sock arm \
  --hash-header authorization \
  --body structured \
  --max-body-bytes 16384 \
  --expiry 300 \
  --max-matches 20
wreath capture /run/wreath/dispatch.sock status
wreath capture /run/wreath/dispatch.sock disarm --arm-id 17
```

The token can be supplied with `--token` or `WREATH_CAPTURE_TOKEN`. A recording
can be replayed at three useful boundaries:

```bash
wreath replay transport dispatch.app:app request.wtr1
wreath replay plan dispatch.app:app --method POST --path /jobs --body '{"kind":"sync"}'
wreath replay to-test dispatch.app:app request.wtr1 --output tests/test_replay.py
```

After a crash, read the flight recorder and then re-drive a selected request:

```bash
wreath flight read crash.wfrr
wreath flight replay crash.wfrr request.wtr1 dispatch.app:app
```

Use the help for the installed version when selecting a request or job attempt;
the recorder refuses incompatible formats rather than guessing.

## Operate durable work

```bash
wreath jobs list dispatch.app:app
wreath passes status dispatch.app:app
wreath passes retry dispatch.app:app --name account-rollup
```

The jobs view defaults to dead-lettered rows. A barred chunked pass only resumes
when its dead-lettered chunks are explicitly retried.

## Build documentation and general static pages

Configure the static builder through the public `wreath.docs` API. The default
`layout="docs"` supplies navigation, search and page-to-page controls;
`layout="page"` keeps content checking and output generation but removes those
documentation assumptions.

```python
from wreath.docs import AssetManifest, Nav, Page, Site, StaticAsset, Theme

assets = AssetManifest(
    StaticAsset("site-css", "web/dist/site.a1.css", "assets/site.a1.css"),
    StaticAsset("site-js", "web/dist/site.b2.js", "assets/site.b2.js"),
)

site = Site(
    "Notes",
    "notes",
    "site",
    Nav(Page("Home", "index.md")),
    layout="page",
    theme=Theme(
        assets=assets,
        stylesheets=("site-css",),
        scripts=("site-js",),
        head_html='<meta name="application-name" content="Notes">',
    ),
)
```

`StaticAsset.output` is the public path, so an external renderer can contribute
hashed filenames without rewriting Wreath's HTML afterward. Use
`AssetManifest.from_mapping()` when that renderer already exposes logical names
mapped to output filenames. A custom `Theme.template` receives a `PageContext`;
`context.asset(name)` resolves each declared asset relative to the page being
rendered. Wreath continues to own Markdown stamping, link checking and final
output, while Bun, Vite or another renderer owns browser bundles.

## Protocol and assurance tools

| Task | Example |
|---|---|
| serve MCP over a local process transport | `wreath mcp stdio dispatch.app:app` |
| build documentation | `wreath docs build` |
| check documentation without publishing | `wreath docs check` |
| inspect a port before emitting it | `wreath port --report-only legacy` |
| run mutation confidence | `wreath mutant --help` |
| fuzz tests with proved controls | `wreath fuzz --help` |

The [tooling API reference](../reference/tooling.md) documents the Python owners
behind infrastructure inference, type generation, porting and mutation. The CLI
remains the task-oriented surface for operators.
