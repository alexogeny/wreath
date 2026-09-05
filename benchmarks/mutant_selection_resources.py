from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure selected-file parsing with same-size controls."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    runner = importlib.import_module("wreath._mutant.runner")
    if runner.__file__ is None or not Path(runner.__file__).is_relative_to(source):
        raise RuntimeError("loaded mutation runner outside requested source")
    metrics = {}
    outputs = []
    with tempfile.TemporaryDirectory(prefix="mutant-selection-") as directory:
        root = Path(directory)
        package = root / "selection_fixture"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        sys.path.insert(0, directory)
        files = []
        for index in range(1000):
            path = package / f"source_{index}.py"
            content = (
                "def authorize(value):\n    if value == 1:\n        return value\n    return None\n"
                if index == 0
                else "\n".join(f"value_{item} = {item}" for item in range(64)) + "\n"
            )
            path.write_text(content, encoding="utf-8")
            importlib.import_module(f"selection_fixture.source_{index}")
            files.append(path)
        for size in (100, 1000):
            for selection in ("one", "all"):
                name = f"{selection}_{size}"
                chosen = files[:1] if selection == "one" else files[:size]
                touched = {str(path.relative_to(root)): range(1, 1000000) for path in chosen}
                with (
                    patch.object(runner, "discover", return_value=files[:size]),
                    patch.object(runner, "changed_lines", return_value=touched),
                ):
                    plan = runner.build_plan([package], root, changed="HEAD")
                    started = time.process_time_ns()
                    for _ in range(3):
                        plan = runner.build_plan([package], root, changed="HEAD")
                    metrics[f"{name}_cpu_ns"] = time.process_time_ns() - started
                if plan.errors or not plan.mutations or plan.sources != list(touched):
                    raise RuntimeError(f"incorrect selected sources or empty plan: {name}")
                if any("source_0.py" not in mutation.identifier for mutation in plan.mutations):
                    raise RuntimeError("no-op sources produced mutations")
                outputs.append(
                    (
                        name,
                        [mutation.identifier for mutation in plan.mutations],
                        plan.watch,
                        {Path(path).name: sorted(lines) for path, lines in plan.watched.items()},
                        len(plan.sources),
                    )
                )
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs, sort_keys=True))


if __name__ == "__main__":
    main()
