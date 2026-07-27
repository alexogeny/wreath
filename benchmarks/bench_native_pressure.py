"""Native CPU and memory-pressure benchmarks for the accelerated server paths.

Each scenario isolates one superlinear or unbounded operation identified in
``docs/plans/native-c-hotspots.md``:

* ``h2-blocked-send``    an ASGI app producing a large awaited response while the
                         peer send window is zero. Proves whether backpressure
                         bounds what the app may construct.
* ``h2-flush-scaling``   releasing a blocked response through small WINDOW_UPDATE
                         increments. Exposes repeated front ``memmove``.
* ``h2-request-queue``   buffering then draining many small DATA chunks. Exposes
                         front deletion from the request queue.
* ``h3-request-limit``   uploading past ``max_body_bytes`` over real HTTP/3.
* ``router-compile``     decision-router compile time against route count.

Every case runs in a **fresh subprocess** so ``ru_maxrss`` is attributable to that
case alone: the parent spawns one child per scenario, the child prints exactly one
scenario record to stdout, and the parent writes one JSON document.

This is a development tool. Report medians and raw trials, never a single run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import subprocess
import sys
from pathlib import Path
from time import perf_counter_ns
from typing import Any

SCENARIOS = (
    "h2-blocked-send",
    "h2-flush-scaling",
    "h2-request-queue",
    "h3-request-limit",
    "router-compile",
)

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]

# ``ru_maxrss`` is KiB on Linux and bytes on macOS (see getrusage(2) on each).
# Normalize to bytes; record the rule applied in every scenario record.
if resource is None:
    RSS_NORMALIZATION = "unavailable: the resource module is not importable"
elif sys.platform == "darwin":
    RSS_NORMALIZATION = "darwin: ru_maxrss is bytes, used as-is"
else:
    RSS_NORMALIZATION = "linux: ru_maxrss is KiB, multiplied by 1024"


def peak_rss_bytes() -> int | None:
    """Peak resident set size of this process in bytes, or None if unavailable."""
    if resource is None:
        return None
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(raw) if sys.platform == "darwin" else int(raw) * 1024


# --------------------------------------------------------------------------
# environment metadata
# --------------------------------------------------------------------------


def wreath_version() -> str:
    try:
        from importlib.metadata import version

        return version("wreath")
    except Exception:  # noqa: BLE001 - metadata is best-effort
        try:
            import wreath

            return str(getattr(wreath, "__version__", "unknown"))
        except Exception:  # noqa: BLE001
            return "unknown"


def native_module_path() -> str:
    try:
        import wreath._native._server as mod

        return str(getattr(mod, "__file__", "unresolved"))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


def make_record(
    scenario: str,
    parameters: dict[str, Any],
    warmup: int,
    trials: int,
    raw_seconds: list[float],
    raw_rss: list[int],
    errors: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scenario": scenario,
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": sys.implementation.name,
        "executable": sys.executable,
        "wreath_version": wreath_version(),
        "native_module": native_module_path(),
        "parameters": parameters,
        "warmup_trials": warmup,
        "measured_trials": trials,
        "raw_seconds": raw_seconds,
        "median_seconds": statistics.median(raw_seconds) if raw_seconds else 0.0,
        "p95_seconds": percentile(raw_seconds, 0.95),
        "raw_peak_rss_bytes": raw_rss,
        "median_peak_rss_bytes": int(statistics.median(raw_rss)) if raw_rss else 0,
        "rss_normalization": RSS_NORMALIZATION,
        "errors": errors,
    }
    if extra:
        record.update(extra)
    return record


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    # Nearest-rank; with 9 trials p95 is the largest sample. Stated explicitly so
    # the number is reproducible rather than interpolation-dependent.
    rank = max(1, min(len(ordered), int(-(-q * len(ordered) // 1))))
    return ordered[rank - 1]


def unavailable_record(scenario: str, reason: str, parameters: dict[str, Any]) -> dict[str, Any]:
    record = make_record(scenario, parameters, 0, 0, [], [], [f"unavailable: {reason}"])
    record["status"] = "unavailable"
    return record


# --------------------------------------------------------------------------
# HTTP/2 in-process driver (fake transport, no TLS, no sockets)
# --------------------------------------------------------------------------


class CountingTransport(asyncio.Transport):
    """Counts and discards written bytes.

    Discarding rather than accumulating keeps peak RSS attributable to the
    protocol's own buffering instead of the test harness's output copy.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bytes_written = 0
        self.closed = False
        self._extra = {
            "sockname": ("127.0.0.1", 8000),
            "peername": ("127.0.0.1", 54321),
        }

    def write(self, data: Any) -> None:
        if not self.closed:
            self.bytes_written += len(data)

    def writelines(self, list_of_data: Any) -> None:
        for chunk in list_of_data:
            self.write(chunk)

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)


