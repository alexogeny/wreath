"""Hardware-counter account for equivalent holistic service applications.

Retired instructions and supported cache events are collected in
non-multiplexed perf passes. PSS and RSS are sampled separately over the full
server process tree at ready, verified, warmed, retained, and observed-peak phases,
so procfs reads cannot perturb hardware-counter samples. Each counter sample is
an alternating N/N/2 process slope, so fixed imports, startup, dependency
construction, warm-up and perf attachment cancel. ``holistic-aa`` rebuilds the
unchanged application as a resolution control. The server and generator run on
separate physical cores.

Run after installing the benchmark group::

    uv sync --inexact --group benchmark
    uv run python -m benchmarks.bench_holistic_stack_instructions \
      --output benchmarks/baselines/e2e-holistic-stack-instructions.json
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import signal
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import time
from base64 import b64encode
from pathlib import Path
from typing import Any

from . import h2load

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
REQUEST_METHOD = "POST"
REQUEST_PATH = "/v1/holistic/42?limit=3&page=1&size=12&sort=-score"
REQUEST_BODY = (
    b'{"title":"Quarterly <report>","lines":['
    b'{"sku":"alpha-1","quantity":2,"price":12.5},'
    b'{"sku":"beta-2","quantity":1,"price":7.25},'
    b'{"sku":"gamma-3","quantity":4,"price":3.5}'
    b'],"labels":{"active":true,"reviewed":false}}'
)
REQUEST_HEADERS = {
    "Authorization": "Bearer holistic-user",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "Host": "operations.example.com",
    "Origin": "https://example.com",
    "Sec-Fetch-Site": "same-origin",
    "X-Trace": "holistic-trace-42",
    "Cookie": "session=holistic-session",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile Safari/537.36",
    "X-Forwarded-For": "4.1.1.1",
}
FRAMEWORKS = ("wreath", "wreath-optimal", "fastapi", "sanic", "blacksheep")
ARMS = ("holistic", "holistic-aa")
COUNTER_GROUPS = (
    (
        "instructions-l1",
        {
            "instructions": "instructions:u",
            "l1d_accesses": "l1-dcache-loads:u",
            "l1d_misses": "l1-dcache-load-misses:u",
            "l1i_accesses": "l1-icache-loads:u",
            "l1i_misses": "l1-icache-load-misses:u",
        },
    ),
    (
        "l2",
        {
            "l2_demand_hits": "l2_cache_req_stat.ic_dc_hit_in_l2:u",
            "l2_prefetch_hits": "l2_pf_hit_l2:u",
            "l2_demand_misses": "l2_cache_req_stat.ic_dc_miss_in_l2:u",
            "l2_prefetch_hits_l3": "l2_pf_miss_l2_hit_l3:u",
            "l2_prefetch_misses_l3": "l2_pf_miss_l2_l3:u",
        },
    ),
)
INSTRUCTION_COUNTER_GROUPS = (("instructions", {"instructions": "instructions:u"}),)
STACK_PACKAGES = (
    "wreath",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic-core",
    "uvicorn",
    "uvloop",
    "httptools",
    "cedarpy",
    "asyncpg",
    "aiohttp",
    "numpy",
    "jinja2",
    "protobuf",
    "msgspec",
    "sanic",
    "blacksheep",
    "granian",
)
_RESPONSE_TOKENS = (
    b"Quarterly &lt;report&gt;",
    b"holistic-user / 42",
    b"POST:/v1/holistic/42",
    b"gamma-3",
    b'data-buckets="730"',
    b'data-lines="288"',
    b'data-paths="11"',
    b'data-vector="128:128:30000/32"',
    b'data-page="12/48"',
    b'data-metrics="5"',
    b'data-client-country="US"',
    b'data-client-agent="Chrome"',
    b'data-client-bot="false"',
    b"<svg",
    b'<path d="M',
    b'data-distance="732"',
)
_ZLIB_BACKED_WREATH = 5_686_525.2
_ZLIB_BACKED_RECORDED = "2026-08-18"
_DICTIONARY_PATH = ROOT / "benchmarks" / "data" / "holistic-dictionary-v1.html"
_DICTIONARY = _DICTIONARY_PATH.read_bytes().removesuffix(b"\n")
_DICTIONARY_TOKEN = ":" + b64encode(hashlib.sha256(_DICTIONARY).digest()).decode() + ":"


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


def _environment(framework: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    env["PYTHONHASHSEED"] = "0"
    env["WREATH_BENCH_DISABLE_TIMERS"] = "1"
    if framework == "wreath-optimal":
        env["WREATH_BENCH_OPTIMAL_COMPRESSION"] = "1"
    if framework in {"sanic", "blacksheep"}:
        env["WREATH_HOLISTIC_FRAMEWORK"] = framework
    return env


def _server_command(framework: str, port: int, cpu: int, certificate: Path, key: Path) -> list[str]:
    prefix = ["taskset", "-c", str(cpu), str(PYTHON)]
    if framework in {"wreath", "wreath-optimal"}:
        return [
            *prefix,
            "-m",
            "benchmarks.wreath_server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app",
            "benchmarks.holistic_e2e:app",
            "--loop",
            "metal",
            "--tls-cert",
            str(certificate),
            "--tls-key",
            str(key),
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
            "benchmarks.holistic_alternatives",
            "--workers",
            "1",
            "--tls-cert",
            str(certificate),
            "--tls-key",
            str(key),
        ]
    if framework == "blacksheep":
        return [
            *prefix,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "benchmarks.holistic_alternatives:app",
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
            "--ssl-certificate",
            str(certificate),
            "--ssl-keyfile",
            str(key),
        ]
    return [
        *prefix,
        "-m",
        "uvicorn",
        "benchmarks.holistic_fastapi:app",
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
        "--ssl-certfile",
        str(certificate),
        "--ssl-keyfile",
        str(key),
    ]


def _request_headers(framework: str) -> dict[str, str]:
    headers = dict(REQUEST_HEADERS)
    if framework == "wreath-optimal":
        # Equal-quality modern clients exercise the canonical Wreath ladder:
        # exact DCZ, prepared fragment gzip, then format-aware gzip.
        headers["Accept-Encoding"] = "dcz, gzip, zstd"
        headers["Available-Dictionary"] = _DICTIONARY_TOKEN
    return headers


def _drive(framework: str, port: int, requests: int, connections: int) -> None:
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
        method=REQUEST_METHOD,
        body=REQUEST_BODY,
        headers=_request_headers(framework),
        tls=True,
    )
    if result.requests != requests or result.errors:
        raise RuntimeError(
            f"load completed {result.requests}/{requests} requests with {result.errors} errors"
        )


def _verify(port: int, framework: str) -> None:
    from http.client import HTTPSConnection

    from wreath._compression import _dcz_decompress

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = HTTPSConnection("127.0.0.1", port, timeout=10, context=context)
    connection.request(
        REQUEST_METHOD,
        REQUEST_PATH,
        body=REQUEST_BODY,
        headers=_request_headers(framework),
    )
    response = connection.getresponse()
    body = response.read()
    headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    if response.status != 200:
        raise RuntimeError(f"{framework} answered {response.status}: {body[:240]!r}")
    expected_coding = "dcz" if framework == "wreath-optimal" else "gzip"
    if headers.get("content-encoding") != expected_coding:
        raise RuntimeError(
            f"{framework} did not emit the expected response coding: "
            f"{headers.get('content-encoding')!r}"
        )
    document = (
        _dcz_decompress(body, _DICTIONARY, max_output_bytes=100_000)
        if expected_coding == "dcz"
        else gzip.decompress(body)
    )
    missing = [token for token in _RESPONSE_TOKENS if token not in document]
    if missing:
        raise RuntimeError(f"{framework} response omitted equivalent facts: {missing!r}")
    expected_headers = {
        "access-control-allow-origin": "https://example.com",
        "cache-control": "private, no-store",
        "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
        "permissions-policy": "geolocation=()",
    }
    mismatches = {
        name: (expected, headers.get(name))
        for name, expected in expected_headers.items()
        if headers.get(name) != expected
    }
    if mismatches:
        raise RuntimeError(f"{framework} response policy mismatch: {mismatches!r}")
    if "wreath_state=" not in headers.get("set-cookie", ""):
        raise RuntimeError(f"{framework} did not persist the equivalent session mutation")
    if framework == "wreath-optimal":
        vary = {part.strip().lower() for part in headers.get("vary", "").split(",")}
        if not {"accept-encoding", "available-dictionary"} <= vary:
            raise RuntimeError(f"DCZ response has an unsafe Vary header: {vary!r}")

        # The same configured path remains useful to every ordinary gzip client.
        connection = HTTPSConnection("127.0.0.1", port, timeout=10, context=context)
        gzip_headers = dict(REQUEST_HEADERS)
        connection.request(REQUEST_METHOD, REQUEST_PATH, body=REQUEST_BODY, headers=gzip_headers)
        fallback = connection.getresponse()
        fallback_body = fallback.read()
        connection.close()
        if fallback.getheader("content-encoding") != "gzip":
            raise RuntimeError("optimal Wreath configuration lost its gzip fallback")
        if gzip.decompress(fallback_body) != document:
            raise RuntimeError("fragment gzip fallback changed the response bytes")
        first = __import__("zlib").decompressobj(wbits=31)
        first.decompress(fallback_body)
        if not first.eof or not first.unused_data.startswith(b"\x1f\x8b"):
            raise RuntimeError("prepared fragment gzip path was not exercised")

        mismatch_headers = _request_headers(framework)
        mismatch_headers["Available-Dictionary"] = ":AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=:"
        connection = HTTPSConnection("127.0.0.1", port, timeout=10, context=context)
        connection.request(
            REQUEST_METHOD, REQUEST_PATH, body=REQUEST_BODY, headers=mismatch_headers
        )
        mismatch = connection.getresponse()
        mismatch_body = mismatch.read()
        connection.close()
        if mismatch.getheader("content-encoding") != "gzip":
            raise RuntimeError("a mismatched dictionary did not take the gzip fallback")
        if gzip.decompress(mismatch_body) != document:
            raise RuntimeError("dictionary-mismatch fallback changed the response bytes")


def _event_name(value: str) -> str:
    value = value.removeprefix("cpu_core/").removeprefix("cpu_atom/")
    return value.removesuffix(":u").removesuffix("/u")


def _parse_counters(stderr: str, events: dict[str, str]) -> dict[str, int]:
    by_event = {_event_name(event): name for name, event in events.items()}
    counters: dict[str, int] = {}
    for line in stderr.splitlines():
        fields = line.split(";")
        if len(fields) <= 2:
            continue
        event = _event_name(fields[2])
        name = by_event.get(event)
        if name is None:
            continue
        value = fields[0].strip()
        if value.isdigit():
            counters[name] = counters.get(name, 0) + int(value)
    missing = [events[name] for name in events.keys() - counters.keys()]
    if missing:
        raise RuntimeError(
            f"perf emitted no counts for {', '.join(missing)}; this benchmark "
            f"requires those userspace hardware events on the selected CPU:\n{stderr}"
        )
    return counters


def _parse_smaps_rollup(contents: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in contents.splitlines():
        name, separator, rest = line.partition(":")
        if separator and name in {"Pss", "Rss"}:
            amount, unit, *_unused = rest.split()
            if unit != "kB" or not amount.isdigit():
                raise RuntimeError(f"unexpected smaps_rollup field: {line!r}")
            values[f"{name.lower()}_bytes"] = int(amount) * 1024
    missing = {"pss_bytes", "rss_bytes"} - values.keys()
    if missing:
        raise RuntimeError(f"smaps_rollup omitted {', '.join(sorted(missing))}")
    return values


def _process_tree_pids(root_pid: int) -> tuple[int, ...]:
    pending = [root_pid]
    found: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text(
                encoding="ascii"
            )
        except OSError:
            continue
        pending.extend(int(child) for child in children.split())
    return tuple(sorted(found))


def _process_tree_memory(root_pid: int) -> dict[str, int]:
    totals = {"pss_bytes": 0, "rss_bytes": 0}
    measured = 0
    for pid in _process_tree_pids(root_pid):
        try:
            values = _parse_smaps_rollup(
                Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="ascii")
            )
        except OSError:
            continue
        measured += 1
        for name in totals:
            totals[name] += values[name]
    if measured == 0:
        raise RuntimeError(f"server process tree rooted at {root_pid} disappeared")
    return totals


def _derive_metrics(counters: dict[str, float]) -> dict[str, float]:
    if counters.keys() == {"instructions"}:
        return counters
    prefetch_misses = counters["l2_prefetch_hits_l3"] + counters["l2_prefetch_misses_l3"]
    return {
        "instructions": counters["instructions"],
        "l1d_hits": counters["l1d_accesses"] - counters["l1d_misses"],
        "l1d_misses": counters["l1d_misses"],
        "l1i_hits": counters["l1i_accesses"] - counters["l1i_misses"],
        "l1i_misses": counters["l1i_misses"],
        "l2_demand_hits": counters["l2_demand_hits"],
        "l2_demand_misses": counters["l2_demand_misses"],
        "l2_prefetch_hits": counters["l2_prefetch_hits"],
        "l2_prefetch_misses": prefetch_misses,
        "l2_all_hits": counters["l2_demand_hits"] + counters["l2_prefetch_hits"],
        "l2_all_misses": counters["l2_demand_misses"] + prefetch_misses,
    }


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
                "the holistic benchmark requires exactly one"
            )
        time.sleep(0.02)
    raise RuntimeError(f"Granian manager {server_pid} did not spawn its worker")


def _summary(samples: list[float]) -> dict[str, float | list[float]]:
    median = statistics.median(samples)
    return {
        "median": median,
        "mad": statistics.median(abs(sample - median) for sample in samples),
        "range": [min(samples), max(samples)],
        "samples": samples,
    }


def _one_count(
    framework: str,
    requests: int,
    connections: int,
    warmup: int,
    server_cpu: int,
    generator_cpu: int,
    certificate: Path,
    key: Path,
    events: dict[str, str],
) -> dict[str, int]:
    port = _available_port()
    env = _environment(framework)
    server = subprocess.Popen(
        _server_command(framework, port, server_cpu, certificate, key),
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_ready(server, port)
        _verify(port, framework)
        perf_pid = _perf_pid(framework, server.pid)
        generator_command = ["taskset", "-c", str(generator_cpu), str(PYTHON), "-c"]
        warmup_code = (
            "from benchmarks.bench_holistic_stack_instructions import _drive;"
            f"_drive({framework!r},{port},{warmup},{connections})"
        )
        subprocess.run(
            [*generator_command, warmup_code],
            cwd=ROOT,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        perf_command = ["perf", "stat", "--no-big-num", "-x", ";"]
        for event in events.values():
            perf_command.extend(("-e", event))
        perf = subprocess.Popen(
            [*perf_command, "-p", str(perf_pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.1)
            measured_code = (
                "from benchmarks.bench_holistic_stack_instructions import _drive;"
                f"_drive({framework!r},{port},{requests},{connections})"
            )
            subprocess.run(
                [*generator_command, measured_code],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            perf.send_signal(signal.SIGINT)
        _stdout, stderr = perf.communicate(timeout=10)
        return _parse_counters(stderr, events)
    finally:
        _stop_server(server, framework)


def _stop_server(server: subprocess.Popen[bytes], framework: str) -> None:
    server.terminate()
    try:
        timeout = 1 if framework in {"fastapi", "sanic", "blacksheep"} else 10
        server.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)


def _memory_lifecycle(
    framework: str,
    requests: int,
    connections: int,
    warmup: int,
    server_cpu: int,
    generator_cpu: int,
    certificate: Path,
    key: Path,
    sample_interval: float,
) -> dict[str, dict[str, int]]:
    port = _available_port()
    env = _environment(framework)
    server = subprocess.Popen(
        _server_command(framework, port, server_cpu, certificate, key),
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_ready(server, port)
        stages = {"ready": _process_tree_memory(server.pid)}
        _verify(port, framework)
        stages["verified"] = _process_tree_memory(server.pid)
        generator_command = ["taskset", "-c", str(generator_cpu), str(PYTHON), "-c"]
        warmup_code = (
            "from benchmarks.bench_holistic_stack_instructions import _drive;"
            f"_drive({framework!r},{port},{warmup},{connections})"
        )
        subprocess.run(
            [*generator_command, warmup_code],
            cwd=ROOT,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        stages["warmed"] = _process_tree_memory(server.pid)
        peak = dict(stages["warmed"])
        measured_code = (
            "from benchmarks.bench_holistic_stack_instructions import _drive;"
            f"_drive({framework!r},{port},{requests},{connections})"
        )
        generator = subprocess.Popen(
            [*generator_command, measured_code],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        while generator.poll() is None:
            current = _process_tree_memory(server.pid)
            for name, value in current.items():
                peak[name] = max(peak[name], value)
            time.sleep(sample_interval)
        _stdout, generator_error = generator.communicate()
        if generator.returncode:
            raise RuntimeError(
                f"memory load generator exited {generator.returncode}: "
                f"{generator_error.decode(errors='replace')}"
            )
        stages["retained"] = _process_tree_memory(server.pid)
        for name, value in stages["retained"].items():
            peak[name] = max(peak[name], value)
        stages["observed_peak"] = peak
        return stages
    finally:
        _stop_server(server, framework)


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


def _counter_groups(profile: str) -> tuple[tuple[str, dict[str, str]], ...]:
    if profile == "instructions":
        return INSTRUCTION_COUNTER_GROUPS
    if profile == "amd-cache":
        return COUNTER_GROUPS
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
    except OSError:
        cpuinfo = ""
    return COUNTER_GROUPS if "AuthenticAMD" in cpuinfo else INSTRUCTION_COUNTER_GROUPS


def _self_signed_certificate(directory: Path) -> tuple[Path, Path]:
    """Create one short-lived certificate shared by every benchmark arm."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(private_key, hashes.SHA256())
    )
    certificate_path = directory / "certificate.pem"
    key_path = directory / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", choices=FRAMEWORKS, action="append")
    parser.add_argument("--arm", choices=ARMS, action="append")
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=16)
    parser.add_argument("--server-cpu", type=int, default=8)
    parser.add_argument("--generator-cpu", type=int, default=0)
    parser.add_argument(
        "--counter-profile",
        choices=("auto", "instructions", "amd-cache"),
        default="auto",
    )
    parser.add_argument(
        "--memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="measure the server process-tree PSS and RSS in separate lifecycle runs",
    )
    parser.add_argument("--memory-sample-ms", type=float, default=2.0)
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
    if args.memory_sample_ms <= 0:
        parser.error("memory sample interval must be positive")

    selected_frameworks = args.framework or list(FRAMEWORKS)
    selected_arms = args.arm or list(ARMS)
    counter_groups = _counter_groups(args.counter_profile)
    counter_names = {name for _group, events in counter_groups for name in events}
    metric_descriptions = {
        "instructions": "retired userspace instructions per successful request",
        "l1d_hits": "L1 data-cache load hits per successful request",
        "l1d_misses": "L1 data-cache load misses per successful request",
        "l1i_hits": "L1 instruction-cache load hits per successful request",
        "l1i_misses": "L1 instruction-cache load misses per successful request",
        "l2_demand_hits": "demand instruction/data requests hitting L2 per successful request",
        "l2_demand_misses": (
            "demand instruction/data requests missing L2 per successful request"
        ),
        "l2_prefetch_hits": "L2 prefetch requests hitting L2 per successful request",
        "l2_prefetch_misses": "L2 prefetch requests missing L2 per successful request",
        "l2_all_hits": "demand plus prefetch L2 hits per successful request",
        "l2_all_misses": "demand plus prefetch L2 misses per successful request",
    }
    derived_names = (
        {"instructions"}
        if counter_names == {"instructions"}
        else set(metric_descriptions)
    )
    document: dict[str, Any] = {
        "schema": "wreath/e2e-holistic-stack-counters/5",
        "recorded": time.strftime("%Y-%m-%d"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "metric": "retired userspace instructions per successful request",
        "metrics": {
            name: metric_descriptions[name]
            for name in metric_descriptions
            if name in derived_names
        },
        "memory_metrics": {
            "pss_bytes": "summed process-tree proportional set size in bytes",
            "rss_bytes": (
                "summed process-tree resident set size in bytes; shared mappings count "
                "once in every process"
            ),
        },
        "memory_limitations": (
            "Observed peaks use repeated procfs snapshots with the configured sleep "
            "between complete process-tree scans; a shorter-lived peak may be missed."
        ),
        "transport": "TLS 1.3 over HTTP/1.1 for every arm",
        "method": (
            "median of alternating N/N2 process slopes; fixed startup, imports, "
            "dependency construction, warmup and perf attachment cancel"
        ),
        "request": {
            "method": REQUEST_METHOD,
            "path": REQUEST_PATH,
            "headers": REQUEST_HEADERS,
            "body": json.loads(REQUEST_BODY),
            "verified_response_facts": [token.decode() for token in _RESPONSE_TOKENS],
        },
        "compression_configurations": {
            "wreath": "format-aware native gzip",
            "wreath-optimal": {
                "selected": "RFC 9842 dcz",
                "dictionary": "real response for neighbouring resource 41",
                "dictionary_sha256": hashlib.sha256(_DICTIONARY).hexdigest(),
                "fallback": "format-aware prepared fragment gzip",
                "fragment": "249-byte dynamic prefix plus cached stable suffix",
            },
            "fastapi": "Starlette gzip backed by zlib",
            "sanic": "direct stdlib gzip on Sanic's native server",
            "blacksheep": "direct stdlib gzip on Granian ASGI",
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
            "counter_profile": (
                "amd-cache" if counter_groups == COUNTER_GROUPS else "instructions"
            ),
            "counter_groups": {name: list(events.values()) for name, events in counter_groups},
            "multiplexed": False,
            "memory": {
                "enabled": args.memory,
                "trials": args.trials if args.memory else 0,
                "requests": args.requests,
                "sample_interval_ms": args.memory_sample_ms,
                "process_scope": "server root and every descendant",
                "collector": "/proc/<pid>/smaps_rollup",
                "counter_pass_separate": True,
            },
        },
        "packages": _versions(),
        "arms": {},
        "memory": {},
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
        document["memory"].update(previous.get("memory", {}))
    with tempfile.TemporaryDirectory(prefix="wreath-holistic-") as directory:
        certificate, key = _self_signed_certificate(Path(directory))
        for framework in selected_frameworks:
            rows: dict[str, Any] = {}
            for arm in selected_arms:
                samples: dict[str, list[float]] = {}
                for trial in range(args.trials):
                    order = ("high", "low") if trial % 2 == 0 else ("low", "high")
                    slopes: dict[str, float] = {}
                    trial_counter_groups = (
                        counter_groups
                        if trial % 2 == 0
                        else tuple(reversed(counter_groups))
                    )
                    for _group_name, events in trial_counter_groups:
                        totals: dict[str, dict[str, int]] = {}
                        for size_name in order:
                            count = args.requests if size_name == "high" else args.requests // 2
                            totals[size_name] = _one_count(
                                framework,
                                count,
                                args.connections,
                                args.warmup,
                                args.server_cpu,
                                args.generator_cpu,
                                certificate,
                                key,
                                events,
                            )
                        denominator = args.requests - args.requests // 2
                        slopes.update(
                            {
                                name: (totals["high"][name] - totals["low"][name]) / denominator
                                for name in events
                            }
                        )
                    metrics = _derive_metrics(slopes)
                    for name, value in metrics.items():
                        samples.setdefault(name, []).append(value)
                    message = (
                        f"{framework:14s} {arm:11s} {trial + 1}/{args.trials}: "
                        f"{metrics['instructions']:,.1f} instructions/request"
                    )
                    if "l1d_hits" in metrics:
                        message += (
                            f", L1D {metrics['l1d_hits']:,.1f}/{metrics['l1d_misses']:,.1f}, "
                            f"L1I {metrics['l1i_hits']:,.1f}/{metrics['l1i_misses']:,.1f}, "
                            f"L2 demand {metrics['l2_demand_hits']:,.1f}/"
                            f"{metrics['l2_demand_misses']:,.1f}, prefetch "
                            f"{metrics['l2_prefetch_hits']:,.1f}/"
                            f"{metrics['l2_prefetch_misses']:,.1f} hits/misses per request"
                        )
                    print(message, flush=True)
                counters = {name: _summary(values) for name, values in samples.items()}
                rows[arm] = {**counters["instructions"], "counters": counters}
            if "holistic" in rows and "holistic-aa" in rows:
                rows["holistic-aa"]["absolute_delta_from_holistic"] = abs(
                    rows["holistic-aa"]["median"] - rows["holistic"]["median"]
                )
            document["arms"][framework] = rows

        if args.memory:
            samples: dict[str, dict[str, dict[str, list[float]]]] = {}
            for trial in range(args.trials):
                frameworks = (
                    selected_frameworks
                    if trial % 2 == 0
                    else list(reversed(selected_frameworks))
                )
                for framework in frameworks:
                    stages = _memory_lifecycle(
                        framework,
                        args.requests,
                        args.connections,
                        args.warmup,
                        args.server_cpu,
                        args.generator_cpu,
                        certificate,
                        key,
                        args.memory_sample_ms / 1_000,
                    )
                    framework_samples = samples.setdefault(framework, {})
                    for stage, values in stages.items():
                        stage_samples = framework_samples.setdefault(stage, {})
                        for name, value in values.items():
                            stage_samples.setdefault(name, []).append(float(value))
                    peak = stages["observed_peak"]
                    print(
                        f"{framework:14s} memory {trial + 1}/{args.trials}: "
                        f"peak PSS {peak['pss_bytes'] / (1024 * 1024):.2f} MiB, "
                        f"RSS {peak['rss_bytes'] / (1024 * 1024):.2f} MiB",
                        flush=True,
                    )
            document["memory"].update({
                framework: {
                    stage: {
                        name: _summary(values) for name, values in stage_samples.items()
                    }
                    for stage, stage_samples in framework_samples.items()
                }
                for framework, framework_samples in samples.items()
            })

    document["historical_control"] = {
        "previous_wreath_gzip_backend": "zlib",
        "previous_recorded": _ZLIB_BACKED_RECORDED,
        "previous_wreath_instructions_per_request": _ZLIB_BACKED_WREATH,
        "transport": "cleartext HTTP/1.1",
        "directly_comparable_to_current_tls_matrix": False,
    }

    document["fairness"] = (
        "All five arms receive the same nested operations-dashboard POST over TLS and are "
        "accepted only after matching security, CORS, session, compression and business "
        "response facts. All perform bearer and Cedar authorization, one PostgreSQL and "
        "one overlapping HTTP wire round trip, eleven requested chart rows projected "
        "from a sparse 730 x 48 x 6 source, eleven chart paths, temporal, geospatial "
        "and vector calculations, ranked "
        "pagination, protobuf and MessagePack exports, escaped templates and compressed HTML. "
        "Wreath uses its built-in native/declarative surfaces. FastAPI uses Starlette, "
        "Pydantic and Uvicorn. Sanic uses its native server; BlackSheep uses Granian's "
        "single-threaded ASGI runtime with uvloop. The three ecosystem arms use cedarpy, "
        "asyncpg, aiohttp, NumPy, Jinja, protobuf and msgspec, and the Sanic/BlackSheep "
        "pair shares the same typed business kernel. The "
        "algorithms are idiomatic implementations with equivalent output contracts, not "
        "byte-identical internal representations. Every request rebuilds the dense chart "
        "projection and its paths in every arm; no arm reuses a final projection result. "
        "Real driver protocols terminate at the "
        "same deterministic in-process peers. No external database, network, DNS, disk, "
        "wall clock, cycles or IPC enters the result. Hardware counters are never "
        "multiplexed. PSS and RSS are collected from separate fresh processes over the "
        "whole server process tree, so procfs reads cannot perturb those counters. The "
        "Wreath optimal arm serves RFC "
        "9842 dcz only after an exact Available-Dictionary hash match and retains a "
        "standards-compatible prepared fragment-gzip fallback for ordinary clients."
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
