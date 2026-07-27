"""Frozen metadata produced by registry compilation.

These objects are the only model description the request path reads. They are
immutable: once ``Registry.compile()` returns, nothing re-derives them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .errors import DeclarationError
from .fields import Column, encode_default
from .types import PgType

if TYPE_CHECKING:
    from .model import Model

#: Bumped whenever the canonical fingerprint encoding changes, so fingerprints
#: from different Wreath versions can never collide.
FINGERPRINT_VERSION = b"wreath-orm-fingerprint-1"

_SCHEMA_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_$]*$")


@dataclass(frozen=True, slots=True)
class SchemaRef:
    """A logical or fixed schema reference compiled by a registry."""

    kind: Literal["fixed", "central", "tenant"]
    name: str | None = None


CENTRAL_SCHEMA = SchemaRef("central")
TENANT_SCHEMA = SchemaRef("tenant")


@dataclass(frozen=True, slots=True)
class SchemaMode:
    """Startup-selected mapping from logical schemas to PostgreSQL namespaces."""

    kind: Literal["single", "isolated"]
    schema: str | None = None
    central: str | None = None
    isolation: Literal["namespace", "role"] = "namespace"

    @classmethod
    def single(cls, schema: str) -> SchemaMode:
        return cls(kind="single", schema=_schema_identifier(schema))

    @classmethod
    def isolated(
        cls,
        *,
        central: str,
        isolation: Literal["namespace", "role"] = "namespace",
    ) -> SchemaMode:
        if isolation not in ("namespace", "role"):
            raise DeclarationError(
                "isolation must be 'namespace' or 'role'"
            )
        return cls(
            kind="isolated",
            central=_schema_identifier(central),
            isolation=isolation,
        )


def _schema_identifier(value: str) -> str:
    if not isinstance(value, str) or not _SCHEMA_IDENTIFIER.match(value):
        raise DeclarationError(
            f"schema name {value!r} is not a valid unquoted PostgreSQL identifier"
        )
    if len(value.encode("utf-8")) > 63:
        raise DeclarationError(f"schema name {value!r} exceeds PostgreSQL's 63-byte limit")
    return value


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A resolved foreign-key target, with its referential actions.

    ``on_delete``/``on_update`` are single PostgreSQL ``confdeltype``-style codes
    (``a`` no action, ``r`` restrict, ``c`` cascade, ``n`` set null, ``d`` set
    default) so the desired signature compares byte-for-byte with the catalog.
    """

    schema: str | SchemaRef
    table: str
    column: str
    position: int
    model_type: type[Model]
    on_delete: str = "a"
    on_update: str = "a"
    deferrable: bool = False


# The spec graph is cyclic (a relationship points at a model that points back),
# so these compare and hash by identity. A generated dataclass __eq__/__hash__
# would recurse forever, and two specs are the same spec only when they are the
# same object anyway.
@dataclass(frozen=True, slots=True, eq=False)
class ColumnSpec:
    python_name: str
    database_name: str
    position: int
    pg_type: PgType
    oid: int
    nullable: bool
    primary_key: bool
    unique: bool
    indexed: bool
    default: Any
    server_default: str | None
    reference: ColumnRef | None
    column: Column

    @property
    def index(self) -> int:
        return self.column.index

    @property
    def index_method(self) -> str | None:
        """The access method ("btree"/"gin") for this column's index, if any."""
        return self.column.index_method


@dataclass(frozen=True, slots=True, eq=False)
class RelationshipSpec:
    name: str
    target: ModelSpec
    local_columns: tuple[ColumnSpec, ...]
    remote_columns: tuple[ColumnSpec, ...]
    cardinality: Literal["one", "many"]
    default_load: Literal["raise", "selectin", "joined"]
    relationship: Any

    @property
    def index(self) -> int:
        return self.relationship.index


@dataclass(frozen=True, slots=True)
class StorageSpec:
    """How instances of one model hold their values."""

    kind: Literal["pure", "native"]
    field_count: int
    relation_count: int
    #: Fixed native struct size, or None for reference storage.
    basicsize: int | None = None


@dataclass(frozen=True, slots=True, eq=False)
class ModelSpec:
    model_type: type[Model]
    schema: str
    schema_ref: SchemaRef
    table: str
    sql_namespace: Literal["qualified", "tenant_search_path"]
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[ColumnSpec, ...]
    relationships: tuple[RelationshipSpec, ...]
    storage: StorageSpec
    by_name: dict[str, ColumnSpec]
    by_database_name: dict[str, ColumnSpec]
    #: Filled in by Registry.compile() once relationships resolve.
    by_relationship_name: dict[str, RelationshipSpec] = field(default_factory=dict)
    #: Table-level composite unique constraints and multi-column indexes,
    #: declared with ``unique(...)`` / ``index(...)`` in the model body.
    table_uniques: tuple[Any, ...] = ()
    table_indexes: tuple[Any, ...] = ()
    fingerprint: bytes = b""

    @property
    def qualified_name(self) -> str:
        if self.sql_namespace == "tenant_search_path":
            return self.table
        return f"{self.schema}.{self.table}"

    @property
    def template_name(self) -> str:
        name = self.schema_ref.name or self.schema_ref.kind
        return f"{self.schema_ref.kind}:{name}.{self.table}"

    def relationship(self, name: str) -> RelationshipSpec | None:
        return self.by_relationship_name.get(name)


