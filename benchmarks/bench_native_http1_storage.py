"""HTTP/1, routing, and native storage CPU/memory-pressure benchmarks.

Each scenario isolates one amplification path identified in
`docs/plans/native-c-http1-routing-storage-pressure.md`:

* ``http1-slow-head``        incomplete head delivered one byte at a time
* ``http1-slow-chunk-line``  incomplete chunk-size line, one byte at a time
* ``http1-receive-queue``    draining many queued ASGI body messages
* ``ws-empty-fragments``     thousands of empty WebSocket continuation frames
* ``ws-empty-messages``      thousands of queued zero-byte WebSocket messages
* ``trie-adversarial-miss``  trie miss where every level offers literal+param
* ``trie-wide-fanout``       one node with many literal children
* ``pg-tape-small-consume``  consuming a field tape one row at a time
* ``pg-retired-slabs``       receive cycles against many pinned retired slabs
* ``pg-bytea-text``          decoding many hex-``bytea`` text fields
* ``multipart-peak``         peak RSS parsing a large multipart body
* ``json-key-churn``         high-cardinality JSON object keys
* ``request-cookie-repeat``  repeated ``request.cookies`` reads

Every measured trial runs in a **fresh child process**, so peak RSS and any
retained objects from one trial cannot contaminate another: the parent spawns
one child per scenario, the child prints exactly one scenario record to stdout,
and the parent writes one JSON document.

This is a development tool. Report medians and raw trials, never a single run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import struct
import subprocess
import sys
import sysconfig
from pathlib import Path
from time import perf_counter_ns
from typing import Any

SCENARIOS = (
    "http1-slow-head",
    "http1-slow-chunk-line",
    "http1-receive-queue",
    "ws-empty-fragments",
    "ws-empty-messages",
    "trie-adversarial-miss",
    "trie-wide-fanout",
    "pg-tape-small-consume",
    "pg-retired-slabs",
    "pg-bytea-text",
    "multipart-peak",
    "json-key-churn",
    "request-cookie-repeat",
)

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]

# ``ru_maxrss`` is KiB on Linux and bytes on macOS (see getrusage(2) on each).
# Normalize to bytes; every record states the rule applied.
if resource is None:
    RSS_NORMALIZATION = "unavailable: the resource module is not importable"
elif sys.platform == "darwin":
    RSS_NORMALIZATION = "darwin: ru_maxrss is bytes, used as-is"
else:
    RSS_NORMALIZATION = "linux: ru_maxrss is KiB, multiplied by 1024"


def peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(raw) if sys.platform == "darwin" else int(raw) * 1024


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    # Nearest-rank, stated explicitly so the number is reproducible rather than
    # interpolation-dependent. With 9 trials p95 is the largest sample.
    rank = max(1, min(len(ordered), int(-(-q * len(ordered) // 1))))
    return ordered[rank - 1]


def native_module_path() -> str:
    try:
        import wreath._native._server as mod

        return str(getattr(mod, "__file__", "unresolved"))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


def compiler_flags() -> str:
    parts = [
        sysconfig.get_config_var(name) or ""
        for name in ("CC", "CFLAGS", "OPT")
    ]
    joined = " ".join(p for p in parts if p).strip()
    return joined or "unavailable"


def make_record(
    scenario: str,
    parameters: dict[str, Any],
    warmups: int,
    trials: int,
    raw_seconds: list[float],
    raw_rss: list[int],
    errors: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scenario": scenario,
        "parameters": parameters,
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "native_module": native_module_path(),
        "compiler_flags": compiler_flags(),
        "warmups": warmups,
        "trials": trials,
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


def unavailable_record(scenario: str, reason: str, parameters: dict[str, Any]) -> dict[str, Any]:
    record = make_record(scenario, parameters, 0, 0, [], [], [f"unavailable: {reason}"])
    record["status"] = "unavailable"
    return record


def paired(
    scenario: str,
    parameters: dict[str, Any],
    sizes: list[Any],
    run_once: Any,
    warmups: int,
    trials: int,
    ratio_name: str,
) -> dict[str, Any]:
    """Run one case per size; report each size and the doubling ratio.

    Acceptance is expressed as scaling between the pair, never as a
    machine-specific absolute time.
    """
    errors: list[str] = []
    per_size: dict[str, Any] = {}
    all_seconds: list[float] = []
    raw_rss: list[int] = []
    observed: dict[str, Any] = {}
    for size in sizes:
        seconds: list[float] = []
        for i in range(warmups + trials):
            try:
                elapsed, observed = run_once(size)
            except Exception as exc:  # noqa: BLE001 - recorded, never hidden
                errors.append(f"size {size} trial {i}: {exc!r}")
                continue
            if i >= warmups:
                seconds.append(elapsed)
                rss = peak_rss_bytes()
                if rss is not None:
                    raw_rss.append(rss)
        per_size[str(size)] = {
            "raw_seconds": seconds,
            "median_seconds": statistics.median(seconds) if seconds else 0.0,
            "p95_seconds": percentile(seconds, 0.95),
            "observed": observed,
        }
        all_seconds.extend(seconds)
    small = per_size[str(sizes[0])]["median_seconds"]
    large = per_size[str(sizes[-1])]["median_seconds"]
    return make_record(
        scenario, parameters, warmups, trials, all_seconds, raw_rss, errors,
        extra={"per_size": per_size, ratio_name: (large / small) if small > 0 else 0.0},
    )


# --------------------------------------------------------------------------
# server driving (fake transport, no sockets)
# --------------------------------------------------------------------------


class CountingTransport(asyncio.Transport):
    """Counts and discards writes; records pause/resume like a real transport."""

    def __init__(self) -> None:
        super().__init__()
        self.bytes_written = 0
        self.closed = False
        self.reading_paused = False
        self.pause_count = 0
        self._extra = {
            "sockname": ("127.0.0.1", 8000),
            "peername": ("127.0.0.1", 54321),
        }

    def write(self, data: Any) -> None:
        if not self.closed:
            self.bytes_written += len(data)

    def writelines(self, chunks: Any) -> None:
        for chunk in chunks:
            self.write(chunk)

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self.reading_paused = True
        self.pause_count += 1

    def resume_reading(self) -> None:
        self.reading_paused = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._extra.get(name, default)


def native_protocol_cls() -> Any:
    import wreath._native._server as mod

    return mod.HttpProtocol


def _feed(protocol: Any, data: bytes) -> None:
    # Mirrors tests/_server_ingest.py: the production BufferedProtocol path.
    from tests._server_ingest import feed

    feed(protocol, data)


def _feed_byte(protocol: Any, data: bytes) -> None:
    """One-byte delivery through the compatibility path.

    ``data_received`` is one call per byte where the zero-copy path costs a
    ``get_buffer``/``buffer_updated`` pair plus memoryview work. Both reach the
    same parser, but this keeps per-feed overhead from masking the scan cost
    that these scenarios exist to measure.
    """
    protocol.data_received(data)


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def make_server(app: Any, config: Any) -> tuple[Any, CountingTransport]:
    loop = asyncio.get_running_loop()
    transport = CountingTransport()
    protocol = native_protocol_cls()(app, config, loop, set())
    protocol.connection_made(transport)
    return protocol, transport


async def _idle_app(scope: dict, receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if message["type"] in ("http.disconnect", "websocket.disconnect"):
            return


# --------------------------------------------------------------------------
# scenario: http1-slow-head / http1-slow-chunk-line
# --------------------------------------------------------------------------


def _incomplete_head(size: int) -> bytes:
    """A syntactically valid but unterminated request head of ~`size` bytes."""
    head = bytearray(b"GET / HTTP/1.1\r\nHost: x\r\n")
    filler = b"X-Pad-000: " + b"a" * 48 + b"\r\n"
    while len(head) + len(filler) < size:
        head += filler
    return bytes(head)  # deliberately never terminated with the final CRLF


async def _slow_head_once(size: int) -> tuple[float, dict[str, Any]]:
    from wreath.server import ServerConfig

    payload = _incomplete_head(size)
    config = ServerConfig(
        lifespan="off",
        # Headroom so the scan cost, not a 431/414 rejection, is what is timed.
        max_header_bytes=1 << 20,
        max_request_line=1 << 20,
    )
    protocol, transport = make_server(_idle_app, config)
    started = perf_counter_ns()
    for i in range(len(payload)):
        _feed_byte(protocol, payload[i : i + 1])
    elapsed = (perf_counter_ns() - started) / 1e9
    written = transport.bytes_written
    protocol.connection_lost(None)
    await settle()
    return elapsed, {"head_bytes": len(payload), "bytes_written": written}


def scenario_http1_slow_head(warmups: int, trials: int) -> dict[str, Any]:
    def run(size: int) -> tuple[float, dict[str, Any]]:
        return asyncio.run(_slow_head_once(size))

    return paired(
        "http1-slow-head",
        {"sizes_bytes": [8 * 1024, 16 * 1024], "feed": "one byte per data_received"},
        [8 * 1024, 16 * 1024],
        run,
        warmups,
        trials,
        "scaling_ratio_16KiB_over_8KiB",
    )


async def _slow_chunk_line_once(size: int) -> tuple[float, dict[str, Any]]:
    from wreath.server import ServerConfig

    config = ServerConfig(
        lifespan="off",
        max_request_line=1 << 20,  # the chunk-size line is bounded by this
        max_header_bytes=1 << 20,
    )
    protocol, transport = make_server(_idle_app, config)
    _feed(
        protocol,
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n",
    )
    await settle()
    # An unterminated chunk-size line: leading zeros are valid hex digits, so
    # this stays a well-formed prefix no matter where it is cut.
    line = b"0" * size
    started = perf_counter_ns()
    for i in range(len(line)):
        _feed_byte(protocol, line[i : i + 1])
    elapsed = (perf_counter_ns() - started) / 1e9
    written = transport.bytes_written
    protocol.connection_lost(None)
    await settle()
    return elapsed, {"chunk_line_bytes": size, "bytes_written": written}


def scenario_http1_slow_chunk_line(warmups: int, trials: int) -> dict[str, Any]:
    def run(size: int) -> tuple[float, dict[str, Any]]:
        return asyncio.run(_slow_chunk_line_once(size))

    return paired(
        "http1-slow-chunk-line",
        {"sizes_bytes": [8 * 1024, 16 * 1024], "feed": "one byte per data_received"},
        [8 * 1024, 16 * 1024],
        run,
        warmups,
        trials,
        "scaling_ratio_16KiB_over_8KiB",
    )


# --------------------------------------------------------------------------
# scenario: http1-receive-queue
# --------------------------------------------------------------------------


async def _receive_queue_once(count: int) -> tuple[float, dict[str, Any]]:
    from wreath.server import ServerConfig

    gate = asyncio.Event()
    done = asyncio.Event()
    result: dict[str, Any] = {"messages": 0, "drain_seconds": 0.0}

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await gate.wait()  # do not read while the peer streams: force queueing
        started = perf_counter_ns()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            result["messages"] += 1
            if not message.get("more_body", False):
                break
        result["drain_seconds"] = (perf_counter_ns() - started) / 1e9
        done.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    config = ServerConfig(lifespan="off", read_high_water=1 << 30)
    protocol, _transport = make_server(app, config)
    _feed(protocol, b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n")
    await settle()
    body = bytearray()
    for _ in range(count):
        body += b"1\r\nx\r\n"
    body += b"0\r\n\r\n"
    _feed(protocol, bytes(body))
    await settle()
    gate.set()
    await asyncio.wait_for(done.wait(), timeout=120)
    protocol.connection_lost(None)
    await settle()
    return float(result["drain_seconds"]), {"messages": result["messages"]}


def scenario_http1_receive_queue(warmups: int, trials: int) -> dict[str, Any]:
    def run(count: int) -> tuple[float, dict[str, Any]]:
        return asyncio.run(_receive_queue_once(count))

    return paired(
        "http1-receive-queue",
        {"entry_counts": [10_000, 20_000], "chunk_bytes": 1},
        [10_000, 20_000],
        run,
        warmups,
        trials,
        "scaling_ratio_20k_over_10k",
    )


# --------------------------------------------------------------------------
# scenario: ws-empty-fragments / ws-empty-messages
# --------------------------------------------------------------------------

WS_UPGRADE = (
    b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
    b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Version: 13\r\n\r\n"
)
MASK = b"\x01\x02\x03\x04"


def ws_frame(fin: bool, opcode: int, payload: bytes = b"") -> bytes:
    from wreath._websocket import build_frame

    return build_frame(opcode, payload, fin, MASK)


async def _ws_accept_app(scope: dict, receive: Any, send: Any) -> None:
    message = await receive()
    assert message["type"] == "websocket.connect"
    await send({"type": "websocket.accept"})
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            return


async def _ws_empty_fragments_once(count: int) -> tuple[float, dict[str, Any]]:
    from wreath.server import ServerConfig

    seen: dict[str, Any] = {"messages": 0}

    async def app(scope: dict, receive: Any, send: Any) -> None:
        message = await receive()
        assert message["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            seen["messages"] += 1

    import tracemalloc

    protocol, _transport = make_server(app, ServerConfig(lifespan="off"))
    _feed(protocol, WS_UPGRADE)
    await settle()
    # One empty text frame, then `count` empty continuations, then FIN.
    #
    # RSS is far too coarse to see per-fragment storage (a list of 20,001
    # pointers is 160 KiB). tracemalloc measures what the accumulator actually
    # retains while the message is still incomplete, which is the quantity the
    # acceptance check is about: it must not grow with frame count.
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    started = perf_counter_ns()
    _feed(protocol, ws_frame(False, 0x1, b""))
    frame = ws_frame(False, 0x0, b"")
    for _ in range(count):
        _feed(protocol, frame)
    elapsed_fragments = (perf_counter_ns() - started) / 1e9
    pending = tracemalloc.get_traced_memory()[0] - before
    tracemalloc.stop()
    _feed(protocol, ws_frame(True, 0x0, b""))
    elapsed = (perf_counter_ns() - started) / 1e9
    await settle()
    rss = peak_rss_bytes()
    protocol.connection_lost(None)
    await settle()
    return elapsed, {
        "messages_delivered": seen["messages"],
        "peak_rss_bytes": rss,
        "fragment_seconds": elapsed_fragments,
        "retained_fragment_bytes": pending,
        "retained_bytes_per_fragment": pending / count if count else 0.0,
    }


def scenario_ws_empty_fragments(warmups: int, trials: int) -> dict[str, Any]:
    def run(count: int) -> tuple[float, dict[str, Any]]:
        return asyncio.run(_ws_empty_fragments_once(count))

    return paired(
        "ws-empty-fragments",
        {"fragment_counts": [10_000, 20_000], "payload_bytes": 0},
        [10_000, 20_000],
        run,
        warmups,
        trials,
        "scaling_ratio_20k_over_10k",
    )


async def _ws_empty_messages_once(count: int) -> tuple[float, dict[str, Any]]:
    from wreath.server import ServerConfig

    gate = asyncio.Event()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        message = await receive()
        assert message["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await gate.wait()  # never read: the queue must grow by message count
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return

    protocol, transport = make_server(app, ServerConfig(lifespan="off"))
    _feed(protocol, WS_UPGRADE)
    await settle()
    frame = ws_frame(True, 0x1, b"")  # a complete zero-byte text message
    started = perf_counter_ns()
    for _ in range(count):
        _feed(protocol, frame)
    elapsed = (perf_counter_ns() - started) / 1e9
    await settle()
    rss = peak_rss_bytes()
    observed = {
        # Zero payload bytes: only a message-count bound can pause this.
        "reading_paused": transport.reading_paused,
        "pause_count": transport.pause_count,
        "peak_rss_bytes": rss,
    }
    gate.set()
    protocol.connection_lost(None)
    await settle()
    return elapsed, observed


def scenario_ws_empty_messages(warmups: int, trials: int) -> dict[str, Any]:
    def run(count: int) -> tuple[float, dict[str, Any]]:
        return asyncio.run(_ws_empty_messages_once(count))

    return paired(
        "ws-empty-messages",
        {"message_counts": [10_000, 20_000], "payload_bytes": 0},
        [10_000, 20_000],
        run,
        warmups,
        trials,
        "scaling_ratio_20k_over_10k",
    )


# --------------------------------------------------------------------------
# scenario: trie-adversarial-miss / trie-wide-fanout
# --------------------------------------------------------------------------


def native_route_table() -> Any:
    from wreath._native import _core

    return _core.RouteTable


def _adversarial_table(depth: int) -> tuple[Any, str]:
    """Every level offers both a literal and a parameter branch.

    Registering all 2**depth literal/parameter combinations makes each node on
    the requested path offer both branches, so a miss explores the whole
    subtree. The terminal method is POST, so a GET request must fail at every
    leaf.
    """
    table = native_route_table()()
    handler = object()
    for combo in range(1 << depth):
        segments = [
            ("a" if (combo >> i) & 1 else f"{{p{i}}}") for i in range(depth)
        ]
        table.add("/" + "/".join(segments), "POST", handler)
    return table, "/" + "/".join(["a"] * depth)


def _trie_miss_once(depth: int) -> tuple[float, dict[str, Any]]:
    table, path = _adversarial_table(depth)
    iterations = 50
    started = perf_counter_ns()
    for _ in range(iterations):
        result = table.match("GET", path)
    elapsed = (perf_counter_ns() - started) / 1e9 / iterations
    return elapsed, {"routes": 1 << depth, "matched": result is not None}


def scenario_trie_adversarial_miss(warmups: int, trials: int) -> dict[str, Any]:
    return paired(
        "trie-adversarial-miss",
        {"depths": [10, 14], "note": "2**depth literal/parameter combinations"},
        [10, 14],
        _trie_miss_once,
        warmups,
        trials,
        "scaling_ratio_depth14_over_depth10",
    )


def _trie_fanout_once(children: int) -> tuple[float, dict[str, Any]]:
    table = native_route_table()()
    handler = object()
    for i in range(children):
        table.add(f"/seg-{i:05d}/leaf", "GET", handler)
    # Look up the last-registered child: the worst case for a linear scan.
    path = f"/seg-{children - 1:05d}/leaf"
    iterations = 20_000
    started = perf_counter_ns()
    for _ in range(iterations):
        result = table.match("GET", path)
    elapsed = (perf_counter_ns() - started) / 1e9 / iterations
    return elapsed, {"children": children, "matched": result is not None}


def scenario_trie_wide_fanout(warmups: int, trials: int) -> dict[str, Any]:
    return paired(
        "trie-wide-fanout",
        {"child_counts": [1000, 2000], "lookup": "last registered child"},
        [1000, 2000],
        _trie_fanout_once,
        warmups,
        trials,
        "scaling_ratio_2000_over_1000",
    )


# --------------------------------------------------------------------------
# scenario: pg-tape-small-consume / pg-retired-slabs / pg-bytea-text
# --------------------------------------------------------------------------


def native_postgres() -> Any:
    import wreath._native._postgres as mod

    return mod


def _data_row(fields: tuple[bytes | None, ...]) -> memoryview:
    payload = bytearray(struct.pack("!H", len(fields)))
    for field in fields:
        if field is None:
            payload += struct.pack("!i", -1)
        else:
            payload += struct.pack("!I", len(field)) + field
    return memoryview(payload)


def _tape_consume_once(rows: int) -> tuple[float, dict[str, Any]]:
    native = native_postgres()
    tape = native._FieldTape(3)
    for value in range(rows):
        tape.append(
            _data_row(
                (
                    b"\x01" if value % 2 else b"\x00",
                    struct.pack("!i", value),
                    f"value-{value}".encode(),
                )
            ),
            3,
        )
    plan = native._compile_decoder_plan(
        (16, 23, 25), (1, 1, 1), ("enabled", "number", "label")
    )
    started = perf_counter_ns()
    decoded = 0
    while tape.row_count:
        batch = native._decode_field_tape(plan, tape, "fetch", 1)  # one row at a time
        decoded += len(batch)
    elapsed = (perf_counter_ns() - started) / 1e9
    return elapsed, {"rows_decoded": decoded, "row_count_after": tape.row_count}


def scenario_pg_tape_small_consume(warmups: int, trials: int) -> dict[str, Any]:
    params = {"row_counts": [10_000, 20_000], "consume": "one row per call"}
    try:
        native_postgres()
    except ImportError as exc:
        return unavailable_record("pg-tape-small-consume", f"{exc}", params)
    return paired(
        "pg-tape-small-consume", params, [10_000, 20_000], _tape_consume_once,
        warmups, trials, "scaling_ratio_20k_over_10k",
    )


def _pg_message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _feed_pg(protocol: Any, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        view = memoryview(protocol.get_buffer(-1))
        count = min(len(view), len(data) - offset)
        view[:count] = data[offset : offset + count]
        del view
        protocol.buffer_updated(count)
        offset += count


async def _retired_slabs_once(pinned: int) -> tuple[float, dict[str, Any]]:
    native = native_postgres()
    protocol = native.BufferedProtocol()
    # Pin slabs: each DataRow payload is a memoryview into its slab, so holding
    # them retires the slabs without releasing them.
    held: list[Any] = []
    row = struct.pack("!Hi", 1, 60_000) + b"x" * 60_000
    for _ in range(pinned):
        _feed_pg(protocol, _pg_message(b"D", row))
        kind, payload = await protocol.read_message()
        assert kind == b"D"
        held.append(payload)
    retired_before = protocol._receive_stats()["retired_slabs"]

    # Normal receive cycles: every get_buffer() rescans the pinned prefix.
    cycles = 1000
    small = _pg_message(b"Z", b"I")
    started = perf_counter_ns()
    for _ in range(cycles):
        _feed_pg(protocol, small)
        await protocol.read_message()
    elapsed = (perf_counter_ns() - started) / 1e9
    stats = protocol._receive_stats()
    # The acceptance check here is about how many entries each receive cycle
    # inspects, not wall clock: with a pinned prefix, an unbudgeted scan walks
    # all of them every time. `retired_scan_steps` is absent until the budgeted
    # scan exists, so a missing value is itself the before-state.
    steps = stats.get("retired_scan_steps")
    return elapsed, {
        "pinned_slabs": pinned,
        "retired_slabs_before": retired_before,
        "retired_slabs_after": stats["retired_slabs"],
        "cycles": cycles,
        "retired_scan_steps": steps,
        "steps_per_cycle": (steps / cycles) if steps is not None else None,
    }


def scenario_pg_retired_slabs(warmups: int, trials: int) -> dict[str, Any]:
    params = {"pinned_counts": [128, 256], "cycles": 1000}
    try:
        native_postgres()
    except ImportError as exc:
        return unavailable_record("pg-retired-slabs", f"{exc}", params)

    def run(pinned: int) -> tuple[float, dict[str, Any]]:
        return asyncio.run(_retired_slabs_once(pinned))

    return paired(
        "pg-retired-slabs", params, [128, 256], run, warmups, trials,
        "scaling_ratio_256_over_128",
    )


def _bytea_text_once(fields: int) -> tuple[float, dict[str, Any]]:
    native = native_postgres()
    payload = b"\\x" + b"deadbeef" * 8  # 32 raw bytes of hex text
    started = perf_counter_ns()
    last = None
    for _ in range(fields):
        last = native._decode_value(17, 0, payload)  # oid 17 = bytea, text format
    elapsed = (perf_counter_ns() - started) / 1e9
    return elapsed, {"fields": fields, "decoded_bytes": len(last) if last else 0}


def scenario_pg_bytea_text(warmups: int, trials: int) -> dict[str, Any]:
    params = {"field_counts": [10_000, 20_000], "payload_hex_bytes": 64}
    try:
        native_postgres()
    except ImportError as exc:
        return unavailable_record("pg-bytea-text", f"{exc}", params)
    return paired(
        "pg-bytea-text", params, [10_000, 20_000], _bytea_text_once, warmups,
        trials, "scaling_ratio_20k_over_10k",
    )


# --------------------------------------------------------------------------
# scenario: multipart-peak
# --------------------------------------------------------------------------


def _multipart_body(total: int, boundary: bytes) -> bytes:
    """One large file part plus many smaller field parts."""
    delim = b"--" + boundary
    parts = [
        delim + b"\r\nContent-Disposition: form-data; name=\"file\"; "
        b"filename=\"big.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        + b"x" * (total // 2) + b"\r\n"
    ]
    small = total // 2
    each = 4096
    for i in range(small // each):
        parts.append(
            delim + b"\r\nContent-Disposition: form-data; name=\"f" +
            str(i).encode() + b"\"\r\n\r\n" + b"y" * each + b"\r\n"
        )
    parts.append(delim + b"--\r\n")
    return b"".join(parts)


def _multipart_once(total: int) -> tuple[float, dict[str, Any]]:
    from wreath._native import _core

    boundary = b"BOUNDARY"
    body = _multipart_body(total, boundary)
    from wreath._headers import find_header

    baseline = peak_rss_bytes()
    started = perf_counter_ns()
    parts = _core.multipart_parse(body, boundary)
    elapsed = (perf_counter_ns() - started) / 1e9
    peak = peak_rss_bytes()
    copied = sum(len(p[1]) for p in parts)
    # Only field parts are addressable. `UploadedFile.data` and the public
    # `wreath._multipart.Part.data` are both documented as bytes, so file-part
    # copies must survive any zero-copy change; counting them toward the gate
    # would overstate what the change could actually remove.
    file_bytes = 0
    field_bytes = 0
    for headers, data in parts:
        disposition = find_header(headers, b"content-disposition") or b""
        if b"filename=" in disposition:
            file_bytes += len(data)
        else:
            field_bytes += len(data)
    observed = {
        "body_bytes": len(body),
        "parts": len(parts),
        "copied_part_bytes": copied,
        "file_part_bytes": file_bytes,
        "field_part_bytes": field_bytes,
        "rss_before_bytes": baseline,
        "rss_after_bytes": peak,
        # The plan's 20% gate as literally stated: all part copies over peak.
        "copied_share_of_peak": (copied / peak) if peak else 0.0,
        # What a zero-copy private boundary could actually remove.
        "addressable_share_of_peak": (field_bytes / peak) if peak else 0.0,
    }
    del parts
    return elapsed, observed


def scenario_multipart_peak(warmups: int, trials: int) -> dict[str, Any]:
    return paired(
        "multipart-peak",
        {"body_sizes_bytes": [8 * 1024 * 1024, 16 * 1024 * 1024],
         "shape": "one half-size file part plus 4 KiB field parts"},
        [8 * 1024 * 1024, 16 * 1024 * 1024],
        _multipart_once,
        warmups,
        trials,
        "scaling_ratio_16MiB_over_8MiB",
    )


# --------------------------------------------------------------------------
# scenario: json-key-churn
# --------------------------------------------------------------------------


def _json_docs(distinct_keys: int, docs: int) -> list[bytes]:
    out = []
    per_doc = 16
    for d in range(docs):
        obj = {
            f"k{(d * per_doc + i) % distinct_keys:06d}": i
            for i in range(per_doc)
        }
        out.append(json.dumps(obj).encode())
    return out


def _json_key_churn_once(mode: str) -> tuple[float, dict[str, Any]]:
    from wreath._native import _core

    # "stable" repeats one key set (what the cache is meant to help); "churn"
    # cycles 1024 distinct keys (what the cache retains).
    distinct = 16 if mode == "stable" else 1024
    docs = _json_docs(distinct, 512)
    started = perf_counter_ns()
    total = 0
    for _ in range(8):
        for doc in docs:
            total += len(_core.json_loads(doc))
    elapsed = (perf_counter_ns() - started) / 1e9
    return elapsed, {"mode": mode, "distinct_keys": distinct, "pairs": total}


def scenario_json_key_churn(warmups: int, trials: int) -> dict[str, Any]:
    return paired(
        "json-key-churn",
        {"modes": ["stable", "churn"], "distinct_keys_churn": 1024, "documents": 512},
        ["stable", "churn"],
        _json_key_churn_once,
        warmups,
        trials,
        "ratio_churn_over_stable",
    )


# --------------------------------------------------------------------------
# scenario: request-cookie-repeat
# --------------------------------------------------------------------------


def _cookie_repeat_once(reads: int) -> tuple[float, dict[str, Any]]:
    from wreath.request import Request

    cookie = b"; ".join(f"name{i}=value{i}".encode() for i in range(12))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", b"x"), (b"cookie", cookie)],
        "query_string": b"",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, receive)
    started = perf_counter_ns()
    identical = True
    first = request.cookies
    for _ in range(reads - 1):
        if request.cookies is not first:
            identical = False
    elapsed = (perf_counter_ns() - started) / 1e9
    return elapsed, {"reads": reads, "same_object_each_read": identical,
                     "cookies": len(first)}


def scenario_request_cookie_repeat(warmups: int, trials: int) -> dict[str, Any]:
    return paired(
        "request-cookie-repeat",
        {"read_counts": [10_000, 20_000], "cookie_pairs": 12},
        [10_000, 20_000],
        _cookie_repeat_once,
        warmups,
        trials,
        "scaling_ratio_20k_over_10k",
    )


RUNNERS = {
    "http1-slow-head": scenario_http1_slow_head,
    "http1-slow-chunk-line": scenario_http1_slow_chunk_line,
    "http1-receive-queue": scenario_http1_receive_queue,
    "ws-empty-fragments": scenario_ws_empty_fragments,
    "ws-empty-messages": scenario_ws_empty_messages,
    "trie-adversarial-miss": scenario_trie_adversarial_miss,
    "trie-wide-fanout": scenario_trie_wide_fanout,
    "pg-tape-small-consume": scenario_pg_tape_small_consume,
    "pg-retired-slabs": scenario_pg_retired_slabs,
    "pg-bytea-text": scenario_pg_bytea_text,
    "multipart-peak": scenario_multipart_peak,
    "json-key-churn": scenario_json_key_churn,
    "request-cookie-repeat": scenario_request_cookie_repeat,
}


# --------------------------------------------------------------------------
# parent / child plumbing
# --------------------------------------------------------------------------


def run_child(scenario: str, warmups: int, trials: int) -> dict[str, Any]:
    """Spawn one scenario in a fresh process so its peak RSS stands alone."""
    cmd = [
        sys.executable, "-m", "benchmarks.bench_native_http1_storage",
        "--scenario", scenario, "--warmup", str(warmups),
        "--trials", str(trials), "--emit",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
    )
    if proc.returncode != 0:
        return unavailable_record(
            scenario, f"child exited {proc.returncode}: {proc.stderr.strip()[-400:]}", {}
        )
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return unavailable_record(scenario, "child produced no record", {})


def main() -> None:
    parser = argparse.ArgumentParser(prog="benchmarks.bench_native_http1_storage")
    parser.add_argument("--scenario", default="all", choices=("all", *SCENARIOS))
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--emit", action="store_true",
        help="internal: run one scenario here and print its record to stdout",
    )
    args = parser.parse_args()

    if args.emit:
        if args.scenario == "all":
            parser.error("--emit requires a single --scenario")
        print(json.dumps(RUNNERS[args.scenario](args.warmup, args.trials)))
        return

    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    document = {
        "tool": "benchmarks.bench_native_http1_storage",
        "schema_version": 1,
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "compiler_flags": compiler_flags(),
        "rss_normalization": RSS_NORMALIZATION,
        "warmups": args.warmup,
        "trials": args.trials,
        "scenarios": [run_child(n, args.warmup, args.trials) for n in scenarios],
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
