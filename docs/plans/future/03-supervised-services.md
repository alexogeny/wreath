# Supervised application services plan

## Status

Future proposal; foundational dependency for jobs, dispatchers, polling, exporters, and managed reconnect loops.

## Objective

Provide explicit application-owned execution for coroutines that outlive one request. Eliminate untracked background tasks and make startup, failure, readiness, cancellation, restart, and shutdown observable and testable.

## Public model

A `Service` has a unique name, asynchronous run callable, restart policy, criticality, dependencies, startup/readiness behavior, and shutdown deadline. `ServiceContext` exposes stopping state, a deterministic clock, bounded child-task creation, readiness reporting, and telemetry.

Illustrative API:

```python
@app.service(
    "dispatcher",
    restart="on_failure",
    critical=True,
    shutdown_timeout=10.0,
)
async def dispatcher(context: ServiceContext) -> None:
    while not context.stopping:
        await dispatch_batch()
        await context.sleep(0.25)
```

Services progress through validated states: registered, starting, running, stopping, stopped, or failed. Restart waiting is metadata on a failed service, not an ambiguous extra state.

## Ownership and failure semantics

- The application owns the root supervisor.
- Each child task has exactly one owning service.
- Dependency order controls startup; reverse order controls shutdown.
- Critical permanent failure makes readiness false and may request application shutdown.
- Restart policy defines mode, attempt window, exponential delay, maximum delay, and bounded jitter.
- Shutdown first requests cooperative stop, then cancels at the deadline.
- `CancelledError` is propagated after cleanup.
- Service failures are retained as structured diagnostics; they are never silently logged and forgotten.

## C and pure split

Python owns `asyncio.Task`, callback invocation, exception propagation, lifecycle, and policy. The optional native runtime may own a compact state table, validated transition function, bounded notification ring, deadline heap, and backoff arithmetic. It never executes user callbacks on a native thread.

`neo._pure.runtime` implements the same state table with enums, `heapq`, and `deque`. Injected clocks and deterministic jitter make parity tests exact.

## Phases

1. Specify states, ownership, restart, readiness, and shutdown contracts.
2. Implement pure supervisor and deterministic test clock.
3. Integrate with Neo lifespan and testing client.
4. Add health snapshots and observability events.
5. Implement native state/deadline primitives only after lifecycle profiling.
6. Add free-threaded and boundary-crossing evaluation.

## Verification

- Startup dependency cycles and partial startup failure.
- Success, synchronous failure, asynchronous failure, and repeated restart.
- Cancellation during startup, work, restart delay, and shutdown.
- Child-task leak detection and reverse-order cleanup.
- Fake-clock tests without wall-clock sleeps.
- Pure/native differential transition sequences.
- Bounded notification and failure-history behavior.

## Completion criteria

No framework-owned long-lived coroutine requires raw untracked `create_task`. Health output identifies every service state and last failure. Application shutdown leaves no service tasks or borrowed resources alive.

## Risks

A supervisor can become an implicit process manager. Keep process creation, deployment orchestration, and durable work semantics outside this component.
