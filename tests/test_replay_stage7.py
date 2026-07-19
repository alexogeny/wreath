"""Stage 7 replay tails: rebinding REPLACE, adapter-fault serialization, and ORM
Session replay through the boundary doubles."""

from __future__ import annotations

from typing import Annotated

import wreath
from wreath.exceptions import NotFound
from wreath.orm import FromORM, Mapped, Model, Session, column
from wreath.orm.types import Int64, Text
from wreath.postgres import Connection
from wreath.replay import (
    AdapterFault,
    AdapterFaultDescriptor,
    AdapterSeam,
    CanonicalRequest,
    DatabaseDouble,
    FaultSchedule,
    PlanMode,
    ReplayAdapters,
    replay_endpoint_plan,
)


class Item(Model, table="replay_items"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)


# --- rebinding REPLACE: binding/validation runs before the substituted handler --


def _validating_app() -> wreath.Wreath:
    app = wreath.Wreath()

    @app.post("/items")
    async def create(request: wreath.Request, item: Item) -> dict:
        raise AssertionError("the real handler must not run in REPLACE mode")

    return app


async def test_replace_runs_owned_binding_then_substitutes_the_result() -> None:
    result = await replay_endpoint_plan(
        _validating_app(),
        CanonicalRequest("POST", "/items", headers=((b"content-type", b"application/json"),),
                         body=b'{"name": "real"}'),
        mode=PlanMode.REPLACE, recorded_return={"name": "stubbed"},
    )
    assert result.status == 200
    assert result.body == b'{"name":"stubbed"}'  # the recorded result, not the handler's
    assert result.deterministic is True and result.best_effort is False


async def test_replace_rejects_an_invalid_body_before_substituting() -> None:
    # The body is missing the required `name`; owned validation must turn it away
    # with a 422 -- the recorded result is never reached.
    result = await replay_endpoint_plan(
        _validating_app(),
        CanonicalRequest("POST", "/items", headers=((b"content-type", b"application/json"),),
                         body=b"{}"),
        mode=PlanMode.REPLACE, recorded_return={"name": "stubbed"},
    )
    assert result.status in (400, 422)


async def test_replace_maps_a_recorded_exception_through_owned_handling() -> None:
    result = await replay_endpoint_plan(
        _validating_app(),
        CanonicalRequest("POST", "/items", headers=((b"content-type", b"application/json"),),
                         body=b'{"name": "x"}'),
        mode=PlanMode.REPLACE, recorded_exception=NotFound("gone"),
    )
    assert result.status == 404


# --- adapter-fault serialization ---------------------------------------------


def test_adapter_faults_round_trip_through_the_schedule() -> None:
    schedule = FaultSchedule(
        adapter_faults=(
            AdapterFaultDescriptor(int(AdapterSeam.DB_ACQUIRE), "main",
                                   AdapterFault.POOL_TIMEOUT.value),
            AdapterFaultDescriptor(int(AdapterSeam.DB_QUERY), "main",
                                   AdapterFault.SERVER_ERROR.value, 2),
            AdapterFaultDescriptor(int(AdapterSeam.HTTP_REQUEST), "api",
                                   AdapterFault.READ_TIMEOUT.value, 1),
        )
    )
    assert FaultSchedule.from_bytes(schedule.to_bytes()) == schedule


async def test_a_serialized_schedule_reconstructs_the_boundary_faults() -> None:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")

    @app.get("/u")
    async def u(request: wreath.Request, db: Connection) -> dict:
        return {"n": len(await db.fetch("SELECT 1"))}

    schedule = FaultSchedule(
        adapter_faults=(
            AdapterFaultDescriptor(int(AdapterSeam.DB_QUERY), "main",
                                   AdapterFault.SERVER_ERROR.value, 0),
        )
    )
    restored = FaultSchedule.from_bytes(schedule.to_bytes())
    adapters = ReplayAdapters.from_faults(restored.adapter_faults)
    result = await replay_endpoint_plan(app, CanonicalRequest("GET", "/u"), adapters=adapters)
    assert result.status == 500
    assert not adapters.databases["main"].leaked


# --- ORM Session replay through the same DatabaseDouble ----------------------


def _orm_app() -> wreath.Wreath:
    app = wreath.Wreath()
    app.postgres("main", dsn="postgres://stub/db")
    app.orm(database="main", models=[Item], validate_schema="off")

    @app.get("/item/{id}")
    async def get_item(
        request: wreath.Request, id: int,
        session: Annotated[Session, FromORM("main")],
    ) -> dict:
        item = await session.get(Item, id)
        return {"found": item is not None}

    return app


async def test_orm_session_fault_maps_to_500_and_releases_the_connection() -> None:
    double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})
    result = await replay_endpoint_plan(
        _orm_app(), CanonicalRequest("GET", "/item/5", path_params={"id": "5"}),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    assert result.status == 500
    assert not double.leaked  # the Session's connection was returned to the pool


async def test_orm_session_adapter_is_restored_after_replay() -> None:
    app = _orm_app()
    app._compile_routes()
    original = app._orm_registries["main"].database
    double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})
    await replay_endpoint_plan(
        app, CanonicalRequest("GET", "/item/5", path_params={"id": "5"}),
        adapters=ReplayAdapters(databases={"main": double}),
    )
    # The registry's database is exactly what it was before the replay.
    assert app._orm_registries["main"].database is original
