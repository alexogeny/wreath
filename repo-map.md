# Repository map

A quick routing guide for Wreath contributors and coding agents. Start with `AGENTS.md` for repository rules, then use `docs/agents/manifest.json` for the machine-readable subsystem map — it gives every subsystem's guides, reference pages, sources, tests, and the invariant `policy` behind it, and `uv run wreath-map-lint` fails if it drifts from what is actually here.

## Top level

| Path | Purpose |
| --- | --- |
| `src/wreath/` | Dependency-free Python framework and server package, plus optional C accelerators. |
| `tests/` | Framework, protocol, native-parity, ORM, and PostgreSQL tests. |
| `benchmarks/` | Reproducible framework/server/ORM benchmarks, competitor apps, load tooling, and reports. |
| `docs/` | User guides, API reference, internals, plans, and agent guidance. |
| `tools/` | Native checks and sanitizer build helpers. |
| `example/` | The canonical camera-trap application built on wreath. Not shipped to users who install the package. |
| `setup.py` | Optional C-extension build definitions and feature detection. |
| `pyproject.toml` | Package metadata, dependency groups, test markers, lint/type configuration, and CLI entry points. |
| `wreath_docs.py` | Documentation site structure and theme, built by `wreath docs`. |
| `README.md` | Public project overview and quick start. |
| `AGENTS.md` | Repository-wide engineering, testing, documentation, and benchmark rules. |
| `CLAUDE.md` | Pointer file so a coding agent loads those rules without being told to. |

Generated or local-only directories such as `build/`, `site/`, `.venv/`, caches, `.sanitizers/`, and the root `benchmark-results`/`benchmark-diagnosis` trees are artifacts rather than source-of-truth code.

## Framework package: `src/wreath/`

**The public API is literal**: each feature lives in the module its name implies,
so `wreath.pagination` is `src/wreath/pagination.py` and `wreath.jobs` is
`src/wreath/jobs.py`. Guess first; the guess is usually right. A leading
underscore means implementation — reach it through the facade that exports it,
not directly.

The table below is the shape of the package. For a specific subsystem's tests,
invariants, and design decisions, look it up in `docs/agents/manifest.json`
rather than reading here.

### Main Python surfaces

