from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from orm.conftest import FakeDatabase, Post, User, user_row

from wreath._nplusone import QueryLedger, query_ledger, watch
from wreath.doctor import (
    Finding,
    NPlusOneDetected,
    NPlusOneGuard,
    Repetition,
    diagnose_n_plus_one,
    find_n_plus_one,
)
from wreath.orm.registry import Registry
from wreath.orm.session import Session

ROUTES = [
    {"id": 1, "method": "GET", "path": "/llamas"},
    {"id": 2, "method": "GET", "path": "/treks"},
]
MODELS = [{"id": 5, "name": "Trek"}, {"id": 6, "name": "Llama"}]


def _phase(name: str, dependency_id: int = 0, duration_us: int = 10) -> dict[str, Any]:
    return {
        "phase": name,
        "coverage": "external",
        "dependency_id": dependency_id,
        "start_offset_us": 0,
        "duration_us": duration_us,
        "sequence": 0,
    }


def _trace(*, request_id: int = 1, route_id: int = 1, phases: list) -> dict[str, Any]:
    return {"request_id": request_id, "route_id": route_id, "phases": phases}


def _herd_trace(treks: int, *, request_id: int = 1) -> dict[str, Any]:
    """One `GET /llamas` that fetched the herd, then each llama's treks."""
    phases = [_phase("db_query"), _phase("orm_hydrate", 6, 400)]
    for _ in range(treks):
        phases += [_phase("db_query"), _phase("orm_hydrate", 5, 30)]
    return _trace(request_id=request_id, route_id=1, phases=phases)


def test_a_repeated_model_query_is_a_finding() -> None:
    findings = find_n_plus_one([_herd_trace(50)], threshold=10, routes=ROUTES, models=MODELS)

    (finding,) = findings
    assert finding.route == "GET /llamas"
    assert finding.request_id == 1
    assert finding.queries == 51  # the one, plus the fifty
    assert finding.worst == Repetition(model="Trek", count=50, total_us=1500)


def test_a_finding_describes_itself_in_one_line() -> None:
    (finding,) = find_n_plus_one([_herd_trace(50)], threshold=10, routes=ROUTES, models=MODELS)
    described = finding.explain()
    assert "GET /llamas" in described
    assert "51" in described  # statements issued
    assert "50" in described  # of them for one model
    assert "Trek" in described


def test_a_query_run_a_handful_of_times_is_not_a_finding() -> None:
    assert find_n_plus_one([_herd_trace(3)], threshold=10, routes=ROUTES, models=MODELS) == []


def test_many_distinct_models_queried_once_each_is_not_a_finding() -> None:
    phases = []
    for model_id in range(1, 40):
        phases += [_phase("db_query"), _phase("orm_hydrate", model_id)]
    assert (
        find_n_plus_one([_trace(phases=phases)], threshold=10, routes=ROUTES, models=MODELS) == []
    )


def test_the_threshold_is_inclusive() -> None:
    assert find_n_plus_one([_herd_trace(10)], threshold=10, routes=ROUTES, models=MODELS)
    assert not find_n_plus_one([_herd_trace(9)], threshold=10, routes=ROUTES, models=MODELS)


def test_an_unmapped_model_id_still_reports() -> None:
    (finding,) = find_n_plus_one([_herd_trace(20)], threshold=10, routes=ROUTES, models=[])
    assert finding.worst.model == "model:5"


def test_an_unmapped_route_id_still_reports() -> None:
    (finding,) = find_n_plus_one([_herd_trace(20)], threshold=10, routes=[], models=MODELS)
    assert finding.route == "route:1"


def test_findings_come_back_worst_first() -> None:
    findings = find_n_plus_one(
        [
            _herd_trace(12, request_id=1),
            _herd_trace(80, request_id=2),
            _herd_trace(30, request_id=3),
        ],
        threshold=10,
        routes=ROUTES,
        models=MODELS,
    )
    assert [f.request_id for f in findings] == [2, 3, 1]


def test_a_trace_with_no_phases_is_ignored() -> None:
    assert find_n_plus_one([_trace(phases=[])], threshold=2, routes=ROUTES, models=MODELS) == []


def test_one_trace_can_name_more_than_one_offender() -> None:
    phases = []
    for _ in range(20):
        phases += [_phase("db_query"), _phase("orm_hydrate", 5)]
    for _ in range(40):
        phases += [_phase("db_query"), _phase("orm_hydrate", 6)]
    (finding,) = find_n_plus_one(
        [_trace(phases=phases)], threshold=10, routes=ROUTES, models=MODELS
    )
    assert [r.model for r in finding.repetitions] == ["Llama", "Trek"]
    assert finding.worst.count == 40


