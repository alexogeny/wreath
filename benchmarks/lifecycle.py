"""End-to-end full-lifecycle benchmark against a fresh podman PostgreSQL per run.

Scenario: an authenticated admin issues a mutation on a user row. Every measured
request performs the complete lifecycle -- HTTP parse, a deep match in a
385-route tree, bearer-token authentication against the database (one SELECT),
an ``admin`` role check, JSON body decode, one UPDATE .. RETURNING on the target
user, and a JSON response with a CSP and five related security headers.

Fairness contract (enforced by this runner):

- Each framework gets its own freshly created PostgreSQL container from the same
  image with the same tuning flags, seeded with an identical schema and rows.
- Every application executes the same SQL with the same per-request operations
  and the same pool size (see ``benchmarks/lifecycle_apps.py``).
- Identical warmup and measured request counts, concurrency, load generator, and
  event-loop policy (uvloop when installed, mirroring ``benchmarks.run``).
- Before measuring, the runner probes each server for a correct 200 mutation
  response and a 403 rejection of a valid non-admin token. After measuring, it
  table back and verifies the number of recorded mutations matches the number of
  authorized requests served, so no framework can win by skipping work.
- Every application emits the same four security headers plus a CSP. HSTS is not
  among them: the scenario is cleartext, where emitting it is wrong.

Run it with:

    uv sync --group benchmark
    uv run python -m benchmarks.lifecycle

Results land in ``benchmark-results-lifecycle/`` as timestamped + ``latest``
JSON/HTML, in the same row format as the main suite.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .lifecycle_routes import ROUTE_BRANCHES, ROUTES_PER_BRANCH, request_path
from .load import LOAD_GENERATOR, LOAD_GENERATOR_VERSION, measure
from .report import generate_report
from .run import _available_port, _wait_until_ready

LIFECYCLE_FRAMEWORKS = ("wreath-native", "wreath-metal", "wreath", "sanic", "blacksheep")
SCENARIO = "lifecycle-admin-mutation"
ADMIN_TOKEN = "admin-token-lifecycle"
NON_ADMIN_TOKEN = "user-token-2"
TARGET_USER_ID = 42
REQUEST_PATH = request_path(7, TARGET_USER_ID)
REQUEST_BODY = b'{"name":"updated-by-admin"}'
EXPECTED_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    # No HSTS: this scenario runs over cleartext HTTP, and RFC 6797 says a
    # sender should not emit Strict-Transport-Security over a non-secure
    # transport and a browser must ignore it if it arrives. Wreath's
    # SecurityHeadersMiddleware implements that (it emits HSTS only when
    # scheme == "https"), so requiring the header here skipped both Wreath arms
    # while Sanic and BlackSheep passed by emitting it unconditionally -- which
    # is both wrong and one header of extra work Wreath was not doing. Dropping it
    # from the scenario is what makes the frameworks comparable again.
}
REQUEST_HEADERS = (
    ("Authorization", f"Bearer {ADMIN_TOKEN}"),
    ("Content-Type", "application/json"),
)

CREATE_SQL = (
    "create table bench_users ("
    "id int4 primary key, username text not null, full_name text not null, "
    "role text not null, token text not null unique, version int4 not null)"
)
ADMIN_ROW_SQL = (
    "insert into bench_users values "
    f"(1, 'admin', 'Administrator', 'admin', '{ADMIN_TOKEN}', 0)"
)
SEED_SQL = (
    "insert into bench_users "
    "select value, 'user-' || value, 'Benchmark User ' || value, 'user', "
    "'user-token-' || value, 0 from generate_series(2, {rows}) as value"
)
MUTATION_COUNT_SQL = "select coalesce(sum(version), 0)::int8 from bench_users"


def _start_container(image: str, host: str, port: int, name: str) -> None:
    subprocess.run(
        [
            "podman", "run", "--rm", "--detach", "--name", name,
            "--publish", f"{host}:{port}:5432",
            "--env", "POSTGRES_USER=wreath",
            "--env", "POSTGRES_PASSWORD=secret",
            "--env", "POSTGRES_DB=wreath",
            image,
            # Identical durability tuning for every run so write scenarios
            # compare framework/driver CPU instead of host-disk fsync latency.
            "-c", "fsync=off",
            "-c", "synchronous_commit=off",
            "-c", "full_page_writes=off",
        ],
        check=True,
        capture_output=True,
    )


def _remove_container(name: str) -> None:
    subprocess.run(
        ["podman", "rm", "--force", "--time", "2", name],
        check=False,
        capture_output=True,
    )


async def _wait_for_postgres(dsn: str, ready_timeout: float) -> None:
    from wreath.postgres import connect

    deadline = time.monotonic() + ready_timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            connection = await asyncio.wait_for(connect(dsn), timeout=5)
        except Exception as error:  # noqa: BLE001 - startup races surface many types
            last_error = error
            await asyncio.sleep(0.2)
            continue
        try:
            await connection.fetchval("select 1")
            return
        except Exception as error:  # noqa: BLE001
            last_error = error
            await asyncio.sleep(0.2)
        finally:
            await connection.close()
    raise TimeoutError(
        f"PostgreSQL did not become ready within {ready_timeout:.0f}s: {last_error}"
    )


async def _seed_database(dsn: str, rows: int) -> str:
    from wreath.postgres import connect

    connection = await connect(dsn)
    try:
        version = await connection.fetchval("select version()")
        await connection.execute("drop table if exists bench_users")
        await connection.execute(CREATE_SQL)
        await connection.execute(ADMIN_ROW_SQL)
        await connection.execute(SEED_SQL.format(rows=rows))
        await connection.execute("vacuum analyze bench_users")
        return str(version)
    finally:
        await connection.close()


async def _recorded_mutations(dsn: str) -> int:
    from wreath.postgres import connect

    connection = await connect(dsn)
    try:
        return int(await connection.fetchval(MUTATION_COUNT_SQL))
    finally:
        await connection.close()


def _http_probe(
    host: str, port: int, token: str | None, body: bytes
) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request("POST", REQUEST_PATH, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, payload, headers
    finally:
        connection.close()


async def _verify_endpoint(host: str, port: int, framework: str) -> tuple[int, int]:
    """Probe one correct mutation and one authenticated, non-admin denial."""
    status, payload, headers = await asyncio.to_thread(
        _http_probe, host, port, ADMIN_TOKEN, REQUEST_BODY
    )
    if status != 200:
        raise RuntimeError(f"{framework}: admin mutation returned HTTP {status}")
    decoded = json.loads(payload)
    expected_name = json.loads(REQUEST_BODY)["name"]
    if (
        decoded.get("id") != TARGET_USER_ID
        or decoded.get("name") != expected_name
        or not isinstance(decoded.get("version"), int)
        or decoded["version"] < 1
    ):
        raise RuntimeError(f"{framework}: wrong mutation response payload: {decoded!r}")
    wrong_headers = {
        name: (headers.get(name), expected)
        for name, expected in EXPECTED_SECURITY_HEADERS.items()
        if headers.get(name) != expected
    }
    if wrong_headers:
        raise RuntimeError(
            f"{framework}: missing or incorrect security headers: {wrong_headers!r}"
        )

    denied_status, _, denied_headers = await asyncio.to_thread(
        _http_probe, host, port, NON_ADMIN_TOKEN, REQUEST_BODY
    )
    if denied_status != 403:
        raise RuntimeError(
            f"{framework}: non-admin token was not rejected (HTTP {denied_status})"
        )
    if any(
        denied_headers.get(name) != value
        for name, value in EXPECTED_SECURITY_HEADERS.items()
    ):
        raise RuntimeError(f"{framework}: denied response omitted security headers")
    return 1, denied_status  # one authorized mutation performed, deny status


def _server_command(
    framework: str, host: str, port: int, loop: str, http_impl: str
) -> tuple[list[str], str]:
    if framework in ("wreath-native", "wreath-metal"):
        if framework == "wreath-metal":
            # The metal tier is defined by its loop; it always runs the reactor.
            native_loop = "metal"
        elif loop == "auto":
            # Mirror uvicorn's `--loop auto` so every stack shares one policy.
            try:
                import uvloop  # noqa: F401

                native_loop = "uvloop"
            except ImportError:
                native_loop = "asyncio"
        else:
            native_loop = loop
        command = [
            sys.executable, "-m", "benchmarks.wreath_server",
            "--host", host, "--port", str(port),
            "--loop", native_loop,
            "--app", "benchmarks.lifecycle_apps:app",
        ]
        label = "wreath-metal" if framework == "wreath-metal" else f"wreath-native ({native_loop})"
        return command, label
    if framework == "sanic":
        command = [
            sys.executable, "-m", "benchmarks.sanic_server",
            "--host", host, "--port", str(port),
            "--app", "benchmarks.lifecycle_apps",
        ]
        return command, "sanic-native"
    command = [
        sys.executable, "-m", "uvicorn", "benchmarks.lifecycle_apps:app",
        "--host", host, "--port", str(port),
        "--loop", loop, "--http", http_impl,
        "--lifespan", "off", "--no-access-log",
    ]
    return command, "uvicorn"


async def _run_framework(args: argparse.Namespace, framework: str) -> dict[str, object]:
    import os

    db_port = _available_port(args.host)
    container = f"wreath-bench-lifecycle-{framework}-{db_port}"
    dsn = f"postgresql://wreath:secret@{args.host}:{db_port}/wreath"
    db_started = time.perf_counter()
    print(f"[db] {framework}: starting fresh {args.image} on port {db_port}", flush=True)
    await asyncio.to_thread(_start_container, args.image, args.host, db_port, container)
    process: subprocess.Popen[bytes] | None = None
    try:
        await _wait_for_postgres(dsn, args.db_timeout)
        postgres_version = await _seed_database(dsn, args.rows)
        db_startup_seconds = time.perf_counter() - db_started

        port = _available_port(args.host)
        command, server = _server_command(framework, args.host, port, args.loop, args.http)
        env = os.environ.copy()
        env["WREATH_BENCH_FRAMEWORK"] = framework
        env["WREATH_BENCH_DSN"] = dsn
        env["WREATH_BENCH_DB_POOL"] = str(args.concurrency)
        process = subprocess.Popen(
            command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        await asyncio.to_thread(_wait_until_ready, process, args.host, port)

        probe_mutations, denied_status = await _verify_endpoint(args.host, port, framework)
        print(
            f"[verify] {framework}: mutation 200 OK, non-admin token rejected "
            f"with {denied_status}",
            flush=True,
        )

        print(
            f"[start] {framework}/{SCENARIO} [POST]: {args.warmup_requests:,} warmup + "
            f"{args.requests:,} measured requests",
            flush=True,
        )

        def show_progress(completed: int, total: int, elapsed: float) -> None:
            percent = completed / total * 100 if total else 100.0
            rate = completed / elapsed if elapsed else 0.0
            print(
                f"[progress] {framework}/{SCENARIO}: {completed:,}/{total:,} "
                f"({percent:5.1f}%) {rate:,.0f} req/s",
                flush=True,
            )

        result = await measure(
            args.host,
            port,
            REQUEST_PATH,
            0.0,
            0.0,
            args.concurrency,
            args.requests,
            args.warmup_requests,
            show_progress,
            "POST",
            REQUEST_BODY,
            REQUEST_HEADERS,
        )

        mutations_expected = probe_mutations + args.warmup_requests + result.requests
        mutations_recorded = await _recorded_mutations(dsn)
        mutations_verified = mutations_recorded == mutations_expected and result.errors == 0
        verdict = "OK" if mutations_verified else "MISMATCH"
        print(
            f"[verify] {framework}: {mutations_recorded} mutations recorded in the "
            f"database, {mutations_expected} expected -> {verdict}",
            flush=True,
        )

        return {
            "framework": framework,
            "server": server,
            "scenario": SCENARIO,
            "method": "POST",
            "path": REQUEST_PATH,
            "protocol": "http/1.1",
            "transport": "tcp",
            "secure": False,
            "alpn": None,
            "connections": args.concurrency,
            "max_streams_per_connection": 1,
            "trial": 1,
            "load_generator": LOAD_GENERATOR,
            "load_generator_version": LOAD_GENERATOR_VERSION,
            "server_tls_version": None,
            "database": "postgresql (podman, fresh per run)",
            "database_image": args.image,
            "database_version": postgres_version,
            "database_driver": "wreath-postgres" if framework.startswith("wreath") else "asyncpg",
            "database_pool_size": args.concurrency,
            "database_startup_seconds": db_startup_seconds,
            "auth_denied_status": denied_status,
            "route_count": ROUTE_BRANCHES * ROUTES_PER_BRANCH + 1,
            "prunable_route_branches": ROUTE_BRANCHES - 1,
            "security_headers_verified": True,
            "mutations_expected": mutations_expected,
            "mutations_recorded": mutations_recorded,
            "mutations_verified": mutations_verified,
            "normalized_100k_seconds": (
                result.duration_seconds * 100_000 / max(1, result.requests + result.errors)
            ),
            **asdict(result),
        }
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.returncode not in (0, -15) and process.stderr is not None:
                error = process.stderr.read().decode("utf-8", errors="replace")
                if error:
                    print(error, file=sys.stderr)
        await asyncio.to_thread(_remove_container, container)


async def run(args: argparse.Namespace) -> None:
    suite_started = time.perf_counter()
    started_at = datetime.now(UTC)
    output_directory = Path(args.output)
    output_directory.mkdir(parents=True, exist_ok=True)
    run_name = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_path = output_directory / f"{run_name}.json"
    report_path = output_directory / f"{run_name}.html"
    rows: list[dict[str, object]] = []
    skipped_frameworks: list[str] = []
    metadata: dict[str, Any] = {
        "timestamp": started_at.isoformat(),
        "status": "running",
        "suite": "full-lifecycle-database",
        "python": sys.version,
        "platform": platform.platform(),
        "server": "uvicorn except wreath-native and sanic-native",
        "loop": args.loop,
        "http": args.http,
        "host": args.host,
        "concurrency": args.concurrency,
        "requests_per_framework": args.requests,
        "warmup_requests": args.warmup_requests,
        "database": "one fresh podman PostgreSQL container per framework run",
        "database_image": args.image,
        "database_rows": args.rows,
        "database_tuning": "fsync=off synchronous_commit=off full_page_writes=off",
        "routing": (
            f"deep target in {ROUTE_BRANCHES} branches x {ROUTES_PER_BRANCH} decoy "
            "leaves; Wreath sibling branches are capability-prunable"
        ),
        "route_count": ROUTE_BRANCHES * ROUTES_PER_BRANCH + 1,
        "per_request_work": (
            "deep protected route match + bearer auth SELECT + admin role check + "
            "JSON body decode + UPDATE .. RETURNING + JSON response + six "
            "browser security headers"
        ),
        "fairness": (
            "identical SQL, pool size, seed data, warmup, and load generator; "
            "responses probed for correctness and mutation counts verified "
            "against the database after each run"
        ),
        "suite_end_to_end_seconds": 0.0,
        "completed_scenarios": 0,
        "total_scenarios": len(args.framework),
        "skipped_frameworks": skipped_frameworks,
        "load_generator": "wreath-stdlib-development-client",
    }
    document: dict[str, Any] = {"metadata": metadata, "results": rows}

    def persist() -> None:
        metadata["suite_end_to_end_seconds"] = time.perf_counter() - suite_started
        payload = json.dumps(document, indent=2) + "\n"
        output_path.write_text(payload, encoding="utf-8")
        (output_directory / "latest.json").write_text(payload, encoding="utf-8")
        generate_report(document, report_path)
        generate_report(document, output_directory / "latest.html")

    persist()
    for framework in args.framework:
        try:
            row = await _run_framework(args, framework)
        except Exception as error:  # noqa: BLE001 - record and continue the suite
            print(f"[skip] {framework}: {error}", flush=True)
            skipped_frameworks.append(framework)
            persist()
            continue
        rows.append(row)
        metadata["completed_scenarios"] = len(rows)
        persist()
        print(
            f"[done] {framework:10} {SCENARIO} "
            f"{row['requests_per_second']:10.0f} req/s "
            f"median={row['latency_ms_median']:8.3f} ms "
            f"p99={row['latency_ms_p99']:8.3f} ms errors={row['errors']}",
            flush=True,
        )

    metadata["status"] = "complete_with_skips" if skipped_frameworks else "complete"
    persist()
    print(f"[report] wrote {output_path}", flush=True)
    print(f"[report] wrote {report_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--framework",
        nargs="+",
        choices=LIFECYCLE_FRAMEWORKS,
        default=list(LIFECYCLE_FRAMEWORKS),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--warmup-requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--rows", type=int, default=1_000, help="seeded bench_users rows")
    parser.add_argument("--image", default="docker.io/library/postgres:17-alpine")
    parser.add_argument("--db-timeout", type=float, default=120.0)
    parser.add_argument("--loop", choices=("auto", "asyncio", "uvloop"), default="auto")
    parser.add_argument("--http", choices=("auto", "h11", "httptools"), default="auto")
    parser.add_argument("--output", default="benchmark-results-lifecycle")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
