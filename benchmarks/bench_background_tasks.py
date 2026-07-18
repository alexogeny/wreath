"""Focused in-process benchmark for response-bound background tasks.

Measures the complete ASGI invocation -- not just task-object construction --
for the current raw callback, the new ``BackgroundTask``/``BackgroundTasks``
primitives, ordered groups, thread-offloaded sync work, and the streaming and
native one-shot integration points. Every arm carries a task-completion counter
so a run that silently drops work is rejected rather than reported as fast.

The measurement rules come from ``src/wreath/_devtools/measure.py``: arms are
interleaved so drift hits each alike, and an A/A control fixes the noise floor
so deltas below it are reported as unresolved rather than zero.

    python -m benchmarks.bench_background_tasks --output \
        benchmark-results-background/latest.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any

from wreath import JSONResponse, Response, Wreath
from wreath._devtools import measure
from wreath._devtools.measure import Arm
from wreath.background import BackgroundTask, BackgroundTasks
from wreath.response import StreamingResponse

# Per-arm completion counters, keyed by arm label. The measured task increments
# its own counter, so total completions must equal requests * tasks-per-request.
_completions: dict[str, int] = {}


def _counter(label: str) -> tuple[Any, Any]:
    """Return (async_noop, sync_noop) task callables that count completions."""
    _completions.setdefault(label, 0)

    async def async_noop() -> None:
        _completions[label] += 1

    def sync_noop() -> None:
        _completions[label] += 1

    return async_noop, sync_noop


async def _yield_once() -> None:
    await asyncio.sleep(0)


def _sync_fixed_work() -> None:
    # A small, fixed CPU cost so the thread-offload floor is measured against
    # useful work, not an empty call.
    total = 0
    for i in range(2000):
        total += i
    if total < 0:  # pragma: no cover - keeps the loop from being optimized out
        raise AssertionError


def _build_arms() -> list[Arm]:
    """One Arm per case. ``payload`` holds the per-arm scope template so the
    native one-shot arm can request the ``wreath.response`` extension without
    changing what the plain-response arms measure."""
    plain = measure.scope()
    native = {**measure.scope(), "extensions": {"wreath.response": {}}}

    arms: list[Arm] = []

    def add(label: str, route: Any, tasks_per_request: int, template: dict[str, Any]) -> None:
        app = Wreath()
        app.get("/")(route)
        arm = Arm(label=label, app=app, payload=template)
        arm.samples = []
        # Stash the expected task count on the arm for the integrity check.
        arm.tasks_per_request = tasks_per_request  # type: ignore[attr-defined]
        arms.append(arm)

    # Controls: no background at all. The A/A twin fixes the noise floor and is
    # placed last so the floor includes within-round drift.
    async def no_background(request: Any) -> Response:
        return JSONResponse({"ok": True})

    add("no-background", no_background, 0, plain)

    # Current implementation baseline: a raw zero-argument async callback.
    raw_async, _ = _counter("raw-callback")

    async def raw_callback(request: Any) -> Response:
        response = JSONResponse({"ok": True})
        response.background = raw_async
        return response

    add("raw-callback", raw_callback, 1, plain)

    # One BackgroundTask wrapping an async no-op: wrapper + dispatch overhead.
    task_async, _ = _counter("task-async")

    async def one_async_task(request: Any) -> Response:
        return JSONResponse({"ok": True}, background=BackgroundTask(task_async))

    add("task-async", one_async_task, 1, plain)

    # One async task that yields once: event-loop scheduling cost.
    _counter("task-yield")

    async def yield_task_body() -> None:
        await _yield_once()
        _completions["task-yield"] += 1

    async def one_yield_task(request: Any) -> Response:
        return JSONResponse({"ok": True}, background=BackgroundTask(yield_task_body))

    add("task-yield", one_yield_task, 1, plain)

    # Groups of 1, 4, 16 async no-op tasks: sequential scaling.
    for n in (1, 4, 16):
        label = f"group-{n}"
        grp_async, _ = _counter(label)

        def make_group_route(count: int, fn: Any) -> Any:
            async def route(request: Any) -> Response:
                tasks = BackgroundTasks()
                for _ in range(count):
                    tasks.add_task(fn)
                return JSONResponse({"ok": True}, background=tasks)

            return route

        add(label, make_group_route(n, grp_async), n, plain)

    # One synchronous no-op task: thread-offload floor.
    _, sync_noop = _counter("sync-noop")

    async def one_sync_task(request: Any) -> Response:
        return JSONResponse({"ok": True}, background=BackgroundTask(sync_noop))

    add("sync-noop", one_sync_task, 1, plain)

    # One synchronous fixed-work task: offload with useful work.
    _counter("sync-work")

    def sync_work() -> None:
        _sync_fixed_work()
        _completions["sync-work"] += 1

    async def one_sync_work_task(request: Any) -> Response:
        return JSONResponse({"ok": True}, background=BackgroundTask(sync_work))

    add("sync-work", one_sync_work_task, 1, plain)

    # Streaming response plus one task: terminal-body integration.
    stream_async, _ = _counter("stream-task")

    async def stream_body() -> Any:
        yield b"chunk"

    async def streaming_task(request: Any) -> StreamingResponse:
        return StreamingResponse(stream_body(), background=BackgroundTask(stream_async))

    add("stream-task", streaming_task, 1, plain)

    # Native one-shot response plus one task: wreath.response integration.
    native_async, _ = _counter("native-task")

    async def native_task(request: Any) -> Response:
        return Response(b"ok", background=BackgroundTask(native_async))

    add("native-task", native_task, 1, native)

    # A/A control at the far end of the round.
    async def no_background_aa(request: Any) -> Response:
        return JSONResponse({"ok": True})

    add("no-background (A/A)", no_background_aa, 0, plain)

    return arms


async def _measure(arms: list[Arm], rounds: int, iterations: int, warmup: int) -> None:
    for arm in arms:
        await measure.run(arm.app, arm.payload, warmup)
    for _ in range(rounds):
        for arm in arms:  # interleaved
            arm.samples.append(await measure.time_app(arm.app, arm.payload, iterations))


async def _integrity_check(arms: list[Arm], requests: int) -> dict[str, dict[str, int]]:
    """Drive each arm ``requests`` times counting completions, and require that
    completed tasks equal requests * tasks-per-request. A dropped or leaked task
    invalidates the whole run."""
    completions: dict[str, dict[str, int]] = {}
    for arm in arms:
        expected_per = arm.tasks_per_request  # type: ignore[attr-defined]
        if expected_per == 0:
            completions[arm.label] = {"expected": 0, "completed": 0}
            continue
        _completions[arm.label] = 0
        await measure.run(arm.app, arm.payload, requests)
        completed = _completions[arm.label]
        expected = requests * expected_per
        if completed != expected:
            raise SystemExit(
                f"bench-background: arm {arm.label!r} completed {completed} tasks, "
                f"expected {expected}. A dropped or leaked task invalidates the run."
            )
        completions[arm.label] = {"expected": expected, "completed": completed}
    return completions


def run(rounds: int, iterations: int, warmup: int, integrity_requests: int) -> dict[str, Any]:
    arms = _build_arms()
    asyncio.run(_measure(arms, rounds, iterations, warmup))
    completions = asyncio.run(_integrity_check(arms, integrity_requests))

    summary = measure.report(arms, baseline="no-background", control="no-background (A/A)")

    loop = asyncio.new_event_loop()
    event_loop = f"{type(loop).__module__}.{type(loop).__qualname__}"
    loop.close()

    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "event_loop": event_loop,
            "rounds": rounds,
            "iterations": iterations,
            "warmup": warmup,
            "integrity_requests": integrity_requests,
        },
        "baseline": summary["baseline"],
        "floor": summary["floor"],
        "arms": [
            {
                "arm": arm.label,
                "tasks_per_request": arm.tasks_per_request,  # type: ignore[attr-defined]
                "median": round(arm.median, 3),
                "p95": round(arm.p95, 3),
                "samples": [round(s, 3) for s in arm.samples],
                "completions": completions.get(arm.label, {}),
            }
            for arm in arms
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=11)
    parser.add_argument("--iterations", type=int, default=4000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--integrity-requests", type=int, default=500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.rounds, args.iterations, args.warmup, args.integrity_requests)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(f"\nwrote {args.output}")


if __name__ == "__main__":
    main()
