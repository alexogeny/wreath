"""Measure ORM unit-of-work bookkeeping in isolation from SQL and the network.

`Session.add()`, `Session.delete()`, and the flush ordering step do work that is
proportional to the number of pending objects. This benchmark drives them
against the fake-database seam the ORM tests already use, so what it times is
scheduling, membership, and ordering -- not statement building, encoding, or a
round trip.

The sizes double so the scaling is readable directly: linear bookkeeping doubles
its cost per step, quadratic bookkeeping quadruples it. Every phase is reported
separately because they have different shapes: `add` is per-object, `order` is a
sort over the whole pending set.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from orm.conftest import FakeDatabase, Membership, Post, User  # noqa: E402

from wreath.orm.registry import Registry  # noqa: E402
from wreath.orm.session import Session, _count_probes  # noqa: E402

SIZES = (1000, 2000, 5000, 10000)


def _summary(samples: list[float], objects: int) -> dict[str, Any]:
    ordered = sorted(samples)
    median = statistics.median(samples)
    return {
        "median_seconds": median,
        "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "objects_per_second": objects / median if median else None,
        "raw_seconds": samples,
    }


def _make(registry: Registry, count: int) -> tuple[Session, list[Any]]:
    session = Session(registry, "write")
    objects: list[Any] = []
    for index in range(count):
        # Interleave models so ordering has real work to do rather than
        # sorting an already-grouped list.
        if index % 2:
            objects.append(Post(id=index, author_id=1, title=f"p{index}"))
        else:
            objects.append(
                User(id=index, email=f"{index}@b.c", name="u", created_at=None)
            )
    return session, objects


def _order_pending(session: Session) -> list[Any]:
    ordinals = session._new_ordinals
    return sorted(
        session._new,
        key=lambda item: (session._order(type(item)), ordinals[id(item)]),
    )


# -- legacy bookkeeping, for a before/after comparison -------------------------
#
# The pre-remediation implementation, reproduced exactly so the same harness can
# measure both algorithms. This is a reconstruction of the replaced code, not a
# checkout of it: it exists to show the shape of the change (quadratic vs
# linear), and its absolute numbers are only comparable within this file.


def _legacy_order(registry: Registry, model: type) -> int:
    for index, spec in enumerate(registry.specs):
        if spec.model_type is model:
            return index
    raise LookupError(model)


def _legacy_add(pending: list[Any], instance: Any) -> None:
    if instance not in pending:  # O(n) equality scan per add
        pending.append(instance)


def _legacy_order_pending(registry: Registry, pending: list[Any]) -> list[Any]:
    return sorted(
        pending,
        key=lambda item: (
            _legacy_order(registry, type(item)),  # scans specs per key
            pending.index(item),  # O(n) per key extraction
        ),
    )


def _legacy_unschedule(pending: list[Any], instance: Any) -> None:
    if instance in pending:
        pending.remove(instance)


def measure_legacy(registry: Registry, count: int, trials: int) -> dict[str, Any]:
    add: list[float] = []
    order: list[float] = []
    unschedule: list[float] = []

    for _ in range(trials):
        _, objects = _make(registry, count)
        pending: list[Any] = []
        gc.collect()

        start = time.perf_counter()
        for item in objects:
            _legacy_add(pending, item)
        add.append(time.perf_counter() - start)

        start = time.perf_counter()
        _legacy_order_pending(registry, pending)
        order.append(time.perf_counter() - start)

        start = time.perf_counter()
        for item in objects:
            _legacy_unschedule(pending, item)
        unschedule.append(time.perf_counter() - start)

    return {
        "objects": count,
        "add": _summary(add, count),
        "order": _summary(order, count),
        "unschedule": _summary(unschedule, count),
        "probes": {"add_and_order": -1, "per_object": -1},
    }


def _probe_counts(registry: Registry, count: int) -> dict[str, int]:
    session, objects = _make(registry, count)
    with _count_probes() as counter:
        for item in objects:
            session._schedule_new(item)
        _order_pending(session)
    return {"add_and_order": counter[0], "per_object": counter[0] // count}


def measure(registry: Registry, count: int, trials: int) -> dict[str, Any]:
    add: list[float] = []
    order: list[float] = []
    unschedule: list[float] = []

    for _ in range(trials):
        session, objects = _make(registry, count)
        gc.collect()

        # The bookkeeping helpers, not add()/delete(): the public methods also
        # run usability, ownership, and registry checks, which the legacy
        # reconstruction below does not. Timing the same layer on both sides is
        # what makes the comparison mean anything.
        start = time.perf_counter()
        for item in objects:
            session._schedule_new(item)
        add.append(time.perf_counter() - start)

        start = time.perf_counter()
        _order_pending(session)
        order.append(time.perf_counter() - start)

        start = time.perf_counter()
        for item in objects:
            session._unschedule_new(item)
        unschedule.append(time.perf_counter() - start)

    return {
        "objects": count,
        "add": _summary(add, count),
        "order": _summary(order, count),
        "unschedule": _summary(unschedule, count),
        "probes": _probe_counts(registry, count),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--json", type=Path)
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="measure the reconstructed pre-remediation bookkeeping instead",
    )
    args = parser.parse_args(argv)

    registry = Registry(FakeDatabase(), [User, Post, Membership], validate_schema="off")
    run = measure_legacy if args.legacy else measure
    for _ in range(args.warmup):
        run(registry, SIZES[0], 1)

    results = [run(registry, size, args.trials) for size in SIZES]
    payload = {
        "benchmark": "orm-flush-bookkeeping",
        "implementation": "legacy-reconstruction" if args.legacy else "current",
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "trials": args.trials,
        "warmup": args.warmup,
        "sizes": list(SIZES),
        "results": results,
    }

    for phase in ("add", "order", "unschedule"):
        print(f"\n{phase}:")
        previous: float | None = None
        for entry in results:
            median = entry[phase]["median_seconds"]
            growth = f"{median / previous:5.2f}x" if previous else "    --"
            scale = entry["objects"] / results[0]["objects"]
            print(
                f"  n={entry['objects']:6d}  median={median * 1e3:8.3f} ms  "
                f"vs-previous={growth}  per-object={median / entry['objects'] * 1e6:6.3f} us"
                f"  (n scale {scale:g}x)"
            )
            previous = median

    if args.legacy:
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(payload, indent=2))
            print(f"\nwrote {args.json}")
        return 0

    print("\nprobes (identity/order operations, must be linear):")
    for entry in results:
        probes = entry["probes"]
        print(
            f"  n={entry['objects']:6d}  total={probes['add_and_order']:7d}  "
            f"per-object={probes['per_object']}"
        )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
