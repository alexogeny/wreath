"""`sparsevec` against a real pgvector, which is the only proof the wire format matches.

`test_sparsevec_codec.py` holds the two twins byte-for-byte equal to *each other*.
That is necessary and not sufficient: two implementations can agree perfectly and
both disagree with the server. Only PostgreSQL can settle whether
`int32 dim, int32 nnz, int32 unused, nnz int32 indices, nnz float4 values` is what
pgvector reads -- and, the thing most likely to be wrong, whether the indices on
that wire really are **0-based** while the text form's are 1-based.

Gated on `WREATH_TEST_POSTGRES_DSN` *and* on the extension being installable, so a
server without pgvector skips with a reason naming the image to use rather than
failing obscurely. `sparsevec` needs pgvector >= 0.7.
"""

from __future__ import annotations

import os

import pytest

from wreath._sparsevec import MAX_SPARSEVEC_DIM, MAX_SPARSEVEC_NNZ
from wreath.postgres import PostgresError, SparseVector

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.database

_SCHEMA = f"wreath_sparsevec_{os.environ.get('PYTEST_XDIST_WORKER', 'solo')}"


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
    """A live schema holding one `sparsevec(5)` table, dropped afterwards."""
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for live sparsevec tests")
    from wreath.postgres import Database, PoolConfig

    database = Database(
        "sparsevec-live", _DSN, pools={"write": PoolConfig(min_size=1, max_size=2)}
    )
    await database.start()
    connection = await _connection(database)
    try:
        rows = await connection.fetch(
            "SELECT 1 AS present FROM pg_type WHERE typname = 'sparsevec'"
        )
        if not rows:
            pytest.skip("this pgvector has no sparsevec type; needs pgvector >= 0.7")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        await connection.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        await connection.execute(
            f'CREATE TABLE "{_SCHEMA}"."docs" '
            "(id bigint PRIMARY KEY, terms sparsevec(5) NOT NULL)"
        )
        yield database, connection
        await connection.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
    finally:
        await database.release("write", connection)
        await database.stop()


async def _bind_real_oid(connection) -> int:
    """Resolve `sparsevec`'s real OID into both codecs, as startup would."""
    from wreath.orm.types import EXT_KIND_SPARSEVEC, bind_extension_oid

    rows = await connection.fetch("SELECT oid FROM pg_type WHERE typname = 'sparsevec'")
    assert rows, "pgvector is installed but has no sparsevec type; needs >= 0.7"
    oid = int(rows[0]["oid"])
    from wreath._pure import postgres as pure

    pure._register_extension_type("sparsevec", oid, EXT_KIND_SPARSEVEC)
    try:
        native = __import__("wreath._native._postgres", fromlist=["x"])
        native._register_extension_type("sparsevec", oid, EXT_KIND_SPARSEVEC)
    except ImportError:  # pragma: no cover - pure-only build
        pass
    bind_extension_oid("sparsevec", oid)
    return oid


async def test_a_binary_bound_sparsevec_round_trips_through_the_server(live) -> None:
    """The contract the twin-parity tests cannot reach.

    Written with a binary bind parameter (our encoder) and read back (our
    decoder), through pgvector's own storage in between. The values are exactly
    representable in float4, so this is an equality assertion.
    """
    database, connection = live
    from wreath.orm.types import Sparsevec

    # Declared before the bind, not after: `bind_extension_oid` walks the types
    # declared *when it is called*, so a column constructed afterwards keeps OID 0
    # and refuses to encode. `ExtensionType.require_oid` documents this exact trap.
    column = Sparsevec(5)
    await _bind_real_oid(connection)
    written = column.coerce(SparseVector(5, {1: 1.5, 3: -2.25}))
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, terms) VALUES ($1, $2)',
        1,
        column.to_wire(written),
    )
    rows = await connection.fetch(f'SELECT terms FROM "{_SCHEMA}"."docs" WHERE id = 1')
    assert rows[0]["terms"] == SparseVector(5, {1: 1.5, 3: -2.25})


async def test_our_one_based_index_is_the_one_the_server_prints(live) -> None:
    """The off-by-one this type is most likely to have, settled by the server.

    Our binary encoder writes `index - 1`. If that conversion were missing, or
    applied twice, this row would still round-trip through our own codec and
    would land in the wrong position in the database -- which only pgvector's own
    text rendering can show.
    """
    database, connection = live
    from wreath.orm.types import Sparsevec

    column = Sparsevec(5)
    await _bind_real_oid(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, terms) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce(SparseVector(5, {1: 1.5, 3: 3.5}))),
    )
    rendered = await connection.fetch(
        f'SELECT terms::text AS t FROM "{_SCHEMA}"."docs" WHERE id = 1'
    )
    assert rendered[0]["t"] == "{1:1.5,3:3.5}/5"