async def settle(rounds: int = 50) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def h2_support() -> Any:
    # The independent reference codec used by the HTTP/2 tests; it is not
    # imported by production code and encodes frames without trusting the
    # implementation under test.
    from tests.http2 import support

    return support


def make_h2(app: Any, config: Any) -> tuple[Any, CountingTransport]:
    from wreath._native._server import Http2Protocol

    loop = asyncio.get_running_loop()
    transport = CountingTransport()
    proto = Http2Protocol(app, config, loop, set())
    proto.connection_made(transport)
    return proto, transport


async def h2_preface(proto: Any, settings: dict[int, int]) -> None:
    support = h2_support()
    proto.data_received(support.PREFACE)
    proto.data_received(support.encode_settings(settings))
    await settle()


# --------------------------------------------------------------------------
# scenario: h2-blocked-send
# --------------------------------------------------------------------------


async def _h2_blocked_send_once(total_bytes: int, chunk_size: int) -> dict[str, Any]:
    from wreath.server import ServerConfig

    support = h2_support()
    progress = {"chunks_reached": 0}
    nchunks = total_bytes // chunk_size

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        payload = b"x" * chunk_size
        for i in range(nchunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": payload,
                    "more_body": i < nchunks - 1,
                }
            )
            progress["chunks_reached"] += 1

    config = ServerConfig(protocols=("h2",), lifespan="off")
    proto, transport = make_h2(app, config)
    # A zero initial window means the peer grants no stream send credit at all.
    await h2_preface(proto, {support.SETTINGS_INITIAL_WINDOW_SIZE: 0})
    proto.data_received(
        support.build_headers_frame(1, support.request_headers(b"GET", b"/"))
    )
    await settle(100)
    reached = progress["chunks_reached"]
    written = transport.bytes_written
    proto.connection_lost(None)
    await settle()
    return {
        "chunks_reached": reached,
        "chunks_offered": nchunks,
        "bytes_written": written,
    }


def scenario_h2_blocked_send(warmup: int, trials: int) -> dict[str, Any]:
    total_bytes = 64 * 1024 * 1024
    chunk_size = 4096
    params = {
        "total_bytes": total_bytes,
        "chunk_size": chunk_size,
        "peer_initial_window": 0,
    }
    raw_seconds: list[float] = []
    raw_rss: list[int] = []
    errors: list[str] = []
    last: dict[str, Any] = {}
    for i in range(warmup + trials):
        started = perf_counter_ns()
        try:
            last = asyncio.run(_h2_blocked_send_once(total_bytes, chunk_size))
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            errors.append(f"trial {i}: {exc!r}")
            continue
        elapsed = (perf_counter_ns() - started) / 1e9
        if i >= warmup:
            raw_seconds.append(elapsed)
            rss = peak_rss_bytes()
            if rss is not None:
                raw_rss.append(rss)
    return make_record(
        "h2-blocked-send",
        params,
        warmup,
        trials,
        raw_seconds,
        raw_rss,
        errors,
        extra={"observed": last},
    )


