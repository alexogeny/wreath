"""Run equivalent framework applications behind one Uvicorn configuration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import h2load
from .load import LOAD_GENERATOR, LOAD_GENERATOR_VERSION, measure
from .report import generate_report
from .scenarios import FRAMEWORKS, SCENARIOS

#: What each server can actually serve. Uvicorn is HTTP/1.1-only, which bounds
#: every framework it hosts; Sanic's own server implements HTTP/1 and HTTP/3 and
#: never implemented HTTP/2 (`sanic.http.constants.HTTP` has no VERSION_2).
#: A (framework, protocol) pair outside this is skipped, not measured and lost.
SERVER_PROTOCOLS: dict[str, frozenset[str]] = {
    "wreath-native": frozenset({"http/1.1", "h2", "h3"}),
    # Experimental metal tier: verified serving HTTP/1.1, HTTP/2 (TLS+ALPN), and
    # HTTP/3 (QUIC) end-to-end on the reactor loop with wheel-backed timers.
    "wreath-metal": frozenset({"http/1.1", "h2", "h3"}),
    "sanic": frozenset({"http/1.1", "h3"}),
}
#: Everything else rides Uvicorn.
DEFAULT_PROTOCOLS = frozenset({"http/1.1"})


_H2LOAD_VERSION: str | None = None


def _h2load_version() -> str:
    """h2load's own version string, recorded so a run is reproducible."""
    global _H2LOAD_VERSION
    if _H2LOAD_VERSION is None:
        found = h2load.capabilities()
        if found is None:
            _H2LOAD_VERSION = "unknown"
        else:
            probe = subprocess.run(
                [found.path, "--version"], capture_output=True, text=True
            )
            version = probe.stdout.strip() or "unknown"
            # The version string is identical with and without HTTP/3, so it
            # cannot stand alone as provenance for an h3 row.
            _H2LOAD_VERSION = f"{version} (http3={'yes' if found.http3 else 'no'})"
    return _H2LOAD_VERSION


def server_supports(framework: str, protocol: str) -> bool:
    return protocol in SERVER_PROTOCOLS.get(framework, DEFAULT_PROTOCOLS)


#: How long to wait for response-bound background work to drain after the
#: measured load stops before declaring the run invalid.
BACKGROUND_DRAIN_SECONDS = 15.0


def _read_stats(host: str, port: int) -> dict[str, int]:
    with urllib.request.urlopen(  # noqa: S310 - loopback cleartext, benchmark only
        f"http://{host}:{port}/background-stats", timeout=5
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def _drain_background(
    host: str,
    port: int,
    baseline: dict[str, int],
    measured_requests: int,
    warmup_requests: int,
) -> dict[str, Any]:
    """Query the app's task counters, waiting for in-flight work to drain.

    ``baseline`` is a snapshot taken before this scenario's load ran, so the
    per-scenario deltas isolate it from earlier scenarios sharing the same
    server process. The counters include the warmup requests as well as the
    measured ones, so the expected task count is ``warmup + measured``. A run is
    invalid if any task failed, if work is still in flight at the drain bound,
    if fewer tasks completed than started, or if completions do not match the
    requests handed over -- any of which would let a framework look faster by
    dropping or backlogging the work it was given.
    """
    deadline = time.monotonic() + BACKGROUND_DRAIN_SECONDS
    started_wait = time.monotonic()
    stats = _read_stats(host, port)
    while (stats["inflight"] > 0 or stats["completed"] < stats["started"]) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.05)
        stats = _read_stats(host, port)
    drain_seconds = time.monotonic() - started_wait
    started = stats["started"] - baseline["started"]
    completed = stats["completed"] - baseline["completed"]
    failed = stats["failed"] - baseline["failed"]
    expected = warmup_requests + measured_requests
    valid = (
        failed == 0
        and stats["inflight"] == 0
        and completed == started
        and completed == expected
    )
    return {
        "started": started,
        "completed": completed,
        "failed": failed,
        "inflight": stats["inflight"],
        "max_inflight": stats["max_inflight"],
        "expected_started": expected,
        "measured_requests": measured_requests,
        "warmup_requests": warmup_requests,
        "drain_seconds": round(drain_seconds, 4),
        "valid": valid,
    }