| Area | Primary paths | Notes |
| --- | --- | --- |
| Application/lifecycle | `app.py`, `router.py`, `state.py`, `exceptions.py` | `Wreath` registration, startup compilation, ASGI dispatch, lifespan, state, and RFC 9457 error handling. |
| Routing | `_routing.py` | Route declaration lives on the app and `Router`; `_native/policy_router.c` is the sole matcher and capability-classification engine. |
| Requests and responses | `request.py`, `response.py`, `background.py`, `_http.py`, `_headers.py`, `_codecs.py`, `_json.py`, `_multipart.py` | Inbound HTTP objects, every response type including SSE and streaming, and response-bound background work. |
| Outbound HTTP | `http_client.py`, `webhooks.py`, `_client_codec.py` | Managed pooling with native codecs, and signed webhook delivery with durable inbox/outbox contracts. |
| Model Context Protocol | `mcp.py`, `_mcp/` | A first-party MCP server: declared callables served to a model as tools over streamable HTTP. Adds no C — the envelope is `_json.py`, the stream is `SSEResponse`, and a call is an ordinary route activation. Tool schemas are derived by `binding.py` and rendered by `openapi.py`, which is the parity that keeps them from drifting. |
| Binding/OpenAPI | `binding.py`, `openapi.py`, `_native/validate.c` | Handler parameter resolution, dependency markers, native body validation (a plan interpreter, with `binding.validate` for the shapes a flat plan cannot express), schema generation, and opt-in docs endpoints. |
| Type generation | `typegen/` | Canonical IR from routes/binding, TypeScript + fetch + React Query targets, `wreath typegen` CLI. |
| Middleware | `middleware/`, `compression.py`, `_native/gzip/`, `cache_control.py`, `_webpolicy.py` | Base pipeline plus CORS, CSRF, sessions, security headers, rate limiting, request IDs, timing, proxy headers, cache, and native format-aware compression. |
| Auth | `auth.py`, `authorization.py`, `_auth/` | Authentication (identity) and authorization (roles, permissions, the built-in Cedar engine), kept firmly apart. |
| Users | `users.py`, `_userkit.py` | Registration, sessions, and the ready-made user router. |
| Organisations and provisioning | `organizations.py`, `_scim/` | Tenancy at the identity layer — organisations, memberships, roles within them, invitations — plus SCIM 2.0 (`scim_router`) as an **adapter** onto exactly those stores: a SCIM group is a declared role, a SCIM user is a `wreath.users` record, and every route is authorized by the application's own Cedar policies. No second membership model and no second authorization path. |
| Data | `postgres.py`, `orm/`, `migrations.py`, `_locks.py` | The PostgreSQL driver, the ORM on top of it (including dataclass projections), the migration stack, and advisory locks reached through the `postgres` facade. |
| Durable work | `jobs.py`, `_jobcore.py`, `messaging.py`, `services.py` | PostgreSQL-backed job runner, message bus, and the supervisor owning their process-lifetime tasks. |
| Durable delivery | `log.py`, `audit_log.py`, `streams.py` | One append-only log read back from a cursor that cannot skip, the audit trail the ORM writes into it, and resumable streams: a producer runs as a durable job, its chunks go to that log, and a reconnecting client attaches from its `Last-Event-ID` rather than re-invoking the producer. |
| Application services | `storage.py`, `pagination.py`, `provenance.py`, `cache.py`, `response_cache.py`, `sync.py`, `templates.py`, `staticfiles.py`, `config.py`, `testing.py` | Object storage, stable cursor pagination, stored-artifact provenance, the read-mostly snapshot cache, response caching with the surrogate keys and CDN purge derived from the same declaration, query subscriptions (a bounded principal-derived shape re-evaluated on each committed write, so a row leaving it is an ordinary tombstone), safe HTML templates, static files, configuration, and the in-process test client. |
| Operations | `health.py`, `flags.py`, `versioning.py` | Health probes, feature flags, and API versioning. |
| Observability | `telemetry.py`, `recording.py`, `replay.py`, `simulation.py`, `inspector.py` | The Native Flight Recorder surfaces: telemetry configuration and the OpenTelemetry bridge, recording policy types, replay and fault injection, interactive socket-free transport simulation, and the read-only local inspector. Crash forensics lives here too — `TelemetryConfig.ring_path` maps the ring from a file so a process that dies badly leaves its records readable, and `recording.read_ring_file` (or `wreath flight read`) decodes them. Exporters are the `_otlp.py`/`_prometheus.py`/`_statsd.py`/`_cloudwatch_emf.py` group. |
| Server/protocols | `server.py`, `cli.py`, `_cli.py`, `_devserver.py`, `websocket.py`, `reactor.py` | Server configuration, CLI application loading/reload supervision, transport selection, lifespan, the WebSocket API, and the metal tier's native event loop. |
| Auditing and porting | `_audit/`, `port.py`, `_port/` | The `wreath audit` accessibility/performance ruleset, and the `wreath port` codemod that analyzes a FastAPI app without importing it. |
| Quality tools | `_test_runner.py`, `_native_test_runner.py`, `_pytest_facade.py`, `_native/_testrunnermodule.c`, `_devtools/dup_scan.py`, `_native/_dupscanmodule.c`, `mutant.py`, `_mutant/` | `wreath test` defaults to a scoped pytest facade, one compiled collection, history-balanced process shards, and stateless native C vectorcall loops; pytest and dual remain explicit oracle modes. Unsupported semantic hooks fail closed. `wreath-dup-scan` groups whole-body structures and optionally runs an operation-local native token-window pass for copied interiors, with alpha-precision normalization, evidence, coverage and hotspot summaries. `wreath mutant` removes one *declared* control at a time and re-runs only tests PEP 669 attribution says reach it; native workers seal that baseline during the ordinary run and forked mutant children inherit the candidate image. Both are reports, not gates. |

### Backend split