def _encode_column(spec: ColumnSpec) -> bytes:
    parts = [
        b"c",
        spec.python_name.encode("utf-8"),
        spec.database_name.encode("utf-8"),
        str(spec.position).encode("ascii"),
        spec.pg_type.name.encode("ascii"),
        str(spec.oid).encode("ascii"),
        b"1" if spec.nullable else b"0",
        b"1" if spec.primary_key else b"0",
        b"1" if spec.unique else b"0",
        b"1" if spec.indexed else b"0",
        encode_default(spec.default),
        b"" if spec.server_default is None else spec.server_default.encode("utf-8"),
    ]
    if spec.reference is not None:
        parts.append(
            f"{spec.reference.schema}.{spec.reference.table}.{spec.reference.column}".encode()
        )
    else:
        parts.append(b"-")
    return b"\x1f".join(parts)


def _encode_relationship(spec: RelationshipSpec) -> bytes:
    return b"\x1f".join(
        (
            b"r",
            spec.name.encode("utf-8"),
            spec.target.qualified_name.encode("utf-8"),
            b",".join(item.database_name.encode("utf-8") for item in spec.local_columns),
            b",".join(item.database_name.encode("utf-8") for item in spec.remote_columns),
            spec.cardinality.encode("ascii"),
            spec.default_load.encode("ascii"),
        )
    )


def fingerprint_model(
    schema: str,
    table: str,
    columns: tuple[ColumnSpec, ...],
    relationships: tuple[RelationshipSpec, ...],
    table_uniques: tuple[Any, ...] = (),
    table_indexes: tuple[Any, ...] = (),
) -> bytes:
    """A stable SHA-256 fingerprint of one compiled model.

    The encoding is versioned and canonical: it never uses ``repr()`` or
    Python's randomized ``hash()``, so a fingerprint is comparable across
    processes and runs. Table-level constraints append nothing when a model
    declares none, so a model without them fingerprints exactly as before.
    """
    digest = hashlib.sha256()
    digest.update(FINGERPRINT_VERSION)
    digest.update(b"\x1e")
    digest.update(f"{schema}.{table}".encode())
    for item in columns:
        digest.update(b"\x1e")
        digest.update(_encode_column(item))
    for item in relationships:
        digest.update(b"\x1e")
        digest.update(_encode_relationship(item))
    for unique_constraint in table_uniques:
        digest.update(b"\x1e")
        digest.update(b"u\x1f")
        digest.update(",".join(unique_constraint.columns).encode("utf-8"))
    for table_index in table_indexes:
        digest.update(b"\x1e")
        digest.update(b"ui\x1f" if table_index.unique else b"i\x1f")
        digest.update(",".join(table_index.columns).encode("utf-8"))
        # A changed predicate is a different index. Without this a partial
        # index could have its WHERE edited and no fingerprint would move.
        predicate = getattr(table_index, "where_sql", None)
        if predicate is not None:
            digest.update(b"\x1f")
            digest.update(predicate.encode("utf-8"))
    return digest.digest()


def fingerprint_registry(specs: tuple[ModelSpec, ...]) -> bytes:
    """A stable deployment fingerprint over every model, in order."""
    digest = hashlib.sha256()
    digest.update(FINGERPRINT_VERSION)
    for spec in specs:
        digest.update(b"\x1e")
        digest.update(spec.qualified_name.encode("utf-8"))
        digest.update(spec.fingerprint)
    return digest.digest()


def fingerprint_registry_template(specs: tuple[ModelSpec, ...]) -> bytes:
    """Fingerprint model semantics without physical schema deployment names."""
    digest = hashlib.sha256()
    digest.update(FINGERPRINT_VERSION)
    digest.update(b"\x1etemplate")
    for spec in specs:
        digest.update(b"\x1e")
        digest.update(spec.template_name.encode("utf-8"))
        for column in spec.columns:
            digest.update(b"\x1f")
            digest.update(column.python_name.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(column.database_name.encode("utf-8"))
            digest.update(column.oid.to_bytes(4, "big"))
            digest.update(
                bytes((column.nullable, column.primary_key, column.unique, column.indexed))
            )
        for relationship in spec.relationships:
            digest.update(b"\x1f")
            digest.update(relationship.name.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(relationship.target.template_name.encode("utf-8"))
    return digest.digest()


__all__ = [
    "FINGERPRINT_VERSION",
    "ColumnRef",
    "ColumnSpec",
    "ModelSpec",
    "RelationshipSpec",
    "StorageSpec",
    "fingerprint_model",
    "fingerprint_registry",
]