# --------------------------------------------------------------------------
# scenario: h2-flush-scaling
# --------------------------------------------------------------------------


async def _h2_flush_once(size: int, increment: int) -> tuple[float, int]:
    from wreath.server import ServerConfig

    support = h2_support()
    parked = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # One body message: the app owns this bytes object for the whole send.
        await send({"type": "http.response.body", "body": b"x" * size})
        # Stay alive. A finished task ends the stream, which would drop pending
        # output before the window ever reopens and measure nothing.
        await parked.wait()

    config = ServerConfig(protocols=("h2",), lifespan="off")
    proto, transport = make_h2(app, config)
    await h2_preface(proto, {support.SETTINGS_INITIAL_WINDOW_SIZE: 0})
    proto.data_received(
        support.build_headers_frame(1, support.request_headers(b"GET", b"/"))
    )
    await settle(100)

    # The whole body is now blocked on flow control. Time only the release.
    rounds = size // increment + 8
    conn_update = support.encode_window_update(0, increment)
    stream_update = support.encode_window_update(1, increment)
    started = perf_counter_ns()
    for _ in range(rounds):
        proto.data_received(conn_update)
        proto.data_received(stream_update)
    elapsed = (perf_counter_ns() - started) / 1e9
    written = transport.bytes_written
    proto.connection_lost(None)
    await settle()
    return elapsed, written


def scenario_h2_flush_scaling(warmup: int, trials: int) -> dict[str, Any]:
    increment = 16 * 1024
    # 16 and 32 MiB are the required pair. 8 and 64 MiB bracket them so scaling
    # is readable from the data itself: a single pair cannot distinguish an
    # algorithmic slope from a cache-residency step, and on a CPU whose L2 falls
    # between 16 and 32 MiB the required pair straddles exactly that boundary.
    sizes = [8 * 1024 * 1024, 16 * 1024 * 1024, 32 * 1024 * 1024, 64 * 1024 * 1024]
    params = {
        "sizes_bytes": sizes,
        "window_update_increment": increment,
        "required_pair_bytes": [16 * 1024 * 1024, 32 * 1024 * 1024],
    }
    errors: list[str] = []
    per_size: dict[str, Any] = {}
    all_seconds: list[float] = []
    raw_rss: list[int] = []
    for size in sizes:
        seconds: list[float] = []
        written = 0
        for i in range(warmup + trials):
            try:
                elapsed, written = asyncio.run(_h2_flush_once(size, increment))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"size {size} trial {i}: {exc!r}")
                continue
            if i >= warmup:
                seconds.append(elapsed)
                rss = peak_rss_bytes()
                if rss is not None:
                    raw_rss.append(rss)
        per_size[str(size)] = {
            "raw_seconds": seconds,
            "median_seconds": statistics.median(seconds) if seconds else 0.0,
            "p95_seconds": percentile(seconds, 0.95),
            # Guards the case: if the body never reached the transport, the
            # timing above measures nothing and must not be read as a win.
            "bytes_written": written,
            "body_fully_sent": written >= size,
            "nanoseconds_per_byte": (
                statistics.median(seconds) * 1e9 / size if seconds else 0.0
            ),
        }
        all_seconds.extend(seconds)

    def ratio(numerator: int, denominator: int) -> float:
        hi = per_size[str(numerator)]["median_seconds"]
        lo = per_size[str(denominator)]["median_seconds"]
        return hi / lo if lo > 0 else 0.0

    mib = 1024 * 1024
    consecutive = {
        "16MiB_over_8MiB": ratio(16 * mib, 8 * mib),
        "32MiB_over_16MiB": ratio(32 * mib, 16 * mib),
        "64MiB_over_32MiB": ratio(64 * mib, 32 * mib),
    }
    return make_record(
        "h2-flush-scaling",
        params,
        warmup,
        trials,
        all_seconds,
        raw_rss,
        errors,
        extra={
            "per_size": per_size,
            # The required acceptance ratio, kept under its original name.
            "scaling_ratio_32MiB_over_16MiB": consecutive["32MiB_over_16MiB"],
            "consecutive_ratios": consecutive,
            "scaling_note": (
                "Read nanoseconds_per_byte across sizes, not one ratio: a flat "
                "cost per byte is linear. A single doubling that also crosses "
                "the CPU's last-level cache reports a step change in memory "
                "cost, not superlinear work."
            ),
        },
    )


