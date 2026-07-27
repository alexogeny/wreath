# Response-bound background tasking plan

Status: ready for test-first implementation

Related material:

- `AGENTS.md`
- `repo-map.md`
- `docs/agents/manifest.json`
- `docs/concepts/request-lifecycle.md`
- `docs/guides/requests-responses.md`
- `docs/reference/http.md`
- `docs/internals/performance.md`
- `benchmarks/README.md`

## Goal

Turn Neo's existing single asynchronous response callback into a small, explicit background-task API that supports argument binding, synchronous and asynchronous callables, and ordered task groups. Background work must start only after the complete response has been emitted, remain part of the ASGI application invocation, and preserve the existing raw callback contract. Establish a retained baseline before implementation and benchmark both client-visible response performance and actual task completion without claiming a win below measured noise.

## Repository constraints

- Target CPython 3.14 and keep `src/neo` free of mandatory third-party runtime dependencies.
- Preserve ASGI response ordering and the existing pure-ASGI, `neo.response`, `HEAD`, streaming, and file-response paths.
- Keep ownership explicit: background tasks belong to a response. Do not add a hidden global queue or detached `asyncio.create_task()` calls.
- Keep the no-background hot path at its current shape: read `response.background`, branch on `None`, and do no task allocation or callable inspection.
- Measure the current callback before replacing or wrapping it. Retain repeated raw trials and environment metadata.
- Treat response-bound tasks as best-effort process-local work, not a durable job queue. Process termination may interrupt them.
- Preserve framework/server separation. The task API must behave the same on any conforming ASGI server.

## Existing implementation seam

Neo already provides the lifecycle hook that this work should extend:

- `src/neo/response.py` stores an optional zero-argument asynchronous `background` callback on `Response`, `StreamingResponse`, and `FileResponse`.
- `src/neo/app.py:649-651` invokes that callback after the normal, `HEAD`, streaming/file, or native one-shot response path has completed.
- `docs/concepts/request-lifecycle.md`, `docs/guides/requests-responses.md`, and `docs/reference/http.md` already describe this callback as post-emission, non-durable application work.
- Convenience classes including `TextResponse`, `JSONResponse`, `HTMLResponse`, `ProblemResponse`, and `RedirectResponse` currently do not expose the inherited `background` parameter in their constructors.
- There are no focused correctness tests or retained benchmarks for the callback.

The implementation should preserve `_finish_http()` as the single execution point. A parallel application-level scheduler would duplicate lifecycle semantics and make shutdown ownership ambiguous.

## Public contract

Add `src/neo/background.py` with two public primitives:

```python
from collections.abc import Awaitable, Callable
from typing import Any

class BackgroundTask:
    def __init__(
        self,
        function: Callable[..., Awaitable[None] | None],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def __call__(self) -> None: ...


class BackgroundTasks:
    def __init__(self, tasks: list[BackgroundTask] | None = None) -> None: ...

    def add_task(
        self,
        function: Callable[..., Awaitable[None] | None],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def __call__(self) -> None: ...
```

Export both names from `src/neo/__init__.py`.

Response constructors should accept a common internal type equivalent to:

```python
Background = Callable[[], Awaitable[None]]
```

`BackgroundTask` and `BackgroundTasks` satisfy that contract, so `src/neo/app.py` does not need to distinguish task types. Existing zero-argument async callbacks remain valid.

Expected usage:

```python
task = BackgroundTask(send_receipt, order_id, recipient=email)
return JSONResponse({"accepted": True}, background=task)
```

```python
tasks = BackgroundTasks()
tasks.add_task(write_audit_record, event)
tasks.add_task(send_receipt, order_id, recipient=email)
return Response(b"accepted", status=202, background=tasks)
```

Do not add request injection, decorators, retries, priorities, scheduling timestamps, or a task result API in this change. Those imply a different application-owned worker subsystem.

## Runtime behavior

### Callable execution

`BackgroundTask` should classify the callable once during construction rather than introspecting it on every invocation.

- Native async functions and async callable objects run directly on the event loop and are awaited.
- Synchronous functions run through `asyncio.to_thread()` so post-response work cannot block the event loop.
- If a callable classified as synchronous returns an awaitable, await that returned value after the thread call. This keeps decorated functions and unusual callable objects correct without executing potentially blocking user code on the event loop.
- Do not catch `BaseException`. Cancellation and process shutdown must retain normal asyncio semantics.

Keep the classifier private and covered by tests for functions, bound methods, `functools.partial`, and objects with async `__call__` where those forms are supported.

### Ordered groups and failures

`BackgroundTasks` executes tasks sequentially in insertion order. If one task raises, propagate that exception and do not run later tasks. This matches the current callback's server-visible failure behavior and avoids inventing aggregation semantics.

A task group is configured before returning the response. Mutation while execution is in progress is unsupported; document that it is not a concurrent queue.

### Response lifecycle

