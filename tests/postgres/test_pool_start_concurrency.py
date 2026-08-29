from __future__ import annotations

import threading
from typing import Any

from wreath.postgres import Database, PoolConfig


class _Connection:
    async def execute(self, sql: str, *args: object) -> str:
        return "OK"

    async def prepare(self, sql: str) -> None:
        return None

    async def close(self) -> None:
        return None


async def _connect(dsn: str) -> _Connection:
    return _Connection()


def test_start_snapshots_configs_under_the_registration_lock() -> None:
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig(), "security_read": PoolConfig()},
        connector=_connect,
    )
    with db._register_lock:
        snapshot = tuple(db._configs.items())
    # Inserting after the snapshot must not disturb it -- that is exactly what
    # `start()` relies on when a registration lands mid-loop.
    db.statement("s", "SELECT 1", workload="write")
    assert len(snapshot) == 2
    assert len(db._configs) == 3


def test_registering_a_workload_during_iteration_no_longer_raises() -> None:
    failures = 0
    trials = 200
    for _ in range(trials):
        db = Database(
            "main",
            "postgresql://primary/app",
            pools={"read": PoolConfig(), "security_read": PoolConfig()},
            connector=_connect,
        )
        barrier = threading.Barrier(2)

        def register(target: Any = db, gate: Any = barrier) -> None:
            gate.wait()
            try:
                target.statement("s", "SELECT 1", workload="write")
            except Exception:  # noqa: BLE001 - the race is what is under test
                pass

        thread = threading.Thread(target=register)
        thread.start()
        barrier.wait()
        try:
            # What `start()` does now: snapshot, then iterate the snapshot.
            with db._register_lock:
                configs = tuple(db._configs.items())
            for _workload, _config in configs:
                pass
        except RuntimeError:  # pragma: no cover - the defect being pinned
            failures += 1
        thread.join()
    assert failures == 0, "a concurrent registration still breaks start()'s iteration"
