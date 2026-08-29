"""Microbenchmark: what a Session.flush() costs per row.

The read path compiles once per query *shape* and reuses the plan; the write
path compiled its statement from the model spec on every instance. This measures
the flush itself -- statement construction plus value marshalling -- against a
fake connection, so the number is CPU in Wreath and not PostgreSQL round trips.

Row counts are swept because the defect is per-instance: a fix that caches per
shape should flatten the per-row cost as the count rises, while leaving the
1-row case (where there is nothing to amortize) roughly where it was.

    python -m benchmarks.bench_orm_writes --output benchmark-results-orm-writes/before.json

Arms are interleaved and an A/A control is measured at the far end of each round,
per the rules in `src/wreath/_devtools/measure.py`. A delta below twice that
floor is unresolved, not zero.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

RESOLUTION_FACTOR = 2.0
_CREATED = datetime.datetime(2024, 1, 1)


def _load() -> tuple[Any, Any, Any]:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    from orm.conftest import FakeDatabase, Membership, Post, User  # type: ignore

    from wreath.orm.registry import Registry
    from wreath.orm.session import Session

    database = FakeDatabase()
    registry = Registry(database, [User, Post, Membership], validate_schema="off")
    return registry, Session, User


def _wide_model(width: int) -> Any:
    """A model with `width` text columns, to expose per-column work.

    The previous INSERT compiler split its column list with `item not in
    columns`, a linear scan per column and so quadratic in the table's own
    width. A 4-column model cannot show that; this can.
    """
    import types as _types

    from wreath.orm import Mapped, Model, column
    from wreath.orm.types import Int64, Text

    def body(namespace: dict[str, Any]) -> None:
        annotations: dict[str, Any] = {"id": Mapped[int]}
        namespace["id"] = column(Int64, primary_key=True)
        for index in range(width):
            annotations[f"f{index}"] = Mapped[str]
            namespace[f"f{index}"] = column(Text)
        namespace["__annotations__"] = annotations

    return _types.new_class(f"Wide{width}", (Model,), {"table": f"wide{width}"}, body)


def _wide_payload(session_cls: Any, model: Any, registry: Any, width: int, rows: int) -> Any:
    """One flush of `rows` fully-loaded instances of a `width`-column model."""
    fields = {f"f{index}": "v" for index in range(width)}

    async def once() -> None:
        session = session_cls(registry, "write")
        for index in range(rows):
            session.add(model(id=index, **fields))
        async with session.begin():
            await session.flush()

    return once


def _insert_payload(registry: Any, session_cls: Any, model: Any, rows: int) -> Any:
    """One flush of `rows` fresh instances, all sharing one write shape."""

    async def once() -> None:
        session = session_cls(registry, "write")
        for index in range(rows):
            session.add(
                model(
                    id=index,
                    email=f"u{index}@e.x",
                    name=f"n{index}",
                    created_at=_CREATED,
                )
            )
        async with session.begin():
            await session.flush()

    return once


def _update_payload(registry: Any, session_cls: Any, model: Any, rows: int) -> Any:
    """One flush of `rows` dirty instances, all sharing one dirty-column set."""

    async def once() -> None:
        session = session_cls(registry, "write")
        instances = []
        for index in range(rows):
            instance = model(id=index, email=f"u{index}@e.x", name=f"n{index}", created_at=_CREATED)
            session.add(instance)
            instances.append(instance)
        async with session.begin():
            await session.flush()
        for instance in instances:
            instance.name = "renamed"
        async with session.begin():
            await session.flush()

    return once


def _time(loop: Any, payload: Any, iterations: int) -> float:
    """Microseconds per flushed row is computed by the caller; this is per call."""

    async def repeat() -> None:
        for _ in range(iterations):
            await payload()

    start = time.perf_counter()
    loop.run_until_complete(repeat())
    return (time.perf_counter() - start) / iterations * 1e6


def _measure(
    loop: Any, payload: Any, rounds: int, iterations: int, warmup: int
) -> tuple[float, float]:
    """Median microseconds per flush, and the A/A noise floor beside it."""
    _time(loop, payload, warmup)
    main: list[float] = []
    control: list[float] = []
    for _ in range(rounds):
        main.append(_time(loop, payload, iterations))
        # The control is entered at the far end of the round from its twin, so
        # the floor includes within-round drift rather than flattering itself.
        control.append(_time(loop, payload, iterations))
    median = statistics.median(main)
    return median, abs(median - statistics.median(control))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--rows", type=int, nargs="+", default=[1, 10, 100, 1000])
    parser.add_argument(
        "--widths",
        type=int,
        nargs="+",
        default=[4, 16, 64],
        help="column counts for the wide-table sweep; doubling should not "
        "quadruple the per-row cost",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    registry, session_cls, model = _load()
    loop = asyncio.new_event_loop()
    try:
        scenarios: list[dict[str, Any]] = []
        for label, builder in (("insert", _insert_payload), ("update", _update_payload)):
            for rows in args.rows:
                # Fewer iterations for the big shapes; the per-call cost is the
                # comparable number, and 1000 rows x 200 calls is 200k flushes.
                iterations = max(4, args.iterations // max(1, rows // 10))
                payload = builder(registry, session_cls, model, rows)
                warmup = max(4, args.warmup // max(1, rows // 10))
                median, floor = _measure(loop, payload, args.rounds, iterations, warmup)
                per_row = median / rows
                resolved = floor * RESOLUTION_FACTOR
                scenarios.append(
                    {
                        "operation": label,
                        "rows": rows,
                        "iterations": iterations,
                        "median_us_per_flush": round(median, 3),
                        "us_per_row": round(per_row, 4),
                        "aa_floor_us": round(floor, 3),
                        "resolution_us": round(resolved, 3),
                    }
                )
                print(
                    f"{label:7s} rows={rows:5d}  {median:10.2f}us/flush  "
                    f"{per_row:8.3f}us/row   A/A floor {floor:.2f}us"
                )
        # Width sweep: one model and registry per width, 50 rows each. `_load`
        # has already put `tests/` on the path.
        from orm.conftest import FakeDatabase  # type: ignore

        from wreath.orm.registry import Registry

        widths: list[dict[str, Any]] = []
        for width in args.widths:
            model = _wide_model(width)
            wide_registry = Registry(FakeDatabase(), [model], validate_schema="off")
            payload = _wide_payload(session_cls, model, wide_registry, width, 50)
            median, floor = _measure(loop, payload, args.rounds, 20, 10)
            widths.append(
                {
                    "columns": width + 1,
                    "rows": 50,
                    "median_us_per_flush": round(median, 3),
                    "us_per_row": round(median / 50, 4),
                    "aa_floor_us": round(floor, 3),
                }
            )
            print(
                f"wide    cols={width + 1:5d}  {median:10.2f}us/flush  "
                f"{median / 50:8.3f}us/row   A/A floor {floor:.2f}us"
            )
    finally:
        loop.close()

    document = {
        "python": sys.version,
        "platform": platform.platform(),
        "implementation": sys.implementation.name,
        "executable": sys.executable,
        "rounds": args.rounds,
        "note": (
            "us_per_row is the comparable figure across row counts. A write-plan "
            "cache should flatten it as rows rise; the 1-row case has nothing to "
            "amortize and is expected to move least."
        ),
        "scenarios": scenarios,
        "width_sweep": widths,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
