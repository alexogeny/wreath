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


class _RecordedCaptures(list):
    """A stand-in for the bound dependency capturer (field_class, data)."""

    def __call__(self, field_class: int, data: bytes) -> None:
        self.append((field_class, bytes(data)))


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
async def test_statement_captures_db_params_when_capturer_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _stub_database(monkeypatch)
    statement = Statement(database, "list", "SELECT $1", "read")
    phases = _RecordedPhases()
    captures = _RecordedCaptures()
    ptoken = fm.phase_marker.set(phases)
    ctoken = fm.capture_marker.set(captures)
    try:
        await statement.fetch(42)
    finally:
        fm.capture_marker.reset(ctoken)
        fm.phase_marker.reset(ptoken)

    kinds = [fc for fc, _ in captures]
    assert fm.CAP_DB_PARAM in kinds  # the params
    assert fm.CAP_DB_ROW in kinds  # and the result rows
    param = next(data for fc, data in captures if fc == fm.CAP_DB_PARAM)
    assert b"42" in param  # the native side redacts; here the capturer sees raw


@pytest.mark.asyncio
async def test_statement_captures_nothing_without_a_capturer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Detailed-armed (phase marker) but no Forensic capturer bound: phases are
    # recorded, params are not -- capture stays deny-by-default.
    database = _stub_database(monkeypatch)
    statement = Statement(database, "list", "SELECT $1", "read")
    phases = _RecordedPhases()
    token = fm.phase_marker.set(phases)
    try:
        await statement.fetch(42)
    finally:
        fm.phase_marker.reset(token)
    assert phases and fm.capture_marker.get(None) is None


@pytest.mark.asyncio
async def test_statement_captures_no_params_when_there_are_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _stub_database(monkeypatch)
    statement = Statement(database, "list", "SELECT 1", "read")
    captures = _RecordedCaptures()
    ptoken = fm.phase_marker.set(_RecordedPhases())
    ctoken = fm.capture_marker.set(captures)
    try:
        await statement.fetch()  # no arguments
    finally:
        fm.capture_marker.reset(ctoken)
        fm.phase_marker.reset(ptoken)
    # No parameters were passed, so no DB_PARAM field -- but the result rows are
    # still dependency data and are captured under the same rule.
    assert not any(fc == fm.CAP_DB_PARAM for fc, _ in captures)
    assert any(fc == fm.CAP_DB_ROW for fc, _ in captures)


@pytest.mark.asyncio
async def test_statement_map_captures_materialized_params_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _stub_database(monkeypatch)
    statement = Statement(database, "fanout", "SELECT $1", "read")
    # A list's params are materialized -> captured; a generator is never drained.
    # The result rows (DB_ROW) are captured either way.
    for argument_sets, expect_params in (([(1,), (2,)], True), ((x for x in [(3,)]), False)):
        captures = _RecordedCaptures()
        ptoken = fm.phase_marker.set(_RecordedPhases())
        ctoken = fm.capture_marker.set(captures)
        try:
            await statement.map("fetch", argument_sets)
        finally:
            fm.capture_marker.reset(ctoken)
            fm.phase_marker.reset(ptoken)
        has_params = any(fc == fm.CAP_DB_PARAM for fc, _ in captures)
        assert has_params is expect_params
        assert any(fc == fm.CAP_DB_ROW for fc, _ in captures)  # rows always captured


@pytest.mark.asyncio
async def test_http_client_captures_outbound_request_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HTTPClient("api", base_url="http://api.internal.test")
    client._flight_dep_id = 3

    async def fake_timed(self, method, target, *, headers, body, idempotency_key):
        return ClientResponse(status=200, headers=(), body=b"pong-out", http_version="1.1")

    monkeypatch.setattr(HTTPClient, "_request_timed", fake_timed)
    captures = _RecordedCaptures()
    ptoken = fm.phase_marker.set(_RecordedPhases())
    ctoken = fm.capture_marker.set(captures)
    try:
        response = await client.request("POST", "/x", body=b"ping-out")
    finally:
        fm.capture_marker.reset(ctoken)
        fm.phase_marker.reset(ptoken)

    assert response.status == 200
    assert (fm.CAP_OUTBOUND_REQUEST, b"ping-out") in captures
    assert (fm.CAP_OUTBOUND_RESPONSE, b"pong-out") in captures