- `src/wreath/_native/` contains CPython C accelerators. Base-wheel module entry files include `_coremodule.c`, `_clientmodule.c`, `_docsmodule.c`, `_dupscanmodule.c`, `_edgemodule.c`, `_servermodule.c`, `_postgresmodule.c`, and `_testrunnermodule.c`; optional wheels add reactor, Flight, and HTTP/3 extensions.
- `src/wreath/_native/postgres/` owns native PostgreSQL protocol, buffering, decoding, codecs, model storage/hydration, and related plans/records.
- `src/wreath/_devtools/` contains native complexity, boundary, GIL, memory, and error linters, profiling support, and `request_trace.py` (`wreath-request-trace`), which counts the Python/native boundary crossings of one request against the realistic app in `sample_app.py` and diffs them against `docs/agents/request-boundary-baseline.json`. `tape_decomp.py` (`wreath-tape-decomp`) prices that same tape, reporting a measured noise floor and refusing to attribute deltas below it. `decomp.py` (`wreath-decomp`) prices the rest of the request -- lifecycle stages, one ORM read, and ns-per-frame/ns-per-await calibrations -- over the shared harness in `measure.py`, which documents the measurement rules. `map_lint.py` (`wreath-map-lint`) checks that this file, `AGENTS.md`, `docs/llms.txt`, and the manifest still describe the repository. `tasks.py` provides `wreath-check`/`wreath-docs`/`wreath-bench`, which install their own dependency group with `uv sync --inexact` so one job never uninstalls another's.

When changing an accelerated feature, keep its public facade, its `_native` implementation and its tests in step. Keep framework and server layers separable.

## Tests

Every subsystem's focused tests are listed in `docs/agents/manifest.json`; look
there before grepping. The broad shape:

- Root `tests/test_*.py` files cover application behavior, binding, routing, request/response handling, middleware/auth/security, server behavior, the Flight Recorder, native parity/lints, and benchmark contracts.
- Subsystem packages hold their own: `tests/orm/`, `tests/postgres/`, `tests/migrations/`, `tests/jobs/`, `tests/messaging/`, `tests/storage/`, `tests/pagination/`, `tests/audit/`, `tests/health/`, `tests/flags/`, `tests/versioning/`, `tests/port/`, `tests/reactor/`, `tests/typegen/`.
- `tests/http2/` and `tests/http3/` cover frames, HPACK, flow control, connection and stream state, limits/timeouts, ASGI behavior, networking, and shutdown.
- `tests/fixtures/` holds reusable test data; `tests/_routing_impls.py` and `tests/_server_ingest.py` provide cross-backend test helpers.

Prefer a focused test near the changed subsystem. The canonical commands and marker guidance remain in `AGENTS.md`.

## Benchmarks and native tooling

- `benchmarks/run.py`, `benchmarks/apps.py`, and `benchmarks/scenarios.py` drive framework comparisons.
- `benchmarks/load.py`, `benchmarks/lifecycle.py`, and `benchmarks/report.py` provide load generation, lifecycle measurement, and reporting.
- `benchmarks/postgres/` contains PostgreSQL and ORM microbenchmarks/workloads.
- `tools/sanitizers/` builds isolated server, PostgreSQL, and HTTP/3 sanitizer variants.
- Keep raw benchmark results and environment metadata; follow `AGENTS.md` before making performance claims.

## Documentation routing

| Need | Start here |
| --- | --- |
| Agent workflow and subsystem lookup | `docs/cookbook/agents/index.md`, `docs/agents/manifest.json` |
| Behavioral invariants | `AGENTS.md`, per-subsystem `policy` fields in `docs/agents/manifest.json` |
| Change playbooks | `docs/cookbook/agents/` (add-an-endpoint, verify-a-change, checks, documenting-a-module) |
| User-facing behavior | `docs/guides/`, `docs/getting-started/`, `docs/cookbook/recipes/` |
| Public API | `docs/reference/` |
| Coming from the FastAPI stack | `docs/from-fastapi/` |
| What is deliberately not shipped yet | `docs/reference/roadmap.md` |
| Native implementation details | `docs/plans/` (the `native-*` designs), `docs/agents/python-complexity-audit.md` |
| Why the code is shaped the way it is | [`AGENTS.md`](AGENTS.md), and the module docstring of whatever you are reading |
| Active or historical design work | `docs/plans/` |
| Compact LLM documentation index | `docs/llms.txt` |

Update the relevant guide, reference page, and `docs/agents/manifest.json` whenever public behavior or subsystem routing changes; `uv run wreath-map-lint` enforces the manifest half of that.