async def test_the_server_agrees_with_our_encoder_byte_for_byte(live) -> None:
    """Our binary bind and pgvector's own text parse must store the same value."""
    database, connection = live
    from wreath.orm.types import Sparsevec

    column = Sparsevec(5)
    await _bind_real_oid(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, terms) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce(SparseVector(5, {2: 0.25, 5: -0.5}))),
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, terms) VALUES '
        "(2, '{2:0.25,5:-0.5}/5'::sparsevec)"
    )
    rows = await connection.fetch(
        f'SELECT id, terms FROM "{_SCHEMA}"."docs" ORDER BY id'
    )
    assert rows[0]["terms"] == rows[1]["terms"] == SparseVector(5, {2: 0.25, 5: -0.5})


async def test_an_empty_sparsevec_is_a_legal_value(live) -> None:
    """A document with no terms is a real case, and its dimension still matters."""
    database, connection = live
    from wreath.orm.types import Sparsevec

    column = Sparsevec(5)
    await _bind_real_oid(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, terms) VALUES ($1, $2)',
        1,
        column.to_wire(column.coerce(SparseVector(5))),
    )
    rows = await connection.fetch(f'SELECT terms FROM "{_SCHEMA}"."docs" WHERE id = 1')
    assert rows[0]["terms"] == SparseVector(5)
    assert rows[0]["terms"].dim == 5


async def test_distance_operators_work_over_sparsevec(live) -> None:
    """`<->`, `<=>` and `<#>` are defined for sparsevec, and rank as expected."""
    database, connection = live
    await _bind_real_oid(connection)
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."docs" (id, terms) VALUES '
        "(1, '{1:1}/5'), (2, '{2:1}/5'), (3, '{3:1}/5')"
    )
    for operator in ("<->", "<=>", "<#>"):
        rows = await connection.fetch(
            f'SELECT id FROM "{_SCHEMA}"."docs" '
            f"ORDER BY terms {operator} '{{1:1}}/5'::sparsevec LIMIT 1"
        )
        assert rows[0]["id"] == 1, operator


async def test_an_hnsw_index_over_sparsevec_needs_its_own_opclass(live) -> None:
    """`sparsevec_l2_ops`, not `vector_l2_ops`.

    The opclasses are per type, and naming `vector`'s on a `sparsevec` column is
    an error pgvector reports at index creation. Both halves are asserted, so the
    guide's claim about which name to use is checked rather than believed.
    """
    database, connection = live
    await connection.execute(
        f'CREATE INDEX docs_hnsw ON "{_SCHEMA}"."docs" '
        "USING hnsw (terms sparsevec_l2_ops)"
    )
    rows = await connection.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 AND indexname = $2",
        _SCHEMA,
        "docs_hnsw",
    )
    assert "hnsw" in rows[0]["indexdef"]
    assert "sparsevec_l2_ops" in rows[0]["indexdef"]

    with pytest.raises(PostgresError):
        await connection.execute(
            f'CREATE INDEX docs_wrong ON "{_SCHEMA}"."docs" '
            "USING hnsw (terms vector_l2_ops)"
        )


async def test_our_declared_bounds_are_the_bounds_the_server_enforces(live) -> None:
    """`MAX_SPARSEVEC_DIM` and `MAX_SPARSEVEC_NNZ` against pgvector's own refusals.

    Both constants were read out of pgvector's source rather than measured, which
    makes them exactly the kind of fact that is right until the version changes.
    A bound that is too *low* refuses values the server would have taken; one that
    is too *high* turns our own clear error into a driver-level surprise. So each
    is pinned from both sides: the largest accepted value and the smallest refused
    one, with the refusal coming from the server rather than from us.
    """
    database, connection = live
    await connection.execute(f'CREATE TABLE "{_SCHEMA}"."bounds" (e sparsevec)')

    # The dimension ceiling. A one-element value carries the declared dimension,
    # so this costs nothing to insert at either size.
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."bounds" (e) VALUES ($1::text::sparsevec)',
        f"{{1:1}}/{MAX_SPARSEVEC_DIM}",
    )
    with pytest.raises(PostgresError) as dim_refusal:
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}"."bounds" (e) VALUES ($1::text::sparsevec)',
            f"{{1:1}}/{MAX_SPARSEVEC_DIM + 1}",
        )
    assert "1000000000 dimensions" in str(dim_refusal.value)

    # The non-zero ceiling, which is independent of the dimension above it.
    nnz_dim = MAX_SPARSEVEC_NNZ + 1
    at_limit = ",".join(f"{i}:1" for i in range(1, MAX_SPARSEVEC_NNZ + 1))
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."bounds" (e) VALUES ($1::text::sparsevec)',
        f"{{{at_limit}}}/{nnz_dim}",
    )
    over_limit = ",".join(f"{i}:1" for i in range(1, MAX_SPARSEVEC_NNZ + 2))
    with pytest.raises(PostgresError) as nnz_refusal:
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}"."bounds" (e) VALUES ($1::text::sparsevec)',
            f"{{{over_limit}}}/{nnz_dim}",
        )
    assert "16000 non-zero elements" in str(nnz_refusal.value)

    # And our own class refuses the same two, so the error a caller sees comes
    # from Wreath before a round trip rather than from the server after one.
    with pytest.raises(ValueError):
        SparseVector(MAX_SPARSEVEC_DIM + 1, {1: 1.0})
    with pytest.raises(ValueError):
        SparseVector(nnz_dim, dict.fromkeys(range(1, MAX_SPARSEVEC_NNZ + 2), 1.0))


