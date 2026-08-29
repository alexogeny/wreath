"""Run the Wreath benchmark application on Wreath's native HTTP server.

`--workers N` runs N processes behind one `SO_REUSEPORT` listener group, each
owning a whole event loop and pinned to its own CPU. That exists so the matrix
can be run two ways: every arm confined to one worker on one core, or every arm
given the same worker count as every other. Comparing a multi-worker server
against a single-worker one measures the deployment, not the framework.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import signal
from typing import Any

from wreath._devtools.quiet import physical_cores
from wreath.server import ServerConfig, TLSConfig, serve


def _worker_cpu(worker_id: int, available: set[int]) -> int | None:
    """Choose one available logical CPU from each physical core in turn."""
    if not available:
        return None
    representatives = [
        next((cpu for cpu in members if cpu in available), None)
        for members in physical_cores().values()
    ]
    candidates = [cpu for cpu in representatives if cpu is not None]
    if not candidates:
        candidates = sorted(available)
    return candidates[worker_id % len(candidates)]


def _pin_worker(worker_id: int) -> int | None:
    """Give this worker one logical CPU out of the mask the harness handed us.

    Same rule as the shipped supervisor (`wreath._cli._apply_metal_worker_affinity`):
    take one representative logical CPU from each physical core before using an
    SMT sibling. CPU numbering does not imply topology: on this Ryzen, 0 and 1
    are siblings, while on other machines the siblings are separated by half
    the logical-CPU count. Reading sysfs is startup-only and avoids silently
    pinning a nominal two-core run onto one physical core.
    """
    if not hasattr(os, "sched_setaffinity"):
        return None
    available = set(os.sched_getaffinity(0))
    cpu = _worker_cpu(worker_id, available)
    if cpu is None:
        return None
    os.sched_setaffinity(0, {cpu})
    return cpu


def _make_loop(kind: str, worker_id: int, reuse_port: bool) -> Any:
    if kind == "metal":
        import wreath.reactor as reactor

        return reactor.metal_event_loop(worker_id=worker_id, reuse_port=reuse_port)
    if kind == "uvloop":
        import uvloop

        loop = uvloop.new_event_loop()
    else:
        loop = asyncio.new_event_loop()
    # `Server._start` reads the marker off the loop, so a worker group works on
    # every loop this arm can run: the reuse-port decision belongs to the
    # deployment, not to the reactor.
    loop._wreath_reuse_port = reuse_port
    return loop


def _serve_forever(
    app: Any,
    config: ServerConfig,
    tls: TLSConfig | None,
    kind: str,
    worker_id: int,
    reuse_port: bool,
) -> None:
    async def run_server() -> None:
        server = await serve(app, config, tls=tls)
        await server.serve_forever()

    asyncio.run(
        run_server(),
        loop_factory=lambda: _make_loop(kind, worker_id, reuse_port),
    )


def _run_worker_group(
    app: Any,
    config: ServerConfig,
    tls: TLSConfig | None,
    kind: str,
    workers: int,
) -> int:
    """Fork `workers` servers onto one SO_REUSEPORT listener group.

    Every child is forked before any of them binds, so the harness's "is the port
    open yet" poll cannot see a half-populated listener group for longer than it
    takes the first child to reach `serve()`.

    Deliberately not the shipped supervisor (`wreath._cli._run_metal_worker_group`):
    that one adds generation restarts and readiness handshakes no benchmark
    exercises, and it is metal-only. The steady state being measured -- N loops,
    N accept queues, one port -- is the same.
    """
    if not hasattr(os, "fork"):
        raise SystemExit("--workers requires a POSIX process model")
    children: list[int] = []
    for worker_id in range(workers):
        pid = os.fork()
        if pid == 0:
            for received in (signal.SIGINT, signal.SIGTERM):
                signal.signal(received, signal.SIG_DFL)
            code = 0
            try:
                _pin_worker(worker_id)
                _serve_forever(app, config, tls, kind, worker_id, True)
            except KeyboardInterrupt:
                pass
            except BaseException:  # noqa: BLE001 -- a process boundary, see below
                # Nothing above this frame can see a child's exception. Without
                # this the child exits 0 and the harness measures a listener
                # group one worker short without ever being told.
                import traceback

                traceback.print_exc()
                code = 1
            finally:
                os._exit(code)
        children.append(pid)

    def forward(signum: int, _frame: Any) -> None:
        for child in children:
            try:
                os.kill(child, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)
    failures = 0
    for child in children:
        try:
            _pid, status = os.waitpid(child, 0)
        except ChildProcessError:
            continue
        if os.waitstatus_to_exitcode(status) not in (0, -signal.SIGTERM, -signal.SIGINT):
            failures += 1
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--app",
        default="benchmarks.apps:app",
        help="module:attribute of the ASGI application to serve",
    )
    parser.add_argument("--loop", choices=("asyncio", "uvloop", "metal"), default="asyncio")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="server processes sharing one SO_REUSEPORT listener group",
    )
    parser.add_argument(
        "--protocol",
        nargs="+",
        default=["http/1.1"],
        choices=("http/1.1", "h2", "h3"),
        help="protocol set to serve (h2/h3 require --tls-cert/--tls-key)",
    )
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    parser.add_argument(
        "--prearm",
        type=int,
        default=0,
        help="synthetic connections driven through the stack before serving",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.workers > 1 and args.port == 0:
        # Each worker binds the port itself, so there is no ephemeral port for
        # them to agree on. The harness always picks a concrete one.
        parser.error("--workers above 1 requires a fixed --port")
    module_name, _, attribute = args.app.partition(":")
    app = getattr(importlib.import_module(module_name), attribute or "app")
    timers_disabled = os.environ.get("WREATH_BENCH_DISABLE_TIMERS") == "1"
    protocols = tuple(dict.fromkeys(args.protocol))
    config = ServerConfig(
        host=args.host,
        port=args.port,
        lifespan="off",
        protocols=protocols,  # type: ignore[arg-type]
        keep_alive_timeout=0.0 if timers_disabled else 5.0,
        request_timeout=0.0 if timers_disabled else 30.0,
        # Off by default even though it helps: the harness warms the process
        # with a separate h2load invocation already, so turning this on would
        # change what the matrix measures without changing the number.
        prearm=args.prearm,
    )
    tls = None
    if args.tls_cert and args.tls_key:
        tls = TLSConfig(certfile=args.tls_cert, keyfile=args.tls_key)

    if args.workers > 1:
        raise SystemExit(_run_worker_group(app, config, tls, args.loop, args.workers))
    _serve_forever(app, config, tls, args.loop, 0, False)


if __name__ == "__main__":
    main()