class Req:
    """The surface the guard touches."""

    def __init__(self, method: str = "GET", path: str = "/llamas") -> None:
        self.method = method
        self.path = path
        self.state = _State()


class _State:
    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)


@pytest.fixture(autouse=True)
def _no_ledger_leaks():
    """A leaked binding would make the next test count the previous one's queries."""
    token = query_ledger.set(None)
    yield
    query_ledger.reset(token)


def test_n_plus_one_limits_must_be_positive() -> None:
    for factory in (
        lambda: QueryLedger(limit=0),
        lambda: NPlusOneGuard(limit=0),
    ):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            factory()


@pytest.mark.asyncio
async def test_the_guard_binds_a_ledger_for_the_request() -> None:
    guard = NPlusOneGuard(limit=5)
    request = Req()
    await guard.before(request)

    ledger = query_ledger.get(None)
    assert isinstance(ledger, QueryLedger)
    assert ledger.route == "GET /llamas"

    await guard.after(request, "response")
    assert query_ledger.get(None) is None  # and unbound again afterwards


@pytest.mark.asyncio
async def test_the_guard_after_hook_is_safe_when_before_never_ran() -> None:
    guard = NPlusOneGuard(limit=5)
    assert await guard.after(Req(), "response") == "response"


@pytest.mark.asyncio
async def test_a_request_under_the_limit_passes_through() -> None:
    guard = NPlusOneGuard(limit=5)
    request = Req()
    await guard.before(request)
    ledger = query_ledger.get(None)
    for _ in range(4):
        ledger.record("Trek")
    assert await guard.after(request, "response") == "response"


@pytest.mark.asyncio
async def test_the_query_that_crosses_the_limit_is_the_one_that_raises() -> None:
    guard = NPlusOneGuard(limit=5)
    await guard.before(Req())
    ledger = query_ledger.get(None)

    for _ in range(4):
        ledger.record("Trek")  # four is fine
    with pytest.raises(NPlusOneDetected) as caught:
        ledger.record("Trek")  # the fifth is not

    message = str(caught.value)
    assert "GET /llamas" in message
    assert "Trek" in message
    assert "5" in message
    assert isinstance(caught.value.finding, Finding)


@pytest.mark.asyncio
async def test_the_guard_can_report_instead_of_raising() -> None:
    seen: list[Finding] = []
    guard = NPlusOneGuard(limit=3, on_detect=seen.append)
    await guard.before(Req())
    ledger = query_ledger.get(None)

    for _ in range(5):
        ledger.record("Trek")  # never raises

    assert [f.worst.model for f in seen] == ["Trek"]
    assert seen[0].worst.count == 3  # reported once, when it tripped


def test_a_ledger_counts_each_model_separately() -> None:
    ledger = QueryLedger(limit=100, route="GET /llamas")
    for _ in range(3):
        ledger.record("Trek")
    ledger.record("Llama")
    assert ledger.counts == {"Trek": 3, "Llama": 1}


def test_a_ledger_below_its_limit_has_no_finding() -> None:
    ledger = QueryLedger(limit=10, route="GET /llamas")
    ledger.record("Trek")
    assert ledger.finding() is None


def test_a_ledger_reports_only_the_models_that_crossed_the_limit() -> None:
    ledger = QueryLedger(limit=3, route="GET /llamas")
    for _ in range(4):
        ledger.record("Trek")
    ledger.record("Llama")
    finding = ledger.finding()
    assert finding is not None
    assert [r.model for r in finding.repetitions] == ["Trek"]


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def registry(database: FakeDatabase) -> Registry:
    return Registry(database, [User, Post])


@pytest.fixture(autouse=True)
def _restore_watching():
    """`WATCHING` latches for the process; tests must not inherit each other's."""
    import wreath._nplusone as module

    before = module.WATCHING
    yield
    module.WATCHING = before


@pytest.mark.asyncio
async def test_a_fetch_tells_the_ledger_which_model_it_hydrated(
    registry: Registry, database: FakeDatabase
) -> None:
    watch()
    ledger = QueryLedger(limit=100, route="GET /users")
    query_ledger.set(ledger)

    database.connection.script("users", [user_row(1)])
    await Session(registry, "read").fetch(User.select())

    # Keyed by module *and* qualname: two models of the same name in different
    # modules must not share a tally. What a reader is *shown* is shortened
    # back to `User` unless the ledger has counted a second one -- see
    # `test_two_models_of_the_same_name_are_counted_separately`.
    assert ledger.counts == {f"{User.__module__}.{User.__qualname__}": 1}