# --------------------------------------------------------------------------
# scenario: h2-request-queue
# --------------------------------------------------------------------------


async def _h2_queue_once(count: int, chunk: int) -> dict[str, Any]:
    from wreath.server import ServerConfig

    support = h2_support()
    gate = asyncio.Event()
    done = asyncio.Event()
    result: dict[str, Any] = {"chunks": 0, "bytes": 0, "consume_seconds": 0.0}

    async def app(scope: dict, receive: Any, send: Any) -> None:
        # Do not read while the peer streams: force the chunks into the queue.
        await gate.wait()
        started = perf_counter_ns()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            result["bytes"] += len(msg.get("body", b""))
            result["chunks"] += 1
            if not msg.get("more_body", False):
                break
        result["consume_seconds"] = (perf_counter_ns() - started) / 1e9
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        done.set()

    config = ServerConfig(
        protocols=("h2",),
        lifespan="off",
        # Headroom so neither the body limit nor receive flow control, rather
        # than the queue itself, is what this case measures.
        max_body_bytes=64 * 1024 * 1024,
        initial_stream_window=16 * 1024 * 1024,
        initial_connection_window=16 * 1024 * 1024,
    )
    proto, _transport = make_h2(app, config)
    await h2_preface(proto, {})
    proto.data_received(
        support.build_headers_frame(
            1, support.request_headers(b"POST", b"/"), end_stream=False
        )
    )
    await settle()

    payload = b"x" * chunk
    frame = support.encode_frame(support.DATA, 0, 1, payload)
    for _ in range(count):
        proto.data_received(frame)
    proto.data_received(support.encode_frame(support.DATA, support.FLAG_END_STREAM, 1, b""))

    gate.set()
    await asyncio.wait_for(done.wait(), timeout=120)
    proto.connection_lost(None)
    await settle()
    return result


def scenario_h2_request_queue(warmup: int, trials: int) -> dict[str, Any]:
    chunk = 8
    counts = [25_000, 50_000]
    params = {"chunk_counts": counts, "chunk_bytes": chunk}
    errors: list[str] = []
    per_count: dict[str, Any] = {}
    all_seconds: list[float] = []
    raw_rss: list[int] = []
    for count in counts:
        seconds: list[float] = []
        observed: dict[str, Any] = {}
        for i in range(warmup + trials):
            try:
                observed = asyncio.run(_h2_queue_once(count, chunk))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"count {count} trial {i}: {exc!r}")
                continue
            if i >= warmup:
                seconds.append(float(observed["consume_seconds"]))
                rss = peak_rss_bytes()
                if rss is not None:
                    raw_rss.append(rss)
        per_count[str(count)] = {
            "raw_seconds": seconds,
            "median_seconds": statistics.median(seconds) if seconds else 0.0,
            "p95_seconds": percentile(seconds, 0.95),
            "chunks_consumed": observed.get("chunks"),
            "bytes_consumed": observed.get("bytes"),
        }
        all_seconds.extend(seconds)
    small, large = per_count[str(counts[0])], per_count[str(counts[1])]
    ratio = (
        large["median_seconds"] / small["median_seconds"]
        if small["median_seconds"] > 0
        else 0.0
    )
    return make_record(
        "h2-request-queue",
        params,
        warmup,
        trials,
        all_seconds,
        raw_rss,
        errors,
        extra={"per_count": per_count, "scaling_ratio_50k_over_25k": ratio},
    )


# --------------------------------------------------------------------------
# scenario: h3-request-limit
# --------------------------------------------------------------------------


