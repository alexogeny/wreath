"""Measure the write-path validation seam: two passes versus one.

``dataclass_then_model`` is the shape you write without this seam: request
binding proves the payload against a dataclass, then every assignment to the
model proves the same value again through its column's type. Each field is
checked twice and copied twice.

``model_body`` validates the payload against the model's own columns in a
single pass, straight into its cells. The column type is still the only source
of the type rules -- this is not a second engine -- it just runs once.

``coerce_only`` is the floor: the type checks alone, with no binding, no
dataclass, and no model.

Needs no database: this measures the boundary between a request and a model.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wreath.binding import validate
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Bool, Int64, Text
from wreath.orm.validation import compile_model_validator


class Item(Model, table="validate_bench_items"):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)
    slug: Mapped[str] = column(Text)
    quantity: Mapped[int] = column(Int64)
    weight: Mapped[int] = column(Int64)
    enabled: Mapped[bool] = column(Bool)


@dataclass
class NewItem:
    """The intermediate a two-pass write path needs."""

    label: str
    slug: str
    quantity: int
    weight: int
    enabled: bool


PAYLOAD = {
    "label": "a bolt",
    "slug": "a-bolt",
    "quantity": 5,
    "weight": 120,
    "enabled": True,
}


def dataclass_then_model(payload: dict[str, Any]) -> Item:
    checked = validate(NewItem, payload, ("body",))
    # Every assignment re-proves a value the dataclass already proved.
    return Item(
        label=checked.label,
        slug=checked.slug,
        quantity=checked.quantity,
        weight=checked.weight,
        enabled=checked.enabled,
    )


def coerce_only(payload: dict[str, Any]) -> tuple[Any, ...]:
    columns = Item.__wreath_columns__[1:]
    return tuple(column.pg_type.coerce(payload[column.python_name]) for column in columns)


def _measure(operation: Callable[[], Any], warmup: int, trials: int, rows: int) -> list[float]:
    for _ in range(warmup):
        operation()
    samples = []
    for _ in range(trials):
        gc.collect()
        started = time.perf_counter()
        for _ in range(rows):
            operation()
        samples.append(time.perf_counter() - started)
    return samples


def _retained(operation: Callable[[], Any], rows: int) -> dict[str, int]:
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    held = [operation() for _ in range(rows)]
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    stats = after.compare_to(before, "filename")
    result = {
        "retained_blocks": sum(item.count_diff for item in stats),
        "retained_bytes": sum(item.size_diff for item in stats),
    }
    del held
    return result


def _summary(samples: list[float], rows: int) -> dict[str, object]:
    ordered = sorted(samples)
    median = statistics.median(samples)
    return {
        "median_seconds": median,
        "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "p99_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
        "bodies_per_second": rows / median,
        "raw_seconds": samples,
    }


def run(args: argparse.Namespace) -> int:
    model_body = compile_model_validator(Item)

    def one_pass() -> Item:
        return model_body(PAYLOAD, ("body",))

    # Both paths must produce the same object, or the comparison is meaningless.
    left, right = dataclass_then_model(PAYLOAD), one_pass()
    for name in ("label", "slug", "quantity", "weight", "enabled"):
        if getattr(left, name) != getattr(right, name):
            raise RuntimeError(f"paths disagree on {name}")

    results = {}
    for name, operation in (
        ("model_body", one_pass),
        ("dataclass_then_model", lambda: dataclass_then_model(PAYLOAD)),
        ("coerce_only", lambda: coerce_only(PAYLOAD)),
    ):
        samples = _measure(operation, args.warmup, args.trials, args.bodies)
        results[name] = {
            **_summary(samples, args.bodies),
            **_retained(operation, args.bodies),
        }

    document = {
        "metadata": {
            "python": sys.version,
            "platform": platform.platform(),
            "storage": Item.__wreath_storage_kind__,
            "bodies": args.bodies,
            "columns": 5,
            "warmup": args.warmup,
            "trials": args.trials,
        },
        "results": results,
        "one_pass_over_two_pass_speedup": (
            results["dataclass_then_model"]["median_seconds"]
            / results["model_body"]["median_seconds"]
        ),
    }
    print(json.dumps(document, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bodies", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--trials", type=int, default=10)
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
