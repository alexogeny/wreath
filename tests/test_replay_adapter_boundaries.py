from __future__ import annotations

from types import SimpleNamespace

import pytest

from wreath._replay_adapters import (
    AdapterFault,
    DatabaseDouble,
    FaultyHttpClient,
    ObjectStoreDouble,
    ReplayAdapters,
    _db_error,
    installed_boundaries,
    observed_boundaries,
    refuse_mapping_rows,
    refuse_parameter_arity,
    refuse_uninferable_cast,
    refuse_what_postgres_refuses,
)
from wreath.http_client import ClientResponse
from wreath.objects import ObjectError
from wreath.postgres import InterfaceError, OperationalError, PostgresError
from wreath.replay import AdapterFaultDescriptor, AdapterSeam


@pytest.mark.parametrize(
    ("fault", "error_type", "message"),
    [
        (
            AdapterFault.CONNECTION_FAILED,
            InterfaceError,
            "PostgreSQL connection failed; every operation on it",
        ),
        (
            AdapterFault.LOST_COMMIT,
            OperationalError,
            "commit acknowledgement lost (ambiguous completion)",
        ),
        (
            AdapterFault.RELEASE_ERROR,
            InterfaceError,
            "returning the connection to the pool failed",
        ),
        (
            AdapterFault.LISTEN_REFUSED,
            OperationalError,
            "LISTEN refused: no connection to the server",
        ),
        (
            AdapterFault.NOTIFY_STREAM_ERROR,
            OperationalError,
            "notification stream lost",
        ),
        (
            AdapterFault.BEGIN_ERROR,
            OperationalError,
            "could not open a transaction",
        ),
        (
            AdapterFault.COMMIT_ERROR,
            OperationalError,
            "commit failed after the work was applied",
        ),
        (
            AdapterFault.STATEMENT_TIMEOUT,
            PostgresError,
            "canceling statement due to statement timeout",
        ),
        (
            AdapterFault.SERVER_ERROR,
            PostgresError,
            "server error after the round trip began",
        ),
    ],
)
def test_database_faults_have_distinct_owned_errors(
    fault: AdapterFault, error_type: type[Exception], message: str
) -> None:
    error = _db_error(fault)

    assert type(error) is error_type
    assert str(error) == message


def test_object_store_url_surfaces_an_unreachable_boundary() -> None:
    store = ObjectStoreDouble("files", op_faults={0: AdapterFault.OBJECT_UNREACHABLE})

    with pytest.raises(ObjectError, match="unreachable.*'avatar.png'"):
        store.url("avatar.png")


@pytest.mark.parametrize(
    ("sql", "args", "message"),
    [
        ("SELECT $0", (), "there is no parameter $0"),
        ("SELECT 1", (1,), "could not determine data type of parameter $1"),
        (
            "SELECT $1, $2",
            (1,),
            "bind message supplies 1 parameters, but prepared statement requires 2",
        ),
    ],
)
def test_parameter_arity_refuses_each_postgres_shape(
    sql: str, args: tuple[object, ...], message: str
) -> None:
    with pytest.raises(PostgresError) as caught:
        refuse_parameter_arity(sql, args)

    assert str(caught.value) == message


def test_missing_cast_argument_is_left_to_parameter_arity() -> None:
    seen = {"SELECT $2::regclass"}

    refuse_uninferable_cast("SELECT $2::regclass", ("one",), seen)


def test_an_encodable_cast_is_not_treated_as_unencodable() -> None:
    seen = {"SELECT $1::text"}

    refuse_uninferable_cast("SELECT $1::text", ("value",), seen)


def test_non_string_sql_is_outside_sql_text_refusals() -> None:
    refuse_what_postgres_refuses(object(), (["not", "bindable"],), set())


def test_mapping_row_refusal_checks_rows_inside_a_result_sequence() -> None:
    with pytest.raises(TypeError, match="scripted result rows"):
        refuse_mapping_rows(([{"id": 1}],))

    refuse_mapping_rows((41, [1, 2], (3, 4)))


@pytest.mark.asyncio
async def test_connection_accepts_non_string_statement_objects_at_the_double_boundary() -> None:
    statement = object()
    double = DatabaseDouble("main")
    connection = await double.acquire("read")

    assert await connection.execute(statement) == "OK"
    assert double.calls == [(statement, ())]


@pytest.mark.asyncio
async def test_claim_lost_returns_each_query_methods_empty_shape() -> None:
    row_double = DatabaseDouble("main", query_faults={0: AdapterFault.CLAIM_LOST})
    row_connection = await row_double.acquire("read")
    rows_double = DatabaseDouble("main", query_faults={0: AdapterFault.CLAIM_LOST})
    rows_connection = await rows_double.acquire("read")

    assert await row_connection.fetchrow("SELECT 1") is None
    assert await rows_connection.fetch("SELECT 1") == []


