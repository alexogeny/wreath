from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
import time
import tracemalloc
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure predicate-only AST traversal work.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    operators = importlib.import_module("wreath._mutant.operators")
    if operators.__file__ is None or not Path(operators.__file__).is_relative_to(source):
        raise RuntimeError("loaded operators outside requested source")
    metrics = {}
    outputs = []
    for count, repetitions in ((32, 200), (512, 50)):
        for mode in ("predicate", "nonpredicate", "no_return"):
            name = f"{mode}_{count}"
            function = "process" if mode == "nonpredicate" else "authorize"
            final = "principal" if mode == "no_return" else "return principal"
            text = (
                f"def {function}(principal):\n"
                + "".join(f"    value_{index} = principal\n" for index in range(count))
                + f"    {final}\n"
            )
            tree = ast.parse(text)
            context = operators._Context(module=None, tree=tree, scopes=operators.tag(tree))
            tracemalloc.start()
            found = list(operators._predicate_operators(context))
            retained, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            metrics[f"{name}_retained_bytes"] = retained
            metrics[f"{name}_peak_bytes"] = peak
            started = time.process_time_ns()
            for _ in range(repetitions):
                found = list(operators._predicate_operators(context))
            metrics[f"{name}_cpu_ns"] = time.process_time_ns() - started
            observed = [(item.operator, item.line, item.scope, item.watch) for item in found]
            expected = (
                [("predicate.always-true", 1, ("authorize",), tuple(range(1, count + 3)))]
                if mode == "predicate"
                else []
            )
            if observed != expected:
                raise RuntimeError(f"incorrect predicate or no-op result for {name}: {observed}")
            outputs.append((name, observed))
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
