"""Vector columns and pgvector indexes through detect/generate/apply/down.

Two things are new here and neither is cosmetic.

* **A vector column spells its own type.** Every built-in type is rendered from
  its OID, and an extension type has no fixed OID to render from -- so the
  descriptor carries `vector(1536)` as text and the renderer emits that. The
  same field is what makes a *re-dimension* visible: `vector(1536)` to
  `vector(3)` keeps pgvector's OID and is still a full table rewrite.
* **An approximate index carries an operator class and method options.** Which
  distance an HNSW index can answer is decided by its opclass, not by the query,
  so `USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ...)` has to survive
  the round trip or every migration run would rediscover the same index.

Most of these render without a database: the descriptor is the ORM's intent and
the catalog snapshot is the other side. The gated tests at the end are the ones
that meet a real pgvector, because rendering agreeing with itself is not the
claim the docs make -- they claim the emitter agrees with *PostgreSQL*, and that
has exactly one failure mode and it is silent. Those tests found a real one -- a
declared *default* operator class disagreeing with a catalog that blanks it, so
an already-correct ivfflat index was rediscovered as drift on every run. See
`test_an_ivfflat_index_round_trips_too` and the offline defaults section.
"""

from __future__ import annotations

import importlib
import os
import struct
import uuid
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import detect_single, generate_single_plan
from wreath.orm import DeclarationError, Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import (
    Bit,
    Halfvec,
    Int64,
    Sparsevec,
    SparseVector,
    Text,
    Vector,
    _unbind_extension_oids,
    bind_extension_oid,
    declared_extension_types,
)
from wreath.postgres import connect

native: Any = importlib.import_module("wreath._native._postgres")

#: The OID this suite pretends the database assigned `vector`. Above 16384, so
#: it is in the range PostgreSQL hands to extensions -- which is exactly why it
#: could not have been a compile-time constant in the codec.
VECTOR_OID = 987654

EMPTY_IMAGE = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"


class Database:
    name = "main"


