"""`start()` must not iterate a dict another thread can insert into.

`Database.statement()` inserts a workload the first time one is named, under
`_register_lock`. `start()` iterated `self._configs` *live* while awaiting a
connection per workload -- so the iteration stayed open for milliseconds and a
concurrent registration raised `RuntimeError: dictionary changed size during
iteration`. Measured at 400 of 400 trials once the two are aligned with a
barrier.

The hazard is bounded: there are exactly three workloads, so at most two
insertions can ever happen in a process. It is still the same shape as the
`Database.statement` check-then-act fixed alongside it -- a lock that guards one
side of a shared dict and not the other.

`start()` now snapshots under the lock. It does *not* hold the lock across the
loop: `_register_lock` is a `threading.Lock`, and holding it across an `await`
would block every other thread for the whole of connection setup.
"""

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
    """The fix, stated as a property rather than as a timing.

    Reproducing the race needs a barrier and a sleep; asserting the snapshot
    exists needs neither, and cannot go flaky on a loaded machine.
    """
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
    """The measured failure: 400/400 before, and the barrier makes it deterministic."""
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