@pytest.mark.asyncio
async def test_http_client_captures_request_body_even_when_the_call_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HTTPClient("api", base_url="http://api.internal.test")

    async def failing_timed(self, method, target, *, headers, body, idempotency_key):
        raise RuntimeError("boom")

    monkeypatch.setattr(HTTPClient, "_request_timed", failing_timed)
    captures = _RecordedCaptures()
    ptoken = fm.phase_marker.set(_RecordedPhases())
    ctoken = fm.capture_marker.set(captures)
    try:
        with pytest.raises(RuntimeError):
            await client.request("POST", "/x", body=b"ping-out")
    finally:
        fm.capture_marker.reset(ctoken)
        fm.phase_marker.reset(ptoken)

    # The outbound request body was captured before the call failed; there is no
    # response body to capture.
    assert captures == [(fm.CAP_OUTBOUND_REQUEST, b"ping-out")]


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


def test_the_lazy_metadata_join_is_idempotent_under_concurrent_builders() -> None:
    """Design 22 item 14, checked as far as this interpreter allows.

    The hypothesis was a torn dict from two concurrent armed requests racing
    `_build_flight_route_ids`. Two corrections came out of checking it:

    * `_build_flight_route_ids` is a plain sync function with **zero** await
      points, so two coroutines on one event loop cannot interleave inside it.
      The stated scenario is not a race at all; only genuine OS threads
      sharing one `Wreath` can reach it concurrently.
    * Every write it makes is idempotent -- the same value derived from the
      same immutable image -- so the worst outcome of a real thread overlap
      is duplicated work, plus a window in which a reader sees a partially
      populated `_flight_model_ids` and misses one ORM_HYDRATE attribution.
      That degrades telemetry transiently; it does not corrupt anything.

    This runs the builder from several threads and asserts the stamps agree.
    Under the GIL that is a logic check, not a free-threading proof: no
    free-threaded interpreter is available here (`Py_GIL_DISABLED` is false),
    so the free-threaded mode `AGENTS.md` requires remains untested for this
    path and is recorded as such rather than assumed.
    """
    import threading

    from wreath import Wreath
    from wreath._flight_metadata import build_metadata_image

    app = Wreath()
    app.postgres("main", dsn="postgres://stub/db")
    app.http_client("api", base_url="http://api.internal.test")

    @app.get("/x")
    async def handler(request) -> str:
        return "ok"

    app._compile_routes()

    results: list[dict] = []
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def build() -> None:
        try:
            start.wait()
            app._flight_route_ids = None  # force every thread down the build path
            results.append(app._build_flight_route_ids())
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 4
    # Same mapping every time, whichever thread produced it.
    assert all(mapping == results[0] for mapping in results)
    # And the dependency stamps settled on the image's IDs, not a torn value.
    image = build_metadata_image(app)
    db_ids = {n.name: n.entry_id for n in image.databases}
    assert app._databases["main"]._flight_dep_id == db_ids["main"] != 0


def test_the_metadata_image_names_each_route_s_auth_policy() -> None:
    """The recorder's answer to "what was guarding this route", interned once.

    A policy name is built by joining the parts of the requirement, and the
    `authenticated` part had never been asserted -- every existing image test
    builds routes with no auth at all, so the whole naming function ran on
    nothing. Dropping that part renames `@roles("admin")` from
    `auth|role:all:admin` to `role:all:admin` and, worse, renames a plain
    `@authenticated()` route to the empty string, which the caller reads as
    *no policy*: a forensic image would then say an authenticated route was
    open, which is the one thing an operator reads it to find out.

    Interning is asserted too -- two ids for two distinct policies, and the
    unguarded route at `ID_NONE`.
    """
    from typing import Any

    from wreath import Wreath
    from wreath._flight_metadata import build_metadata_image
    from wreath.auth import BearerTokenBackend, Identity, authenticated
    from wreath.authorization import roles

    app = Wreath()
    app.configure_auth(BearerTokenBackend({"t": Identity(id="ada", type="User")}))

    @app.get("/open")
    async def open_route(request: Any) -> str:
        return "ok"

    @app.get("/private")
    @authenticated()
    async def private(request: Any) -> str:
        return "ok"

    @app.get("/admin")
    @roles("admin")
    async def admin(request: Any) -> str:
        return "ok"

    app._compile_routes()
    app._build_flight_route_ids()
    image = build_metadata_image(app)

    names = {entry.name: entry.entry_id for entry in image.auth_policies}
    assert set(names) == {"auth", "auth|role:all:admin"}
    by_path = {route.path: route.auth_policy_id for route in image.routes}
    assert by_path["/private"] == names["auth"]
    assert by_path["/admin"] == names["auth|role:all:admin"]
    # A route nothing guards carries no policy id rather than a stale one.
    assert by_path["/open"] == 0
