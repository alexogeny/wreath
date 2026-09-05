import argparse
import gc
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from time import process_time_ns
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--values", type=int, default=256)
    parser.add_argument("--selected", type=int, default=256)
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--function-only", action="store_true")
    args = parser.parse_args()
    if args.values < 1 or args.iterations < 1 or not 0 <= args.selected <= args.values:
        parser.error("require positive sizes and 0 <= selected <= values")
    from wreath._mutant import runner

    spec = importlib.util.spec_from_file_location("wreath._mutant.watch_benchmark", args.runner)
    subject = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = subject
    spec.loader.exec_module(subject)
    source_text = "".join(f"LIMIT_{index} = {index + 1}\n" for index in range(args.values))
    source_text += "def authorize(value):\n    value = bool(value)\n    return value\n"
    selected = frozenset(
        f"value.widen-bound@fixture.py:{index + 1}" for index in range(args.selected)
    )
    if args.function_only:
        selected = frozenset((f"predicate.always-true@fixture.py:{args.values + 1}",))
    with tempfile.TemporaryDirectory(prefix="wreath-watch-benchmark-") as directory:
        root = Path(directory)
        fixture = root / "fixture.py"
        fixture.write_text(source_text)
        fixture_spec = importlib.util.spec_from_file_location("watch_benchmark_fixture", fixture)
        module = importlib.util.module_from_spec(fixture_spec)
        sys.modules[module.__name__] = module
        fixture_spec.loader.exec_module(module)
        subject.discover = lambda roots: [fixture]
        subject.module_name_for = lambda path: module.__name__
        counts = {"source_reads": 0, "source_bytes": 0}
        original = Path.read_text

        def counted(path, *read_args, **kwargs):
            text = original(path, *read_args, **kwargs)
            if path == fixture:
                counts["source_reads"] += 1
                counts["source_bytes"] += len(text)
            return text

        gc.collect()
        started = process_time_ns()
        with patch.object(Path, "read_text", counted):
            for _ in range(args.iterations):
                watched, whole = subject.watch_selected_identifiers([root], root, selected)
        metrics = {"workload_cpu_ns": process_time_ns() - started, **counts}
        if args.function_only:
            expected_lines = list(range(args.values + 1, args.values + 4))
        else:
            expected_lines = list(range(1, args.values + 4)) if args.selected else []
        expected = {str(fixture): frozenset(expected_lines)} if expected_lines else {}
        expected_whole = (
            frozenset((str(fixture),)) if args.selected and not args.function_only else frozenset()
        )
        if watched != expected or whole != expected_whole:
            raise RuntimeError("Watch output differs from source-line oracle")
        metrics["sources"] = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (args.runner, Path(runner.__file__))
        }
        args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
        print(json.dumps([expected_lines, bool(whole)]))


if __name__ == "__main__":
    main()
