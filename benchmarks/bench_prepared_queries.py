"""Measure declared-query execution with and without its prepared shape path.

Both arms execute the same declared query through a fresh request-scoped
session and the same scripted database. ``legacy declared`` deliberately binds
an ordinary Select and enters ``Session.fetch_one``; ``prepared declared`` uses
the declaration's normal bound-call API. Arms are interleaved with a legacy
A/A control and raw samples are retained when ``--output`` is supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import platform
import sys
from pathlib import Path
from typing import Any

from wreath._devtools import measure
from wreath._devtools.sample_app import (
    TracedPost,
    TracedUser,
    _ScriptedDatabase,
)
from wreath.orm.registry import Registry
from wreath.orm.session import Session
from wreath.queries import Param, Queries, query


class _Users(Queries[TracedUser]):
    by_id = query(TracedUser.id == Param("id")).one()


def _registry() -> Registry:
    database = _ScriptedDatabase()
    database.connection.script(
        "users", [[1, "a@b.c", "A", datetime.datetime(2024, 1, 1)]]
    )
    return Registry(database, [TracedUser, TracedPost], validate_schema="off")


def _arms() -> list[measure.Arm]:
    registry = _registry()

    def legacy(iterations: int) -> None:
        async def body() -> None:
            for _ in range(iterations):
                session = Session(registry, "read")
                try:
                    await session.fetch_one(_Users.by_id.bind(id=1))
                finally:
                    await session.close()

        asyncio.run(body())

    def prepared(iterations: int) -> None:
        async def body() -> None:
            for _ in range(iterations):
                session = Session(registry, "read")
                try:
                    await _Users(session).by_id(id=1)
                finally:
                    await session.close()

        asyncio.run(body())

    return [
        measure.Arm("legacy declared", payload=legacy),
        measure.Arm("prepared declared", payload=prepared),
        measure.Arm("control legacy", payload=legacy),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=21)
    parser.add_argument("--iterations", type=int, default=4000)
    parser.add_argument("--warmup", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    arms = _arms()
    measure.measure_callables(
        arms,
        rounds=args.rounds,
        iterations=args.iterations,
        warmup=args.warmup,
    )
    result = measure.report(arms, "legacy declared", "control legacy")
    document: dict[str, Any] = {
        "metadata": {
            "command": " ".join(sys.argv),
            "python": sys.version,
            "platform": platform.platform(),
            "rounds": args.rounds,
            "iterations": args.iterations,
            "warmup": args.warmup,
        },
        "result": result,
        "samples_us": {
            arm.label: [round(sample, 6) for sample in arm.samples] for arm in arms
        },
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