def h3_available() -> bool:
    try:
        from wreath.server import _http3_available

        return bool(_http3_available())
    except Exception:  # noqa: BLE001
        return False


def curl_h3_available() -> bool:
    import shutil

    curl = shutil.which("curl")
    if curl is None:
        return False
    try:
        out = subprocess.run(
            [curl, "--version"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "HTTP3" in out or "http3" in out


async def _h3_limit_once(limit: int, upload: int) -> dict[str, Any]:
    from tests.http3.conftest import curl_http3, make_self_signed_cert
    from wreath.server import ServerConfig, TLSConfig, serve

    accepted = {"bytes": 0, "disconnected": False}

    async def app(scope: dict, receive: Any, send: Any) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                accepted["disconnected"] = True
                return
            accepted["bytes"] += len(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(accepted["bytes"]).encode()})

    cert, key = make_self_signed_cert()
    server = await serve(
        app,
        ServerConfig(
            host="127.0.0.1", port=0, lifespan="off", protocols=("h3",),
            max_body_bytes=limit,
        ),
        tls=TLSConfig(cert, key),
    )
    port = server.datagram_addresses[0][1]
    started = perf_counter_ns()
    try:
        import tempfile

        with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as fh:
            fh.write(b"x" * upload)
            body_path = fh.name
        rc, out = await curl_http3(port, "/", "--data-binary", f"@{body_path}")
    finally:
        elapsed = (perf_counter_ns() - started) / 1e9
        await server.close()
    return {
        "seconds": elapsed,
        "curl_rc": rc,
        "curl_body": out.decode("utf-8", "replace")[:200],
        "accepted_bytes": accepted["bytes"],
        "app_saw_disconnect": accepted["disconnected"],
        "limit_bytes": limit,
        "uploaded_bytes": upload,
    }


def scenario_h3_request_limit(warmup: int, trials: int) -> dict[str, Any]:
    # Both sizes stay inside the 65535-byte QUIC initial stream window. Uploading
    # past it stalls (the ingress does not extend max_stream_data as the app
    # consumes), which would measure that stall instead of the body limit.
    limit = 16 * 1024
    upload = 48 * 1024
    params = {
        "max_body_bytes": limit,
        "uploaded_bytes": upload,
        "note": "sized under the QUIC initial stream window (65535)",
    }
    if not h3_available():
        return unavailable_record(
            "h3-request-limit", "wreath._native._http3 not built (WREATH_BUILD_HTTP3=1)", params
        )
    if not curl_h3_available():
        return unavailable_record(
            "h3-request-limit", "no HTTP/3-capable curl on PATH", params
        )
    raw_seconds: list[float] = []
    raw_rss: list[int] = []
    errors: list[str] = []
    last: dict[str, Any] = {}
    for i in range(warmup + trials):
        try:
            last = asyncio.run(_h3_limit_once(limit, upload))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"trial {i}: {exc!r}")
            continue
        if i >= warmup:
            raw_seconds.append(float(last["seconds"]))
            rss = peak_rss_bytes()
            if rss is not None:
                raw_rss.append(rss)
    return make_record(
        "h3-request-limit",
        params,
        warmup,
        trials,
        raw_seconds,
        raw_rss,
        errors,
        extra={"observed": last},
    )


# --------------------------------------------------------------------------
# scenario: router-compile
# --------------------------------------------------------------------------


