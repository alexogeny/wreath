"""The health recipe's code blocks, executed.

`docs/cookbook/recipes/health-checks.md` shipped a probe spelling that cannot
work: `db.pool("read").fetchval("SELECT 1")` asks a `Pool` -- which leases
connections and has no query methods -- for a query method. The health machinery
catches the resulting `AttributeError` and reports it as a failed check, so the
symptom is not a crash: `/ready` answers 503 forever with the reason buried in a
JSON body, and a load balancer takes every instance out of rotation. Someone
finds that during an incident.

Prose and code drifted apart because nothing ran the prose. So these tests
*execute the recipe*: every ```python block is extracted from the markdown and
run, in order, in one shared namespace -- which also proves the recipe reads as a
coherent sequence rather than three unrelated fragments. There is no allowlist of
blocks to skip, deliberately; a block that cannot run is a block that should not
be in the recipe.

The doubles are pinned to the real classes by `test_the_double_matches_the_real
_database_api`, because a double that drifts is how a green suite stops meaning
anything.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest

from wreath import Wreath
from wreath.postgres import Database, Pool, Statement
from wreath.testing import TestClient

RECIPE = Path(__file__).resolve().parents[2] / "docs/cookbook/recipes/health-checks.md"
_PYTHON_BLOCK = re.compile(r"^```python[^\n]*\n(.*?)^```", re.M | re.S)

#: Query methods a caller reaches for. `Pool` has none of them -- it leases
#: connections -- and that is exactly what the broken recipe assumed otherwise.
_QUERY_METHODS = ("fetchval", "fetch", "fetchrow", "execute")


def _blocks() -> list[str]:
    return [m.group(1) for m in _PYTHON_BLOCK.finditer(RECIPE.read_text(encoding="utf-8"))]


# --- doubles, pinned to the real API below -----------------------------------


class _FakeConnection:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def fetchval(self, sql: str, *args: object) -> object:
        if self.fail:
            raise ConnectionRefusedError("no route to host")
        return 1


class _FakeStatement:
    """Stands in for `wreath.postgres.Statement`: the object `db.statement()`
    returns, whose bound `fetchval` is what `database_check` wants."""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.calls = 0

    async def fetchval(self, *args: object) -> object:
        self.calls += 1
        return 1


class _FakeDatabase:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[str] = []
        self._statements: dict[str, _FakeStatement] = {}

    def statement(self, name: str, sql: str, *, workload: str = "read") -> _FakeStatement:
        if name in self._statements:      # the real one refuses a claimed name
            raise ValueError(f"statement name already registered: {name}")
        self._statements[name] = _FakeStatement(sql)
        return self._statements[name]

    async def acquire(self, workload: str = "read") -> _FakeConnection:
        self.acquired.append(workload)
        return _FakeConnection()

    async def release(self, workload: str, connection: object) -> None:
        self.released.append(workload)


class _FakeCache:
    async def ping(self) -> dict[str, int]:
        return {"hits": 1}


def _namespace(db: object) -> dict[str, object]:
    app = Wreath()
    app.state.cache = _FakeCache()
    app.state.draining = False
    return {"db": db, "app": app}


def _run_recipe(db: object) -> dict[str, object]:
    """Execute every block in order, re-seeding `app` so each `app.health()`
    call mounts onto a clean router. Everything else accumulates, so a later
    block using an earlier block's `checks` is exercised as written."""
    blocks = _blocks()
    assert blocks, "the recipe has no python blocks -- this test would check nothing"
    namespace = _namespace(db)
    for source in blocks:
        namespace.update(_namespace(db))
        exec(compile(source, str(RECIPE), "exec"), namespace)   # noqa: S102 - the doc is the input
    return namespace


# --- the recipe runs ---------------------------------------------------------


def test_every_python_block_in_the_recipe_executes() -> None:
    """Before the fix this raised nothing -- it *reported* a failed check. The
    endpoint assertions below are what actually caught it."""
    namespace = _run_recipe(_FakeDatabase())
    assert "checks" in namespace, "the recipe stopped defining `checks`"