@pytest.mark.asyncio
async def test_the_orm_seam_is_inert_until_a_guard_exists(
    registry: Registry, database: FakeDatabase
) -> None:
    import wreath._nplusone as module

    module.WATCHING = False
    ledger = QueryLedger(limit=1, route="GET /users")
    query_ledger.set(ledger)

    database.connection.script("users", [user_row(1)])
    await Session(registry, "read").fetch(User.select())

    assert ledger.counts == {}


@pytest.mark.asyncio
async def test_a_fetch_without_a_ledger_is_untouched(
    registry: Registry, database: FakeDatabase
) -> None:
    watch()
    database.connection.script("users", [user_row(1)])
    users = await Session(registry, "read").fetch(User.select())
    assert len(users) == 1


@pytest.mark.asyncio
async def test_the_guard_stops_a_real_loop_at_the_query_that_crossed_it(
    registry: Registry, database: FakeDatabase
) -> None:
    guard = NPlusOneGuard(limit=5)
    await guard.before(Req(path="/users"))

    database.connection.script("users", [user_row(1)])
    session = Session(registry, "read")
    with pytest.raises(NPlusOneDetected) as caught:
        for _ in range(10):
            await session.fetch(User.select())

    assert caught.value.finding.worst == Repetition(model="User", count=5)
    # The fifth query never reached the database: the ledger is consulted before
    # the statement runs, so the guard stops the loop instead of watching it.
    assert len(database.connection.calls) == 4


@pytest.mark.asyncio
async def test_an_armed_request_records_an_orm_hydrate_phase(
    registry: Registry, database: FakeDatabase
) -> None:
    from wreath._flight_markers import phase_marker
    from wreath._flight_schema import PhaseKind

    registry._flight_model_ids[User] = 7
    recorded: list[tuple[int, int, int, int]] = []
    token = phase_marker.set(lambda *args: recorded.append(args))
    try:
        database.connection.script("users", [user_row(1)])
        await Session(registry, "read").fetch(User.select())
    finally:
        phase_marker.reset(token)

    hydrations = [p for p in recorded if p[0] == int(PhaseKind.ORM_HYDRATE)]
    assert len(hydrations) == 1
    assert hydrations[0][1] == 7  # dependency_id names the model
    assert hydrations[0][3] >= 0  # and it was timed


@pytest.mark.asyncio
async def test_an_unstamped_model_records_no_phase(
    registry: Registry, database: FakeDatabase
) -> None:
    from wreath._flight_markers import phase_marker
    from wreath._flight_schema import PhaseKind

    recorded: list[tuple[int, int, int, int]] = []
    token = phase_marker.set(lambda *args: recorded.append(args))
    try:
        database.connection.script("users", [user_row(1)])
        await Session(registry, "read").fetch(User.select())
    finally:
        phase_marker.reset(token)

    assert not [p for p in recorded if p[0] == int(PhaseKind.ORM_HYDRATE)]


def test_the_metadata_image_knows_its_model_names(registry: Registry) -> None:
    from wreath._flight_metadata import _model_names

    assert _model_names(registry) == ["Post", "User"]


class StubInspector:
    """The three calls `wreath doctor` makes against a live Inspector."""

    def __init__(self, traces: list, *, tables: dict | None = None) -> None:
        self.traces = traces
        self.tables = tables if tables is not None else {"routes": ROUTES, "models": MODELS}
        self.asked: list[str] = []

    async def timeline(self, *, offset: int = 0, limit: int = 256) -> dict:
        self.asked.append(f"timeline:{limit}")
        return {
            "traces": self.traces[offset : offset + limit],
            "total": len(self.traces),
            "assembled": len(self.traces),
        }

    async def metadata(self, table: str, *, offset: int = 0, limit: int = 256) -> dict:
        self.asked.append(f"metadata:{table}")
        return {
            "table": table,
            "rows": self.tables.get(table, []),
            "total": len(self.tables.get(table, [])),
        }


@pytest.mark.asyncio
async def test_the_doctor_diagnoses_a_running_server() -> None:
    client = StubInspector([_herd_trace(50), _herd_trace(2, request_id=2)])
    findings = await diagnose_n_plus_one(client, threshold=10)

    assert [f.explain() for f in findings] == [
        "GET /llamas issued 51 statements; 50 of them hydrated Trek"
    ]
    assert client.asked == ["timeline:256", "metadata:routes", "metadata:models"]


