import argparse
import gc
import hashlib
import importlib.util
import json
import sys
import tempfile
import tracemalloc
from pathlib import Path
from time import process_time_ns


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load Python module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _result(plan):
    from wreath._mutant.patch import ValuePatch

    if plan.errors:
        raise RuntimeError(f"unexpected planning errors: {plan.errors}")
    result = []
    for mutation in plan.mutations:
        if not isinstance(mutation.patch, ValuePatch):
            raise TypeError("expected a value patch")
        result.append(
            [
                mutation.identifier,
                list(mutation.patch.path),
                mutation.patch.value,
                [
                    [target.scope, list(target.positional), list(target.keywords)]
                    for target in mutation.patch.captured_defaults
                ],
            ]
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--values", type=int, default=64)
    parser.add_argument("--selected", type=int, default=64)
    parser.add_argument("--defaults", type=int, default=128)
    parser.add_argument("--iterations", type=int, default=16)
    args = parser.parse_args()
    if (
        args.values < 1
        or args.iterations < 1
        or args.defaults < 0
        or not 0 <= args.selected <= args.values
    ):
        parser.error("require positive sizes, nonnegative defaults, and 0 <= selected <= values")
    subject = _load("wreath._mutant.defaults_benchmark", args.runner)
    text = "".join(f"LIMIT_{i} = {i + 1}\n" for i in range(args.values))
    text += "".join(
        f"def read_{i}(value=LIMIT_{i % args.values}, *, limit=LIMIT_{i % args.values}):\n"
        "    return value, limit\n"
        for i in range(args.defaults)
    )
    selected = frozenset(f"value.widen-bound@fixture.py:{i + 1}" for i in range(args.selected))
    expected = [
        [
            f"value.widen-bound@fixture.py:{i + 1}",
            [f"LIMIT_{i}"],
            1 << 40,
            [[f"read_{j}", [0], ["limit"]] for j in range(i, args.defaults, args.values)],
        ]
        for i in range(args.selected)
    ]
    with tempfile.TemporaryDirectory(prefix="wreath-defaults-benchmark-") as directory:
        root = Path(directory)
        source = root / "fixture.py"
        source.write_text(text)
        module = _load("_wreath_defaults_benchmark_fixture", source)
        subject.discover = lambda roots: [source]
        subject.module_name_for = lambda path: module.__name__

        def build():
            return subject.build_plan([root], root, selected_ids=selected)

        gc.collect()
        started = process_time_ns()
        for _ in range(args.iterations):
            plan = build()
        cpu_ns = process_time_ns() - started
        actual = _result(plan)
        if actual != expected:
            raise RuntimeError("captured-default plan differs from exact target oracle")
        del plan
        gc.collect()
        tracemalloc.start()
        plan = build()
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if _result(plan) != expected:
            raise RuntimeError("traced plan differs from exact target oracle")
        args.metrics.write_text(
            json.dumps(
                {
                    "workload_cpu_ns": cpu_ns,
                    "retained_bytes": retained,
                    "peak_bytes": peak,
                    "iterations": args.iterations,
                    "runner_sha256": hashlib.sha256(args.runner.read_bytes()).hexdigest(),
                },
                sort_keys=True,
            )
            + "\n"
        )
        print(json.dumps(actual, separators=(",", ":")))


if __name__ == "__main__":
    main()