REQUEST_TIERS = {
    "wreath": 1_000,
    "wreath-native": 1_000,
    "wreath-metal": 1_000,
    "starlette": 1_000,
    "fastapi": 1_000,
    "sanic": 1_000,
    "blacksheep": 1_000,
    "django": 100,
    "flask": 100,
}


def _available_port(host: str) -> int:
    with socket.socket() as candidate:
        candidate.bind((host, 0))
        return candidate.getsockname()[1]


def _wait_until_ready(process: subprocess.Popen[bytes], host: str, port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with status {process.returncode}")
        try:
            with socket.create_connection((host, port), timeout=0.1):
                pass
            time.sleep(0.05)
            if process.poll() is not None:
                raise RuntimeError(f"server exited with status {process.returncode}")
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("server did not become ready within 30 seconds")


def _wait_until_ready_h3(process: subprocess.Popen[bytes], host: str, port: int) -> None:
    """Wait for an HTTP/3 server, which has no TCP port to connect to.

    A UDP socket accepts a connect() whether or not anything is listening, so
    the only honest readiness signal for QUIC is a request that actually
    completes. This performs one, which also means an h3 row is never recorded
    against a server that never answered h3 at all.
    """
    found = h2load.capabilities()
    if found is None or not found.http3:
        raise RuntimeError(
            "measuring h3 needs an h2load built with HTTP/3 (see benchmarks/README.md)"
        )
    deadline = time.monotonic() + 30
    last = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with status {process.returncode}")
        probe = subprocess.run(
            [found.path, "--h3", "-n", "1", "-c", "1", f"https://{host}:{port}/"],
            capture_output=True, text=True, timeout=20,
        )
        last = probe.stdout + probe.stderr
        if "1 succeeded" in last:
            return
        time.sleep(0.2)
    raise TimeoutError(
        f"HTTP/3 server did not answer within 30 seconds; last probe: {last.strip()[:200]}"
    )


async def _run_framework(
    args: argparse.Namespace,
    framework: str,
    on_result: Callable[[dict[str, object]], None],
) -> bool:
    """One protocol at a time; each trial below boots its own server.

    Not merely tidy: Sanic's `version=3` serves HTTP/3 and nothing else, so a
    single process cannot cover h1 and h3, and wreath-native serving all three
    at once would give it a warmed, shared process while Sanic got a cold one
    per protocol. Per-trial processes (see _run_protocol) keep every arm
    equally cold on top of that.
    """
    wanted = [p for p in args.protocol if server_supports(framework, p)]
    if not wanted:
        print(f"[skip] {framework}: serves none of {', '.join(args.protocol)}", flush=True)
        return True
    ok = True
    for protocol in wanted:
        if not await _run_protocol(args, framework, protocol, on_result):
            ok = False
    return ok


def _server_command(
    args: argparse.Namespace, framework: str, protocol: str, port: int, tls: bool
) -> tuple[list[str], str]:
    """Build one server invocation and its display label for a single boot."""
    if framework in ("wreath-native", "wreath-metal"):
        if framework == "wreath-metal":
            # The metal tier is defined by its loop; it always runs on the reactor.
            native_loop = "metal"
        elif args.loop == "auto":
            # Mirror uvicorn's `--loop auto`: uvloop when installed, else
            # asyncio, so the native-deployment comparison uses the same
            # event loop policy as every uvicorn-hosted framework.
            try:
                import uvloop  # noqa: F401

                native_loop = "uvloop"
            except ImportError:
                native_loop = "asyncio"
        else:
            native_loop = args.loop
        command = [
            sys.executable,
            "-m",
            "benchmarks.wreath_server",
            "--host",
            args.host,
            "--port",
            str(port),
            "--loop",
            native_loop,
        ]
        if tls:
            command += ["--protocol", protocol, "--tls-cert", args.tls_cert,
                        "--tls-key", args.tls_key]
        server = f"{framework} ({native_loop})"
    elif framework == "sanic":
        command = [
            sys.executable,
            "-m",
            "benchmarks.sanic_server",
            "--host",
            args.host,
            "--port",
            str(port),
        ]
        if tls:
            command += ["--protocol", protocol, "--tls-cert", args.tls_cert,
                        "--tls-key", args.tls_key]
        server = "sanic-native"
    else:
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "benchmarks.apps:app",
            "--host",
            args.host,
            "--port",
            str(port),
            "--loop",
            args.loop,
            "--http",
            args.http,
            "--lifespan",
            "off",
            "--no-access-log",
        ]
        if tls:
            command += ["--ssl-certfile", args.tls_cert, "--ssl-keyfile", args.tls_key]
        server = "uvicorn"
    # The load generator runs in this process, so without pinning it competes
    # with the server for cores. On a hybrid CPU that competition also decides
    # whether either lands on a performance or an efficiency core, which is
    # worth ~2x and turns run-to-run noise into nonsense. Give the server its
    # own cores and pin the generator elsewhere (see benchmarks/README.md).
    server_cpus = os.environ.get("WREATH_BENCH_SERVER_CPUS")
    if server_cpus:
        command = ["taskset", "-c", server_cpus, *command]
        server = f"{server} [cpus {server_cpus}]"
    return command, server


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process.returncode not in (0, -15):
        error = (
            process.stderr.read().decode("utf-8", errors="replace")
            if process.stderr
            else ""
        )
        if error:
            print(error, file=sys.stderr)


