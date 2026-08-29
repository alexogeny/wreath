"""Retired-instruction decomposition of four equivalent pragmatic stacks.

This deliberately does not collect elapsed time, cycles or IPC. Each sample is
the slope between N and N/2 successful socket requests into a fresh, pinned
server process. Startup, imports, route compilation, client construction and
the fixed perf attachment interval cancel. Seven cumulative arms identify the
cost that entered at each service layer; ``complete-aa`` rebuilds the complete
stack unchanged to put an observed resolution beside the deltas.

Run after installing the benchmark group:

    uv sync --inexact --group benchmark
    uv run python -m benchmarks.bench_e2e_instructions --output \
      benchmarks/baselines/e2e-stack-instructions.json
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import h2load
from .e2e_stack import (
    ALLOWED_ORIGIN,
    ARM_ORDER,
    EXPECTED,
    REQUEST_BODY,
    REQUEST_HEADERS,
    REQUEST_PATH,
)

ROOT = Path(__file__).resolve().parents[1]
# Keep the virtual-environment launcher. Resolving its symlink to /usr/bin/python
# silently drops the benchmark group installed in this environment.
PYTHON = Path(sys.executable)
FRAMEWORKS = ("wreath", "fastapi", "sanic", "blacksheep")
ARMS = (*ARM_ORDER, "complete-aa")
STACK_PACKAGES = (
    "wreath",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic-core",
    "cedarpy",
    "asyncpg",
    "aiohttp",
    "yarl",
    "multidict",
    "uvicorn",
    "uvloop",
    "httptools",
    "sanic",
    "blacksheep",
    "granian",
    "msgspec",
)


def _available_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _wait_ready(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            error = process.stderr.read().decode(errors="replace") if process.stderr else ""
            raise RuntimeError(f"server exited {process.returncode}: {error}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError("server did not become ready within 30 seconds")


def _environment(framework: str, arm: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    env["PYTHONHASHSEED"] = "0"
    env["WREATH_BENCH_DISABLE_TIMERS"] = "1"
    env["WREATH_E2E_FRAMEWORK"] = framework
    env["WREATH_E2E_ARM"] = arm
    return env


def _server_command(framework: str, port: int, cpu: int) -> list[str]:
    prefix = ["taskset", "-c", str(cpu), str(PYTHON)]
    if framework == "wreath":
        return [
            *prefix,
            "-m",
            "benchmarks.wreath_server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app",
            "benchmarks.e2e_stack:app",
            "--loop",
            "metal",
        ]
    if framework == "sanic":
        return [
            *prefix,
            "-m",
            "benchmarks.sanic_server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app",
            "benchmarks.e2e_stack",
            "--workers",
            "1",
        ]
    if framework == "blacksheep":
        return [
            *prefix,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "benchmarks.e2e_stack:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--runtime-threads",
            "1",
            "--blocking-threads",
            "1",
            "--runtime-mode",
            "st",
            "--loop",
            "uvloop",
            "--http",
            "1",
            "--no-access-log",
            "--no-log",
        ]
    return [
        *prefix,
        "-m",
        "uvicorn",
        "benchmarks.e2e_stack:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--loop",
        "uvloop",
        "--http",
        "httptools",
        "--no-access-log",
        "--log-level",
        "warning",
    ]


def _drive(port: int, requests: int, connections: int) -> None:
    result = h2load.measure(
        "127.0.0.1",
        port,
        REQUEST_PATH,
        "http/1.1",
        requests=requests,
        connections=connections,
        streams_per_connection=1,
        threads=1,
        warmup_requests=0,
        method="POST",
        body=REQUEST_BODY,
        headers=REQUEST_HEADERS,
        tls=False,
    )
    if result.requests != requests or result.errors:
        raise RuntimeError(
            f"load completed {result.requests}/{requests} requests with {result.errors} errors"
        )


def _verify(port: int, arm: str) -> None:
    from http.client import HTTPConnection

    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", REQUEST_PATH, body=REQUEST_BODY, headers=REQUEST_HEADERS)
    response = connection.getresponse()
    body = response.read()
    headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    if response.status != 200:
        raise RuntimeError(f"{arm} answered {response.status}: {body[:240]!r}")
    decoded = json.loads(body)
    if decoded != EXPECTED:
        raise RuntimeError(f"{arm} returned {decoded!r}, expected {EXPECTED!r}")
    expects_cors = ARM_ORDER.index("cors") <= ARM_ORDER.index(
        "complete" if arm == "complete-aa" else arm
    )
    actual_origin = headers.get("access-control-allow-origin")
    if expects_cors and actual_origin != ALLOWED_ORIGIN:
        raise RuntimeError(f"{arm} did not emit the equivalent CORS header: {actual_origin!r}")
    if not expects_cors and actual_origin is not None:
        raise RuntimeError(f"{arm} unexpectedly emitted a CORS header")


def _parse_instructions(stderr: str) -> int:
    for line in stderr.splitlines():
        fields = line.split(";")
        if len(fields) > 2 and fields[2].split(":", 1)[0] == "instructions":
            value = fields[0].strip()
            if value.isdigit():
                return int(value)
    raise RuntimeError(f"perf emitted no retired-instruction count:\n{stderr}")


def _perf_pid(framework: str, server_pid: int) -> int:
    """Return the sole request-serving PID, excluding Granian's manager."""
    if framework != "blacksheep":
        return server_pid
    children_path = Path(f"/proc/{server_pid}/task/{server_pid}/children")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            children = children_path.read_text(encoding="ascii").split()
        except OSError:
            children = []
        workers = []
        for child in children:
            try:
                command = Path(f"/proc/{child}/cmdline").read_bytes()
            except OSError:
                continue
            if b"multiprocessing.spawn" in command:
                workers.append(child)
        if len(workers) == 1:
            return int(workers[0])
        if len(workers) > 1:
            raise RuntimeError(
                f"Granian manager {server_pid} spawned {len(workers)} request workers; "
                "the e2e benchmark requires exactly one"
            )
        time.sleep(0.02)
    raise RuntimeError(f"Granian manager {server_pid} did not spawn its worker")


