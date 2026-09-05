from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import sys
import tempfile
import tracemalloc
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from time import process_time_ns
from types import CodeType
from unittest.mock import patch as replace


def fixture_source(count: int, mode: str) -> tuple[str, tuple[tuple[str, int], ...]]:
    if count < 1 or mode not in {"all", "first", "last"}:
        raise ValueError("choose positive functions and all, first, or last selection")
    source = []
    targets = []
    for index in range(count):
        selected = mode == "all" or index == (0 if mode == "first" else count - 1)
        name = f"{'authorize' if selected else 'ordinary'}_{index}"
        source.append(f"def {name}(value):\n    value = bool(value)\n    return value\n")
        if selected:
            targets.append((name, 3 * index + 1))
    return "".join(source), tuple(targets)


def code_facts(code: CodeType) -> tuple:
    return (
        code.co_name,
        code.co_qualname,
        code.co_code,
        code.co_consts,
        code.co_names,
        code.co_varnames,
        code.co_flags,
        code.co_argcount,
        code.co_kwonlyargcount,
    )


def verify_plan(plan, targets: tuple[tuple[str, int], ...]) -> dict:
    if not targets or plan.errors or len(plan.mutations) != len(targets):
        raise ValueError("scope plan differs from complete-mutation oracle")
    digest = hashlib.sha256()
    for mutation, (scope, line) in zip(plan.mutations, targets, strict=True):
        expected_id = f"predicate.always-true@fixture.py:{line}"
        compiled = compile(
            f"def {scope}(value):\n    return True\n",
            "oracle.py",
            "exec",
            dont_inherit=True,
            optimize=0,
        )
        expected = next(item for item in compiled.co_consts if isinstance(item, CodeType))
        if mutation.identifier != expected_id or code_facts(mutation.patch.code) != code_facts(
            expected
        ):
            raise ValueError("scope replacement differs from stdlib-compiled oracle")
        digest.update(expected_id.encode())
        digest.update(mutation.patch.code.co_code)
    return {"mutations": len(targets), "sha256": digest.hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--functions", type=int, default=128)
    parser.add_argument("--selection", choices=("all", "first", "last"), default="all")
    parser.add_argument("--iterations", type=int, default=8)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    source, targets = fixture_source(args.functions, args.selection)
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath._mutant import operators, patch, runner

    paths = [Path(module.__file__).resolve() for module in (operators, patch, runner)]
    if any(not path.is_relative_to(root) for path in paths):
        raise ValueError("mutation implementation loaded outside selected source root")
    with tempfile.TemporaryDirectory(prefix="wreath-scope-") as temporary, ExitStack() as overrides:
        directory = Path(temporary)
        path = directory / "fixture.py"
        path.write_text(source)
        spec = importlib.util.spec_from_file_location("scope_benchmark_fixture", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("fixture source must have a Python loader")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module.__name__] = module
        spec.loader.exec_module(module)

        def fixture_paths(roots: Sequence[Path]) -> list[Path]:
            return [path]

        def fixture_name(path: Path) -> str | None:
            return module.__name__

        overrides.enter_context(replace.object(runner, "discover", fixture_paths))
        overrides.enter_context(replace.object(runner, "module_name_for", fixture_name))

        def build():
            return runner.build_plan([directory], directory, operators=("predicate.always-true",))

        verify_plan(build(), targets)
        gc.collect()
        started = process_time_ns()
        for _ in range(args.iterations):
            plan = build()
        elapsed = process_time_ns() - started
        output = verify_plan(plan, targets)
        del plan
        gc.collect()
        tracemalloc.start()
        try:
            retained_plan = build()
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if verify_plan(retained_plan, targets) != output:
            raise ValueError("traced replay differs from untraced oracle")
        args.metrics.write_text(
            json.dumps(
                {
                    "cpu_ns": elapsed,
                    "traced_current_bytes": current,
                    "traced_peak_bytes": peak,
                    "sources": {
                        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
                    },
                }
            )
            + "\n"
        )
        print(json.dumps({**output, "functions": args.functions, "iterations": args.iterations}))


if __name__ == "__main__":
    main()
