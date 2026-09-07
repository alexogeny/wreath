from __future__ import annotations

import argparse
import gc
import importlib
import json
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any
from unittest.mock import patch


def resident_bytes() -> dict[str, int]:
    fields = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        if line.startswith(("Rss:", "Pss:")):
            name, value, _ = line.split()
            fields[name[:-1].lower()] = int(value) * 1024
    return fields


def measure_resident(module: Any) -> tuple[dict[str, int], list[tuple[int, bool]]]:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:3] != ["git", "-C", "/unused"] or kwargs["timeout"] != 60:
            raise RuntimeError("unexpected subprocess request")
        output = "" if command[3] == "diff" else "\n".join(f"new{index}.py" for index in range(8))
        return subprocess.CompletedProcess(command, 0, output, "")

    with patch.object(subprocess, "run", run):
        gc.collect()
        before = resident_bytes()
        result = module.changed_lines(Path("/unused"), "HEAD")
        gc.collect()
        after = resident_bytes()
        observed = [
            (len(lines), 999999 in lines and 1000000 not in lines) for lines in result.values()
        ]
        if observed != [(999999, True)] * 8:
            raise RuntimeError("incorrect resident scenario membership")
        metrics = {
            f"{field}_retained_delta_bytes": after[field] - before[field] for field in before
        }
        metrics.update({f"{field}_before_bytes": value for field, value in before.items()})
        metrics.update({f"{field}_after_bytes": value for field, value in after.items()})
    return metrics, observed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure changed-line membership storage with stubbed Git."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--resident-only", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    module = importlib.import_module("wreath._mutant.runner")
    if module.__file__ is None or not Path(module.__file__).is_relative_to(source):
        raise RuntimeError("loaded mutation runner outside requested source")
    if args.resident_only:
        metrics, observed = measure_resident(module)
        args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
        print(json.dumps(observed))
        return
    metrics = {}
    outputs = []
    patcher = None
    try:
        for count in (0, 1, 4, 8):
            name = f"files_{count}"
            paths = [f"new{index}.py" for index in range(count)]
            diff = "+++ b/tracked.py\n@@ -1 +3,2 @@\n"
            untracked = "\n".join(paths)

            def run(
                command: list[str], *, diff: str = diff, untracked: str = untracked, **kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                if command[:3] != ["git", "-C", "/unused"] or kwargs["timeout"] != 60:
                    raise RuntimeError("unexpected subprocess request")
                return subprocess.CompletedProcess(
                    command, 0, diff if command[3] == "diff" else untracked, ""
                )

            if patcher is not None:
                patcher.stop()
            patcher = patch.object(subprocess, "run", run)
            patcher.start()
            tracemalloc.start()
            result = module.changed_lines(Path("/unused"), "HEAD")
            retained, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            metrics[f"{name}_retained_bytes"] = retained
            metrics[f"{name}_peak_bytes"] = peak
            if result["tracked.py"] != {3, 4} or list(result) != ["tracked.py", *paths]:
                raise RuntimeError("incorrect tracked lines or ordering")
            expected = [False, False, True, True, True, False]
            for path in paths:
                observed = [line in result[path] for line in (-1, 0, 1, 500000, 999999, 1000000)]
                if observed != expected:
                    raise RuntimeError("incorrect untracked membership")
            members = result[paths[0]] if paths else result["tracked.py"]
            started = time.process_time_ns()
            found = 0
            for _ in range(10000):
                for line in (0, 1, 3, 500000, 999999, 1000000):
                    found += line in members
            metrics[f"{name}_membership_cpu_ns"] = time.process_time_ns() - started
            if found != (40000 if paths else 10000):
                raise RuntimeError("incorrect repeated membership")
            del result, members
            started = time.process_time_ns()
            for _ in range(3 if count else 10000):
                result = module.changed_lines(Path("/unused"), "HEAD")
                del result
            metrics[f"{name}_cpu_ns"] = time.process_time_ns() - started
            outputs.append((name, found))
    finally:
        if patcher is not None:
            patcher.stop()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
