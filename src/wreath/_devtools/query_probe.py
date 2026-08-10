"""What one PostgreSQL query costs, in units a busy machine cannot move.

`wreath-cpu-probe` answers this for the request path; this answers it for the
driver, and it exists because the driver is the half of a database request that
wall time is worst at measuring. A query waits on a socket, so elapsed time per
query is mostly the server and the loop; run the same benchmark while another
container is busy and the number moves 30% with no code change at all. That
happened during the work this module came out of: an unchanged pure driver
measured 33.6us in one session and 44.3us an hour later, which is enough to
call a real improvement a regression and go looking for it in the wrong place.

Two counts here do not have that problem.

**Instructions per query.** Retired instructions are the work the process
performed. They do not move with the governor, with another tenant on the box,
or with how long PostgreSQL took to answer. Measured by slope (see
`cpu_probe.per_operation`): the same command at N queries and N/2, differenced,
so connection setup, imports and warmup cancel instead of inflating.

**Python calls per query.** Exact, from `sys.setprofile`, not sampled. This is
the count that says *why* the instruction count moved: work leaving the
interpreter shows up here first and most legibly. The profiler's own overhead
makes the timing meaningless during the count, which is fine -- nothing is
timed while it is on.

## The ungraft A/B

`--ungraft` restores the Python `_submit`, `_flush`, `_finish_operation` and
the query entry points onto the Connection at runtime, reproducing
the driver as it was before `_native/postgres/pipeline.c` existed. That makes
the before/after a single flag on one build, in one session, on one box --
rather than two builds measured minutes apart, which is exactly the comparison
that is not trustworthy on a machine somebody is using.

    uv run wreath-query-probe                     # native, every arm
    uv run wreath-query-probe --ungraft           # the Python state machine
    uv run wreath-query-probe --compare           # both, and the delta
    uv run wreath-query-probe --arm fetchrow --calls

Needs a database. `WREATH_QUERY_PROBE_DSN`, or `WREATH_TEST_POSTGRES_DSN`, or
the TechEmpower one at `tfb-database` -- whichever is set first. The arms only
read, and only from tables they create themselves under `wreath_query_probe`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import cpu_probe

#: Where the driver's own methods came from before the port. Restoring these
#: onto the native type is the A/B; see the module docstring.
GRAFTED = (
    "_submit", "_flush", "_closes_prefix", "_finish_operation",
    "_publish_completed", "execute", "fetch", "fetchrow", "fetchval",
    "_fetch_into",
)

#: Each arm is one query shape. They differ in the axis named, and nothing
#: else, so the difference between two of them is that axis.
ARMS: dict[str, str] = {
    "fetchrow": "one row, one column pair -- the smallest complete round trip",
    "fetch12": "twelve rows -- the Fortunes shape; adds per-row decode",
    "execute": "no result set -- submission and completion with no decode",
    "map20": "twenty round trips in one pipeline -- amortises submission",
    "noop": "no query at all -- one await that actually suspends. The floor "
            "every arm doing I/O is measured against",
    "resumed": "await a future that is already resolved -- the await machinery "
               "with no suspension. `noop` minus this is what one suspension "
               "and reschedule costs",
    "build": "build one cached-plan query packet and nothing else -- no "
             "Operation, no future, no I/O",
    "nothing": "the harness's own per-iteration cost and nothing else -- "
               "subtract it from every arm below before reading them",
    "operation": "allocate one Operation and nothing else",
    "future": "create one future on this loop and nothing else",
    "awaitable": "allocate the submission awaitable and drop it -- `_submit` is "
                 "lazy, so this is the awaitable's allocation and nothing else",
    "is_txn": "the transaction-control test `_submit` runs on every SQL string",
    "plan_get": "one plan-cache lookup and nothing else",
    "flush_idle": "one `_flush` with an empty queue -- the bookkeeping, no write",
    "submit": "submit and flush an operation cancelled before it reaches the "
              "wire -- Operation, packet, future, queue and flush bookkeeping, "
              "with no I/O and no completion",
}


def dsn() -> str | None:
    for name in (
        "WREATH_QUERY_PROBE_DSN",
        "WREATH_TEST_POSTGRES_DSN",
    ):
        value = os.environ.get(name)
        if value:
            return value
    # The TechEmpower database, when this box has one up.
    if os.environ.get("WREATH_QUERY_PROBE_TFB"):
        return (
            "postgresql://benchmarkdbuser:benchmarkdbpass"
            "@tfb-database:5432/hello_world"
        )
    return None


def _child_command(
    arm: str, queries: int, *, ungraft: bool, calls: bool, target: str
) -> list[str]:
    """The command that runs `queries` of `arm` and exits."""
    argv = [
        sys.executable, "-m", "wreath._devtools.query_probe",
        "--run-child", arm, "--queries", str(queries), "--dsn", target,
    ]
    if ungraft:
        argv.append("--ungraft")
    if calls:
        argv.append("--calls")
    return argv


# ------------------------------------------------------------------ #
# The child: performs the queries and exits.
# ------------------------------------------------------------------ #

def _reference_state_machine() -> Any:
    """The module the native Connection inherits its Python state machine from.

    Read off the type rather than named: `_native/postgres/connection.c` makes
    the native `Connection` a *subclass* of the reference one, so its base is
    the same object a hardcoded import would give and stays right if that
    module moves. Everything `pipeline.c` did not override still runs from
    there, which is what makes `--ungraft` a restore rather than a rewrite.

    `import_module` rather than `from wreath._native import _postgres`, for the
    same reason `wreath/_native/__init__.py` uses it: the compiled submodule is
    invisible to static analysis, and the plain import form is an unresolved
    member to `ty`.
    """
    import importlib

    native = importlib.import_module("wreath._native._postgres")
    return sys.modules[native.Connection.__mro__[1].__module__]


def _transaction_test(*, ungrafted: bool) -> Any:
    """The transaction-control test the *installed* `_submit` actually runs.

    Native `_submit` decides it in C; the ungrafted Python state machine calls
    the module global beside it. An arm that priced the Python one in both
    halves would print a delta of zero for a difference that is real, in a tool
    whose whole claim is that its before and after are one build in one session.
    """
    if ungrafted:
        return _reference_state_machine()._is_transaction_sql

    from wreath.postgres import _is_transaction_sql

    return _is_transaction_sql


def _graft_plan(reference: type) -> list[tuple[str, Any]]:
    """Every method `--ungraft` would restore, or a refusal naming what is gone.

    Decided separately from applying it because applying it rewrites a type for
    the life of the process, so the decision is the only half a test can drive.
    A partial graft is refused rather than measured: it reports a delta of
    nearly nothing, which reads as "the port bought little" instead of "the A/B
    did not happen", and that is a wrong number rather than a missing one.
    """
    plan = [(name, getattr(reference, name, None)) for name in GRAFTED]
    missing = [name for name, original in plan if original is None]
    if missing:
        raise SystemExit(
            f"--ungraft cannot find {', '.join(missing)} on "
            f"{reference.__module__}.{reference.__qualname__}: the Python state "
            "machine has moved, and the A/B would compare the native pipeline "
            "against itself."
        )
    return plan


def _ungraft_native() -> None:
    """Put the Python state machine back on the native Connection."""
    import importlib

    plan = _graft_plan(_reference_state_machine().Connection)
    native = importlib.import_module("wreath._native._postgres")
    for name, original in plan:
        setattr(native.Connection, name, original)


async def _run_arm(
    arm: str, queries: int, target: str, count_calls: bool, *, ungrafted: bool
) -> None:
    import collections

    from wreath.postgres import connect

    _is_transaction_sql = _transaction_test(ungrafted=ungrafted)

    concurrency = 16
    connections = [await connect(target) for _ in range(concurrency)]
    try:
        await _prepare(connections[0])
        one = 'SELECT id, payload FROM wreath_query_probe.rows WHERE id = $1'
        twelve = 'SELECT id, payload FROM wreath_query_probe.rows LIMIT 12'

        # Priced by `build` and `submit`: the two stages of a query that
        # happen before anything is written, isolated so the round trip can be
        # attributed rather than guessed at. Resolved after the warmup below,
        # because the plan does not exist until a query has been through once.
        plan: Any = None

        def build_packet(connection: Any) -> None:
            connection._build_cached(plan, (1,), "fetchrow")

        def allocate_operation(connection: Any) -> None:
            # The same constructor `_submit` calls, with the same argument
            # shape, so the difference between this and `submit` excludes it.
            connection._operation_type(1, one, (1,), "fetchrow", None, None)

        def create_future(connection: Any) -> None:
            connection._loop.create_future()

        def allocate_awaitable(connection: Any) -> None:
            # Never awaited: submission is deferred to the first step, so this
            # allocates the awaitable, and drops it, and touches nothing else.
            connection._submit("fetchrow", one, (1,))

        def is_transaction(connection: Any) -> None:
            _is_transaction_sql(one)

        def plan_get(connection: Any) -> None:
            connection._plans.get(one)

        def flush_idle(connection: Any) -> None:
            connection._flush()

        def submit_and_drop(connection: Any) -> None:
            """Everything `_submit` does, stopped before the wire.

            The awaitable is lazy, so stepping it once is what performs the
            submission; cancelling the operation's future then makes `_flush`
            drop it as a tombstone when it next runs. `_eager_flush_idle` is
            off for this arm so the step does not write.
            """
            awaitable = connection._submit("fetchrow", one, (1,))
            iterator = awaitable.__await__()
            try:
                next(iterator)
            except StopIteration:
                return
            operation = connection._waiting[-1]
            operation.future.cancel()
            # Flush it away. A cancelled operation is dropped as a tombstone
            # and writes nothing, but the flush is what decrements
            # `_waiting_live` -- without it the queue fills to
            # `max_queued_operations` and the arm refuses itself.
            connection._flush()

        async def once(connection: Any) -> None:
            if arm == "nothing":
                return
            if arm == "build":
                build_packet(connection)
            elif arm == "operation":
                allocate_operation(connection)
            elif arm == "future":
                create_future(connection)
            elif arm == "awaitable":
                allocate_awaitable(connection)
            elif arm == "is_txn":
                is_transaction(connection)
            elif arm == "plan_get":
                plan_get(connection)
            elif arm == "flush_idle":
                flush_idle(connection)
            elif arm == "submit":
                submit_and_drop(connection)
            elif arm == "resumed":
                await _already_resolved()
            elif arm == "noop":
                # One suspension and one resumption on this loop, with no
                # driver in the path. Everything above this line is the cost of
                # awaiting at all; subtracting it from an arm leaves the driver.
                await _resolved()
            elif arm == "fetchrow":
                await connection.fetchrow(one, 1)
            elif arm == "fetch12":
                await connection.fetch(twelve)
            elif arm == "execute":
                await connection.execute("SELECT 1")
            else:
                await connection.map(
                    "fetchrow", one, [(i % 12 + 1,) for i in range(20)],
                    max_in_flight=20,
                )

        for connection in connections:  # warm plans, codec, statement cache
            for _ in range(3):
                await connection.fetchrow(one, 1)
        if arm in ("build", "submit", "operation", "future", "nothing",
                   "is_txn", "plan_get", "flush_idle", "awaitable"):
            plan = connections[0]._plans.get(one)
            if plan is None:
                raise SystemExit("no cached plan; the warmup did not run")
        if arm == "submit":
            # Queue rather than write, so this arm never reaches the transport.
            # On the type: the native Connection has no instance dict, and
            # this is a class-level policy flag either way.
            type(connections[0])._eager_flush_idle = False
        for connection in connections:
            for _ in range(3):
                await once(connection)

        allocated_before = sys.getallocatedblocks()
        counts: collections.Counter[str] = collections.Counter()

        def profile(frame: Any, event: str, _arg: Any) -> None:
            if event == "call":
                counts[frame.f_code.co_filename] += 1

        per_worker = max(1, queries // concurrency)

        async def worker(connection: Any) -> None:
            for _ in range(per_worker):
                await once(connection)

        import asyncio

        if count_calls:
            sys.setprofile(profile)
        await asyncio.gather(*(worker(c) for c in connections))
        if count_calls:
            sys.setprofile(None)

        allocated = sys.getallocatedblocks() - allocated_before
        if count_calls:
            performed = per_worker * concurrency
            # `map` performs 20 queries per call; every other arm performs one.
            scale = 20 if arm == "map20" else 1
            total = sum(counts.values())
            payload = {
                "arm": arm,
                "calls_per_query": total / (performed * scale),
                # Net live blocks is not the allocation *rate* -- most objects
                # are freed again within the query -- so this is reported only
                # as a leak check. `allocations` below is the one that counts.
                "net_blocks_per_query": allocated / (performed * scale),
                "by_file": {
                    name.split("/wreath/")[-1] if "/wreath/" in name else name:
                        count / (performed * scale)
                    for name, count in counts.most_common(8)
                },
            }
            print(json.dumps(payload))
    finally:
        for connection in connections:
            await connection.close()


async def _already_resolved() -> None:
    """An await that never suspends: the future is done before it is awaited.

    Awaiting a resolved future runs the whole await protocol -- coroutine
    object, `__await__`, the `send` that returns immediately -- and never
    yields to the scheduler. `noop` does the same and yields once, so the
    difference between them is one suspension: a `call_soon`, a scheduler turn,
    and the task resumption.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    future.set_result(None)
    await future


