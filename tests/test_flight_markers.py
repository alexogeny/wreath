"""Stage 3 slice 2b (dependency seams): phase markers at the PostgreSQL and
HTTP-client seams, driven through the ContextVar propagation without a live
server. The marker is bound the way dispatch binds it for an armed request;
each seam then records its phase with the dependency's metadata-image ID."""

from __future__ import annotations

import pytest

from wreath import _flight_markers as fm
from wreath.http_client import ClientResponse, HTTPClient
from wreath.postgres import Database, Statement


class _RecordedPhases(list):
    def __call__(self, phase_id: int, dep_id: int, coverage: int, duration_ns: int) -> None:
        self.append((phase_id, dep_id, coverage, duration_ns))


class _StubConnection:
    async def fetch(self, sql: str, *args: object) -> list:
        return [("row",)]

    async def map(self, method: str, sql: str, argument_sets, *, max_in_flight: int = 32):
        return [1, 2]


class _StubPool:
    def __init__(self) -> None:
        self.connection = _StubConnection()

    async def acquire(self):
        return self.connection

    async def release(self, connection) -> None:
        return None


def _stub_database(monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database("main", "postgres://stub/db")
    database._flight_dep_id = 7
    pool = _StubPool()
    monkeypatch.setattr(Database, "pool", lambda self, workload: pool)
    return database


@pytest.mark.asyncio
async def test_statement_records_pool_wait_and_query_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _stub_database(monkeypatch)
    statement = Statement(database, "list", "SELECT 1", "read")
    phases = _RecordedPhases()
    token = fm.phase_marker.set(phases)
    try:
        rows = await statement.fetch()
    finally:
        fm.phase_marker.reset(token)

    assert rows == [("row",)]
    assert [(p[0], p[1], p[2]) for p in phases] == [
        (fm.PH_DB_POOL_WAIT, 7, fm.COV_PYTHON),
        (fm.PH_DB_QUERY, 7, fm.COV_EXTERNAL),
    ]
    assert all(p[3] >= 0 for p in phases)


@pytest.mark.asyncio
async def test_statement_map_records_one_query_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _stub_database(monkeypatch)
    statement = Statement(database, "fanout", "SELECT $1", "read")
    phases = _RecordedPhases()
    token = fm.phase_marker.set(phases)
    try:
        results = await statement.map("fetch", [(1,), (2,)])
    finally:
        fm.phase_marker.reset(token)

    assert results == [1, 2]
    kinds = [p[0] for p in phases]
    assert kinds == [fm.PH_DB_POOL_WAIT, fm.PH_DB_QUERY]


@pytest.mark.asyncio
async def test_unmarked_context_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No marker bound: the seams add a single ContextVar read and no phases.
    database = _stub_database(monkeypatch)
    statement = Statement(database, "list", "SELECT 1", "read")
    assert fm.phase_marker.get(None) is None
    assert await statement.fetch() == [("row",)]


@pytest.mark.asyncio
async def test_http_client_request_records_http_client_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HTTPClient("api", base_url="http://api.internal.test")
    client._flight_dep_id = 3

    async def fake_timed(self, method, target, *, headers, body, idempotency_key):
        return ClientResponse(status=204, headers=(), body=b"", http_version="1.1")

    monkeypatch.setattr(HTTPClient, "_request_timed", fake_timed)
    phases = _RecordedPhases()
    token = fm.phase_marker.set(phases)
    try:
        response = await client.get("/ping")
    finally:
        fm.phase_marker.reset(token)

    assert response.status == 204
    assert [(p[0], p[1], p[2]) for p in phases] == [
        (fm.PH_HTTP_CLIENT, 3, fm.COV_EXTERNAL)
    ]


@pytest.mark.asyncio
async def test_http_client_phase_is_recorded_even_when_the_call_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HTTPClient("api", base_url="http://api.internal.test")

    async def failing_timed(self, method, target, *, headers, body, idempotency_key):
        raise RuntimeError("boom")

    monkeypatch.setattr(HTTPClient, "_request_timed", failing_timed)
    phases = _RecordedPhases()
    token = fm.phase_marker.set(phases)
    try:
        with pytest.raises(RuntimeError):
            await client.get("/ping")
    finally:
        fm.phase_marker.reset(token)

    assert [p[0] for p in phases] == [fm.PH_HTTP_CLIENT]


def test_flight_route_ids_stamp_dependency_ids() -> None:
    # The lazy metadata join stamps each live Database/HTTPClient with its
    # image ID so seam markers attribute without a per-call name lookup.
    from wreath import Wreath
    from wreath._flight_metadata import build_metadata_image

    app = Wreath()
    app.postgres("main", dsn="postgres://stub/db")
    app.http_client("api", base_url="http://api.internal.test")

    @app.get("/x")
    async def handler(request) -> str:
        return "ok"

    app._compile_routes()
    app._build_flight_route_ids()
    image = build_metadata_image(app)
    db_ids = {n.name: n.entry_id for n in image.databases}
    client_ids = {n.name: n.entry_id for n in image.clients}
    assert app._databases["main"]._flight_dep_id == db_ids["main"] != 0
    assert app._http_clients["api"]._flight_dep_id == client_ids["api"] != 0