async def test_hnsw_indexes_a_sparsevec_to_a_thousand_non_zero_elements(live) -> None:
    """The documented HNSW limit, which is far below the column's own 16,000.

    This is the third fact the guide states from reading pgvector's source, and
    the one most likely to bite: a `sparsevec(20000)` column accepts 16,000
    non-zero elements happily until an HNSW index exists on it, after which the
    same insert fails. The limit belongs to the access method, not the type, so
    nothing about the column declaration reveals it.
    """
    database, connection = live
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."indexed" (id bigint PRIMARY KEY, e sparsevec(20000))'
    )
    await connection.execute(
        f'CREATE INDEX indexed_hnsw ON "{_SCHEMA}"."indexed" '
        "USING hnsw (e sparsevec_l2_ops)"
    )
    at_limit = ",".join(f"{i}:1" for i in range(1, 1001))
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."indexed" (id, e) VALUES (1, $1::text::sparsevec)',
        f"{{{at_limit}}}/20000",
    )
    over_limit = ",".join(f"{i}:1" for i in range(1, 1002))
    with pytest.raises(PostgresError) as refusal:
        await connection.execute(
            f'INSERT INTO "{_SCHEMA}"."indexed" (id, e) VALUES (2, $1::text::sparsevec)',
            f"{{{over_limit}}}/20000",
        )
    assert "1000 non-zero elements for hnsw" in str(refusal.value)


async def test_a_sparse_column_costs_what_it_stores_not_what_it_declares(live) -> None:
    """The claim the type is chosen for, measured rather than asserted from theory.

    Same 256 rows: one table of dense `vector(512)`, one of `sparsevec(512)` with
    four non-zero elements each. The sparse table must be appreciably smaller.

    **The size function must be `pg_table_size`, not `pg_relation_size`.** A
    `vector(512)` is 2,056 bytes, past the ~2KB threshold at which PostgreSQL
    moves a value out of line, so the dense table's *main* fork holds pointers
    and measures 16KB while its TOAST relation holds the 800KB of actual floats.
    A four-element `sparsevec` is 40 bytes and stays inline, so by
    `pg_relation_size` the sparse table looks larger than the dense one it
    shrinks by fourteen. `pg_table_size` counts the TOAST relation, and excludes
    the identical primary-key index on both sides.
    """
    database, connection = live
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."dense" (id bigint PRIMARY KEY, e vector(512))'
    )
    await connection.execute(
        f'CREATE TABLE "{_SCHEMA}"."sparse" (id bigint PRIMARY KEY, e sparsevec(512))'
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."dense" (id, e) '
        "SELECT g, (SELECT '[' || string_agg((random())::text, ',') || ']' "
        "FROM generate_series(1, 512))::vector FROM generate_series(1, 256) g"
    )
    await connection.execute(
        f'INSERT INTO "{_SCHEMA}"."sparse" (id, e) '
        "SELECT g, '{1:0.5,7:0.25,64:1,512:2}/512'::sparsevec "
        "FROM generate_series(1, 256) g"
    )
    sizes = {}
    for table in ("dense", "sparse"):
        # The identifier is interpolated rather than bound: `regclass` has no
        # binary bind encoder, and both halves of this name are ours.
        rows = await connection.fetch(
            f"SELECT pg_table_size('\"{_SCHEMA}\".{table}'::regclass) AS bytes"
        )
        sizes[table] = int(rows[0]["bytes"])
    assert sizes["sparse"] < sizes["dense"], sizes