async def _run_protocol(
    args: argparse.Namespace,
    framework: str,
    protocol: str,
    on_result: Callable[[dict[str, object]], None],
) -> bool:
    request_count = args.requests if args.requests is not None else REQUEST_TIERS[framework]
    env = os.environ.copy()
    env["WREATH_BENCH_FRAMEWORK"] = framework
    # h2/h3 need TLS. For HTTP/1.1 we now prefer h2load *cleartext* whenever it is
    # installed: the pure-Python built-in client caps every server at its own
    # throughput (~80k req/s here) and makes native servers indistinguishable, so
    # a fast C client is essential to measure the server rather than the client.
    tls = bool(args.tls_cert and args.tls_key)
    if args.load_generator == "h2load":
        use_h2load = True
    elif args.load_generator == "auto":
        use_h2load = protocol != "http/1.1" or h2load.capabilities() is not None
    else:
        use_h2load = False
    if use_h2load and not tls and protocol != "http/1.1":
        print(f"[skip] {framework} {protocol}: needs --tls-cert/--tls-key", flush=True)
        return True
    for scenario in args.scenario:
        spec = SCENARIOS[scenario]
        if not spec.supports(framework):
            continue
        method, path = spec.method, spec.path
        # h2load measures GET and body-backed POST, and speaks no WebSocket.
        # Other HTTP/1.1 methods fall to the built-in client. h2/h3 have no
        # built-in client, so unsupported methods stay on h2load and skip.
        # Every non-WebSocket scenario rides h2load: bodyless non-GET
        # methods go through its :method pseudo-header override, so the
        # slow built-in Python generator (which caps out around 50k rps
        # and was silently the measured bottleneck on the PUT/PATCH/DELETE
        # routing rows) is reserved for WebSocket upgrades only.
        scenario_h2load = use_h2load and not spec.websocket
        print(
            f"[start] {framework}/{scenario} [{method}]: {args.warmup_requests:,} warmup + "
            f"{request_count:,} measured requests",
            flush=True,
        )

        def show_progress(
            completed: int,
            total: int,
            elapsed: float,
            scenario_name: str = scenario,
        ) -> None:
            percent = completed / total * 100 if total else 100.0
            rate = completed / elapsed if elapsed else 0.0
            print(
                f"[progress] {framework}/{scenario_name}: {completed:,}/{total:,} "
                f"({percent:5.1f}%) {rate:,.0f} req/s",
                flush=True,
            )

        # Repeat the measurement `--trials` times; every trial emits its own
        # row (tagged with the trial number) so the report can take run-to-run
        # medians/ranges instead of trusting a single noisy shot.
        for trial in range(1, max(1, args.trials) + 1):
            # Every trial is a fully cold arm: its own server process on a
            # fresh port, warmed by this trial's warmup requests alone.
            # Nothing survives from one trial into the next — no keep-alive
            # connections, allocator or buffer-pool state, caches, or
            # accumulated fd/timer state — so trial-to-trial deltas measure
            # cold-start variance, never accumulation from earlier trials.
            port = args.port or _available_port(args.host)
            command, server = _server_command(args, framework, protocol, port, tls)
            process = subprocess.Popen(
                command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            try:
                try:
                    # An h3-only server has no TCP listener; probe it over QUIC.
                    if protocol == "h3":
                        await asyncio.to_thread(
                            _wait_until_ready_h3, process, args.host, port
                        )
                    else:
                        await asyncio.to_thread(
                            _wait_until_ready, process, args.host, port
                        )
                except (TimeoutError, RuntimeError) as error:
                    print(f"[skip] {framework}: {error}", flush=True)
                    return False
                bg_before: dict[str, int] | None = None
                if spec.background:
                    # With a per-trial process this snapshot is always zero,
                    # but reading it keeps the reconciliation arithmetic
                    # self-contained rather than trusting that assumption.
                    bg_before = await asyncio.to_thread(_read_stats, args.host, port)
                if scenario_h2load:
                    try:
                        result = await asyncio.to_thread(
                            h2load.measure,
                            args.host,
                            port,
                            path,
                            protocol,
                            requests=request_count,
                            connections=args.connections or args.concurrency,
                            streams_per_connection=args.streams_per_connection,
                            warmup_requests=args.warmup_requests,
                            method=method,
                            body=spec.body,
                            headers=dict(spec.headers),
                            tls=tls,
                        )
                    except h2load.H2LoadError as error:
                        # Never fall back to the built-in generator here: it
                        # would answer HTTP/1.1 and the row would say h3.
                        print(f"[skip] {framework}/{scenario} {protocol}: {error}",
                              flush=True)
                        break
                    generator = h2load.LOAD_GENERATOR
                    generator_version = _h2load_version()
                    transport = "udp" if protocol == "h3" else "tcp"
                    alpn: str | None = protocol
                else:
                    result = await measure(
                        args.host,
                        port,
                        path,
                        args.duration,
                        args.warmup,
                        args.concurrency,
                        request_count,
                        args.warmup_requests,
                        show_progress,
                        method,
                        spec.body,
                        spec.headers,
                        spec.websocket,
                    )
                    generator = LOAD_GENERATOR
                    generator_version = LOAD_GENERATOR_VERSION
                    transport = "tcp"
                    alpn = None
                background_stats: dict[str, Any] | None = None
                if spec.background and bg_before is not None:
                    # Drain and read the app's task counters before the server
                    # is stopped in the finally block, so a dropped or
                    # backlogged task is caught rather than lost with the
                    # process.
                    background_stats = await asyncio.to_thread(
                        _drain_background,
                        args.host,
                        port,
                        bg_before,
                        result.requests,
                        args.warmup_requests,
                    )
                    if not background_stats["valid"]:
                        print(
                            f"[INVALID] {framework}/{scenario}: background work did "
                            f"not reconcile: {background_stats}",
                            flush=True,
                        )
                row: dict[str, object] = {
                    "framework": framework,
                    "server": server,
                    "scenario": scenario,
                    "method": method,
                    "path": path,
                    # The protocol the connection actually negotiated: the
                    # h2load path refuses to return at all when the ALPN it was
                    # given back is not the one it asked for.
                    "protocol": protocol,
                    "transport": transport,
                    "secure": bool(tls and scenario_h2load),
                    "alpn": alpn,
                    "connections": args.connections or args.concurrency,
                    "max_streams_per_connection": (
                        args.streams_per_connection if scenario_h2load else 1
                    ),
                    "trial": trial,
                    "load_generator": generator,
                    "load_generator_version": generator_version,
                    "server_tls_version": None,
                    "normalized_100k_seconds": (
                        result.duration_seconds * 100_000
                        / max(1, result.requests + result.errors)
                    ),
                    **asdict(result),
                }
                if background_stats is not None:
                    row["background"] = background_stats
                on_result(row)
                print(
                    f"[done] {framework:10} {scenario:10} {protocol:8} "
                    f"{result.requests_per_second:10.0f} req/s "
                    f"p99={result.latency_ms_p99:8.3f} ms errors={result.errors}"
                    f"{'' if args.trials == 1 else f' (trial {trial})'}",
                    flush=True,
                )
            finally:
                _stop_server(process)
    return True


async def run(args: argparse.Namespace) -> None:
    suite_started = time.perf_counter()
    started_at = datetime.now(UTC)
    output_directory = Path(args.output)
    output_directory.mkdir(parents=True, exist_ok=True)
    run_name = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_path = output_directory / f"{run_name}.json"
    report_path = output_directory / f"{run_name}.html"
    latest_json = output_directory / "latest.json"
    latest_report = output_directory / "latest.html"
    rows: list[dict[str, object]] = []
    skipped_frameworks: list[str] = []
    unavailable_scenarios = [
        f"{framework}/{scenario}"
        for framework in args.framework
        for scenario in args.scenario
        if not SCENARIOS[scenario].supports(framework)
    ]
    runnable_scenarios = sum(
        SCENARIOS[scenario].supports(framework)
        for framework in args.framework
        for scenario in args.scenario
    )
    metadata: dict[str, Any] = {
        "timestamp": started_at.isoformat(),
        "status": "running",
        "python": sys.version,
        "platform": platform.platform(),
        "server": "uvicorn except wreath-native and sanic-native",
        "loop": args.loop,
        "http": args.http,
        "host": args.host,
        "port": args.port or "ephemeral-per-trial",
        "concurrency": args.concurrency,
        "request_tiers": {
            framework: args.requests if args.requests is not None else REQUEST_TIERS[framework]
            for framework in args.framework
        },
        "warmup_requests_per_scenario": args.warmup_requests,
        "suite_end_to_end_seconds": 0.0,
        "completed_scenarios": 0,
        "total_scenarios": runnable_scenarios * max(1, args.trials),
        "skipped_frameworks": skipped_frameworks,
        "unavailable_scenarios": unavailable_scenarios,
        "load_generator": "pending",
    }
    document: dict[str, Any] = {"metadata": metadata, "results": rows}

    def persist() -> None:
        metadata["suite_end_to_end_seconds"] = time.perf_counter() - suite_started
        payload = json.dumps(document, indent=2) + "\n"
        output_path.write_text(payload, encoding="utf-8")
        latest_json.write_text(payload, encoding="utf-8")
        generate_report(document, report_path)
        generate_report(document, latest_report)

    def on_result(row: dict[str, object]) -> None:
        rows.append(row)
        generators = sorted({str(item["load_generator"]) for item in rows})
        metadata["load_generators"] = generators
        metadata["load_generator"] = generators[0] if len(generators) == 1 else "mixed"
        metadata["completed_scenarios"] = len(rows)
        persist()
        print(
            f"[report] updated {latest_report} "
            f"({len(rows)}/{metadata['total_scenarios']})",
            flush=True,
        )

    persist()
    print(f"[report] live report: {latest_report}", flush=True)
    for framework in args.framework:
        if not await _run_framework(args, framework, on_result):
            skipped_frameworks.append(framework)
            persist()

    metadata["status"] = "complete_with_skips" if skipped_frameworks else "complete"
    persist()
    print(f"[report] wrote {output_path}", flush=True)
    print(f"[report] wrote {report_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", nargs="+", choices=FRAMEWORKS, default=list(FRAMEWORKS))
    parser.add_argument("--scenario", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=0, help="fixed port; default chooses a free port"
    )
    parser.add_argument("--duration", type=float, default=10.0, help=argparse.SUPPRESS)
    parser.add_argument("--warmup", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--requests",
        type=int,
        help="override tiered request counts for every selected framework",
    )
    parser.add_argument("--warmup-requests", type=int, default=10)
    parser.add_argument("--loop", choices=("auto", "asyncio", "uvloop"), default="auto")
    parser.add_argument("--http", choices=("auto", "h11", "httptools"), default="auto")
    parser.add_argument("--output", default="benchmark-results")
    # Protocol result dimension. The built-in generator measures HTTP/1.1 over
    # cleartext loopback; h2/h3 measurement requires an independent, protocol-
    # capable generator (h2load), invoked as a subprocess -- not by importing a
    # client protocol library into this process.
    parser.add_argument(
        "--protocol", nargs="+", default=["http/1.1"],
        choices=("http/1.1", "h2", "h3"),
        help="protocols to measure (h2/h3 require --load-generator h2load + TLS)",
    )
    parser.add_argument("--trials", type=int, default=1,
                        help="measured trials per row (>=5 for protocol comparisons)")
    parser.add_argument("--connections", type=int, default=None,
                        help="in-flight connections (defaults to --concurrency)")
    parser.add_argument("--streams-per-connection", type=int, default=1)
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    parser.add_argument(
        "--load-generator", choices=("auto", "builtin", "h2load"), default="auto",
        help="'builtin' is HTTP/1.1-only; 'h2load' exercises h2/h3 as a subprocess",
    )
    parsed = parser.parse_args()
    if parsed.trials < 1:
        parser.error("--trials must be positive")
    if parsed.connections is not None:
        parsed.concurrency = parsed.connections
    asyncio.run(run(parsed))


if __name__ == "__main__":
    main()
