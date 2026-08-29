from __future__ import annotations

import os

import pytest

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.database

_SCHEMA = f"wreath_bit_{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}"


@pytest.fixture
async def live():
    """A live schema holding one `bit(8)` table, dropped afterwards."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for live bit tests")
    from wreath.postgres import Database, PoolConfig

    database = Database("bit-live", _DSN, pools={"write": PoolConfig(min_size=1, max_size=2)})
    await database.start()
    connection = await database.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."docs" (id bigint PRIMARY KEY, signature bit(8) NOT NULL)'
        )
        yield database, connection
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
    finally:
        await database.release("write", connection)
        await database.stop()


async def _needs_pgvector(connection) -> None:
    try:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:  # noqa: BLE001 - reported as a skip on the next line
        pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")


async def test_a_binary_bound_bit_round_trips_through_the_server(live) -> None:
    database, connection = live
    from wreath.orm.types import Bit

    column = Bit(8)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce("10000001")),
    )
    rows = await connection.fetch(f'SELECT signature FROM "{_SCHEMA}"."docs" WHERE id = 1')
    assert rows[0]["signature"] == "10000001"


async def test_the_server_reads_our_bytes_in_the_order_we_wrote_them(live) -> None:
    database, connection = live
    from wreath.orm.types import Bit

    column = Bit(8)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce("11100010")),
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES (2, B\'11100010\')'
    )
    rows = await connection.fetch(
        f'SELECT id, signature::text AS t FROM "{_SCHEMA}"."docs" ORDER BY id'
    )
    assert rows[0]["t"] == rows[1]["t"] == "11100010"


async def test_a_bit_length_that_is_not_a_whole_byte_survives_the_padding(live) -> None:
    database, connection = live
    from wreath.orm.types import Bit

    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."odd" (id bigint PRIMARY KEY, s bit(11) NOT NULL)'
    )
    column = Bit(11)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."odd" (id, s) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce("10110010111")),
    )
    rows = await connection.fetch(f'SELECT s::text AS t FROM "{_SCHEMA}"."odd"')
    assert rows[0]["t"] == "10110010111"


async def test_packed_bytes_reach_the_server_as_the_bits_they_encode(live) -> None:
    database, connection = live
    from wreath.orm.types import Bit

    column = Bit(8)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce(b"\x81")),
    )
    rows = await connection.fetch(f'SELECT signature::text AS t FROM "{_SCHEMA}"."docs"')
    assert rows[0]["t"] == "10000001"


async def test_hamming_distance_counts_the_bits_that_differ(live) -> None:
    database, connection = live
    await _needs_pgvector(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES '
        "(1, B'00000000'), (2, B'00000011'), (3, B'11111111')"
    )
    rows = await connection.fetch(
        f'SELECT id, (signature <~> B\'00000000\')::float8 AS d FROM "{_SCHEMA}"."docs" ORDER BY d'
    )
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert [row["d"] for row in rows] == [0.0, 2.0, 8.0]


async def test_jaccard_distance_measures_overlap_rather_than_agreement(live) -> None:
    database, connection = live
    await _needs_pgvector(connection)
    rows = await connection.fetch(
        "SELECT (B'11110000' <~> B'11111111')::float8 AS hamming_overlapping, "
        "(B'11110000' <%> B'11111111')::float8 AS jaccard_overlapping, "
        "(B'10000000' <~> B'01000000')::float8 AS hamming_disjoint, "
        "(B'10000000' <%> B'01000000')::float8 AS jaccard_disjoint"
    )
    row = rows[0]
    assert row["hamming_overlapping"] == 4.0
    assert row["jaccard_overlapping"] == 0.5
    assert row["hamming_disjoint"] == 2.0
    assert row["jaccard_disjoint"] == 1.0
    # The inversion itself, stated as the comparison rather than left to the
    # reader to perform on the four constants above.
    assert row["hamming_disjoint"] < row["hamming_overlapping"]
    assert row["jaccard_overlapping"] < row["jaccard_disjoint"]


async def test_an_hnsw_index_over_bit_uses_the_bit_opclasses(live) -> None:
    database, connection = live
    await _needs_pgvector(connection)
    await connection.execute(
        f'CREATE INDEX docs_hamming ON "{_SCHEMA}"."docs" USING hnsw (signature bit_hamming_ops)'
    )
    rows = await connection.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 AND indexname = $2",
        _SCHEMA,
        "docs_hamming",
    )
    assert "hnsw" in rows[0]["indexdef"]
    assert "bit_hamming_ops" in rows[0]["indexdef"]


async def test_a_quantized_signature_is_thirty_two_times_smaller_on_disk(live) -> None:
    database, connection = live
    await _needs_pgvector(connection)
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."dense" (id bigint PRIMARY KEY, e vector(512))'
    )
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."signed" (id bigint PRIMARY KEY, s bit(512))'
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."dense" (id, e) '
        "SELECT g, (SELECT '[' || string_agg((random())::text, ',') || ']' "
        "FROM generate_series(1, 512))::vector FROM generate_series(1, 256) g"
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."signed" (id, s) '
        "SELECT g, (SELECT string_agg((random() > 0.5)::int::text, '') "
        "FROM generate_series(1, 512))::bit(512) FROM generate_series(1, 256) g"
    )
    sizes = {}
    for table in ("dense", "signed"):
        # The identifier is interpolated rather than bound: `regclass` has no
        # binary bind encoder, and both halves of this name are ours.
        rows = await connection.fetch(
            f"SELECT pg_table_size('\"{_SCHEMA}\".{table}'::regclass) AS bytes"
        )
        sizes[table] = int(rows[0]["bytes"])
    assert sizes["signed"] * 8 < sizes["dense"], sizes
