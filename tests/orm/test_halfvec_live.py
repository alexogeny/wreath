from __future__ import annotations

import os

import pytest

from wreath.postgres import PostgresError

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.database

_SCHEMA = f"wreath_halfvec_{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}"


async def _connection(database):
    connection = await database.acquire("write")
    try:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:  # noqa: BLE001 - reported as a skip by the caller
        await database.release("write", connection)
        pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
    return connection


@pytest.fixture
async def live():
    """A live schema holding one `halfvec(3)` table, dropped afterwards."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for live halfvec tests")
    from wreath.postgres import Database, PoolConfig

    database = Database("halfvec-live", _DSN, pools={"write": PoolConfig(min_size=1, max_size=2)})
    await database.start()
    connection = await _connection(database)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."docs" '
            "(id bigint PRIMARY KEY, embedding halfvec(3) NOT NULL)"
        )
        yield database, connection
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
    finally:
        await database.release("write", connection)
        await database.stop()


async def _bind_real_oid(connection) -> int:
    """Resolve `halfvec`'s real OID into both codecs, as startup would."""
    from wreath.orm.types import EXT_KIND_HALFVEC, bind_extension_oid

    rows = await connection.fetch("SELECT oid FROM pg_type WHERE typname = 'halfvec'")
    assert rows, "pgvector is installed but has no halfvec type; needs pgvector >= 0.7"
    oid = int(rows[0]["oid"])
    from wreath import _pgdriver as pure

    pure._register_extension_type("halfvec", oid, EXT_KIND_HALFVEC)
    try:
        native = __import__("wreath._native._postgres", fromlist=["x"])
        native._register_extension_type("halfvec", oid, EXT_KIND_HALFVEC)
    except ImportError:  # pragma: no cover - pure-only build
        pass
    bind_extension_oid("halfvec", oid)
    return oid


async def test_a_binary_bound_halfvec_round_trips_through_the_server(live) -> None:
    database, connection = live
    from wreath.orm.types import Halfvec

    # Declared before the bind, not after: `bind_extension_oid` walks the types
    # declared *when it is called*, so a column constructed afterwards keeps OID 0
    # and refuses to encode. `ExtensionType.require_oid` documents this exact trap.
    column = Halfvec(3)
    await _bind_real_oid(connection)
    written = column.coerce([1.5, -2.25, 0.5])
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, embedding) VALUES ($1, $2)',
        1,
        column.to_wire(written),
    )
    rows = await connection.fetch(f'SELECT embedding FROM "{_SCHEMA}"."docs" WHERE id = 1')
    assert list(rows[0]["embedding"]) == [1.5, -2.25, 0.5]


async def test_the_server_agrees_with_our_encoder_byte_for_byte(live) -> None:
    database, connection = live
    from wreath.orm.types import Halfvec

    # Declared before the bind, not after: `bind_extension_oid` walks the types
    # declared *when it is called*, so a column constructed afterwards keeps OID 0
    # and refuses to encode. `ExtensionType.require_oid` documents this exact trap.
    column = Halfvec(3)
    await _bind_real_oid(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, embedding) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce([0.25, -0.5, 2.0])),
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, embedding) VALUES (2, \'[0.25,-0.5,2]\'::halfvec)'
    )
    rows = await connection.fetch(f'SELECT id, embedding FROM "{_SCHEMA}"."docs" ORDER BY id')
    ours, theirs = list(rows[0]["embedding"]), list(rows[1]["embedding"])
    assert ours == theirs == [0.25, -0.5, 2.0]


async def test_the_result_format_decides_which_decimal_comes_back(live) -> None:
    database, connection = live
    from wreath.orm.types import Halfvec

    column = Halfvec(3)
    await _bind_real_oid(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, embedding) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce([0.1, 0.2, 0.3])),
    )
    rows = await connection.fetch(f'SELECT embedding FROM "{_SCHEMA}"."docs"')
    assert list(rows[0]["embedding"]) == [0.099975586, 0.19995117, 0.30004883]

    # The same row, and the exact stored value, via pgvector's own text cast --
    # so this asserts the *server's* rendering rather than our parse of it.
    rendered = await connection.fetch(f'SELECT embedding::text AS t FROM "{_SCHEMA}"."docs"')
    assert rendered[0]["t"] == "[0.099975586,0.19995117,0.30004883]"

    # And the precision really is binary16's: 0.1 does not survive.
    assert list(rows[0]["embedding"])[0] != 0.1


async def test_distance_operators_work_over_halfvec(live) -> None:
    database, connection = live
    await _bind_real_oid(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, embedding) VALUES '
        "(1, '[1,0,0]'), (2, '[0,1,0]'), (3, '[0,0,1]')"
    )
    for operator in ("<->", "<=>", "<#>"):
        rows = await connection.fetch(
            f'SELECT id FROM "{_SCHEMA}"."docs" '
            f"ORDER BY embedding {operator} '[1,0,0]'::halfvec LIMIT 1"
        )
        assert rows[0]["id"] == 1, operator


async def test_an_hnsw_index_over_halfvec_is_accepted_with_its_own_opclass(live) -> None:
    database, connection = live
    await connection.execute(
        f'CREATE INDEX docs_hnsw ON "{_SCHEMA}"."docs" USING hnsw (embedding halfvec_cosine_ops)'
    )
    rows = await connection.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 AND indexname = $2",
        _SCHEMA,
        "docs_hnsw",
    )
    assert "hnsw" in rows[0]["indexdef"]
    assert "halfvec_cosine_ops" in rows[0]["indexdef"]

    # PostgreSQL reports the wrong opclass itself, so this is the server's refusal
    # rather than ours -- which is the point: the opclass names are per type.
    with pytest.raises(PostgresError):
        await connection.execute(
            f'CREATE INDEX docs_wrong ON "{_SCHEMA}"."docs" '
            "USING hnsw (embedding vector_cosine_ops)"
        )


async def test_a_halfvec_index_is_half_the_size_of_the_vector_equivalent(live) -> None:
    database, connection = live
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."full" (id bigint PRIMARY KEY, e vector(512))'
    )
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."half" (id bigint PRIMARY KEY, e halfvec(512))'
    )
    for table, cast in (("full", "vector"), ("half", "halfvec")):
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}"."{table}" (id, e) '
            "SELECT g, (SELECT '[' || string_agg((random())::text, ',') || ']' "
            f"FROM generate_series(1, 512))::{cast} FROM generate_series(1, 256) g"
        )
        await connection.execute(
            f'CREATE INDEX {table}_hnsw ON "{_SCHEMA}"."{table}" USING hnsw (e {cast}_l2_ops)'
        )
    sizes = {}
    for table in ("full", "half"):
        # The identifier is interpolated rather than bound: `regclass` has no
        # binary bind encoder, and both halves of this name are ours.
        rows = await connection.fetch(
            f"SELECT pg_relation_size('\"{_SCHEMA}\".{table}_hnsw'::regclass) AS bytes"
        )
        sizes[table] = int(rows[0]["bytes"])
    assert sizes["half"] < sizes["full"], sizes
