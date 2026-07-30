"""The dynamic extension-OID mechanism, and the two invariants it must keep.

pgvector's `vector` OID is assigned by `CREATE EXTENSION`, so it differs between
databases. Two things must *not* move with it:

* **the plan-cache shape token.** An OID in there gives one query two cache
  entries against two databases, and changes when the extension is reinstalled.
  The failure mode is a silently duplicated plan cache -- nothing breaks, and
  nobody finds out until they profile it. So the token is name-derived, and this
  suite pins it.
* **the model fingerprint.** Same argument one layer up: a fingerprint that
  moved with the database would report every model as drifted.

The third invariant is a failure rather than a stability: a `Vector` column on a
database without the extension must fail at *startup*, naming the extension,
rather than at the first query with an unrecognised OID.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from wreath.orm import Mapped, Model, column
from wreath.orm.errors import ExtensionNotInstalledError
from wreath.orm.introspection import probe_extension_types, resolve_extension_types
from wreath.orm.registry import Registry
from wreath.orm.schema import fingerprint_model
from wreath.orm.types import (
    EXT_KIND_VECTOR,
    ExtensionType,
    Int64,
    Text,
    Vector,
    bind_extension_oid,
    declared_extension_types,
)
from wreath.postgres import connect

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

#: The OID this process pretends the database assigned `vector`. It has to match
#: `tests/orm/test_vector_codec.py` and `tests/migrations/test_vector.py`,
#: because a process resolves an extension type exactly once -- which is itself
#: one of the invariants under test here.
VECTOR_OID = 987654


class Database:
    name = "main"


class Document(Model, table="documents", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    body: Mapped[str] = column(Text)
    embedding: Mapped[list] = column(Vector(4))


def _vector_oid() -> int:
    """The OID this process holds for `vector`, binding every declaration.

    A process resolves an extension type exactly once -- that is the invariant
    `tests/orm/test_extension_oid.py` pins -- so a suite that needs no server
    must not insist on its *own* made-up OID when a live suite has already read
    the real one out of a catalog. Whichever it is, it is consistent within the
    run, which is all these assertions need. Re-running the idempotent binder
    also reaches declarations constructed since the first resolution.
    """
    oid = VECTOR_OID
    for item in declared_extension_types():
        if item.type_name == "vector" and item.oid:
            oid = item.oid
            break
    bind_extension_oid("vector", oid)
    return oid


def _fingerprint(spec: Any) -> bytes:
    return fingerprint_model(
        spec.schema, spec.table, spec.columns, spec.relationships,
        spec.table_uniques, spec.table_indexes,
    )


# -- the shape token ----------------------------------------------------------


def test_the_shape_token_is_name_derived_not_oid_derived() -> None:
    vector = Vector(1536)
    assert vector.shape_value == b"xvector(1536)"
    assert b"0" not in vector.shape_value[:8]  # no OID digits in the token


def test_the_shape_token_does_not_change_when_the_oid_does() -> None:
    vector = Vector(7)
    before = vector.shape_value
    assert vector.oid == 0
    assert vector.oid != _vector_oid()
    assert vector.shape_value == before


def test_the_token_distinguishes_dimensions_and_would_distinguish_types() -> None:
    assert Vector(3).shape_value != Vector(4).shape_value
    other = ExtensionType(
        "vector", "halfvec", "halfvec(3)", lambda value: value, kind=EXT_KIND_VECTOR
    )
    assert other.shape_value != Vector(3).shape_value


def test_the_model_fingerprint_does_not_move_with_the_oid() -> None:
    # Resolve the OID *first*. `_vector_oid` binds one when the process holds
    # none, so reading `spec.oid` before calling it asserts against 0 whenever
    # this test runs before the ones that bind -- which is every xdist worker
    # that did not happen to draw them, and `wreath-check` runs with `-n 6`.
    oid = _vector_oid()
    registry = Registry(Database(), [Document], validate_schema="off")
    spec = registry.spec_for(Document)
    before = _fingerprint(spec)
    assert spec.by_name["embedding"].oid == oid  # non-zero: the OID really moved
    assert _fingerprint(spec) == before


def test_two_dimensions_fingerprint_differently() -> None:
    class Wide(Model, table="wide", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        embedding: Mapped[list] = column(Vector(8))

    class Narrow(Model, table="wide", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        embedding: Mapped[list] = column(Vector(4))

    wide = Registry(Database(), [Wide], validate_schema="off").spec_for(Wide)
    narrow = Registry(Database(), [Narrow], validate_schema="off").spec_for(Narrow)
    assert _fingerprint(wide) != _fingerprint(narrow)


# -- binding ------------------------------------------------------------------


def test_the_column_spec_reads_the_live_oid() -> None:
    """`ColumnSpec.oid` reads through the type, it does not snapshot it.

    A snapshot taken while the registry compiled would be 0 forever, because
    resolution happens later -- at startup, against the live catalog.
    """
    oid = _vector_oid()   # bound first; see the note in the fingerprint test
    registry = Registry(Database(), [Document], validate_schema="off")
    spec = registry.spec_for(Document)
    assert spec.by_name["embedding"].oid == oid


def test_rebinding_to_a_different_oid_is_refused(monkeypatch: Any) -> None:
    """One process holds one codec table, so one type holds one OID.

    Two databases whose `vector` OIDs disagree cannot both be served from one
    interpreter, and the honest failure is here rather than in a decoder reading
    one database's rows with the other's rules.
    """
    from wreath.orm import types as orm_types

    monkeypatch.setitem(orm_types._EXTENSION_KINDS, "twinkind", EXT_KIND_VECTOR)
    declared = ExtensionType(
        "twin", "twinkind", "twinkind(2)", lambda value: value, kind=EXT_KIND_VECTOR
    )
    try:
        bind_extension_oid("twinkind", 700001)
        assert declared.oid == 700001
        with pytest.raises(ValueError, match="already bound"):
            bind_extension_oid("twinkind", 700002)
    finally:
        orm_types._DECLARED_EXTENSION_TYPES.remove(declared)


def test_binding_a_nonsense_oid_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid OID"):
        bind_extension_oid("vector", 0)


# -- a type declared after resolution -----------------------------------------
#
# `bind_extension_oid` walks the types declared *at the time it is called*, so
# an `ExtensionType` constructed afterwards -- a model class defined after the
# application started -- keeps OID 0 until something resolves again. That is not
# reachable in the normal startup order, and it is the wrong failure mode for a
# value that decides wire framing and migration descriptors: 0 is a legal-looking
# OID meaning "unspecified" on the wire and "built-in" in a descriptor.
#
# Unbound stays legitimate at declaration, at fingerprint time, and while the
# probe that is about to resolve it runs; the refusal lives at the points that
# *consume* the OID.


def _late_vector() -> Any:
    """A vector type constructed after `vector` was already resolved."""
    _vector_oid()          # resolution has happened for this process
    return Vector(4)       # ... and this instance was not there for it


def test_declaring_after_resolution_is_still_allowed() -> None:
    """The refusal must not fire where unbound is the legitimate state."""
    late = _late_vector()
    try:
        assert late.oid == 0
        assert late.shape_value == b"xvector(4)"
        assert late.fingerprint_oid != 0

        class Late(Model, table="late_declared", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            embedding: Mapped[list] = column(late)

        registry = Registry(Database(), [Late], validate_schema="off")
        spec = registry.spec_for(Late)
        assert spec.by_name["embedding"].oid == 0
        assert _fingerprint(spec)
    finally:
        from wreath.orm import types as orm_types

        orm_types._DECLARED_EXTENSION_TYPES.remove(late)


def test_binding_a_value_of_an_unbound_type_names_the_call() -> None:
    late = _late_vector()
    try:
        with pytest.raises(ExtensionNotInstalledError) as caught:
            late.to_wire([1.0, 2.0, 3.0, 4.0])
    finally:
        from wreath.orm import types as orm_types

        orm_types._DECLARED_EXTENSION_TYPES.remove(late)
    message = str(caught.value)
    assert "vector" in message
    assert "resolve_extension_types" in message
    assert caught.value.extension == "vector"


def test_a_migration_descriptor_refuses_an_unbound_type() -> None:
    """The silent one. Without this the column is described as OID 0 with an
    empty type spelling -- indistinguishable from a built-in, rediscovered as
    drift on every run, with nothing saying why."""
    import wreath.migrations as migrations

    late = _late_vector()
    try:

        class LateIndexed(Model, table="late_descriptor", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            embedding: Mapped[list] = column(late)

        registry = Registry(Database(), [LateIndexed], validate_schema="off")
        with pytest.raises(ExtensionNotInstalledError) as caught:
            migrations._registry_descriptor(registry)
    finally:
        from wreath.orm import types as orm_types

        orm_types._DECLARED_EXTENSION_TYPES.remove(late)
    message = str(caught.value)
    assert "LateIndexed.embedding" in message
    assert "resolve_extension_types" in message


def test_every_declared_extension_type_is_discoverable() -> None:
    declared = declared_extension_types()
    assert any(item.sql == "vector(4)" for item in declared)
    assert all(isinstance(item, ExtensionType) for item in declared)


# -- startup ------------------------------------------------------------------


class _NoExtensionConnection:
    """A connection whose database has no extensions at all."""

    async def fetchrow(self, sql: str, *args: Any) -> tuple[Any, ...]:
        return (0, "", "public")


class _FakeDatabase:
    name = "vectorless"

    def pool(self, workload: str) -> Any:
        raise KeyError(workload)

    async def acquire(self, workload: str) -> Any:
        return _NoExtensionConnection()

    async def release(self, workload: str, connection: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_an_absent_extension_fails_at_startup_naming_it() -> None:
    registry = Registry(_FakeDatabase(), [Document], validate_schema="off")
    with pytest.raises(ExtensionNotInstalledError) as caught:
        await resolve_extension_types(registry)
    message = str(caught.value)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in message
    assert "Document.embedding" in message
    assert caught.value.extension == "vector"


@pytest.mark.asyncio
async def test_a_registry_without_extension_types_does_no_io() -> None:
    class Plain(Model, table="plain", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Refusing:
        name = "unused"

        async def acquire(self, workload: str) -> Any:
            raise AssertionError("a registry with no extension types must not connect")

    registry = Registry(Refusing(), [Plain], validate_schema="off")
    assert await resolve_extension_types(registry) == ()


# -- against a real database --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_the_real_oid_is_read_from_the_catalog() -> None:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for extension resolution tests")
    db = await connect(_DSN)
    try:
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # noqa: BLE001 - reported as a skip, see below
            # A server without pgvector cannot answer this, and the suite must
            # say so rather than pass by accident. `AGENTS.md` names this as the
            # container image to use: pgvector/pgvector:pg17.
            pytest.skip("this PostgreSQL has no pgvector; use pgvector/pgvector:pg17")
        found = await probe_extension_types(db, {"vector": "vector"})
        assert len(found) == 1
        assert found[0].installed
        assert found[0].oid > 16384, "an extension OID is user-assigned, not built in"
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_missing_type_reports_not_installed_rather_than_raising() -> None:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for extension resolution tests")
    db = await connect(_DSN)
    try:
        found = await probe_extension_types(
            db, {f"not_a_type_{uuid.uuid4().hex[:8]}": "nothing"}
        )
        assert not found[0].installed
        assert found[0].current_schema
    finally:
        await db.close()
