from __future__ import annotations

import argparse
import gc
import importlib
import json
import sys
import time
import tracemalloc
from pathlib import Path


def resident_bytes() -> dict[str, int]:
    fields = Path("/proc/self/smaps_rollup").read_text().splitlines()
    return {
        parts[0][:-1].lower(): int(parts[1]) * 1024
        for line in fields
        if (parts := line.split())[0] in {"Rss:", "Pss:"}
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure empty and warm queue resources.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--resident", choices=("fifo", "heap", "lanes"))
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    module = importlib.import_module("wreath.queue")
    native = importlib.import_module("wreath._native._core")
    for loaded in (module, native):
        if loaded.__file__ is None or not Path(loaded.__file__).is_relative_to(source):
            raise RuntimeError("queue implementation loaded outside selected source")
    kinds = {"fifo": module.Queue, "heap": module.PriorityQueue}
    metrics = {}
    outputs = []
    if args.resident:
        gc.collect()
        before = resident_bytes()
        if args.resident == "lanes":
            values = [module.RoundRobin(capacity=4096, lanes=map(str, range(128)))]
        else:
            values = [kinds[args.resident](capacity=4096) for _ in range(128)]
        gc.collect()
        after = resident_bytes()
        if any(len(value) for value in values):
            raise RuntimeError("new queues were not empty")
        for field in before:
            metrics[f"{field}_before_bytes"] = before[field]
            metrics[f"{field}_after_bytes"] = after[field]
            metrics[f"{field}_growth_bytes"] = after[field] - before[field]
        outputs.append((args.resident, len(values), 0))
    else:
        for name, kind in kinds.items():
            tracemalloc.start()
            values = [kind(capacity=65536) for _ in range(4)]
            retained, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            metrics[f"{name}_empty_retained_bytes"] = retained
            metrics[f"{name}_empty_peak_bytes"] = peak
            if any(len(value) or value.capacity != 65536 for value in values):
                raise RuntimeError("incorrect empty queue state")
            del values

            started = time.process_time_ns()
            for _ in range(400):
                value = kind(capacity=65536)
                del value
            metrics[f"{name}_empty_cpu_ns"] = time.process_time_ns() - started

            values = [kind(capacity=4096) for _ in range(128)]
            started = time.process_time_ns()
            for value in values:
                value.offer(7)
            metrics[f"{name}_first_offer_cpu_ns"] = time.process_time_ns() - started
            if [value.get_nowait() for value in values] != [7] * 128:
                raise RuntimeError("first offer lost values")
            del values

            started = time.process_time_ns()
            for _ in range(2000):
                value = kind(capacity=4096)
                value.offer(7)
                value.get_nowait()
                del value
            metrics[f"{name}_cold_lifecycle_cpu_ns"] = time.process_time_ns() - started

            started = time.process_time_ns()
            for _ in range(20000):
                value = kind(capacity=1)
                value.offer(7)
                value.get_nowait()
                del value
            metrics[f"{name}_tiny_lifecycle_cpu_ns"] = time.process_time_ns() - started

            value = kind(capacity=4096)
            value.offer(7)
            value.get_nowait()
            started = time.process_time_ns()
            total = 0
            for _ in range(500000):
                value.offer(7)
                total += value.get_nowait()
            metrics[f"{name}_warm_cpu_ns"] = time.process_time_ns() - started
            if total != 3500000 or (value.offered, value.dropped) != (500001, 0):
                raise RuntimeError("warm queue values or counters differ")

            for _ in range(256):
                value.offer(7)
            started = time.process_time_ns()
            total = 0
            for _ in range(500000):
                value.offer(7)
                total += value.get_nowait()
            metrics[f"{name}_dense_cpu_ns"] = time.process_time_ns() - started
            if total != 3500000 or len(value) != 256:
                raise RuntimeError("dense queue lost resident values")
            if value.drain() != [7] * 256:
                raise RuntimeError("dense queue backlog differs")

            started = time.process_time_ns()
            for _ in range(1000):
                for item in range(256):
                    value.offer(item)
                result = value.drain()
            metrics[f"{name}_refill_cpu_ns"] = time.process_time_ns() - started
            if result != list(range(256)) or len(value) != 0:
                raise RuntimeError("refill order or drain state differs")
            outputs.append((name, total, result, value.offered, value.dropped))

        tracemalloc.start()
        value = module.RoundRobin(capacity=65536, lanes=map(str, range(32)))
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        metrics["lanes_empty_retained_bytes"] = retained
        metrics["lanes_empty_peak_bytes"] = peak
        if len(value) != 0:
            raise RuntimeError("new round-robin lanes were not empty")
        outputs.append(("lanes", len(value)))
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