def _one_count(
    framework: str,
    arm: str,
    requests: int,
    connections: int,
    warmup: int,
    server_cpu: int,
    generator_cpu: int,
) -> int:
    port = _available_port()
    env = _environment(framework, arm)
    server = subprocess.Popen(
        _server_command(framework, port, server_cpu),
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_ready(server, port)
        _verify(port, arm)
        perf_pid = _perf_pid(framework, server.pid)
        generator_command = ["taskset", "-c", str(generator_cpu), str(PYTHON), "-c"]
        code = (
            "from benchmarks.bench_e2e_instructions import _drive;"
            f"_drive({port},{warmup},{connections})"
        )
        subprocess.run(
            [*generator_command, code],
            cwd=ROOT,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        perf = subprocess.Popen(
            [
                "perf",
                "stat",
                "--no-big-num",
                "-x",
                ";",
                "-e",
                "instructions:u",
                "-p",
                str(perf_pid),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.1)
            code = (
                "from benchmarks.bench_e2e_instructions import _drive;"
                f"_drive({port},{requests},{connections})"
            )
            subprocess.run(
                [*generator_command, code],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            perf.send_signal(signal.SIGINT)
        _stdout, stderr = perf.communicate(timeout=10)
        return _parse_instructions(stderr)
    finally:
        server.terminate()
        try:
            timeout = 1 if framework in {"fastapi", "sanic", "blacksheep"} else 10
            server.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _versions() -> dict[str, str]:
    versions = {}
    for package in STACK_PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition(":")
            if separator and name.strip() == "model name":
                return value.strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=FRAMEWORKS, action="append")
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--requests", type=int, default=4_000)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument("--server-cpu", type=int, default=8)
    parser.add_argument("--generator-cpu", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--merge",
        action="store_true",
        help="retain compatible framework arms already present in --output",
    )
    args = parser.parse_args(argv)
    if args.requests < 2 or args.trials < 3 or args.connections < 1 or args.warmup < 1:
        parser.error("requests >= 2, trials >= 3, connections >= 1 and warmup >= 1 required")
    if args.server_cpu == args.generator_cpu:
        parser.error("server and generator must use different logical CPUs")

    selected_frameworks = args.framework or list(FRAMEWORKS)
    selected_arms = args.arm or list(ARMS)
    document: dict[str, Any] = {
        "schema": "wreath/e2e-stack-instructions/1",
        "recorded": time.strftime("%Y-%m-%d"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "metric": "retired userspace instructions per successful request",
        "method": (
            "median of alternating N/N2 process slopes; fixed startup, imports, "
            "warmup and perf attachment cancel"
        ),
        "request": {
            "method": "POST",
            "path": REQUEST_PATH,
            "headers": REQUEST_HEADERS,
            "body": json.loads(REQUEST_BODY),
            "expected_response": EXPECTED,
        },
        "measurement": {
            "requests_high": args.requests,
            "requests_low": args.requests // 2,
            "trials": args.trials,
            "connections": args.connections,
            "warmup": args.warmup,
            "server_cpu": args.server_cpu,
            "generator_cpu": args.generator_cpu,
            "pythonhashseed": 0,
        },
        "packages": _versions(),
        "arms": {},
    }
    if args.merge and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        if previous.get("schema") != document["schema"]:
            parser.error(
                f"cannot merge schema {previous.get('schema')!r}; expected {document['schema']!r}"
            )
        if previous.get("measurement") != document["measurement"]:
            parser.error("cannot merge an artifact recorded with different measurement options")
        document["arms"].update(previous.get("arms", {}))
    for framework in selected_frameworks:
        rows: dict[str, Any] = {}
        for arm in selected_arms:
            samples = []
            for trial in range(args.trials):
                order = ("high", "low") if trial % 2 == 0 else ("low", "high")
                totals: dict[str, int] = {}
                for size in order:
                    count = args.requests if size == "high" else args.requests // 2
                    totals[size] = _one_count(
                        framework,
                        arm,
                        count,
                        args.connections,
                        args.warmup,
                        args.server_cpu,
                        args.generator_cpu,
                    )
                slope = (totals["high"] - totals["low"]) / (args.requests - args.requests // 2)
                samples.append(slope)
                print(
                    f"{framework:7s} {arm:11s} {trial + 1}/{args.trials}: "
                    f"{slope:,.1f} instructions/request",
                    flush=True,
                )
            rows[arm] = {
                "median": statistics.median(samples),
                "range": [min(samples), max(samples)],
                "samples": samples,
            }
        previous: float | None = None
        for arm in ARM_ORDER:
            if arm not in rows:
                continue
            current = float(rows[arm]["median"])
            rows[arm]["added_from_previous"] = None if previous is None else current - previous
            previous = current
        if "complete" in rows and "complete-aa" in rows:
            rows["complete-aa"]["absolute_delta_from_complete"] = abs(
                rows["complete-aa"]["median"] - rows["complete"]["median"]
            )
        document["arms"][framework] = rows

    document["fairness"] = (
        "All four stacks receive and verify the same successful request, response and "
        "CORS behavior. "
        "Wreath uses its native metal HTTP/1.1 server, binding, bearer backend, "
        "startup-compiled Cedar engine, PostgreSQL driver and HTTP client. FastAPI "
        "uses Uvicorn with uvloop+httptools, Pydantic, HTTPBearer and Starlette "
        "CORSMiddleware. Sanic uses its native single-process server and response "
        "middleware; BlackSheep uses its built-in CORS policy on Granian ASGI with "
        "uvloop. Sanic and BlackSheep share msgspec binding, cedarpy's public stateless "
        "authorize call, asyncpg and aiohttp. "
        "Both database and HTTP clients speak their real wire protocols to the same "
        "deterministic in-process peers. The server process owns those peers, so their "
        "small instruction cost is included equally. No external database, network, "
        "wall clock, cycles or IPC enters the result."
    )
    document["limitations"] = (
        "Sanic and BlackSheep share a hand-written msgspec success-path adapter for "
        "binding and bearer authentication. It does not reproduce Wreath's structured "
        "validation refusals, case-insensitive bearer parsing, duplicate-credential "
        "refusal, Identity publication, protected-route resolution or 401 Bearer "
        "challenge. Their binding and auth increments, and every later cumulative arm, "
        "therefore measure the exact successful applications rather than equivalent "
        "framework feature costs."
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