@pytest.mark.asyncio
async def test_the_recipes_app_answers_ready_not_503() -> None:
    """The defect's actual signature: every probe fails, so readiness is 503 and
    the load balancer drains the fleet. `status` must be `ready`."""
    namespace = _run_recipe(_FakeDatabase())
    app = namespace["app"]

    async with TestClient(app) as client:
        live = await client.get("/health")
        ready = await client.get("/ready")

    assert live.status == 200
    assert ready.status == 200, ready.json()
    body = ready.json()
    assert body["status"] == "ready", body
    for name, entry in body["checks"].items():
        assert entry["status"] == "pass", f"{name}: {entry}"


@pytest.mark.asyncio
async def test_the_recipes_drain_switch_flips_liveness() -> None:
    """The third block's `is_live=` is the one an operator relies on to drain."""
    namespace = _run_recipe(_FakeDatabase())
    app = namespace["app"]

    async with TestClient(app) as client:
        assert (await client.get("/health")).status == 200
        app.state.draining = True
        draining = await client.get("/health")

    assert draining.status == 503
    assert draining.json()["status"] == "shutting_down"


@pytest.mark.asyncio
async def test_the_registered_ping_is_reusable_across_probes() -> None:
    """`statement()` refuses a name it already holds, so a probe that registers
    one per call works once and raises forever after -- the same shape as the
    defect this recipe used to carry. The recipe registers it at startup."""
    database = _FakeDatabase()
    namespace = _run_recipe(database)
    checks = namespace["checks"]

    from wreath.health import evaluate

    first_serving, _ = await evaluate(checks)
    second_serving, detail = await evaluate(checks)

    assert first_serving is True
    assert second_serving is True, detail


# --- why the old spelling was wrong, pinned ----------------------------------


def test_pool_has_no_query_methods() -> None:
    """The recipe's old probe called `db.pool("read").fetchval(...)`. If `Pool`
    ever grows query methods this test goes red, and the recipe *could* say that
    again -- until then it must not."""
    for method in _QUERY_METHODS:
        assert not hasattr(Pool, method), (
            f"Pool grew {method!r}; the health recipe's guidance can be revisited"
        )


def test_pool_acquire_is_not_an_async_context_manager() -> None:
    """Four doc sites assumed `async with pool.acquire() as conn`. It is a plain
    coroutine returning a connection, so that spelling raises `TypeError`."""
    assert not hasattr(Pool.acquire, "__aenter__")
    assert inspect.iscoroutinefunction(Pool.acquire)


def test_the_double_matches_the_real_database_api() -> None:
    """A double that drifts from the driver turns a green suite into decoration.

    Compares parameter names positionally, not defaults or annotations: the
    point is that a call written against the real `Database` binds identically
    against this fake, which is the only property these tests rely on.
    """
    pairs = [
        (Database.statement, _FakeDatabase.statement),
        (Database.acquire, _FakeDatabase.acquire),
        (Database.release, _FakeDatabase.release),
        (Statement.fetchval, _FakeStatement.fetchval),
    ]
    for real, fake in pairs:
        real_params = [p.name for p in inspect.signature(real).parameters.values()]
        fake_params = [p.name for p in inspect.signature(fake).parameters.values()]
        assert real_params == fake_params, (
            f"{fake.__qualname__} has drifted from {real.__qualname__}: "
            f"{fake_params} != {real_params}"
        )


# --- the same recipe, against a real database --------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="needs WREATH_TEST_POSTGRES_DSN",
)
async def test_the_recipe_runs_against_a_real_database() -> None:
    """The fake cannot tell you the probe reaches PostgreSQL; this can.

    It is also the test that first reproduced the defect: run against the old
    recipe it reported `'Pool' object has no attribute 'fetchval'` in the check
    body while answering 503.
    """
    from wreath.postgres import PoolConfig

    database = Database(
        "main",
        os.environ["WREATH_TEST_POSTGRES_DSN"],
        pools={
            "read": PoolConfig(min_size=1, max_size=2),
            "security_read": PoolConfig(min_size=1, max_size=2),
        },
    )
    await database.start()
    try:
        namespace = _run_recipe(database)
        async with TestClient(namespace["app"]) as client:
            ready = await client.get("/ready")
        body = ready.json()
        assert ready.status == 200, body
        assert body["status"] == "ready", body
        assert body["db"]["status"] == "pass" if "db" in body else True
    finally:
        await database.stop()
