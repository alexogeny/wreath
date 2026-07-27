"""Task entry points that install the dependency group they need.

`uv sync` reconciles the venv to exactly the groups it was given and **removes
everything else**. That is the right default for a reproducible install and the
wrong one for switching between jobs: `uv sync --group benchmark` silently
uninstalls mkdocs, then `uv run --group docs mkdocs` uninstalls sanic, and each
tool works only until the next one runs. Every command here has been on the
losing side of that at least once.

So each task ensures its own group with `uv sync --inexact`, which adds without
removing, and then runs the tool:

    uv run wreath-docs                 # build the docs, strictly
    uv run wreath-docs --serve         # ... and watch them
    uv run wreath-bench --framework wreath starlette
    uv run wreath-check                # ruff, ty, pytest, native lints, baseline

`--inexact` is the whole point: `wreath-docs` must not cost you `wreath-bench`.

Nothing here pins or resolves anything itself -- `uv.lock` remains the only
source of versions. These just make sure the group is present before use.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .native_lint import repo_root


def _uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise SystemExit(
            "wreath tasks need the `uv` executable on PATH; see https://docs.astral.sh/uv/"
        )
    return executable


def ensure_groups(*groups: str) -> None:
    """Install `groups` without evicting whatever else is already installed."""
    command = [_uv(), "sync", "--inexact", *(f"--group={group}" for group in groups)]
    result = subprocess.run(command, cwd=repo_root())
    if result.returncode != 0:
        raise SystemExit(
            f"wreath tasks: `{' '.join(command)}` failed; the group is not installed."
        )


def _run(command: list[str], *, cwd: Path | None = None) -> int:
    return subprocess.run(command, cwd=cwd or repo_root()).returncode


def docs(argv: list[str] | None = None) -> int:
    """Build the documentation, strictly. `--serve` watches instead."""
    parser = argparse.ArgumentParser(
        prog="wreath-docs", description="Build (or serve) the documentation."
    )
    parser.add_argument(
        "--serve", action="store_true", help="serve with live reload instead of building"
    )
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="passed to mkdocs")
    args = parser.parse_args(argv)

    ensure_groups("docs")
    if args.serve:
        return _run([sys.executable, "-m", "mkdocs", "serve", *args.rest])
    # --strict, always: a warning that is not an error is a warning nobody reads.
    return _run([sys.executable, "-m", "mkdocs", "build", "--strict", *args.rest])


#: PostgreSQL image and container used by the DB battery. Matches the durability
#: tuning `benchmarks/lifecycle.py` uses so numbers compare CPU, not disk fsync.
_BENCH_PG_IMAGE = "docker.io/library/postgres:17-alpine"
_BENCH_PG_NAME = "wreath-bench-full-pg"
_MATRIX_RESULTS = "benchmark-results"


def _top_freq_cpus() -> set[int]:
    """The logical CPUs in the highest `cpuinfo_max_freq` cluster.

    On a hybrid Intel part that is the P-core set; on a uniform CPU it is every
    core. Returns an empty set when the sysfs interface is unavailable (non-Linux,
    containers), which the caller reads as "do not pin".
    """
    rows: list[tuple[int, int]] = []
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*"):
        try:
            cpu = int(os.path.basename(path)[3:])
            freq = int(Path(f"{path}/cpufreq/cpuinfo_max_freq").read_text())
        except (OSError, ValueError):
            continue
        rows.append((cpu, freq))
    if not rows:
        return set()
    top = max(freq for _, freq in rows)
    return {cpu for cpu, freq in rows if freq == top}


def _resolve_pin(mode: str) -> set[int]:
    """Turn a --pin value (`pcores`, `none`, or a CPU list like `0-11`) into CPUs."""
    if mode == "none":
        return set()
    if mode == "pcores":
        return _top_freq_cpus()
    cpus: set[int] = set()
    for part in mode.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            cpus.update(range(int(low), int(high) + 1))
        else:
            cpus.add(int(part))
    return cpus


def _apply_pin(cpus: set[int]) -> bool:
    """Pin this process (and so every benchmark subprocess it spawns) to `cpus`.

    Child processes inherit the affinity mask, so setting it once here keeps the
    whole battery off the slow E-cores without a `taskset` re-exec.
    """
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return False
    try:
        os.sched_setaffinity(0, cpus)
        return True
    except OSError:
        return False


def _ensure_native_arm(forwarded: list[str]) -> list[str]:
    """Guarantee wreath's own-HTTP arm (`wreath-native`) whenever `wreath` is asked for.

    wreath's own HTTP+JSON stack is the `wreath-native` server (its native
    h1/h2/h3 parser); the plain `wreath` arm runs the same app on uvicorn, so
    httptools frames the HTTP -- that arm is the like-for-like "framework overhead
    on a common server" comparison, kept and labeled by its `server` column. If
    the caller narrowed `--framework` to include `wreath` but not `wreath-native`,
    add the native arm so wreath's own-stack number is never missing. With no
    `--framework`, `benchmarks.run`'s default already runs both.
    """
    if "--framework" not in forwarded:
        return forwarded
    result = list(forwarded)
    start = result.index("--framework") + 1
    end = start
    while end < len(result) and not result[end].startswith("-"):
        end += 1
    values = result[start:end]
    if "wreath" in values and "wreath-native" not in values:
        result.insert(end, "wreath-native")
    return result


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_pg_ready(name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = subprocess.run(
            ["podman", "exec", name, "pg_isready", "-U", "wreath", "-d", "wreath"],
            capture_output=True,
        )
        if ready.returncode == 0:
            return True
        time.sleep(0.5)
    return False


def _db_battery() -> list[Path]:
    """Run every benchmark that needs a live PostgreSQL, returning result JSONs.

    Provisions one throwaway podman container for the ORM and webhook comparisons
    (the full lifecycle benchmark provisions its own, one per framework). Every
    step is best-effort: a missing podman or a failed container degrades to a
    printed skip so a bare `wreath-bench` still completes the matrix.
    """
    produced: list[Path] = []
    if shutil.which("podman") is None:
        print("[skip] podman not found: skipping ORM/webhook/lifecycle DB benchmarks")
        return produced

    port = _free_port()
    dsn = f"postgresql://wreath:secret@127.0.0.1:{port}/wreath"
    subprocess.run(["podman", "rm", "-f", _BENCH_PG_NAME], capture_output=True)
    started = _run([
        "podman", "run", "--rm", "--detach", "--name", _BENCH_PG_NAME,
        "--publish", f"127.0.0.1:{port}:5432",
        "--env", "POSTGRES_USER=wreath", "--env", "POSTGRES_PASSWORD=secret",
        "--env", "POSTGRES_DB=wreath", _BENCH_PG_IMAGE,
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    ])
    if started != 0:
        print("[skip] could not start the benchmark PostgreSQL container")
        return produced

    orm = repo_root() / "benchmark-results-orm-competitors" / "latest.json"
    webhooks = repo_root() / "benchmark-results-webhooks" / "postgres-latest.json"
    try:
        if not _wait_pg_ready(_BENCH_PG_NAME, 45):
            print("[skip] benchmark PostgreSQL did not become ready in time")
            return produced
        if _run([sys.executable, "-m", "benchmarks.postgres.bench_orm_competitors",
                 "--dsn", dsn, "--output", str(orm)]) == 0 and orm.exists():
            produced.append(orm)
        if _run([sys.executable, "-m", "benchmarks.postgres.bench_webhooks",
                 "--dsn", dsn, "--output", str(webhooks)]) == 0 and webhooks.exists():
            produced.append(webhooks)
    finally:
        subprocess.run(["podman", "rm", "-f", _BENCH_PG_NAME], capture_output=True)

    # The full-lifecycle benchmark provisions its own fresh container per
    # framework; it only needs podman, which we just confirmed.
    lifecycle = repo_root() / "benchmark-results-lifecycle" / "latest.json"
    if _run([sys.executable, "-m", "benchmarks.lifecycle"]) == 0 and lifecycle.exists():
        produced.append(lifecycle)
    return produced


def bench(argv: list[str] | None = None) -> int:
    """Run the full Wreath benchmark battery, pinned and repeated, into one report.

    This is the canonical benchmark entry point. With no arguments it:

    * pins itself (and every subprocess) to the P-cores, off the slow E-cores;
    * runs the framework matrix three times so the report has run-to-run ranges
      and can only crown a winner whose worst pass beats the runner-up's best;
    * runs the webhook microbenchmarks, and the ORM, PostgreSQL-webhook, and
      full request-lifecycle database benchmarks (each behind a throwaway
      container); and
    * combines the matrix passes and the lifecycle rows into one self-contained
      HTML report at ``benchmark-results/full-battery.html``.

    It owns a few options and forwards everything else to ``benchmarks.run``, so
    ``wreath-bench --framework wreath starlette`` still narrows the matrix.
    """
    parser = argparse.ArgumentParser(
        prog="wreath-bench",
        description="Run the full Wreath benchmark battery, pinned and repeated.",
    )
    parser.add_argument("--passes", type=int, default=3,
                        help="framework-matrix passes to run and combine (default 3)")
    parser.add_argument("--pin", default="pcores",
                        help="CPUs to pin to: 'pcores' (default), 'none', or a list like '0-11'")
    parser.add_argument("--matrix-only", action="store_true",
                        help="run only the framework matrix (skip webhook and database benches)")
    parser.add_argument("--no-db", action="store_true",
                        help="skip the benchmarks that need podman/PostgreSQL")
    args, forwarded = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    # Pin the server and the load generator to *disjoint* P-cores. If they share
    # cores they steal cycles from each other, and on a hybrid CPU the loser can
    # land on a slow E-core -- worth ~2x and the dominant source of run-to-run
    # noise. We reserve one P-core (two HT threads) for the server via
    # WREATH_BENCH_SERVER_CPUS and pin this process (which hosts the generator)
    # to the rest.
    pcores = sorted(_resolve_pin(args.pin))
    if len(pcores) >= 4 and "WREATH_BENCH_SERVER_CPUS" not in os.environ:
        server_cpus, client_cpus = pcores[:2], set(pcores[2:])
        os.environ["WREATH_BENCH_SERVER_CPUS"] = ",".join(str(c) for c in server_cpus)
        pinned = _apply_pin(client_cpus)
        if pinned:
            print(f"[pin] server -> CPUs {server_cpus}; generator -> {sorted(client_cpus)}")
    else:
        pinned = _apply_pin(set(pcores))
        if pinned:
            print(f"[pin] benchmark tree pinned to CPUs {sorted(os.sched_getaffinity(0))}")
    if not pinned and args.pin != "none":
        print("[pin] no CPU pinning applied (sysfs unavailable or empty selection)")

    forwarded = _ensure_native_arm(forwarded)
    print("[wreath] `wreath-native` is wreath's own HTTP+JSON stack (the headline "
          "wreath number); the `wreath` arm is wreath on uvicorn/httptools, kept as "
          "the common-server framework-overhead comparison and labeled by server.")

    ensure_groups("benchmark")

    results_dir = repo_root() / _MATRIX_RESULTS
    matrix_jsons: list[Path] = []
    passes = max(1, args.passes)
    for index in range(passes):
        print(f"\n=== framework matrix pass {index + 1}/{passes} " + "=" * 30)
        before = set(results_dir.glob("*Z.json")) if results_dir.exists() else set()
        if _run([sys.executable, "-m", "benchmarks.run", *forwarded]) != 0:
            print("wreath-bench: a matrix pass failed; aborting.")
            return 1
        fresh = sorted(
            set(results_dir.glob("*Z.json")) - before, key=lambda p: p.stat().st_mtime
        )
        if fresh:
            matrix_jsons.append(fresh[-1])

    report_inputs = list(matrix_jsons)
    if not args.matrix_only:
        print("\n=== webhook microbenchmarks " + "=" * 30)
        _run([sys.executable, "-m", "benchmarks.bench_webhooks",
              "--output", "benchmark-results-webhooks/latest.json"])
        _run([sys.executable, "-m", "benchmarks.bench_webhook_inbound",
              "--output", "benchmark-results-webhooks/inbound-latest.json"])
        _run([sys.executable, "-m", "benchmarks.bench_webhook_dispatcher",
              "--output", "benchmark-results-webhooks/dispatcher-latest.json"])

        print("\n=== wreath-metal timer " + "=" * 35)
        _run([sys.executable, "-m", "benchmarks.bench_timing_wheel",
              "--output", "benchmark-results-timing-wheel/latest.json"])

        print("\n=== migration resolution " + "=" * 32)
        migrations = repo_root() / "benchmark-results-migrations" / "latest.json"
        if _run([sys.executable, "-m", "benchmarks.bench_migration_resolution",
                 "--output", str(migrations)]) == 0 and migrations.exists():
            report_inputs.append(migrations)

        print("\n=== cedar authorization " + "=" * 33)
        cedar = repo_root() / "benchmark-results-cedar" / "latest.json"
        if _run([sys.executable, "-m", "benchmarks.bench_cedar",
                 "--output", str(cedar)]) == 0 and cedar.exists():
            report_inputs.append(cedar)
        if not args.no_db:
            print("\n=== database battery (ORM, PostgreSQL webhooks, lifecycle) " + "=" * 8)
            report_inputs.extend(_db_battery())

    if report_inputs:
        combined = repo_root() / _MATRIX_RESULTS / "full-battery.html"
        from .bench_report import main as _report_main

        _report_main([*(str(path) for path in report_inputs), "-o", str(combined)])
        print(f"\nwreath-bench: combined report written to {combined}")
    return 0


def _pytest_command() -> list[str]:
    """`pytest -q`, parallelised to fit the machine.

    The suite used to run in about 3.5 seconds, where an xdist worker's
    re-import of the native extensions cost more than it saved. It has since
    grown past 4,400 tests and 30 seconds, and the trade has inverted.
    Measured here (12 cores, best of two runs): serial 30.7s, `-n 2` 16.8s,
    `-n 4` 9.8s, `-n 6` 8.1s, `-n 8` 8.1s, `-n 12` 9.5s -- so the curve
    flattens at six and turns back up once workers outnumber the cores they
    have to share with the extensions they each load.

    Capped rather than `-n auto` for that last reason: `auto` is the core
    count, which is past the flat part on any machine this wide. A bare
    `uv run pytest` stays serial, because that is the one you attach a
    debugger to.
    """
    workers = min(_PYTEST_MAX_WORKERS, os.cpu_count() or 1)
    command = [sys.executable, "-m", "pytest", "-q"]
    return command if workers < 2 else [*command, "-n", str(workers)]


#: Past this the per-worker cost of importing the native extensions outweighs
#: the parallelism; see `_pytest_command` for the measurements.
_PYTEST_MAX_WORKERS = 6


#: The gates a change has to pass, in the order that fails cheapest first.
_CHECKS: tuple[tuple[str, list[str]], ...] = (
    ("map-lint", [sys.executable, "-m", "wreath._devtools.map_lint"]),
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ("ty", [sys.executable, "-m", "ty", "check"]),
    ("pytest", _pytest_command()),
    ("native-lint", [sys.executable, "-m", "wreath._devtools.native_lint"]),
    ("native-error-lint", [sys.executable, "-m", "wreath._devtools.native_error_lint"]),
    ("native-memory-lint", [sys.executable, "-m", "wreath._devtools.native_memory_lint"]),
    ("native-gil-lint", [sys.executable, "-m", "wreath._devtools.native_gil_lint"]),
    ("complexity", [sys.executable, "-m", "wreath._devtools.complexity_probe",
                    "--group", "metal-http1", "--check"]),
    ("request-trace", [sys.executable, "-m", "wreath._devtools.request_trace", "--check"]),
)


def check(argv: list[str] | None = None) -> int:
    """Run every gate a change must pass, and report which failed.

    Runs all of them rather than stopping at the first failure: knowing that
    three things broke is worth more than the seconds saved by finding one.
    """
    parser = argparse.ArgumentParser(
        prog="wreath-check", description="Run lint, types, tests, native lints, baseline."
    )
    parser.add_argument(
        "--docs", action="store_true", help="also build the docs (installs the docs group)"
    )
    args = parser.parse_args(argv)

    ensure_groups("dev")
    checks = list(_CHECKS)
    if args.docs:
        ensure_groups("docs")
        checks.append(("docs", [sys.executable, "-m", "mkdocs", "build", "--strict"]))

    failed: list[str] = []
    for name, command in checks:
        print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))
        sys.stdout.flush()
        if _run(command) != 0:
            failed.append(name)

    print("\n" + "=" * 66)
    if failed:
        print(f"wreath-check: {len(failed)} of {len(checks)} failed: {', '.join(failed)}")
        _warn_unverified()
        return 1
    print(f"wreath-check: all {len(checks)} checks passed.")
    _warn_unverified()
    return 0


#: Printed last, because the pytest banner that says the same thing scrolls past
#: eight more gates before anyone reads the verdict. `tests/conftest.py` owns the
#: count; this only has to say the gates were not all green in the way they look.
def _warn_unverified() -> None:
    """Say, last of all, that the database suites did not run.

    Checked here rather than reported up from pytest: the env var is the whole
    condition, and this process can read it. No state to pass, nothing to parse,
    and it cannot disagree with what the test run actually did.
    """
    if os.environ.get("WREATH_TEST_POSTGRES_DSN"):
        return
    runtime = next(
        (name for name in ("docker", "podman", "nerdctl") if shutil.which(name)), None
    )
    print(
        "\nNOTE: the database-backed tests did not run -- "
        "WREATH_TEST_POSTGRES_DSN is unset."
    )
    if runtime is None:
        print("      No container runtime found, so they cannot run on this machine.")
    print("      See the pytest section above for the count and the command.")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(check())
