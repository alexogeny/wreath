from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--scenario", choices=("sample", "history", "pretty"), required=True)
    parser.add_argument("--entries", type=int, default=5000)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.entries <= 5000 or not 1 <= args.repeats <= 10:
        parser.error("entries must be 1..5000 and repeats 1..10")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath import _test_runner as runner

    source = Path(runner.__file__).resolve()
    if not source.is_relative_to(args.source.resolve()):
        raise RuntimeError("history benchmark loaded the wrong source root")
    path = args.metrics.with_suffix(".history.json")
    names = [f"test_fixture.py::test_{index}" for index in range(args.entries)]
    selected = frozenset(names)
    stamp = "2026-09-05T00:00:00Z"
    report = {
        "finished_at": stamp,
        "exitstatus": 0,
        "wall_seconds": 1.0,
        "workers": 2,
        "counts": {"passed": args.entries},
        "files": [],
        "tests": [{"nodeid": name, "seconds": 0.25, "outcome": "passed"} for name in names],
    }
    empty = {"version": 1, "runs": [], "files": {}, "tests": {}}
    path.write_text(json.dumps(empty) + "\n")

    def write():
        if args.scenario == "sample":
            runner._write_mutation_sample_cache(path, {}, selected, {}, frozenset(), {})
        elif args.scenario == "history":
            runner._update_history(path, report)
        else:
            runner._atomic_json(path, report)

    started = process_time_ns()
    for _ in range(args.repeats):
        write()
    elapsed = process_time_ns() - started
    tracemalloc.start()
    try:
        write()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if args.scenario == "pretty":
        expected = report
    elif args.scenario == "sample":
        expected = {
            **empty,
            "mutation_sample": {
                "key": {},
                "selected": sorted(names),
                "watched": {},
                "whole_files": [],
                "selection": {},
            },
        }
    else:
        run = {
            key: report[key]
            for key in ("finished_at", "exitstatus", "wall_seconds", "workers", "counts")
        }
        expected = {
            **empty,
            "runs": [run] * (args.repeats + 1),
            "tests": {
                name: {
                    "samples": args.repeats + 1,
                    "mean_seconds": 0.25,
                    "last_seconds": 0.25,
                    "last_outcome": "passed",
                    "last_seen": stamp,
                }
                for name in names
            },
        }
    raw = path.read_bytes()
    document = json.loads(raw)
    if document != expected or not raw.endswith(b"\n"):
        raise RuntimeError("stored history differs from independent schema/value oracle")
    if (
        args.scenario == "pretty"
        and raw != (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode()
    ):
        raise RuntimeError("human-readable report formatting changed")
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "peak_bytes": peak,
                "output_bytes": len(raw),
                "source_path": str(source),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "scenario": args.scenario,
                "entries": args.entries,
                "sha256": hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
