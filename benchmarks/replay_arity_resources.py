from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns


def resident_bytes():
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            values[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument(
        "--scenario", choices=("valid", "first", "middle", "extra", "empty"), required=True
    )
    parser.add_argument("--iterations", type=int, default=512)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if args.size < 2 or args.iterations < 1:
        parser.error("size must be at least two and iterations positive")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath import _replay_adapters
    from wreath.postgres import PostgresError

    source = Path(_replay_adapters.__file__).resolve()
    if not source.is_relative_to(root):
        raise RuntimeError("Replay adapter source outside requested root")
    declared = 0 if args.scenario == "empty" else args.size
    references = list(range(1, declared + 1))
    expected = "ok"
    if args.scenario in {"first", "middle"}:
        missing = 1 if args.scenario == "first" else args.size // 2
        references.remove(missing)
        expected = f"could not determine data type of parameter ${missing}"
    elif args.scenario == "extra":
        references.append(args.size + 1)
        expected = (
            f"bind message supplies {declared} parameters, "
            f"but prepared statement requires {args.size + 1}"
        )
    sql = "SELECT " + (", ".join(f"${index}" for index in references) or "1")
    parameters = (None,) * declared
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    started = process_time_ns()
    for _ in range(args.iterations):
        result = "ok"
        try:
            _replay_adapters.refuse_parameter_arity(sql, parameters)
        except PostgresError as error:
            result = str(error)
        if result != expected:
            raise RuntimeError("Parameter refusal differs from PostgreSQL precedence oracle")
    metrics = {"workload_cpu_ns": process_time_ns() - started}
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    metrics.update(
        source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps({"calls": args.iterations, "result": result}, sort_keys=True))


if __name__ == "__main__":
    main()
