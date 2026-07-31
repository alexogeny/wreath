"""Which parts of the native request path get *worse* as the CPU gets slower.

Wall-clock microbenchmarks answer "how fast is this box". They cannot answer the
question that decides whether a framework is usable on a small ARM board: as the
clock falls, does every cost fall with it, or do some costs come to dominate?

The distinction is real and it is measurable.

    time = instructions / (IPC x clock)

**Instructions per request is frequency-invariant.** A phase costing 40,000
instructions costs 40,000 instructions at 4.5 GHz and at 400 MHz; only the time
to retire them changes, and it changes exactly with 1/clock. That is the *floor*:
work that scales straight down with the clock, and therefore the work that
dominates a slow machine. Cutting it is what makes the framework more capable at
low Hz.

**Stall cycles are not frequency-invariant.** A DRAM miss costs a roughly fixed
number of *nanoseconds*, so at a lower core clock it costs proportionally fewer
*cycles*. Memory-bound phases therefore get relatively cheaper as the clock
drops -- their IPC rises. That is the *ceiling*: cutting cache misses buys a fast
machine a lot and a slow one comparatively little.

So a phase's `instructions/request` ranks it as a floor target, and the way its
IPC moves between two clocks says how much of its cost was ceiling rather than
floor. A phase with high instruction count and already-high IPC is pure
interpreter work: the best thing to attack for a small board.

## What this measures

The native HTTP/1 path -- `server_http1.c`'s parser, `_RequestContext`
construction, routing, handler invocation and response emission -- driven
through `HttpProtocol` over an in-process transport, as
`bench_native_request_bridge.py` does. No sockets and no kernel I/O, on purpose:
io_uring is what separates `metal` from `native`, and kernel I/O time is not a
framework floor cost. What is measured here is the CPU work both tiers share.

Arms are cumulative, so each adjacent difference is one layer:

    static      a fixed route returning a prebuilt Response
    params      + a path parameter to extract
    bound       + typed parameter binding
    validated   + a return annotation, so the response is validated
    middleware  + the seven-middleware global tape

## Running it

This runs unprivileged and reports instruction counts, which is the half of the
answer that does not need root: `perf` counts `instructions` for a process the
caller owns even at `perf_event_paranoid=3`, as `:u` (userspace-only) events.

Cycles, IPC and cache-misses need the clock *pinned* to be worth reading. On a
varying clock they moved by more than 60% between identical runs while
instruction counts held to within a few percent -- which is the thesis of this
file demonstrating itself. Pinning needs root; an uncommitted
`clock-scaling-run.sh` does it, takes both clocks, and restores the governor and
frequency limits on exit.

    uv run python benchmarks/bench_clock_scaling.py --measure
    uv run python benchmarks/bench_clock_scaling.py --measure --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from wreath import Response, Wreath
from wreath._devtools import cpu_probe as _cpu_probe
from wreath.server import ServerConfig

_native_server: Any = importlib.import_module("wreath._native._server")
HttpProtocol = _native_server.HttpProtocol

#: The counting itself lives in `_devtools/cpu_probe`, so `wreath-cpu-probe`
#: and this file cannot drift on how a per-request count is taken.
COUNTERS = _cpu_probe.COUNTERS

_BODY = b'{"ok":1}'


class _Transport(asyncio.Transport):
    """Counts bytes and nothing else, so the arm measures the framework."""

    def __init__(self) -> None:
        super().__init__()
        self.bytes_written = 0
        self.closed = False

    def write(self, data: Any) -> None:
        self.bytes_written += len(data)

    def writelines(self, list_of_data: Any) -> None:
        for chunk in list_of_data:
            self.bytes_written += len(chunk)

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sockname":
            return ("127.0.0.1", 8000)
        if name == "peername":
            return ("127.0.0.1", 50000)
        return default


def _request(path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\nhost: localhost\r\n"
        "user-agent: wreath-bench\r\naccept: */*\r\n\r\n"
    ).encode()


def _static(tick: Any) -> tuple[Wreath, bytes]:
    app = Wreath()
    body = Response(_BODY)

    @app.get("/plain")
    async def plain(request: Any) -> Response:
        tick()
        return body

    return app, _request("/plain")


def _params(tick: Any) -> tuple[Wreath, bytes]:
    app = Wreath()
    body = Response(_BODY)

    @app.get("/i/{x}")
    async def item(request: Any) -> Response:
        tick()
        return body

    return app, _request("/i/42")


def _bound(tick: Any) -> tuple[Wreath, bytes]:
    app = Wreath()
    body = Response(_BODY)

    @app.get("/i/{x}")
    async def item(request: Any, x: int) -> Response:
        tick()
        return body

    return app, _request("/i/42")


def _validated(tick: Any) -> tuple[Wreath, bytes]:
    app = Wreath()

    @app.get("/i/{x}")
    async def item(request: Any, x: int) -> dict[str, Any]:
        tick()
        return {"id": x, "ok": True}

    return app, _request("/i/42")


def _middleware(tick: Any) -> tuple[Wreath, bytes]:
    from wreath._devtools.sample_app import MIDDLEWARE_FACTORIES

    app = Wreath()
    body = Response(_BODY)
    for factory in MIDDLEWARE_FACTORIES:
        app.add_middleware(factory())

    @app.get("/plain")
    async def plain(request: Any) -> Response:
        tick()
        return body

    return app, _request("/plain")


def _with_headers(count: int) -> Any:
    """The static arm carrying `count` extra request headers.

    The slope across these is the per-header cost of whatever the parser does
    with a header the application never reads -- which is the question, because
    a real request carries eight to fifteen and a handler touches one or two.
    """

    def build(tick: Any) -> tuple[Wreath, bytes]:
        app = Wreath()
        body = Response(_BODY)

        @app.get("/plain")
        async def plain(request: Any) -> Response:
            tick()
            return body

        extra = "".join(f"x-pad-{i}: {'v' * 24}\r\n" for i in range(count))
        raw = (
            f"GET /plain HTTP/1.1\r\nhost: localhost\r\n{extra}\r\n"
        ).encode()
        return app, raw

    return build


#: The names `http.c` interns. A header using one of these allocates no bytes
#: object for its name; anything else does.
_INTERNED = (
    "accept-encoding", "accept-language", "user-agent", "cache-control",
    "referer", "origin", "cookie", "authorization",
)


def _named_headers(names: tuple[str, ...]) -> Any:
    """Eight extra headers drawn from `names`, to split name from value cost."""

    def build(tick: Any) -> tuple[Wreath, bytes]:
        app = Wreath()
        body = Response(_BODY)

        @app.get("/plain")
        async def plain(request: Any) -> Response:
            tick()
            return body

        extra = "".join(f"{n}: {'v' * 24}\r\n" for n in names)
        raw = f"GET /plain HTTP/1.1\r\nhost: localhost\r\n{extra}\r\n".encode()
        return app, raw

    return build


def _resp_headers(count: int) -> Any:
    """The static arm whose handler adds `count` response headers."""

    def build(tick: Any) -> tuple[Wreath, bytes]:
        app = Wreath()
        body = Response(_BODY)
        for i in range(count):
            body.headers.append((f"x-out-{i}".encode(), b"v" * 24))

        @app.get("/plain")
        async def plain(request: Any) -> Response:
            tick()
            return body

        return app, _request("/plain")

    return build


def _raw_asgi(tick: Any) -> tuple[Any, bytes]:
    """A bare ASGI callable, so the protocol floor can be told from Wreath's.

    Not a `Wreath`: the server drives it through the dict-scope path, so this
    arm pays the parser, the scope, one call and the response write, and none
    of the routing, binding or dispatch. `static` minus this is what the
    framework costs on top of the server.
    """

    async def app(scope: Any, receive: Any, send: Any) -> None:
        tick()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": _BODY})

    return app, _request("/plain")


def _sync_handler(tick: Any) -> tuple[Wreath, bytes]:
    """`static`, but the handler is `def`. No coroutine object per request.

    Dispatch calls the handler and awaits only what came back awaitable, so a
    `def` route skips creating a coroutine, sending to it, and unwinding its
    StopIteration. Same answer, and nothing about the application changes but
    the keyword -- which makes it the cheapest possible thing to adopt.
    """
    app = Wreath()
    body = Response(_BODY)

    @app.get("/plain")
    def plain(request: Any) -> Response:
        tick()
        return body

    return app, _request("/plain")


def _fresh_response(tick: Any) -> tuple[Wreath, bytes]:
    """`static`, but building the Response per request instead of reusing one."""
    app = Wreath()

    @app.get("/plain")
    async def plain(request: Any) -> Response:
        tick()
        return Response(_BODY)

    return app, _request("/plain")


def _dict_return(tick: Any) -> tuple[Wreath, bytes]:
    """`static`, but returning a dict for the framework to serialize."""
    app = Wreath()

    @app.get("/plain")
    async def plain(request: Any) -> Any:
        tick()
        return {"ok": 1}

    return app, _request("/plain")


ARMS: dict[str, Any] = {
    "raw-asgi": _raw_asgi,
    "static": _static,
    "sync-handler": _sync_handler,
    "fresh-response": _fresh_response,
    "dict-return": _dict_return,
    "req-hdr-0": _with_headers(0),
    "req-hdr-8": _with_headers(8),
    "req-hdr-20": _with_headers(20),
    "hdr8-interned": _named_headers(_INTERNED),
    "hdr8-novel": _named_headers(tuple(f"x-novel-{i}-pad" for i in range(8))),
    "resp-hdr-0": _resp_headers(0),
    "resp-hdr-8": _resp_headers(8),
    "params": _params,
    "bound": _bound,
    "validated": _validated,
    "middleware": _middleware,
}


async def _drive(arm: str, requests: int) -> tuple[float, int, int]:
    """Push `requests` pipelined requests through the native protocol.

    The last handler sets an event rather than the driver polling a counter: a
    handler may complete on a later loop iteration, and spinning on
    `asyncio.sleep(0)` until it does would put the driver's own yields inside
    the measured window.
    """
    served = 0
    done = asyncio.Event()

    def tick() -> None:
        nonlocal served
        served += 1
        if served >= requests:
            done.set()

    app, raw = ARMS[arm](tick)
    if hasattr(app, "_compile_routes"):
        app._compile_routes()
    loop = asyncio.get_running_loop()
    protocol = HttpProtocol(app, ServerConfig(), loop, set())
    transport = _Transport()
    protocol.connection_made(transport)

    started = time.perf_counter()
    for _ in range(requests):
        protocol.data_received(raw)
    if requests:
        await done.wait()
    elapsed = time.perf_counter() - started
    protocol.connection_lost(None)
    return elapsed, served, transport.bytes_written


def _check(arm: str) -> None:
    """An arm that answers anything but 200 is measuring the wrong response."""
    done = asyncio.Event()
    app, raw = ARMS[arm](done.set)
    if hasattr(app, "_compile_routes"):
        app._compile_routes()

    class _Recording(_Transport):
        """Keeps the bytes, so the status line can be read back."""

        def __init__(self) -> None:
            super().__init__()
            self.seen: list[bytes] = []

        def write(self, data: Any) -> None:
            self.seen.append(bytes(data))
            super().write(data)

        def writelines(self, list_of_data: Any) -> None:
            for chunk in list_of_data:
                self.seen.append(bytes(chunk))
            super().writelines(list_of_data)

    async def once() -> bytes:
        loop = asyncio.get_running_loop()
        protocol = HttpProtocol(app, ServerConfig(), loop, set())
        transport = _Recording()
        protocol.connection_made(transport)
        protocol.data_received(raw)
        await done.wait()
        protocol.connection_lost(None)
        return b"".join(transport.seen)

    head = asyncio.run(once())
    if not head.startswith(b"HTTP/1.1 200"):
        status = head.split(b"\r\n", 1)[0][:48] if head else b"<nothing written>"
        raise SystemExit(
            f"bench-clock-scaling: arm {arm!r} answered {status!r}, not 200. "
            "Its counters would describe that response, not a served request."
        )


def _run_arm(arm: str, requests: int, trials: int, warmup: int) -> float:
    """Median seconds for `requests` requests, for the in-process mode.

    `warmup` is an absolute request count, deliberately not a fraction of
    `requests`: the counting mode differences two runs with different request
    counts, and that only cancels the warmup if both runs warmed up by the same
    amount.
    """

    async def main() -> float:
        if warmup:
            await _drive(arm, warmup)
        samples = []
        for _ in range(trials):
            elapsed, _served, _written = await _drive(arm, requests)
            samples.append(elapsed)
        return statistics.median(samples)

    return asyncio.run(main())


def _counted(arm: str, requests: int, trials: int, warmup: int) -> dict[str, float] | None:
    """Counters per request, via the shared slope machinery in `cpu_probe`."""
    script = str(Path(__file__).resolve())
    return _cpu_probe.per_operation(
        lambda n: [
            sys.executable, script, "--arm", arm, "--trials", str(trials),
            "--warmup", str(warmup), "--requests", str(n),
        ],
        requests,
        scale=trials,
    )


def _frequency_mhz() -> float:
    return _cpu_probe.observed_mhz()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=sorted(ARMS), help="run one arm and exit")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--measure", action="store_true", help="all arms, with counters")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--label", default=None, help="names this run in the output")
    args = parser.parse_args(argv)

    # Worker mode: one arm, no reporting. This is what runs under `perf stat`,
    # so it must do nothing the zero-request run would not also do.
    if args.arm and not args.measure:
        _run_arm(args.arm, args.requests, max(1, args.trials), args.warmup)
        return 0

    for arm in ARMS:
        _check(arm)

    mhz = _frequency_mhz()
    label = args.label or f"{mhz:.0f}MHz"
    print(f"python {platform.python_version()} on {platform.platform()}")
    print(f"observed clock {mhz:.0f} MHz  label={label}")
    print(f"requests={args.requests} trials={args.trials}\n")

    results: dict[str, Any] = {}
    counted = True
    for arm in ARMS:
        seconds = _run_arm(arm, args.requests, args.trials, args.warmup)
        per_request_ns = seconds / args.requests * 1e9
        counters = _counted(arm, args.requests, args.trials, args.warmup) if args.measure else None
        if counters is None:
            counted = False
        results[arm] = {"ns": per_request_ns, "counters": counters}

    if counted:
        header = (
            f"{'arm':12s} {'ns/req':>9s} {'instr/req':>11s} {'cycles/req':>11s} "
            f"{'IPC':>6s} {'cache-miss':>11s}"
        )
        print(header)
        print("-" * len(header))
        for arm, row in results.items():
            c = row["counters"]
            ipc = c["instructions"] / c["cycles"] if c["cycles"] else 0.0
            print(
                f"{arm:12s} {row['ns']:8.0f}n {c['instructions']:11,.0f} "
                f"{c['cycles']:11,.0f} {ipc:6.2f} {c['cache-misses']:11,.1f}"
            )
        print("\nlayer costs (each arm minus the one above it):\n")
        previous = None
        for arm, row in results.items():
            if previous is not None:
                d_ns = row["ns"] - previous["ns"]
                d_instr = row["counters"]["instructions"] - previous["counters"]["instructions"]
                print(f"  {arm:12s} {d_ns:+8.0f} ns  {d_instr:+12,.0f} instructions")
            previous = row
    else:
        print("hardware counters unavailable -- wall time only.")
        print("Run tools/clock-scaling-run.sh (needs root) for the real answer.\n")
        print(f"{'arm':12s} {'ns/req':>9s}")
        print("-" * 22)
        for arm, row in results.items():
            print(f"{arm:12s} {row['ns']:8.0f}n")

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "label": label,
                    "observed_mhz": mhz,
                    "platform": platform.platform(),
                    "requests": args.requests,
                    "trials": args.trials,
                    "arms": results,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
