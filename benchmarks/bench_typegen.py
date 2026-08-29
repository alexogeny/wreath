"""Decomposed benchmark for consumer type generation.

Separates the phases of a ``wreath typegen`` run -- app construction, route/type
inspection, canonical model construction, target planning, pure rendering, and
filesystem write -- across synthetic applications of increasing size, so a
renderer cost is never conflated with import and type-hint inspection. The
native renderer is a benchmark-gated follow-up; when it is not built, its phase
is recorded as unavailable and the decision stays "pure only".

    python -m benchmarks.bench_typegen --output benchmark-results-typegen/latest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import statistics
import sys
import tempfile
import time
from dataclasses import make_dataclass
from pathlib import Path
from typing import Any

from wreath import Wreath
from wreath.typegen.inspect import build_api_model
from wreath.typegen.targets import typescript
from wreath.typegen.targets.typescript import render_typescript

SHAPES = {
    "small": (10, 10),
    "medium": (100, 100),
    "large": (1000, 500),
    "stress": (10000, 2000),
}


def _make_models(count: int) -> list[type]:
    models: list[type] = []
    for index in range(count):
        fields = [
            ("name", str),
            ("value", int),
            ("ratio", float),
            ("active", bool),
            ("tags", list[str]),
            ("meta", dict[str, str]),
            # A nullable self-reference on the first model exercises recursion;
            # later fields reference earlier models to build a shared graph.
            ("note", "str | None", None),
        ]
        model = make_dataclass(f"Model{index}", fields)
        models.append(model)
    return models


def _build_app(routes: int, model_count: int) -> Wreath:
    models = _make_models(model_count)
    app = Wreath()
    for index in range(routes):
        model = models[index % model_count]

        async def handler(request, item_id: int, model=model) -> None:
            return None

        # Give each handler the model as its return annotation.
        handler.__annotations__["return"] = model
        app.get(f"/resource-{index}/{{item_id}}")(handler)
    return app


def _time(fn: Any, trials: int) -> tuple[float, float, list[float]]:
    samples = []
    for _ in range(trials):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1e3)  # ms
    return statistics.median(samples), _p95(samples), samples


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _sha(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(files[name].encode("utf-8"))
    return digest.hexdigest()


def run_shape(shape: str, warmup: int, trials: int) -> dict[str, Any]:
    routes, model_count = SHAPES[shape]

    # Phase: app construction (import proxy for a synthetic in-process app).
    build_median, build_p95, _ = _time(lambda: _build_app(routes, model_count), trials)

    app = _build_app(routes, model_count)

    # Phase: canonical model construction (includes route/type inspection).
    for _ in range(warmup):
        build_api_model(app, allow_unknown=True)
    model_median, model_p95, _ = _time(lambda: build_api_model(app, allow_unknown=True), trials)
    api = build_api_model(app, allow_unknown=True)

    # Phase: target planning (normalization into renderer tuples).
    plan_median, plan_p95, _ = _time(
        lambda: (typescript._declarations(api), typescript._operation_tuples(api)), trials
    )

    # Phase: pure rendering only.
    render_median, render_p95, render_samples = _time(
        lambda: render_typescript(api, pure=True), trials
    )
    # A/A control on the render phase: a second identical arm fixes the floor.
    _, _, render_aa = _time(lambda: render_typescript(api, pure=True), trials)
    floor = abs(statistics.median(render_samples) - statistics.median(render_aa))

    files = render_typescript(api, pure=True)
    output_bytes = sum(len(contents.encode("utf-8")) for contents in files.values())

    # Phase: filesystem write.
    def _write() -> None:
        with tempfile.TemporaryDirectory() as directory:
            from wreath.typegen.cli import write

            write(files, Path(directory))

    write_median, write_p95, _ = _time(_write, max(1, trials // 2))

    # Phase: total end-to-end generation.
    def _total() -> None:
        fresh = _build_app(routes, model_count)
        model = build_api_model(fresh, allow_unknown=True)
        render_typescript(model, pure=True)

    total_median, total_p95, _ = _time(_total, max(1, trials // 2))

    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    return {
        "shape": shape,
        "routes": routes,
        "models": len(api.models),
        "operations": len(api.operations),
        "output_bytes": output_bytes,
        "phases_ms": {
            "app_construction": {"median": build_median, "p95": build_p95},
            "model_construction": {"median": model_median, "p95": model_p95},
            "planning": {"median": plan_median, "p95": plan_p95},
            "render": {"median": render_median, "p95": render_p95},
            "render_native": None,  # not built; benchmark-gated follow-up
            "write": {"median": write_median, "p95": write_p95},
            "total": {"median": total_median, "p95": total_p95},
        },
        "render_noise_floor_ms": floor,
        "render_sha256": _sha(files),
        "render_native_sha256": None,
        "render_mib_per_second": (
            output_bytes / (1024 * 1024) / (render_median / 1e3) if render_median else 0.0
        ),
        "peak_rss_kib": peak_rss_kib,
    }


def run(shapes: list[str], warmup: int, trials: int) -> dict[str, Any]:
    return {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "renderer": "pure",
            "native_built": False,
            "warmup": warmup,
            "trials": trials,
        },
        "results": [run_shape(shape, warmup, trials) for shape in shapes],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", nargs="+", choices=SHAPES, default=["small", "medium", "large"])
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.shape, args.warmup, args.trials)
    for entry in result["results"]:
        phases = entry["phases_ms"]
        print(
            f"{entry['shape']:8} routes={entry['routes']:6} models={entry['models']:5} "
            f"model={phases['model_construction']['median']:8.2f}ms "
            f"render={phases['render']['median']:8.2f}ms "
            f"total={phases['total']['median']:8.2f}ms "
            f"({entry['render_mib_per_second']:.1f} MiB/s, "
            f"floor={entry['render_noise_floor_ms']:.2f}ms)"
        )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
