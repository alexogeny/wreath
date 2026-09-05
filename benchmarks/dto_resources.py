import argparse
import dataclasses
import gc
import hashlib
import importlib.util
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns
from typing import get_type_hints

from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64


def _load(path):
    spec = importlib.util.spec_from_file_location("wreath.orm.dto_benchmark", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load Python module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _verify(projected, names, defaults):
    if not names or not dataclasses.is_dataclass(projected):
        raise RuntimeError("projection must be a nonempty dataclass")
    if tuple(item.name for item in dataclasses.fields(projected)) != names:
        raise RuntimeError("projection field order differs from declaration oracle")
    if get_type_hints(projected) != dict.fromkeys(names, int):
        raise RuntimeError("projection annotations differ from oracle")
    if projected.__name__ != "ProjectionFixtureData":
        raise RuntimeError("projection name differs from oracle")
    if dataclasses.asdict(projected()) != defaults:
        raise RuntimeError("projection defaults differ from oracle")
    values = {name: value + 100 for name, value in defaults.items()}
    if dataclasses.asdict(projected(**values)) != values:
        raise RuntimeError("projection values differ from oracle")
    return {"fields": names, "defaults": defaults, "values": values, "cached_identity": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--shape", choices=("narrow", "dense", "default"), required=True)
    parser.add_argument("--cold", action="store_true")
    parser.add_argument("--iterations", type=int, default=10000)
    args = parser.parse_args()
    if args.size < 1 or args.iterations < 1:
        parser.error("size and iterations must be positive")
    subject = _load(args.subject)
    all_names = tuple(f"field_{i}" for i in range(args.size))
    namespace = {"__annotations__": dict.fromkeys(all_names, Mapped[int])}
    namespace.update(
        {name: column(Int64, primary_key=i == 0, default=i) for i, name in enumerate(all_names)}
    )
    model = type("ProjectionFixture", (Model,), namespace, table="projection_fixture")
    names = all_names[-1:] if args.shape == "narrow" else all_names
    defaults = (
        {names[0]: args.size - 1}
        if args.shape == "narrow"
        else dict(zip(names, range(args.size), strict=True))
    )
    kwargs = {} if args.shape == "default" else {"include": tuple(reversed(names))}

    def build():
        return subject.model_dataclass(model, **kwargs)

    expected = build()
    _verify(expected, names, defaults)
    if build() is not expected:
        raise RuntimeError("projection cache identity differs from oracle")
    gc.collect()
    started = process_time_ns()
    for _ in range(args.iterations):
        if args.cold:
            delattr(model, "__wreath_dataclass_projections__")
        projected = build()
        if not args.cold and projected is not expected:
            raise RuntimeError("warm projection cache identity changed")
    cpu_ns = process_time_ns() - started
    result = _verify(projected, names, defaults)
    if build() is not projected:
        raise RuntimeError("final projection cache identity differs from oracle")
    gc.collect()
    if args.cold:
        delattr(model, "__wreath_dataclass_projections__")
    tracemalloc.start()
    projected = build()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if _verify(projected, names, defaults) != result or build() is not projected:
        raise RuntimeError("traced projection differs from oracle")
    args.metrics.write_text(
        json.dumps(
            {
                "workload_cpu_ns": cpu_ns,
                "peak_bytes": peak,
                "iterations": args.iterations,
                "subject_sha256": hashlib.sha256(args.subject.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
        + "\n"
    )
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
