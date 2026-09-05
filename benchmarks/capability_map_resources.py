from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure capability capacity scans with native same-size controls."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    module = importlib.import_module("wreath._capability_map")
    if not Path(module.__file__).is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded CapabilityMap outside source: {module.__file__}")
    mapping_type = module.CapabilityMap
    table_type = module.KV
    cases = [
        (f"{mode}_update_{size}", mode, size, "update", 5000)
        for size in (512, 1024, 2048)
        for mode in ("evict", "native")
    ]
    cases.extend(
        [
            ("earliest_update", "earliest", 1024, "update", 5000),
            ("refuse_same_deadline", "refuse", 64, "update", 10000),
        ]
    )
    cases.extend(
        (f"{mode}_insert", mode, 512, "insert", 0) for mode in ("evict", "earliest", "refuse")
    )
    metrics = {}
    outputs = []

    def run(mode: str, size: int, operation: str, count: int) -> Any:
        if mode == "native":
            table = table_type(max_entries=size, ttl=100, clock=lambda: 0.0)
            for index in range(size):
                table.set(index, index, None, 0)
            for index in range(count):
                table.set(0, index, None, 0)
            return table
        mapping = mapping_type(max_entries=size, ttl=100, overflow=mode, clock=lambda: 0.0)
        for index in range(size):
            mapping.put(index, index, now=0)
        for index in range(count):
            mapping.put(0, index, now=0)
        return mapping

    for name, mode, size, operation, count in cases:
        for _ in range(2):
            run(mode, size, operation, count)
        tracemalloc.start()
        try:
            result = run(mode, size, operation, count)
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        metrics[f"{name}_retained_bytes"] = retained
        metrics[f"{name}_peak_bytes"] = peak
        started = time.process_time_ns()
        for _ in range(3):
            result = run(mode, size, operation, count)
        metrics[f"{name}_cpu_ns"] = time.process_time_ns() - started
        table = result if mode == "native" else result._table
        observed = (table.count(now=0), table.peek(0, None, 0))
        expected = (size, count - 1 if count else 0)
        if observed != expected:
            raise RuntimeError(f"incorrect capability results for {name}: {observed}")
        outputs.append((name, size, operation, count, observed))
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