The lifecycle rules are non-negotiable:

1. The handler returns a response.
2. Applicable global after-hooks finish and may replace the response.
3. Neo emits the complete response, including the terminal streaming body frame.
4. Neo awaits the response's background callback or task group.
5. The ASGI application invocation returns.

Consequences:

- A client may receive a committed response before a background failure becomes visible to the server.
- A background failure must not be passed to an HTTP exception handler, because a second response cannot be emitted.
- If response emission raises, background work does not start.
- Client or server cancellation propagates normally; Neo does not shield background work.
- `HEAD` runs background work only after the body-suppressing send wrapper completes.
- The `neo.response` extension runs background work only after its one-shot `send()` returns.
- Streaming and file responses run background work only after completion, not after response headers.

### Request-scoped resources

Background work must not extend the lifetime of borrowed request dependencies implicitly. Existing binder cleanup remains authoritative. In particular, database connections borrowed for a handler or stream must be returned before background work attempts to run.

Documentation and tests should direct users to pass durable values such as identifiers, serialized payloads, or application-owned pools into a task rather than retaining a request-borrowed connection. If a task needs a database connection, it opens or borrows one under its own explicit lifetime.

## Implementation work

### Establish red tests and the current baseline

- [ ] Add failing public-API and lifecycle tests before adding the task classes.
- [ ] Add `benchmarks/bench_background_tasks.py` with the current raw callback and no-background controls.
- [ ] Run repeated baseline trials and retain them under `benchmark-results-background/baseline/` with Python version, platform, event-loop policy, iteration count, warmup, trial count, and raw sample values.
- [ ] Record an A/A control so deltas below the run's noise floor are reported as unresolved rather than zero.

### Add the task primitives

- [ ] Create `src/neo/background.py` with `BackgroundTask`, `BackgroundTasks`, the cached callable classifier, and no third-party dependencies.
- [ ] Keep task instances lightweight and slotted; avoid per-call wrapper closures.
- [ ] Export `BackgroundTask` and `BackgroundTasks` from `src/neo/__init__.py` and add them to `__all__`.
- [ ] Preserve direct assignment of an existing zero-argument async callback to `response.background`.

### Expose tasks consistently on responses

- [ ] Update the background annotations on `Response`, `StreamingResponse`, and `FileResponse` to use the shared internal contract.
- [ ] Add keyword-only `background=` parameters to `TextResponse`, `JSONResponse`, `HTMLResponse`, `ProblemResponse`, and `RedirectResponse`, forwarding them to `Response` without changing existing positional argument behavior.
- [ ] Keep `Response`, `StreamingResponse`, and `FileResponse` constructor compatibility intact.
- [ ] Leave `_finish_http()` as the sole task execution point; only adjust typing or a narrowly justified helper there.

### Prove lifecycle behavior

Add focused coverage in `tests/test_background.py` and integration coverage near the existing response paths:

- [ ] Async and synchronous callables receive positional and keyword arguments.
- [ ] Sync work is executed off the event-loop thread.
- [ ] Async functions, partials, bound methods, and supported async callable objects are classified correctly.
- [ ] A sync callable returning an awaitable is fully awaited.
- [ ] A task group runs sequentially in insertion order.
- [ ] The first failure stops the group and propagates from the ASGI invocation.
- [ ] Existing raw async callbacks still run.
- [ ] The response's final body message is observed before task execution starts.
- [ ] Failed response emission prevents task execution.
- [ ] `HEAD`, `neo.response`, streaming, and file-response paths preserve post-emission ordering.
- [ ] Cancellation is not swallowed or shielded.
- [ ] Borrowed handler and streaming database resources are released before background execution.

Use existing test helpers and native-extension feature guards rather than creating a second ASGI harness.

## Benchmark design

### Focused in-process benchmark

`benchmarks/bench_background_tasks.py` should use the shared measurement rules from `src/neo/_devtools/measure.py` where practical and produce machine-readable JSON. Measure the complete ASGI invocation, not only task-object construction.

Include these cases:

| Case | What it isolates |
| --- | --- |
| no background | Existing hot-path control |
| raw async no-op callback | Current implementation baseline |
| one `BackgroundTask`, async no-op | Wrapper and dispatch overhead |
| one async task that yields once | Event-loop scheduling cost |
| groups of 1, 4, and 16 async no-op tasks | Sequential scaling |
| one synchronous no-op task | Thread-offload floor |
| one synchronous fixed-work task | Offload with useful work |
| streaming response plus one task | Terminal-body integration |
| native one-shot response plus one task, when available | `neo.response` integration |

For each case record warmups, repeated raw trials, median, p95, task completions, errors, and the A/A noise floor. Report per-request and per-task values separately. Do not subtract loop overhead unless the subtraction clears the measured noise threshold.

### End-to-end benchmark proof point

Extend the existing comparison harness rather than creating a parallel load generator:

