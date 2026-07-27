# First-class CLI and development server plan

## Status

In progress. Phases 1 and 2 provide the dependency-free `neo run` command, application loader, configuration mapping, `python -m neo`, and the polling `neo dev` reload supervisor. Stress hardening and the separately designed worker manager remain. This work does not change request routing, middleware, validation, PostgreSQL, or ORM behavior.

## Product position

Neo should be straightforward to run without requiring an application-owned `asyncio.run()` wrapper or an unrelated ASGI server:

```console
neo dev myservice.app:app
neo run myservice.app:app --host 0.0.0.0 --port 8000
python -m neo dev myservice.app:app
```

`neo dev` is an explicitly development-only, reload-enabled supervisor. `neo run` is a single-process foreground server with no implicit reload and no claim of production readiness beyond the underlying documented server guarantees.

The CLI remains dependency-free. Pydantic is permitted only in tests and benchmarks, not in Neo runtime code or public integration surfaces. SQLAlchemy integration is out of scope because Neo ships its own PostgreSQL driver and ORM.

## Existing foundation

The implementation should reuse rather than duplicate these pieces:

- `src/neo/server.py` already provides validated `ServerConfig` and `TLSConfig` values.
- `serve()` starts a server inside an existing event loop.
- `run()` owns an event loop, installs `SIGINT`/`SIGTERM` handlers, and performs graceful shutdown.
- `Server.close()` stops accepting work, drains active requests up to `shutdown_timeout`, closes protocols, and runs lifespan shutdown.
- `benchmarks/neo_server.py` already demonstrates `module:attribute` loading, common host/port/protocol/TLS arguments, and explicit asyncio/uvloop selection. It is benchmark tooling, not a reusable public CLI.
- `pyproject.toml` exposes maintenance commands but no `neo` application command.

## Goals

1. Provide stable `neo dev` and `neo run` commands plus equivalent `python -m neo` invocation.
2. Load an ASGI application or explicit application factory from a deterministic import target.
3. Expose the server settings most users need without duplicating protocol implementation.
4. Provide reliable, debounced source reload using only the standard library.
5. Preserve lifespan and graceful-shutdown semantics across normal exits and reload generations.
6. Produce concise startup diagnostics and actionable errors with stable nonzero exit codes.
7. Keep CLI, loader, watcher, and process supervision independently testable.

## Non-goals

- Middleware, trusted-proxy behavior, rate limiting, request IDs, or observability policy. Another workstream owns middleware.
- Pydantic runtime integration or a Pydantic compatibility layer.
- SQLAlchemy integration or support for replacing Neo's driver/ORM.
- A package/project generator in the first release.
- Shell completion, interactive dashboards, or a plugin-command system.
- Implicit `.env` loading or a new settings format.
- A production process manager in the initial release.
- Zero-downtime reload. The first implementation shuts down the old child cleanly before binding the new child.

## Command surface

### `neo dev`

```console
neo dev MODULE[:ATTRIBUTE] [options]
```

Defaults:

- attribute: `app`
- host: `127.0.0.1`
- port: `8000`
- protocols: `http/1.1`
- lifespan: `auto`
- loop: `asyncio`
- reload: enabled
- reload root: the resolved top-level package/project directory

Development-only options:

- `--factory`: treat the selected attribute as a zero-argument application factory.
- `--reload-dir PATH`: repeatable additional/override watch roots.
- `--reload-include GLOB`: repeatable inclusion pattern; default `*.py`.
- `--reload-exclude GLOB`: repeatable exclusion pattern.
- `--reload-delay SECONDS`: polling interval, default approximately `0.25` seconds.
- `--reload-debounce SECONDS`: quiet period before restart, default approximately `0.10` seconds.

Reload should remain mandatory for `dev`; users wanting a non-reloading foreground process use `neo run`. This keeps accidental deployment modes obvious.

### `neo run`

```console
neo run MODULE[:ATTRIBUTE] [options]
```

Shared options:

- `--factory`
- `--host`, `--port`, `--backlog`
- `--protocol {http/1.1,h2,h3}`, repeatable while preserving order
- `--lifespan {auto,on,off}`
- `--loop {asyncio,uvloop}` with `asyncio` as the deterministic default
- `--tls-cert`, `--tls-key`, and optional `--tls-password-file`
- request, header, body, buffering, keep-alive, request, and shutdown limits corresponding directly to `ServerConfig`

TLS certificate and key must be supplied together. Password input must not be accepted directly on the command line because process listings expose arguments. Selecting uvloop without it installed should produce a short installation/action message rather than an import traceback. uvloop remains optional and is never auto-selected.

`--version` and top-level/subcommand `--help` must work without importing the target application or native extensions.

## Application loading contract

Create a small public-neutral loader, initially internal to the CLI:

```python
load_application("myservice.app:app", factory=False) -> ASGIApplication
```

Rules:

1. Targets use importable module names, not filesystem paths. This avoids ambiguous `sys.path` mutation.
2. A missing attribute defaults to `app`.
3. Empty modules/attributes, relative modules, nested `:` separators, and malformed targets fail before server startup.
4. Without `--factory`, the attribute is used as the ASGI callable and is never invoked during loading.
5. With `--factory`, the attribute must be callable and is invoked exactly once in the worker.
6. The first release supports synchronous zero-argument factories. Async factories can be added later only with an explicit lifecycle contract.
7. Import and factory failures retain exception chaining in verbose/debug output, while default output identifies the target and root cause concisely.
8. The reload supervisor never imports application code. Every generation imports in a fresh child process, eliminating stale module/global state.

The loader should validate callable shape only to the extent possible without request-time introspection. ASGI conformance remains a runtime responsibility.

## Architecture

### Modules

- `src/neo/cli.py`: argument parser, validation, display, exit-code mapping, and `main()`.
- `src/neo/__main__.py`: delegates to `neo.cli.main()`.
- `src/neo/_devserver.py`: app loading, file snapshots, change detection, debounce state, child command construction, and reload supervision.
- `src/neo/_cli_worker.py` or an equivalent hidden mode: imports the app, builds `ServerConfig`/`TLSConfig`, selects the event-loop runner, and calls the existing serving API.

Add this console script:

```toml
[project.scripts]
neo = "neo.cli:main"
```

Keep CLI modules out of `neo.__init__` so importing `neo` does not import `argparse`, process-management code, or watcher logic.

### Configuration construction

The parser should produce a dedicated immutable CLI options value. A pure conversion function then builds `ServerConfig` and `TLSConfig`. Do not modify `ServerConfig` to understand CLI strings, environment variables, or argparse namespaces.

Parser-level validation should cover relationships such as paired TLS paths and development reload values. `ServerConfig` remains authoritative for protocol and numerical bounds, and its validation errors should be converted into normal CLI usage errors.

### Worker process

Both commands execute application code in the serving process:

- `neo run` may execute the worker path in-process after parsing.
- `neo dev` always launches a fresh subprocess using `sys.executable` and an argv list, never a shell command.
- Child configuration should be serialized as explicit arguments or a bounded internal JSON payload; do not use pickle.
- Environment inheritance should be ordinary subprocess inheritance with only a small namespaced internal marker if needed.

A child must print its listening addresses only after startup/lifespan succeeds. Startup failure exits nonzero. Under reload, the parent stays alive after an import/startup failure and retries only after a detected source change.

### Standard-library reloader

Use a portable polling watcher first. Native filesystem APIs and third-party watchers can be evaluated later from measurements, but are not needed for correctness.

Each scan records normalized path plus `mtime_ns` and size. A change includes file creation, deletion, or metadata change. Watch only regular files matching includes. Symlink traversal is off by default to avoid cycles and watching outside explicit roots.

Default exclusions should include:

- `.git`, `.hg`, `.svn`, `.venv`, `venv`
- `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`
- `build`, `dist`, `site`, `.sanitizers`
- Neo benchmark result/diagnosis artifact trees

Operational behavior:

1. Build an initial snapshot before launching the first child.
2. Launch one child generation.
3. Poll while also checking child status.
4. Coalesce bursts until the debounce quiet period expires.
5. Ask the old child to shut down gracefully.
6. Wait no longer than its configured shutdown timeout plus a small supervisor allowance.
7. Force termination only if graceful shutdown fails.
8. Launch exactly one replacement generation and refresh the snapshot.

Only one restart may be pending at a time. Changes that occur during shutdown must be folded into the next generation rather than causing back-to-back restarts.

Reload with `port=0` should be rejected initially because each generation would advertise a different port. Shared/pre-bound socket support can remove this restriction later.

### Signals and exits

- `Ctrl-C`/`SIGTERM` delivered to the supervisor stops reload, requests child shutdown, waits boundedly, and exits once.
- A normal `neo run` interruption exits with status 0 after graceful shutdown.
- CLI syntax/configuration/import failures exit with a stable nonzero status.
- Unexpected supervisor failures exit nonzero and must not leave a child process running.
- Platform-specific signal/process-group code must be isolated behind a small adapter and tested conditionally.

Avoid forwarding one signal through multiple ownership layers. The parent owns terminal signals in development; the child owns them in non-reloading `run` mode.

## Process workers

`neo run --workers N` should not be faked with independently bound listeners or introduced as part of the first CLI increment. A robust implementation needs a deliberate socket-ownership design:

1. Extend the server startup API to accept pre-bound TCP sockets and, separately, UDP/QUIC sockets.
2. Let a parent bind once and pass handles to spawned workers.
3. Define lifespan ownership: once per worker versus a future coordinator lifecycle.
4. Specify worker readiness, crash backoff, termination, logging, and platform behavior.
5. Verify fair distribution, graceful replacement, HTTP/2 connection ownership, HTTP/3 constraints, and database pool sizing.

Until that exists, recommend an external process supervisor for multiple single-process instances. This is preferable to presenting `SO_REUSEPORT` as a portable worker manager.

## Delivery phases

### Phase 1: loader and non-reloading CLI

- Add `neo`, `python -m neo`, `neo run`, help, and version output.
- Implement target/factory loading and clear error handling.
- Map common server, protocol, loop, TLS, lifespan, timeout, and resource-limit options.
- Reuse `serve()`/`run()`; do not change protocol implementations.
- Document the command without claiming production readiness.

### Phase 2: development supervisor

- Add `neo dev` with fresh-process imports.
- Implement bounded polling, exclusions, include patterns, debounce, and graceful replacement.
- Keep the supervisor alive through app import/startup errors.
- Verify cleanup and signal behavior with subprocess integration tests.

### Phase 3: ergonomics and hardening

- Improve diagnostics, TTY-aware concise output, and listening-address reporting.
- Add explicit reload roots/pattern docs and common monorepo examples.
- Run repeated reload, file-churn, startup-failure, WebSocket, streaming, and lifespan stress tests.
- Profile watcher CPU and filesystem work before optimizing or adding a native backend.

### Phase 4: separately designed worker manager

Proceed only after pre-bound socket injection and lifecycle ownership are specified and tested. This phase is not required to call the development server first-class.

## Test plan

Add focused tests rather than embedding CLI behavior in protocol tests:

- `tests/test_cli.py`
  - help/version do not import app code;
  - parsing and defaults for `dev` and `run`;
  - malformed targets, missing modules/attributes, factory misuse;
  - TLS pairing, uvloop absence, protocols, limits, and exit codes;
  - `ServerConfig` conversion parity.
- `tests/test_devserver.py`
  - snapshot detects create/change/delete;
  - includes, excludes, symlinks, and multiple roots;
  - burst changes produce one restart;
  - changes during shutdown do not produce duplicate restarts;
  - dead child and failed import remain recoverable;
  - shutdown escalation is bounded.
- `tests/fixtures/cli_apps/`
  - plain app, sync factory, import failure, startup failure, lifespan counter, and slow shutdown fixtures.
- Mark real subprocess/socket tests appropriately so the default suite remains fast and serial.
- Run at least one end-to-end test with `NEO_PURE=1`; native server tests need only prove the CLI selects the same public server facade.
- Assert `src/neo/cli.py`, `_devserver.py`, and worker code import no third-party packages.

Verification for implementation phases:

```console
uv run pytest tests/test_cli.py tests/test_devserver.py tests/test_server.py
uv run ruff check src/neo/cli.py src/neo/__main__.py src/neo/_devserver.py tests/test_cli.py tests/test_devserver.py
uv run ty check
uv run --group docs mkdocs build --strict
```

Run the full default suite after integration. Exercise full network/subprocess marks before stabilizing reload behavior.

## Documentation updates

Implementation should update together:

- `README.md`: replace the hand-written `run()` wrapper as the primary quick-start path while retaining the programmatic API.
- `docs/getting-started/index.md`: introduce `neo dev`.
- `docs/getting-started/deployment.md`: document `neo run`, its single-process scope, and external supervision.
- `docs/guides/server.md` and `docs/reference/runtime.md`: complete option mapping and programmatic-versus-CLI ownership.
- `docs/agents/manifest.json` and `repo-map.md`: route CLI/dev-server changes to their source and focused tests.
- `docs/llms.txt`: add CLI entry points if its compact map format requires them.

## Acceptance criteria

The first-class CLI/dev-server feature is complete when:

1. A clean installation exposes both `neo` and `python -m neo` with equivalent behavior.
2. `neo run package.module:app` serves the app and shuts down gracefully on interruption.
3. `neo dev package.module:app` reloads exactly once for a burst of matching file changes.
4. Every reload generation imports application code in a fresh process and runs lifespan startup/shutdown once.
5. Import or startup errors are actionable; the dev supervisor recovers after a source edit.
6. Common `ServerConfig`, TLS, protocol, loop, and resource-limit settings are expressible and validated.
7. CLI help/version paths have no application or native-extension import side effects.
8. The implementation introduces no mandatory dependency and no Pydantic or SQLAlchemy runtime integration.
9. Focused CLI/reloader tests, the server suite, lint, types, native lint, and strict docs build pass.
10. Documentation labels reload as development-only and accurately describes the single-process boundary of `neo run`.
