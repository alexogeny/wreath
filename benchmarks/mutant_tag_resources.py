import argparse
import ast
import gc
import hashlib
import importlib.util
import json
import sys
import tempfile
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
    parser.add_argument("--scenario", choices=("plan", "sample", "watch", "scan"), required=True)
    parser.add_argument("--functions", type=int, default=128)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if min(args.functions, args.iterations) < 1:
        parser.error("workload sizes must be positive")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath._mutant import operators, runner

    sources = [Path(module.__file__).resolve() for module in (operators, runner)]
    if any(not source.is_relative_to(root) for source in sources):
        raise RuntimeError("Mutation source outside requested root")
    source_text = "\n".join(
        f"def {'authorize' if index == 0 else 'ordinary'}_{index}(value):\n"
        "    value = bool(value)\n    return value\n"
        for index in range(args.functions)
    )
    expected_ids = ["predicate.always-true@fixture.py:1"]
    selected = frozenset(expected_ids[:1])
    with tempfile.TemporaryDirectory(prefix="wreath-mutant-tag-") as directory:
        fixture_root = Path(directory)
        fixture = fixture_root / "fixture.py"
        fixture.write_text(source_text)
        spec = importlib.util.spec_from_file_location("tag_benchmark", fixture)
        module = importlib.util.module_from_spec(spec)
        sys.modules["tag_benchmark"] = module
        spec.loader.exec_module(module)
        runner.discover = lambda roots: [fixture]
        runner.module_name_for = lambda path: "tag_benchmark"
        gc.collect()
        before = resident_bytes()
        if args.trace:
            tracemalloc.start()
        start = process_time_ns()
        for _ in range(args.iterations):
            if args.scenario == "plan":
                result = runner.build_plan([fixture_root], fixture_root)
            elif args.scenario == "sample":
                result = runner.select_sample([fixture_root], fixture_root, args.functions)
            elif args.scenario == "watch":
                result = runner.watch_selected_identifiers([fixture_root], fixture_root, selected)
            else:
                result = operators.scan(ast.parse(source_text), "tag_benchmark")
        metrics = {"workload_cpu_ns": process_time_ns() - start}
        if args.trace:
            metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        metrics.update(resident_bytes())
        metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
        if args.scenario == "plan":
            actual = [mutation.identifier for mutation in result.mutations]
            valid = actual == expected_ids[:1] and not result.errors
        elif args.scenario == "sample":
            actual = list(result.identifiers)
            valid = set(actual) == set(expected_ids) and not result.errors
        elif args.scenario == "watch":
            watched, whole = result
            valid = watched == {str(fixture): frozenset([1, 2, 3])} and not whole
            actual = [sorted(lines) for lines in watched.values()]
        else:
            actual = [f"{candidate.operator}@fixture.py:{candidate.line}" for candidate in result]
            valid = actual == expected_ids
        if not valid:
            raise RuntimeError("Mutation selection differs from source declaration oracle")
        metrics["sources"] = {
            str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources
        }
        args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
        print(json.dumps(actual))


if __name__ == "__main__":
    main()