- `benchmarks/apps.py`: add equivalent Neo and Starlette routes using their native response-bound background APIs.
- `benchmarks/scenarios.py`: add `background-noop` and `background-yield` scenarios initially, limited to `neo`, `neo-native`, and `starlette`.
- `benchmarks/run.py`: after each measured background scenario, query a small unmeasured statistics endpoint before stopping the server and record started, completed, failed, and in-flight counts.
- `benchmarks/README.md`: document the routes, verification, commands, and interpretation.

Each application should maintain explicit process-local counters owned by the benchmark app. The measured task increments `started`, performs identical work, then increments `completed`; failures increment `failed`. The statistics endpoint must not participate in the timed request samples.

The harness must wait for in-flight work to drain up to a recorded bound after load stops. A result is invalid if completed tasks do not equal successful measured requests, if failures are nonzero, or if work remains in flight at shutdown. This prevents a framework from appearing faster by dropping work or accumulating an unobserved backlog.

Report both:

- client-visible throughput, median, p95, p99, errors, and RSS;
- completed-task throughput, maximum observed in-flight work, and post-load drain duration.

Run Neo on the same ASGI server and event-loop configuration as Starlette, then measure `neo-native` separately. Use identical response bodies and task work. Retain repeated raw results under `benchmark-results-background/`; do not make a performance claim from the development load generator alone.

### Regression gates

- The no-background control must remain within the measured A/A noise floor unless a regression is explicitly investigated and accepted.
- The task wrapper's incremental cost must be reported against the existing raw callback, not only against a response with no task.
- Task groups should scale approximately linearly with task count; investigate superlinear behavior before acceptance.
- Sync task results must be labeled as thread-offload measurements and must not be compared as equivalent to async no-op work.
- Any claimed framework comparison must include task-completion verification and identical server/loop settings.

## Documentation changes

Update the existing documentation rather than adding a parallel guide:

- `docs/guides/requests-responses.md`: show `BackgroundTask` and `BackgroundTasks`, argument binding, and sync/async examples.
- `docs/concepts/request-lifecycle.md`: specify ordering, cancellation, failure-after-commit behavior, and request-resource lifetime.
- `docs/reference/http.md`: document constructors, accepted callables, ordered groups, and exports.
- `repo-map.md`: add `background.py`, `tests/test_background.py`, and `benchmarks/bench_background_tasks.py` to the response/lifecycle and benchmark routing entries.
- `docs/agents/manifest.json`: route the new source, test, and benchmark files through the existing HTTP/response and performance subsystems.
- `benchmarks/README.md`: document focused and end-to-end commands plus result validity rules.

Keep the warning prominent: response-bound tasks are non-durable, run in the web process, and may be interrupted by shutdown. Recommend an external durable queue for work that must survive process loss, without adding such a dependency to Neo.

## Likely files touched

```text
src/neo/background.py
src/neo/response.py
src/neo/app.py
src/neo/__init__.py
tests/test_background.py
tests/test_app.py
tests/test_response.py
tests/postgres/test_app_integration.py
benchmarks/bench_background_tasks.py
benchmarks/apps.py
benchmarks/scenarios.py
benchmarks/run.py
benchmarks/README.md
docs/guides/requests-responses.md
docs/concepts/request-lifecycle.md
docs/reference/http.md
docs/agents/manifest.json
repo-map.md
```

## Out of scope

- Durable queues, retries, persistence, scheduling, priorities, and distributed workers.
- Detached tasks that outlive the ASGI application invocation.
- Application-level task registries or hidden global state.
- Concurrent task-group execution or exception aggregation.
- WebSocket background-task APIs.
- Passing request-scoped database connections into post-response work.
- Native/C task execution; Python user callables remain a Python-boundary operation after response emission.

## Acceptance checks

- `from neo import BackgroundTask, BackgroundTasks` works and is documented.
- A response can run one sync or async callable with bound arguments after its final ASGI response message.
- `BackgroundTasks` runs tasks sequentially in insertion order and stops on the first failure.
- Existing zero-argument async callbacks remain source- and behavior-compatible.
- Convenience response classes expose keyword-only `background=` without changing existing positional calls.
- `HEAD`, streaming, file, portable ASGI, and `neo.response` paths all run tasks after successful emission and not after failed emission.
- Background exceptions propagate as server-visible application errors without attempting a second HTTP response.
- Cancellation remains observable and is not shielded.
- Request-borrowed database resources are released before task execution, with a regression test proving the ordering.
- Focused benchmark artifacts contain repeated raw trials, completion counts, environment metadata, and an A/A noise floor.
- End-to-end background results are rejected unless completed tasks equal successful measured requests and the backlog drains.
- The no-background path shows no attributable regression beyond measured noise, or any accepted regression is documented with evidence.
- Focused tests, the default suite, Ruff, ty, native lints/request-boundary check, and strict documentation build pass.