async def _resolved() -> None:
    """An await that suspends exactly once, like a query that returned instantly."""
    import asyncio

    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    loop.call_soon(lambda: future.done() or future.set_result(None))
    await future


async def _prepare(connection: Any) -> None:
    """Create and fill the probe's own table; idempotent."""
    await connection.execute("CREATE SCHEMA IF NOT EXISTS wreath_query_probe")
    await connection.execute(
        "CREATE TABLE IF NOT EXISTS wreath_query_probe.rows ("
        "id int PRIMARY KEY, payload text NOT NULL)"
    )
    existing = await connection.fetchval(
        "SELECT count(*) FROM wreath_query_probe.rows"
    )
    if not existing:
        for index in range(1, 13):
            await connection.execute(
                "INSERT INTO wreath_query_probe.rows (id, payload) VALUES ($1, $2)",
                index, f"row {index} payload",
            )


def _child_main(args: argparse.Namespace) -> int:
    import asyncio

    if args.ungraft:
        _ungraft_native()

    def _loop() -> Any:
        try:
            import wreath.reactor as reactor

            return reactor.metal_event_loop(worker_id=0, reuse_port=False)
        except Exception:  # noqa: BLE001 -- the stdlib loop is a fine fallback
            return asyncio.new_event_loop()

    asyncio.run(
        _run_arm(
            args.run_child, args.queries, args.dsn, args.calls,
            ungrafted=args.ungraft,
        ),
        loop_factory=_loop,
    )
    return 0


