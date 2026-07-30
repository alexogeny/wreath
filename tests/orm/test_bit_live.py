"""`bit` and pgvector's two distances over it, against a real server.

`test_bit_codec.py` holds the two twins byte-for-byte equal to *each other*, and
asserts the MSB-first packing against literal bytes. Neither settles the question
this file exists for: whether PostgreSQL reads those bytes as the same bit string
we wrote, and whether pgvector's `<~>` and `<%>` then rank by it.

The failure this guards is quiet. Reversed bit order still round-trips through
our own codec, still stores, still indexes, and still returns *an* ordering --
just the wrong one, on a column whose whole purpose is to rank candidates
approximately, where "slightly worse recall" is exactly what a real bug would
look like.

Gated on `WREATH_TEST_POSTGRES_DSN`; the distance tests additionally need
pgvector, since only the operators come from the extension -- `bit` itself is
PostgreSQL's own type and needs nothing.
"""

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

    database = Database(
        "bit-live", _DSN, pools={"write": PoolConfig(min_size=1, max_size=2)}
    )
    await database.start()
    connection = await database.acquire("write")
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."docs" '
            "(id bigint PRIMARY KEY, signature bit(8) NOT NULL)"
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
    """Our encoder in, our decoder out, PostgreSQL's own storage in between."""
    database, connection = live
    from wreath.orm.types import Bit

    column = Bit(8)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce("10000001")),
    )
    rows = await connection.fetch(
        f'SELECT signature FROM "{_SCHEMA}"."docs" WHERE id = 1'
    )
    assert rows[0]["signature"] == "10000001"


async def test_the_server_reads_our_bytes_in_the_order_we_wrote_them(live) -> None:
    """The decision the whole codec turns on, settled by the server rather than us.

    `'10000001'` is asymmetric, so a reversed packing produces a *different*
    string rather than the same one -- which a palindrome like `'10011001'` would
    have hidden. Compared against PostgreSQL's own literal, so the assertion is
    about the server's reading and not about our round trip.
    """
    database, connection = live
    from wreath.orm.types import Bit

    column = Bit(8)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce("11100010")),
    )
    await connection.execute(
        f"INSERT INTO \"{_SCHEMA}\".\"docs\" (id, signature) VALUES (2, B'11100010')"
    )
    rows = await connection.fetch(
        f'SELECT id, signature::text AS t FROM "{_SCHEMA}"."docs" ORDER BY id'
    )
    assert rows[0]["t"] == rows[1]["t"] == "11100010"


async def test_a_bit_length_that_is_not_a_whole_byte_survives_the_padding(live) -> None:
    """The final byte is padded on the right, and the pad must not become data."""
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
    """The convenience path: `numpy.packbits(...).tobytes()` and nothing else."""
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
    """`<~>` is pgvector's, over PostgreSQL's own type."""
    database, connection = live
    await _needs_pgvector(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, signature) VALUES '
        "(1, B'00000000'), (2, B'00000011'), (3, B'11111111')"
    )
    rows = await connection.fetch(
        f'SELECT id, (signature <~> B\'00000000\')::float8 AS d '
        f'FROM "{_SCHEMA}"."docs" ORDER BY d'
    )
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert [row["d"] for row in rows] == [0.0, 2.0, 8.0]


async def test_jaccard_distance_measures_overlap_rather_than_agreement(live) -> None:
    """The two operators rank the same two pairs in opposite orders.

    Jaccard ignores the positions where both values are zero; Hamming counts them
    as agreement. So the two disagree exactly when the shared zeros outnumber
    everything else, which for a quantized signature is the common case:

    - `11110000` against `11111111` differs in four positions and overlaps in four
      of the four-or-eight it could -- Hamming 4, Jaccard 1 - 4/8 = 0.5.
    - `10000000` against `01000000` differs in only two positions, but shares no
      set bit at all -- Hamming 2, Jaccard 1 - 0/2 = 1, the maximum.

    Hamming calls the second pair the closer one; Jaccard calls the first. That
    inversion is the whole reason to have the second operator, and it is why a
    sparse signature ranked by Hamming drifts toward whichever rows are emptiest.
    """
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
    """`bit_hamming_ops` and `bit_jaccard_ops`, not any `vector` opclass."""
    database, connection = live
    await _needs_pgvector(connection)
    await connection.execute(
        f'CREATE INDEX docs_hamming ON "{_SCHEMA}"."docs" '
        "USING hnsw (signature bit_hamming_ops)"
    )
    rows = await connection.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 AND indexname = $2",
        _SCHEMA,
        "docs_hamming",
    )
    assert "hnsw" in rows[0]["indexdef"]
    assert "bit_hamming_ops" in rows[0]["indexdef"]


async def test_a_quantized_signature_is_thirty_two_times_smaller_on_disk(live) -> None:
    """The claim the type is chosen for, measured rather than asserted from theory.

    Same 256 rows at 512 dimensions: one table of `vector(512)`, one of the
    `bit(512)` signature of the same thing. Toast and page overhead keep this
    from being exactly 32x, so the assertion is a generous factor of eight.

    **The size function must be `pg_table_size`, not `pg_relation_size`.** A
    `vector(512)` is 2,056 bytes, past the ~2KB threshold at which PostgreSQL
    moves a value out of line, so the dense table's *main* fork holds pointers
    and measures 16KB while its TOAST relation holds the 800KB of actual floats.
    The `bit(512)` is 64 bytes and stays inline, so by `pg_relation_size` the
    quantized table looks two times *larger* than the dense one it shrinks by
    fourteen. `pg_table_size` counts the TOAST relation; it also excludes
    indexes, which is wanted here, since the identical `bigint` primary key on
    both sides is noise this comparison should not carry.
    """
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
