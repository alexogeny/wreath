"""Compare native literal-map storage, compilation, and verified matching.

Pass --source for a frozen tree containing its isolated compiled extension.
CPU covers the named phase without tracing. Traced storage covers a second
table's compilation; whole-command instructions include that measurement.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns

from route_image import resident_bytes


def declarations(scenario: str, size: int):
    rows = []
    for index in range(size):
        if scenario == "low":
            parts = [str((index >> (2 * position)) & 3) for position in range(6)]
            tail = "/".join(parts)
            path = f"/api/{{id}}/{tail}"
            concrete = f"/api/value/{tail}"
            params = {"id": "value"}
        elif scenario in {"high", "exact"}:
            tail = ("x" * 25 if scenario == "exact" else "") + f"{index:05}"
            path = f"/api/{{id}}/{tail}"
            concrete = f"/api/value/{tail}"
            params = {"id": "value"}
        else:
            path = "/" + "/".join(f"{{p{part}}}" for part in range(index + 1))
            concrete = "/" + "/".join("value" for _ in range(index + 1))
            params = {f"p{part}": "value" for part in range(index + 1)}
        rows.append((path, concrete, index, params))
    return rows


def populate(table_type, rows):
    table = table_type()
    for path, _, index, _ in rows:
        table.add(path, "GET", index, (0,))
    return table


def verify(table, rows, repeats):
    checksum = 0
    for _ in range(repeats):
        for _, path, expected, params in rows:
            result = table.match("GET", path, 0)
            if result != (expected, params):
                raise RuntimeError(f"incorrect match for {path}: {result}")
            checksum += result[0] + 1
        if table.match("POST", "/missing", 0) is not None:
            raise RuntimeError("unexpected method match")
    return checksum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--scenario", choices=("low", "high", "exact", "params", "empty"))
    parser.add_argument("--size", type=int, default=4096)
    parser.add_argument("--phase", choices=("compile", "match"), default="compile")
    parser.add_argument("--repeats", type=int, default=16)
    args = parser.parse_args()
    if not 0 <= args.size <= 4096 or args.repeats <= 0:
        parser.error("--size must be 0..4096 and --repeats must be positive")
    if args.scenario == "params" and args.size > 64:
        parser.error("parameter-only tables support --size up to 64 segment counts")
    if (args.scenario == "empty") != (args.size == 0):
        parser.error("only --scenario empty requires --size 0")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath._native import _core

    loaded = Path(_core.__file__).resolve()
    if not loaded.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {loaded}, expected native extension under {args.source}")
    rows = declarations(args.scenario, args.size)
    table = populate(_core.PolicyRouteTable, rows)
    if args.phase == "match":
        table.compile()
        verify(table, rows, 1)
    gc.collect()
    started = process_time_ns()
    if args.phase == "compile":
        table.compile()
        checksum = None
    else:
        checksum = verify(table, rows, args.repeats)
    elapsed = process_time_ns() - started
    memory = resident_bytes()
    if checksum is None:
        checksum = verify(table, rows, 1)
    measured = populate(_core.PolicyRouteTable, rows)
    gc.collect()
    tracemalloc.start()
    try:
        measured.compile()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    expected = args.size * (args.size + 1) // 2
    if args.phase == "match":
        expected *= args.repeats
    if checksum != expected or table.stats()["routes"] != args.size:
        raise RuntimeError("workload did not visit every declared route")
    metrics = {
        "cpu_ns": elapsed,
        "retained_bytes": retained,
        "peak_bytes": peak,
        **memory,
        "source": str(loaded),
        "extension_sha256": hashlib.sha256(loaded.read_bytes()).hexdigest(),
    }
    args.metrics.write_text(json.dumps(metrics) + "\n")
    print(json.dumps({"checksum": checksum, "stats": table.stats()}, sort_keys=True))


if __name__ == "__main__":
    main()