@pytest.mark.asyncio
async def test_non_string_prepared_poison_does_not_invent_a_cache_key() -> None:
    statement = object()
    double = DatabaseDouble("main", query_faults={0: AdapterFault.PREPARED_POISON})
    connection = await double.acquire("read")

    assert await connection.execute(statement) == "OK"
    assert double.poisoned == set()


@pytest.mark.asyncio
async def test_map_accepts_non_string_statements_and_an_absent_argument_sequence() -> None:
    double = DatabaseDouble("main")
    connection = await double.acquire("read")

    assert await connection.map("fetch", object(), None) == []
    assert await connection.map("fetch", "SELECT 1", None) == []


def test_http_double_refuses_two_competing_script_sources() -> None:
    response = ClientResponse(200, (), b"", "1.1")

    with pytest.raises(ValueError, match="responses or exchanges"):
        FaultyHttpClient("api", responses=(response,), exchanges=(object(),))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_http_double_falls_back_after_scripted_responses_are_consumed() -> None:
    scripted = ClientResponse(204, (), b"scripted", "1.1")
    client = FaultyHttpClient("api", responses=(scripted,))
    kwargs = {"headers": (), "body": b"", "idempotency_key": None}

    first = await client._request_timed("GET", "/one", **kwargs)
    second = await client._request_timed("GET", "/two", **kwargs)

    assert first is scripted
    assert second.status == 200
    assert second.body == b""


def test_non_object_fault_does_not_create_an_object_store_double() -> None:
    descriptor = AdapterFaultDescriptor(
        int(AdapterSeam.DB_QUERY), "main", AdapterFault.SERVER_ERROR.value, 0
    )

    adapters = ReplayAdapters.from_faults((descriptor,))

    assert set(adapters.databases) == {"main"}
    assert adapters.clients == {}
    assert adapters.object_stores == {}


def test_observer_updates_and_restores_the_state_object_store_alias() -> None:
    original = object()
    scope = SimpleNamespace(
        _object_stores={"files": original},
        state=SimpleNamespace(objects_files=original),
    )

    with observed_boundaries(scope, object()):
        observed = scope._object_stores["files"]
        assert observed is not original
        assert scope.state.objects_files is observed

    assert scope._object_stores["files"] is original
    assert scope.state.objects_files is original


def test_observer_does_not_replace_an_unrelated_state_value() -> None:
    original = object()
    unrelated = object()
    scope = SimpleNamespace(
        _object_stores={"files": original},
        state=SimpleNamespace(objects_files=unrelated),
    )

    with observed_boundaries(scope, object()):
        assert scope.state.objects_files is unrelated

    assert scope.state.objects_files is unrelated


def test_observer_tolerates_state_without_an_object_store_registry() -> None:
    scope = SimpleNamespace(state=SimpleNamespace())

    with observed_boundaries(scope, object()):
        assert not hasattr(scope, "_object_stores")


def test_installer_updates_and_restores_orm_and_state_aliases() -> None:
    database = object()
    store = object()
    registry = SimpleNamespace(database=database)
    scope = SimpleNamespace(
        _databases={"main": database},
        _orm_registries={"main": registry},
        _object_stores={"files": store},
        state=SimpleNamespace(objects_files=store),
        _dirty=False,
    )
    database_double = DatabaseDouble("main")
    store_double = ObjectStoreDouble("files")
    adapters = ReplayAdapters(
        databases={"main": database_double},
        object_stores={"files": store_double},
    )

    with installed_boundaries(scope, adapters):
        assert scope._databases["main"] is database_double
        assert registry.database is database_double
        assert scope._object_stores["files"] is store_double
        assert scope.state.objects_files is store_double

    assert scope._databases == {"main": database}
    assert registry.database is database
    assert scope._object_stores == {"files": store}
    assert scope.state.objects_files is store
    assert scope._dirty is True


def test_installer_leaves_unrelated_state_and_non_database_registries_alone() -> None:
    store = object()
    unrelated = object()
    registry = SimpleNamespace(label="not an ORM registry")
    scope = SimpleNamespace(
        _orm_registries={"main": registry},
        _object_stores={"files": store},
        state=SimpleNamespace(objects_files=unrelated),
    )
    adapters = ReplayAdapters(
        databases={"main": DatabaseDouble("main")},
        object_stores={"files": ObjectStoreDouble("files")},
    )

    with installed_boundaries(scope, adapters):
        assert not hasattr(registry, "database")
        assert scope.state.objects_files is unrelated

    assert not hasattr(registry, "database")
    assert scope.state.objects_files is unrelated