@pytest.mark.asyncio
async def test_a_healthy_server_yields_no_findings() -> None:
    findings = await diagnose_n_plus_one(StubInspector([_herd_trace(2)]))
    assert findings == []


@pytest.mark.asyncio
async def test_the_doctor_survives_a_server_with_no_model_table() -> None:
    client = StubInspector([_herd_trace(50)], tables={"routes": ROUTES})
    (finding,) = await diagnose_n_plus_one(client, threshold=10)
    assert finding.worst.model == "model:5"


@pytest.mark.asyncio
async def test_extension_check_skips_a_registry_without_extension_columns(
    monkeypatch,
) -> None:
    from wreath.doctor import check_extension_types
    from wreath.orm import introspection

    class Database:
        name = "primary"

        async def acquire(self, workload):
            pytest.fail("an empty registry must not acquire a connection")

    registry = type("Registry", (), {"database": Database()})()
    monkeypatch.setattr(introspection, "declared_extension_columns", lambda registry: ())

    assert await check_extension_types(registry) == []


@pytest.mark.asyncio
async def test_extension_check_reports_only_missing_types_and_releases(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from wreath.doctor import check_extension_types
    from wreath.orm import introspection
    from wreath.orm.introspection import ExtensionTypeResolution

    vector_type = SimpleNamespace(type_name="vector", extension="vector")
    vector_column = SimpleNamespace(
        pg_type=vector_type,
        python_name="embedding",
    )
    sparse_type = SimpleNamespace(type_name="sparsevec", extension="vector")
    sparse_column = SimpleNamespace(
        pg_type=sparse_type,
        python_name="sparse_embedding",
    )
    model_type = type("Document", (), {})
    spec = SimpleNamespace(model_type=model_type)
    released: list[tuple[str, object]] = []
    connection = object()

    class Database:
        name = "primary"

        async def acquire(self, workload):
            assert workload == "write"
            return connection

        async def release(self, workload, released_connection):
            released.append((workload, released_connection))

    registry = type("Registry", (), {"database": Database()})()
    monkeypatch.setattr(
        introspection,
        "declared_extension_columns",
        lambda registry: ((spec, vector_column), (spec, sparse_column)),
    )

    async def probe(connection, wanted):
        assert wanted == {"vector": "vector", "sparsevec": "vector"}
        return (
            ExtensionTypeResolution("vector", "vector", 0, "", ""),
            ExtensionTypeResolution("sparsevec", "vector", 0, "", "tenant"),
            ExtensionTypeResolution("halfvec", "vector", 12, "public", "public"),
        )

    monkeypatch.setattr(introspection, "probe_extension_types", probe)

    assert await check_extension_types(registry) == [
        "the 'vector' extension is not installed on 'primary' (current schema '?'), "
        "so the 'vector' type used by Document.embedding has no OID; run "
        "CREATE EXTENSION IF NOT EXISTS vector",
        "the 'vector' extension is not installed on 'primary' "
        "(current schema 'tenant'), so the 'sparsevec' type used by "
        "Document.sparse_embedding has no OID; run "
        "CREATE EXTENSION IF NOT EXISTS vector",
    ]
    assert released == [("write", connection)]


def _same_named_key(module: str) -> str:
    """The ledger key a top-level `Invoice` in `module` would produce."""
    return f"{module}.Invoice"


def test_two_models_of_the_same_name_are_counted_separately() -> None:
    tripped: list[Finding] = []
    ledger = QueryLedger(limit=2, route="GET /invoices", on_exceeded=tripped.append)

    ledger.record(_same_named_key("billing"))
    ledger.record(_same_named_key("reporting"))

    assert ledger.counts == {"billing.Invoice": 1, "reporting.Invoice": 1}
    assert tripped == []
    assert ledger.finding() is None


def test_an_ambiguous_name_is_reported_with_its_module() -> None:
    ledger = QueryLedger(limit=2, route="GET /invoices")
    for _ in range(2):
        ledger.record(_same_named_key("billing"))
    ledger.record(_same_named_key("reporting"))

    finding = ledger.finding()
    assert finding is not None
    assert finding.worst.model == "billing.Invoice"


def test_an_unambiguous_name_keeps_its_short_form() -> None:
    ledger = QueryLedger(limit=2, route="GET /treks")
    for _ in range(3):
        ledger.record("myapp.models.Trek")

    finding = ledger.finding()
    assert finding is not None
    assert finding.worst.model == "Trek"
    assert "myapp" not in finding.explain()