class Document(Model, table="documents", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(
        Vector(1536),
        index="hnsw",
        index_ops="vector_cosine_ops",
        index_with={"m": 16, "ef_construction": 64},
    )


class Shrunk(Model, table="documents", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(
        Vector(3),
        index="hnsw",
        index_ops="vector_cosine_ops",
        index_with={"m": 16, "ef_construction": 64},
    )


class Approximate(Model, table="points", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    point: Mapped[list] = column(
        Vector(3), index="ivfflat", index_ops="vector_l2_ops", index_with={"lists": 100}
    )


class Plain(Model, table="plain", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    point: Mapped[list] = column(Vector(3), index="hnsw")


# The default-operator-class arms below compare whole descriptor images, so their
# models are declared here rather than inside a test: an extension type declared
# after the process resolved `vector` never receives the OID (see `_vector_oid`),
# and a column whose OID is 0 spells itself differently in the image.


class Bare(Model, table="points", schema="app"):
    """`Approximate` with the operator class left unsaid -- the catalog's shape."""

    id: Mapped[int] = column(Int64, primary_key=True)
    point: Mapped[list] = column(Vector(3), index="ivfflat", index_with={"lists": 100})


class CosineIvfflat(Model, table="points", schema="app"):
    """A *non*-default operator class on the method that has a default."""

    id: Mapped[int] = column(Int64, primary_key=True)
    point: Mapped[list] = column(
        Vector(3), index="ivfflat", index_ops="vector_cosine_ops",
        index_with={"lists": 100},
    )


class Euclidean(Model, table="documents", schema="app"):
    """ivfflat's default opclass name, on the method it is *not* default for."""

    id: Mapped[int] = column(Int64, primary_key=True)
    embedding: Mapped[list] = column(Vector(3), index="hnsw", index_ops="vector_l2_ops")


def _vector_oid() -> int:
    """The OID this process holds for `vector`, binding a plausible one if none.

    A process resolves an extension type exactly once -- that is the invariant
    `tests/orm/test_extension_oid.py` pins -- so a suite that needs no server
    must not insist on its *own* made-up OID when a live suite has already read
    the real one out of a catalog. Whichever it is, it is consistent within the
    run, which is all these assertions need.
    """
    for item in declared_extension_types():
        if item.type_name == "vector" and item.oid:
            # Bind again rather than returning straight away.
            # `bind_extension_oid` walks the types declared *when it is called*,
            # so a `Vector` constructed since the last call is still unbound --
            # which is the whole reason the descriptor now refuses one. Binding
            # the same OID a second time is idempotent by contract.
            bind_extension_oid("vector", item.oid)
            return item.oid
    bind_extension_oid("vector", VECTOR_OID)
    return VECTOR_OID


@pytest.fixture(autouse=True)
def _resolved() -> None:
    """Bind the extension OID, as startup resolution would."""
    _vector_oid()


def _statements(tape: bytes) -> list[tuple[int, str]]:
    offset = 12
    out: list[tuple[int, str]] = []
    for _ in range(struct.unpack_from("<I", tape, 8)[0]):
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        out.append((flags, tape[offset : offset + length].decode()))
        offset += length
    return out


def _image(*models: type) -> bytes:
    registry = Registry(Database(), list(models), validate_schema="off")
    return migrations._registry_descriptor(registry)


def _sql(desired: bytes, actual: bytes = EMPTY_IMAGE) -> list[tuple[int, str]]:
    plan = native._migration_plan_descriptors(desired, actual)
    return _statements(native._migration_render_sql(plan))


def _forward(*models: type) -> list[str]:
    return [sql for _flags, sql in _sql(_image(*models))]


# -- columns ------------------------------------------------------------------


def test_a_vector_column_is_created_with_its_dimension() -> None:
    assert any(
        'add column "embedding" vector(1536) not null;' in sql
        for sql in _forward(Document)
    )


def test_nothing_about_a_vector_model_falls_back_to_manual() -> None:
    assert not any(flags & 2 for flags, _sql in _sql(_image(Document)))


def test_re_dimensioning_emits_a_rewrite_rather_than_nothing() -> None:
    statements = [sql for _flags, sql in _sql(_image(Shrunk), _image(Document))]
    assert any(
        'alter column "embedding" type vector(3);' in sql for sql in statements
    ), statements


def test_re_dimensioning_is_not_reported_as_manual() -> None:
    plan = native._migration_plan_descriptors(_image(Shrunk), _image(Document))
    assert not any(flags & 2 for flags, _sql in _statements(
        native._migration_render_sql(plan)
    ))


def test_an_unchanged_vector_column_produces_no_statement() -> None:
    assert _sql(_image(Document), _image(Document)) == []


# -- indexes ------------------------------------------------------------------


def test_hnsw_index_names_its_access_method_operator_class_and_options() -> None:
    created = [sql for sql in _forward(Document) if sql.startswith("create index")]
    assert len(created) == 1
    assert 'using hnsw ("embedding" vector_cosine_ops)' in created[0]
    assert "with (ef_construction = 64, m = 16)" in created[0]


def test_index_options_render_in_a_stable_order() -> None:
    # Sorted by option name, so a reordered dict literal is not drift. The
    # catalog echoes back the order the index was created in, which is this one.
    created = [sql for sql in _forward(Document) if sql.startswith("create index")]
    assert created[0].index("ef_construction") < created[0].index("m = 16")


def test_ivfflat_index_renders_its_own_method_and_options() -> None:
    created = [sql for sql in _forward(Approximate) if sql.startswith("create index")]
    assert 'using ivfflat ("point" vector_l2_ops)' in created[0]
    assert "with (lists = 100)" in created[0]


def test_an_index_without_an_operator_class_names_none() -> None:
    created = [sql for sql in _forward(Plain) if sql.startswith("create index")]
    assert 'using hnsw ("point")' in created[0]
    assert " with (" not in created[0]


def test_down_drops_the_vector_index_and_column() -> None:
    plan = native._migration_plan_descriptors(_image(Document), EMPTY_IMAGE)
    reversed_plan = native._migration_reverse_plan(plan)
    statements = [sql for _flags, sql in _statements(
        native._migration_render_sql(reversed_plan)
    )]
    assert any(sql.startswith("drop index ") for sql in statements)
    assert any('drop column "embedding";' in sql for sql in statements)
    assert not any(flags & 2 for flags, _sql in _statements(
        native._migration_render_sql(reversed_plan)
    ))


def test_changing_the_operator_class_is_surfaced_rather_than_ignored() -> None:
    class Cosine(Model, table="points", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        point: Mapped[list] = column(
            Vector(3), index="ivfflat", index_ops="vector_cosine_ops",
            index_with={"lists": 100},
        )

    # This `Vector(3)` was constructed inside the test body, so the autouse
    # fixture's resolution -- which ran before it existed -- never reached it.
    # Rebind before describing it: without this the column is described with
    # OID 0 and no type spelling, `Cosine` and `Approximate` differ in their
    # *column* as well as their index, and the operator-class change this test
    # is about stops being the only thing under comparison.
    _vector_oid()

    plan = native._migration_plan_descriptors(_image(Cosine), _image(Approximate))
    statements = _statements(native._migration_render_sql(plan))
    # An index whose *signature* changed but whose identity did not is an ALTER,
    # and there is no ALTER INDEX that rebuilds an operator class -- so it is
    # emitted as MANUAL rather than as a statement that would not do it.
    assert statements
    assert all(flags & 2 for flags, _sql in statements)


# -- default operator classes --------------------------------------------------
#
# A declared operator class that is this database's *default* for the access
# method has to be written as the empty string, because that is what the catalog
# read records for it -- PostgreSQL does not remember that a default was named.
# The live round trip below proves the pair agrees; these prove the rule itself,
# including that it applies to the pair (method, indexed type) rather than to a
# method alone, and that it does nothing when the answer is unknown.


def _image_with_defaults(model: type, defaults: dict) -> bytes:
    registry = Registry(Database(), [model], validate_schema="off")
    registry.default_opclasses = defaults
    return migrations._registry_descriptor(registry)


def test_a_default_operator_class_is_omitted_from_the_emitted_index() -> None:
    image = _image_with_defaults(
        Approximate, {("ivfflat", _vector_oid()): "vector_l2_ops"}
    )
    created = [sql for _flags, sql in _sql(image) if sql.startswith("create index")]
    assert 'using ivfflat ("point")' in created[0]
    # The options are unaffected -- only the operator class is defaulted away.
    assert "with (lists = 100)" in created[0]


def test_a_declared_default_matches_a_blanked_catalog_signature() -> None:
    """The defect itself, without a server: the two sides must agree."""
    defaults = {("ivfflat", _vector_oid()): "vector_l2_ops"}
    assert _image_with_defaults(Approximate, defaults) == _image(Bare)
    # And without the defaults, they differ -- which is the drift that was
    # rediscovered on every run.
    assert _image(Approximate) != _image(Bare)


def test_a_non_default_operator_class_survives_the_normalisation() -> None:
    image = _image_with_defaults(
        CosineIvfflat, {("ivfflat", _vector_oid()): "vector_l2_ops"}
    )
    created = [sql for _flags, sql in _sql(image) if sql.startswith("create index")]
    assert 'using ivfflat ("point" vector_cosine_ops)' in created[0]


def test_another_access_methods_default_does_not_apply() -> None:
    """`opcdefault` belongs to a (method, type) pair, not to an opclass name.

    `vector_l2_ops` is ivfflat's default and is *not* hnsw's -- pgvector marks
    every hnsw opclass `opcdefault = false` -- so an hnsw index that names it
    must keep naming it, or the index built would answer a different distance.
    """
    image = _image_with_defaults(
        Euclidean, {("ivfflat", _vector_oid()): "vector_l2_ops"}
    )
    created = [sql for _flags, sql in _sql(image) if sql.startswith("create index")]
    assert 'using hnsw ("embedding" vector_l2_ops)' in created[0]


def test_a_database_with_no_known_default_changes_nothing() -> None:
    assert _image_with_defaults(Approximate, {}) == _image(Approximate)


# -- declaration --------------------------------------------------------------


def test_index_ops_requires_an_index() -> None:
    with pytest.raises(DeclarationError, match="requires an index="):
        column(Vector(3), index_ops="vector_cosine_ops")


def test_index_with_requires_an_index() -> None:
    with pytest.raises(DeclarationError, match="requires an index="):
        column(Vector(3), index_with={"m": 16})


@pytest.mark.parametrize(
    "ops",
    [
        "vector cosine ops",
        "Vector_Cosine_Ops",
        "ops); drop table t --",
        "",
        # `$` matches immediately before a trailing newline, so the anchored
        # `^...$` these validators used accepted a name carrying one straight
        # into `USING hnsw (embedding <opclass>)`.
        "vector_cosine_ops\n",
    ],
)
def test_a_hostile_operator_class_is_refused_at_declaration(ops: str) -> None:
    with pytest.raises(DeclarationError, match="operator class"):
        column(Vector(3), index="hnsw", index_ops=ops)


@pytest.mark.parametrize(
    "options",
    [
        {"m": "16); drop table t --"},
        {"m 16": 16},
        {"m": object()},
        # The trailing-newline hole, in the option name and in its value.
        {"m\n": 16},
        {"m": "16\n"},
        # `WITH (m = --)`. Not an injection -- `--` comments out the rest of the
        # line and closes nothing -- but a value that opens a comment is not a
        # value, and the old character class admitted `-` and `.` anywhere.
        {"m": "--"},
        {"m": "-"},
        {"m": "."},
        {"m": "a-b"},
    ],
)
def test_a_hostile_index_option_is_refused_at_declaration(options: dict) -> None:
    with pytest.raises(DeclarationError):
        column(Vector(3), index="hnsw", index_with=options)


@pytest.mark.parametrize("value", [16, 0.5, -1, 1e-05, True, "on", "off", "auto"])
def test_a_real_index_option_value_still_declares(value: Any) -> None:
    """The narrowed value class still admits every shape pgvector and btree use.

    Paired with the refusals above on purpose: a validator tightened until it
    rejects `--` is only correct if it still accepts `m = 16`, `lists = 100`
    and `fastupdate = on`.
    """
    resolved = column(Vector(3), index="hnsw", index_with={"m": value})
    assert resolved.index_with[0][0] == "m"


def test_an_unknown_index_method_is_refused() -> None:
    with pytest.raises(DeclarationError, match="index="):
        column(Vector(3), index="brin")


# --- against a real server ---------------------------------------------------
#
# Everything above renders the descriptor against a synthetic catalog image, which
# proves the emitter agrees with itself. It cannot prove the emitter agrees with
# *PostgreSQL* -- and that is the claim the vector guide and the roadmap row both
# make: "HNSW and IVFFlat indexes round-trip with their operator class and method
# options, so a matching index is not rediscovered as drift on every run."
#
# That claim has one failure mode and it is silent. If wreath's opclass or option
# spelling differs from what `pg_get_indexdef` deparses back by a single byte,
# `detect` reports drift on an index it just created, forever, with nothing
# actually wrong -- the same defect the generated-column suite exists to catch.
# So it is asserted here against a real pgvector rather than assumed.

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


async def _round_trip_index(
    model_of, schema: str, type_names: tuple[str, ...] = ("vector",)
) -> None:
    """Apply a model's DDL, then assert a second `generate` has nothing to say.

    `type_names` are the extension types this model's columns need resolved. It is
    not always `("vector",)`: `halfvec` and `sparsevec` are separate entries in
    `pg_type` that one `CREATE EXTENSION vector` installs, and `bit` is
    PostgreSQL's own type needing none of this, so a `Bit` model passes `()`.
    """
    db = await connect(_DSN)
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # noqa: BLE001 -- any failure here means "no pgvector"
            pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
        await db.execute(f'CREATE SCHEMA "{schema}"')
        # The real OIDs this server assigned, not the invented one the offline
        # tests bind -- the whole point here is to meet the actual catalog. One
        # process resolves an extension type once, so the fake has to be released
        # first; the autouse fixture rebinds it for whatever runs next.
        resolved = {}
        for type_name in type_names:
            rows = await db.fetch(
                "SELECT oid FROM pg_type WHERE typname = $1", type_name
            )
            if not rows:
                pytest.skip(f"this pgvector has no {type_name} type; needs >= 0.7")
            resolved[type_name] = int(rows[0][0])
        _unbind_extension_oids()
        for type_name, oid in resolved.items():
            bind_extension_oid(type_name, oid)
        registry = Registry(Database(), [model_of], validate_schema="off")

        generation = await generate_single_plan(registry, db)
        emitted = _statements(generation.sql.tape)
        assert emitted
        # Nothing may land as MANUAL: an index wreath cannot emit is one an
        # operator has to write by hand, which is not what the docs promise.
        assert not any(flags & 2 for flags, _sql in emitted), emitted
        for _flags, statement in emitted:
            await db.execute(statement)

        # The round trip itself.
        assert (await detect_single(registry, db)).current
        assert _statements((await generate_single_plan(registry, db)).sql.tape) == []
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the pgvector index catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_an_hnsw_index_with_opclass_and_options_round_trips() -> None:
    schema = f"wreath_vector_{uuid.uuid4().hex[:12]}"

    class Document(Model, table="documents", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        body: Mapped[str] = column(Text)
        embedding: Mapped[list[float]] = column(
            Vector(8),
            index="hnsw",
            index_ops="vector_cosine_ops",
            index_with={"m": 16, "ef_construction": 64},
        )

    await _round_trip_index(Document, schema)


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the pgvector index catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_an_ivfflat_index_round_trips_too() -> None:
    """A different access method, a different opclass, one option rather than two.

    This arm is the one that found the default-operator-class defect.
    `vector_l2_ops` is ivfflat's default -- the only default pgvector defines,
    which is why hnsw never showed it -- and PostgreSQL does not record that a
    default was named. The catalog read blanks it; the desired descriptor used to
    keep it, so `detect` reported drift and `generate` emitted a MANUAL forever
    for an index that was already exactly right. The desired side now learns this
    database's defaults (`resolve_default_opclasses`) and blanks a declared one to
    match, so both sides say the same thing.
    """
    schema = f"wreath_vector_{uuid.uuid4().hex[:12]}"

    class Point(Model, table="points", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        embedding: Mapped[list[float]] = column(
            Vector(4), index="ivfflat", index_ops="vector_l2_ops", index_with={"lists": 4}
        )

    await _round_trip_index(Point, schema)


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the pgvector index catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_this_server_reports_ivfflats_default_and_not_hnsws() -> None:
    """The asymmetry the whole fix turns on, read out of a real pg_opclass.

    The offline tests above hand the descriptor a defaults map; this is where the
    map comes from. If pgvector ever marked an hnsw opclass default, or stopped
    marking ivfflat's, the normalisation would be wrong in exactly the direction
    nothing else here would notice.
    """
    from wreath.orm.introspection import probe_default_opclasses

    db = await connect(_DSN)
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # noqa: BLE001 -- any failure here means "no pgvector"
            pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
        rows = await db.fetch("SELECT oid FROM pg_type WHERE typname = 'vector'")
        vector_oid = int(rows[0][0])
        found = await probe_default_opclasses(db, ("hnsw", "ivfflat"))
        assert found[("ivfflat", vector_oid)] == "vector_l2_ops"
        assert ("hnsw", vector_oid) not in found
        # An access method this server has never heard of is not an error; it
        # simply contributes nothing, and every declared opclass stays explicit.
        assert await probe_default_opclasses(db, ("no_such_access_method",)) == {}
    finally:
        await db.close()


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the pgvector index catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_vector_index_without_options_round_trips() -> None:
    """No `WITH (...)` at all: the empty case is where an option renderer misprints."""
    schema = f"wreath_vector_{uuid.uuid4().hex[:12]}"

    class Plain(Model, table="plain", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        embedding: Mapped[list[float]] = column(
            Vector(3), index="hnsw", index_ops="vector_l2_ops"
        )

    await _round_trip_index(Plain, schema)


# The three types added after `Vector` reach the catalog through the same generic
# extension-typed-column path, which is why nothing above needed changing when they
# landed. "Expected to work" is not "shown to work", though, and each has its own
# spelling for the renderer to get wrong: `halfvec(n)` and `sparsevec(n)` carry a
# dimension the way `vector(n)` does but resolve to different OIDs, and `bit(n)`
# carries a *length* through no extension machinery at all. Each also has its own
# operator classes, which is the half that silently drifts.


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the pgvector index catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_halfvec_index_round_trips_with_its_own_opclass() -> None:
    """`halfvec_cosine_ops` on a `halfvec(n)` column, options and all."""
    schema = f"wreath_halfvec_{uuid.uuid4().hex[:12]}"

    class Document(Model, table="documents", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        embedding: Mapped[list[float]] = column(
            Halfvec(8),
            index="hnsw",
            index_ops="halfvec_cosine_ops",
            index_with={"m": 16, "ef_construction": 64},
        )

    await _round_trip_index(Document, schema, type_names=("halfvec",))


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the pgvector index catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_sparsevec_index_round_trips_with_its_own_opclass() -> None:
    """The round trip the handoff recorded as expected-but-undemonstrated.

    `sparsevec(dim)` renders like `vector(dim)` and the extension-typed-column
    path is generic, so this was predicted to pass. It does -- but the prediction
    covered the column and said nothing about `sparsevec_l2_ops`, which is a
    per-type opclass name and so a fresh chance for the emitted spelling and the
    deparsed one to differ by a byte.
    """
    schema = f"wreath_sparsevec_{uuid.uuid4().hex[:12]}"

    class Document(Model, table="documents", schema=schema):
        terms: Mapped[SparseVector] = column(
            Sparsevec(8), index="hnsw", index_ops="sparsevec_l2_ops"
        )
        id: Mapped[int] = column(Int64, primary_key=True)

    await _round_trip_index(Document, schema, type_names=("sparsevec",))


@pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the pgvector index catalog round trip",
)
@pytest.mark.asyncio
@pytest.mark.database
async def test_a_bit_index_round_trips_through_no_extension_machinery() -> None:
    """`bit(n)` with `bit_hamming_ops` -- a built-in type under an extension opclass.

    This is the one combination where the *type* needs no resolution and the
    *index* still needs pgvector, so it exercises a path none of the arms above
    reach: `type_names=()`, and an opclass whose type is not an `ExtensionType`.
    """
    schema = f"wreath_bit_{uuid.uuid4().hex[:12]}"

    class Signature(Model, table="signatures", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        signature: Mapped[str] = column(Bit(8), index="hnsw", index_ops="bit_hamming_ops")

    await _round_trip_index(Signature, schema, type_names=())
