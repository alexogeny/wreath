# Repository map

A quick routing guide for Wreath contributors and coding agents. Start with `AGENTS.md` for repository rules, then use `docs/agents/manifest.json` for the machine-readable subsystem map and focused test locations.

## Top level

| Path | Purpose |
| --- | --- |
| `src/wreath/` | Dependency-free Python framework and server package, plus optional C accelerators. |
| `tests/` | Framework, protocol, native-parity, ORM, and PostgreSQL tests. |
| `benchmarks/` | Reproducible framework/server/ORM benchmarks, competitor apps, load tooling, and reports. |
| `docs/` | User guides, API reference, internals, ADRs, plans, and agent guidance. |
| `tools/` | Native checks and sanitizer build helpers. |
| `setup.py` | Optional C-extension build definitions and feature detection. |
| `pyproject.toml` | Package metadata, dependency groups, test markers, lint/type configuration, and CLI entry points. |
| `mkdocs.yml` | Documentation site structure and configuration. |
| `README.md` | Public project overview and quick start. |
| `AGENTS.md` | Repository-wide engineering, testing, documentation, and benchmark rules. |

Generated or local-only directories such as `build/`, `site/`, `.venv/`, caches, `.sanitizers/`, and the root `benchmark-results*`/`benchmark-diagnosis*` trees are artifacts rather than source-of-truth code.

## Framework package: `src/wreath/`

### Main Python surfaces

| Area | Primary paths | Notes |
| --- | --- | --- |
| Application/lifecycle | `app.py`, `state.py`, `exceptions.py` | `Wreath` registration, startup compilation, ASGI dispatch, lifespan, state, and error handling. |
| Routing | `router.py`, `routing.py` | Public router composition and route declarations; implementation backends live under `_pure/` and `_native/`. |
| HTTP and webhooks | `request.py`, `response.py`, `background.py`, `http.py`, `http_client.py`, `webhooks.py`, `_client_codec.py`, `headers.py`, `codecs.py`, `json.py`, `multipart.py` | Inbound HTTP objects, response emission/background work, managed outbound pooling and native codecs, signed webhook delivery, and durable inbox/outbox contracts. |
| Binding/OpenAPI | `binding.py`, `openapi.py`, `_native/validate.c` | Handler parameter resolution, dependency markers, native body validation (plan interpreter, pure twin in `binding.validate`), schema generation, and docs endpoints. |
| Type generation | `typegen/`, `_pure/typegen.py` | Canonical IR from routes/binding, TypeScript + fetch + React Query targets, `wreath typegen` CLI; pure reference renderer (native gated). |
| Server/protocols | `server.py`, `cli.py`, `_cli.py`, `_devserver.py`, `__main__.py`, `websocket.py`, `ws.py` | Server configuration, CLI application loading/reload supervision, transport selection, lifespan, WebSocket API, and pure/native protocol dispatch. |
| Services | `postgres.py`, `cache.py`, `compression.py`, `config.py`, `staticfiles.py`, `testing.py`, `webpolicy.py` | PostgreSQL facade, configuration, caching/compression, static files, test client, and web policy. |
| Authentication | `auth/` | Backends, identity models, requirements/decorators, and Cedar support. |
| Middleware | `middleware/` | Base pipeline plus CORS, CSRF, sessions, security, cache, and compression middleware. |
| ORM | `orm/`, `_native/orm_shape.c` | Models/fields, expressions, constraints, compiler (native query cache-key `shape_of`, pure twin `_shape_of_pure`), relations, registry, validation, introspection, and request-scoped sessions. |

### Backend split

- `src/wreath/_pure/` contains the Python reference/fallback implementations for routing, codecs, HTTP, server protocols, PostgreSQL, compression, security, and WebSocket behavior.
- `src/wreath/_native/` contains optional CPython C accelerators. Module entry files are `_coremodule.c`, `_clientmodule.c`, `_servermodule.c`, `_postgresmodule.c`, and `_http3module.c`.
- `src/wreath/_native/postgres/` owns native PostgreSQL protocol, buffering, decoding, codecs, model storage/hydration, and related plans/records.
- `src/wreath/_devtools/` contains native complexity, boundary, GIL, memory, and error linters, profiling support, and `request_trace.py` (`wreath-request-trace`), which counts the Python/native boundary crossings of one request against the realistic app in `sample_app.py` and diffs them against `docs/agents/request-boundary-baseline.json`. `tape_decomp.py` (`wreath-tape-decomp`) prices that same tape, reporting a measured noise floor and refusing to attribute deltas below it. `decomp.py` (`wreath-decomp`) prices the rest of the request -- lifecycle stages, one ORM read, and ns-per-frame/ns-per-await calibrations -- over the shared harness in `measure.py`, which documents the measurement rules. `tasks.py` provides `wreath-check`/`wreath-docs`/`wreath-bench`, which install their own dependency group with `uv sync --inexact` so one job never uninstalls another's.

When changing an accelerated feature, preserve parity between its public facade, `_pure` implementation, `_native` implementation, and parity tests. Keep framework and server layers separable.

## Tests

- Root `tests/test_*.py` files cover application behavior, binding, routing, request/response handling, middleware/auth/security, server behavior, native parity/lints, and benchmark contracts.
- `tests/http2/` covers frames, HPACK, flow control, connection state, ASGI behavior, networking, and shutdown.
- `tests/http3/` covers availability, headers/settings, stream state, limits/timeouts, ASGI behavior, networking, and interoperability.
- `tests/postgres/` covers codecs, protocol/connection/pool behavior, receive buffering, pipelines, direct paths, and app/auth integration.
- `tests/orm/` covers declarations, binding, compilation, constraints, validation, sessions, introspection, and pure/native storage/hydration parity.
- `tests/fixtures/` holds reusable test data; `tests/_routing_impls.py` and `tests/_server_ingest.py` provide cross-backend test helpers.

Prefer a focused test near the changed subsystem. The canonical commands and marker guidance remain in `AGENTS.md`.

## Benchmarks and native tooling

- `benchmarks/run.py`, `apps.py`, and `scenarios.py` drive framework comparisons.
- `benchmarks/load.py`, `lifecycle.py`, and `report.py` provide load generation, lifecycle measurement, and reporting.
- `benchmarks/bench_*.py` target native HTTP storage/pressure, request bridging, outbound HTTP, signed webhooks, webhook dispatcher backlog/outcomes, routing, pipelines, web policy, response-bound background tasks, and type generation.
- `benchmarks/postgres/` contains PostgreSQL and ORM microbenchmarks/workloads.
- `tools/sanitizers/` builds isolated server, PostgreSQL, and HTTP/3 sanitizer variants.
- Keep raw benchmark results and environment metadata; follow `AGENTS.md` before making performance claims.

## Documentation routing

| Need | Start here |
| --- | --- |
| Agent workflow and subsystem lookup | `docs/agents/index.md`, `docs/agents/manifest.json` |
| Behavioral invariants | `docs/agents/contracts.md` |
| Change playbooks | `docs/agents/playbooks.md` |
| User-facing behavior | `docs/guides/`, `docs/getting-started/`, `docs/cookbook/` |
| Public API | `docs/reference/` |
| Request/ASGI concepts | `docs/concepts/` |
| Native implementation details | `docs/native/`, `docs/internals/performance.md` |
| Architectural decisions | `docs/decisions/` |
| Active or historical design work | `docs/plans/` |
| Compact LLM documentation index | `docs/llms.txt` |

Update the relevant guide/reference/agent map whenever public behavior or subsystem routing changes.