# ------------------------------------------------------------------ #
# The parent: drives the child and reports.
# ------------------------------------------------------------------ #

def measure(
    arm: str, queries: int, target: str, *, ungraft: bool
) -> dict[str, float] | None:
    scale = 20 if arm == "map20" else 1
    return cpu_probe.per_operation(
        lambda n: _child_command(
            arm, n, ungraft=ungraft, calls=False, target=target
        ),
        queries,
        scale=scale,
    )


def syscalls(
    arm: str, queries: int, target: str, *, ungraft: bool
) -> dict[str, float] | None:
    """Syscalls per query, by the same slope, counted with `strace -c`.

    Not `perf stat -e raw_syscalls:sys_enter`: that is a tracepoint, and
    tracepoints need `kernel.perf_event_paranoid <= 1` while instruction
    counting works at 2. Requiring a sysctl change to see the number would mean
    most runs simply do not have it. `strace` needs no privilege for a process
    the caller launches (it does need `ptrace_scope <= 1`, which is the common
    default) and it counts exactly rather than sampling.

    It also slows the child by an order of magnitude, which does not matter:
    the count is what is wanted, and both halves of the slope pay it equally.
    """
    import shutil
    import subprocess

    if shutil.which("strace") is None:
        return None

    def total_for(count: int) -> dict[str, float] | None:
        done = subprocess.run(
            ["strace", "-c", "-f", "-U", "name,calls",
             *_child_command(arm, count, ungraft=ungraft, calls=False, target=target)],
            capture_output=True, text=True, check=False,
        )
        if done.returncode != 0:
            return None
        counts: dict[str, float] = {}
        for line in done.stderr.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].isdigit():
                counts[fields[0]] = float(fields[1])
        return counts or None

    high = total_for(queries)
    low = total_for(queries // 2)
    if high is None or low is None:
        return None
    scale = 20 if arm == "map20" else 1
    spread = (queries - queries // 2) * scale
    if spread <= 0:
        return None
    per_query = {
        name: (high[name] - low.get(name, 0.0)) / spread
        for name in high
        if name != "total"
    }
    # Slope noise puts tiny counters slightly negative; those are syscalls that
    # do not scale with the query count at all (setup, imports), which is
    # exactly what the slope is meant to remove. Drop rather than report.
    resolved = {name: value for name, value in per_query.items() if value > 0.01}
    resolved["total"] = sum(resolved.values())
    return resolved


def python_calls(arm: str, queries: int, target: str, *, ungraft: bool) -> Any:
    import subprocess

    done = subprocess.run(
        _child_command(arm, queries, ungraft=ungraft, calls=True, target=target),
        capture_output=True, text=True, check=False,
    )
    if done.returncode != 0:
        return None
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return None


def _render(results: dict[str, dict[str, Any]]) -> None:
    """Instructions first, deliberately.

    `cpu_probe.report` leads with ns/op, which is the right first column for
    the request path and the wrong one here: the number this tool exists to
    replace. IPC is printed because it says how much of an arm was stalls
    rather than work, but it is only meaningful with the governor pinned --
    `cpu_probe`'s docstring has the measurement that established that.
    """
    header = (
        f"{'arm':<22} {'instr/query':>13} {'cycles/query':>13} {'IPC':>6} "
        f"{'cache-miss':>11}"
    )
    print(header)
    print("-" * len(header))
    for label, row in results.items():
        cycles = row.get("cycles") or 0.0
        # A slope between two noisy counters can land below zero, and a negative
        # cycle count is not a small cycle count -- it is "unresolved". Printing
        # the number anyway would give an IPC of -4.69, which reads as data.
        # Instructions do not do this; that is the reason they are column one.
        if cycles > 0:
            cycles_text = f"{cycles:>13,.0f}"
            ipc_text = f"{row['instructions'] / cycles:>6.2f}"
        else:
            cycles_text, ipc_text = f"{'unresolved':>13}", f"{'-':>6}"
        misses = row.get("cache-misses", 0.0)
        miss_text = f"{misses:>11,.1f}" if misses > 0 else f"{'-':>11}"
        print(
            f"{label:<22} {row['instructions']:>13,.0f} {cycles_text} "
            f"{ipc_text} {miss_text}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-query-probe",
        description="Instructions and Python calls per PostgreSQL query.",
    )
    parser.add_argument("--run-child", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--queries", type=int, default=8000)
    parser.add_argument("--arm", action="append", default=None,
                        help=f"limit to these arms: {', '.join(ARMS)}")
    parser.add_argument("--ungraft", action="store_true",
                        help="restore the Python state machine onto the native "
                             "Connection -- the driver as it was before pipeline.c")
    parser.add_argument("--compare", action="store_true",
                        help="measure both ways and print the delta")
    parser.add_argument("--calls", action="store_true",
                        help="also count Python calls per query (exact, not sampled)")
    parser.add_argument("--syscalls", action="store_true",
                        help="also count syscalls per query, via strace")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    target = args.dsn or dsn()
    if target is None:
        print(
            "wreath-query-probe needs a database. Set one of:\n"
            "    WREATH_QUERY_PROBE_DSN=postgresql://...\n"
            "    WREATH_TEST_POSTGRES_DSN=postgresql://...\n"
            "    WREATH_QUERY_PROBE_TFB=1   (uses tfb-database:5432)\n",
            file=sys.stderr,
        )
        return 2

    if args.run_child:
        args.dsn = target
        return _child_main(args)

    if not cpu_probe.available():
        print("perf cannot count hardware events here; "
              "instructions/query is unavailable", file=sys.stderr)
        return 2

    arms = args.arm or list(ARMS)
    unknown = [name for name in arms if name not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {', '.join(unknown)}. Known: {', '.join(ARMS)}")

    modes = [False, True] if args.compare else [args.ungraft]
    results: dict[str, dict[str, Any]] = {}
    for arm in arms:
        for ungrafted in modes:
            label = f"{arm} [python]" if ungrafted else arm
            counters = measure(arm, args.queries, target, ungraft=ungrafted)
            if counters is None:
                print(f"  {label}: perf produced no counters", file=sys.stderr)
                continue
            row: dict[str, Any] = dict(counters)
            if args.calls:
                calls = python_calls(arm, args.queries, target, ungraft=ungrafted)
                if calls is not None:
                    row["python_calls"] = calls["calls_per_query"]
                    row["by_file"] = calls["by_file"]
            if args.syscalls:
                traced = syscalls(arm, args.queries, target, ungraft=ungrafted)
                if traced is not None:
                    row["syscalls"] = traced.pop("total")
                    row["by_syscall"] = traced
            results[label] = row

    _render(results)
    if args.calls:
        print("\n  python calls per query")
        for name, row in results.items():
            if "python_calls" in row:
                print(f"    {name:<20} {row['python_calls']:>8.2f}")
                for where, count in list(row.get("by_file", {}).items())[:4]:
                    print(f"        {count:>6.2f}  {where}")
    if args.syscalls:
        print("\n  syscalls per query")
        for name, row in results.items():
            if "syscalls" in row:
                print(f"    {name:<20} {row['syscalls']:>8.2f}")
                top = sorted(row.get("by_syscall", {}).items(),
                             key=lambda item: -item[1])[:4]
                for call, count in top:
                    print(f"        {count:>6.2f}  {call}")
    if args.compare:
        print("\n  delta (native pipeline vs the Python state machine)")
        for arm in arms:
            native = results.get(arm, {}).get("instructions")
            python = results.get(f"{arm} [python]", {}).get("instructions")
            if native and python:
                print(f"    {arm:<12} {python - native:>10,.0f} fewer instructions"
                      f"  ({100 * (python - native) / python:.1f}%)")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