def build_router_app(routes: int) -> Any:
    """Build (but do not compile) a decision-routed app with `routes` routes.

    Shape: common prefixes, a repeated literal group, distinct per-leaf literals,
    a path parameter (wildcard) per route, and access clauses inherited from two
    nested protected routers.
    """
    from wreath import Router, Wreath

    leaves = 100
    branches = max(1, routes // leaves)

    async def endpoint(request: Any) -> bytes:
        return b"ok"

    protected = Router(prefix="/control", permissions=("control:access",))
    for branch in range(branches):
        tenant = Router(prefix=f"/tenant-{branch}", permissions=(f"tenant:{branch}:read",))
        for leaf in range(leaves):
            tenant.get(
                f"/services/group-{leaf % 10}/resource-{leaf}/{{item_id}}"
            )(endpoint)
        protected.include_router(tenant)
    app = Wreath(routing="decision")
    app.include_router(protected)
    return app


def scenario_router_compile(warmup: int, trials: int) -> dict[str, Any]:
    counts = [5_000, 10_000]
    params = {"route_counts": counts, "leaves_per_branch": 100, "routing": "decision"}
    errors: list[str] = []
    per_count: dict[str, Any] = {}
    all_seconds: list[float] = []
    raw_rss: list[int] = []
    for count in counts:
        seconds: list[float] = []
        for i in range(warmup + trials):
            try:
                # Route construction is deliberately outside the timer.
                app = build_router_app(count)
                started = perf_counter_ns()
                app._compile_routes()
                elapsed = (perf_counter_ns() - started) / 1e9
            except Exception as exc:  # noqa: BLE001
                errors.append(f"count {count} trial {i}: {exc!r}")
                continue
            if i >= warmup:
                seconds.append(elapsed)
                rss = peak_rss_bytes()
                if rss is not None:
                    raw_rss.append(rss)
        per_count[str(count)] = {
            "raw_seconds": seconds,
            "median_seconds": statistics.median(seconds) if seconds else 0.0,
            "p95_seconds": percentile(seconds, 0.95),
        }
        all_seconds.extend(seconds)
    small, large = per_count[str(counts[0])], per_count[str(counts[1])]
    ratio = (
        large["median_seconds"] / small["median_seconds"]
        if small["median_seconds"] > 0
        else 0.0
    )
    return make_record(
        "router-compile",
        params,
        warmup,
        trials,
        all_seconds,
        raw_rss,
        errors,
        extra={"per_count": per_count, "scaling_ratio_10k_over_5k": ratio},
    )


RUNNERS = {
    "h2-blocked-send": scenario_h2_blocked_send,
    "h2-flush-scaling": scenario_h2_flush_scaling,
    "h2-request-queue": scenario_h2_request_queue,
    "h3-request-limit": scenario_h3_request_limit,
    "router-compile": scenario_router_compile,
}


# --------------------------------------------------------------------------
# parent / child plumbing
# --------------------------------------------------------------------------


def run_child(scenario: str, warmup: int, trials: int) -> dict[str, Any]:
    """Spawn one scenario in a fresh process so its peak RSS stands alone."""
    cmd = [
        sys.executable,
        "-m",
        "benchmarks.bench_native_pressure",
        "--scenario",
        scenario,
        "--warmup",
        str(warmup),
        "--trials",
        str(trials),
        "--emit",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
    )
    if proc.returncode != 0:
        return unavailable_record(
            scenario,
            f"child exited {proc.returncode}: {proc.stderr.strip()[-400:]}",
            {},
        )
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return unavailable_record(scenario, "child produced no record", {})


def main() -> None:
    parser = argparse.ArgumentParser(prog="benchmarks.bench_native_pressure")
    parser.add_argument("--scenario", default="all", choices=("all", *SCENARIOS))
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--emit",
        action="store_true",
        help="internal: run one scenario here and print its record to stdout",
    )
    args = parser.parse_args()

    if args.emit:
        if args.scenario == "all":
            parser.error("--emit requires a single --scenario")
        record = RUNNERS[args.scenario](args.warmup, args.trials)
        print(json.dumps(record))
        return

    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    results = [run_child(name, args.warmup, args.trials) for name in scenarios]
    document = {
        "tool": "benchmarks.bench_native_pressure",
        "schema_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": sys.implementation.name,
        "executable": sys.executable,
        "wreath_version": wreath_version(),
        "rss_normalization": RSS_NORMALIZATION,
        "warmup_trials": args.warmup,
        "measured_trials": args.trials,
        "scenarios": results,
    }
    text = json.dumps(document, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
        print(f"wrote {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
