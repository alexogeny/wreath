import argparse
import gc
import hashlib
import importlib.util
import json
import sys
import tempfile
import tracemalloc
from dataclasses import asdict
from pathlib import Path
from time import process_time_ns
from types import ModuleType
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--candidates", type=int, default=2048)
    parser.add_argument("--selected", type=int, default=192)
    parser.add_argument("--families", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--synthetic-scan", action="store_true")
    args = parser.parse_args()
    if min(args.candidates, args.selected, args.families, args.iterations) < 1:
        parser.error("sizes must be positive")
    if args.families > 1 and not args.synthetic_scan:
        parser.error("multiple families require --synthetic-scan")
    from wreath._mutant.operators import Candidate

    spec = importlib.util.spec_from_file_location("wreath._mutant.rank_benchmark", args.runner)
    subject = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = subject
    spec.loader.exec_module(subject)
    module = ModuleType("rank_benchmark_fixture")
    sys.modules[module.__name__] = module
    values = {f"LIMIT_{index}": index + 1 for index in range(args.candidates)}
    vars(module).update(values)
    source_text = "" if args.synthetic_scan else "".join(
        f"{name} = {value}\n" for name, value in values.items()
    )
    if args.synthetic_scan:
        candidates = [
            Candidate(f"family.{index % args.families:04}", "fixture", index + 1, ())
            for index in range(args.candidates)
        ]
        subject.scan = lambda *args, **kwargs: candidates
    with tempfile.TemporaryDirectory(prefix="wreath-rank-benchmark-") as directory:
        root = Path(directory)
        fixture = root / "fixture.py"
        fixture.write_text(source_text)
        subject.discover = lambda roots: [fixture]
        subject.module_name_for = lambda path: module.__name__

        def select():
            return subject.select_sample([root], root, args.selected)

        gc.collect()
        started = process_time_ns()
        for _ in range(args.iterations):
            selected = select()
        cpu_ns = process_time_ns() - started
        if selected.eligible_candidates != args.candidates:
            raise RuntimeError("candidate corpus size differs from requested workload")
        if len(selected.identifiers) != min(args.candidates, args.selected):
            raise RuntimeError("sample size differs from requested workload")
        if selected.errors or selected.unsupported_declarations:
            raise RuntimeError("fixture did not produce a clean candidate corpus")
        hashes = 0
        original = subject.hashlib.blake2b

        def counted(*args, **kwargs):
            nonlocal hashes
            hashes += 1
            return original(*args, **kwargs)

        with patch.object(subject.hashlib, "blake2b", counted):
            checked = select()
        gc.collect()
        tracemalloc.start()
        traced = select()
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if checked != selected or traced != selected:
            raise RuntimeError("instrumented sample differs from timed sample")
        metrics = {
            "workload_cpu_ns": cpu_ns,
            "hash_calls": hashes,
            "peak_traced_bytes": peak_bytes,
            "iterations": args.iterations,
            "runner_sha256": hashlib.sha256(args.runner.read_bytes()).hexdigest(),
        }
        args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
        print(json.dumps(asdict(selected), sort_keys=True))


if __name__ == "__main__":
    main()
