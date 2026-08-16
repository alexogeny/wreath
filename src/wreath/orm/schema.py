"""Frozen metadata produced by registry compilation.

These objects are the only model description the request path reads. They are
immutable: once `Registry.compile()` returns, nothing re-derives them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .._pgname import validate_unquoted_identifier
from .errors import DeclarationError
from .fields import Column, encode_default
from .types import PgType

if TYPE_CHECKING:
    from .model import Model

#: Bumped whenever the canonical fingerprint encoding changes, so fingerprints
#: from different Wreath versions can never collide.
FINGERPRINT_VERSION = b"wreath-orm-fingerprint-1"

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
    return validate_unquoted_identifier(
        value, "schema name", error=DeclarationError
    )


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A resolved foreign-key target, with its referential actions.

    `on_delete`/`on_update` are single PostgreSQL `confdeltype`-style codes
    (`a` no action, `r` restrict, `c` cascade, `n` set null, `d` set
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
    nullable: bool
    primary_key: bool
    unique: bool
    indexed: bool
    default: Any
    server_default: str | None
    reference: ColumnRef | None
    column: Column
    #: The `GENERATED ALWAYS AS (...)` expression, in PostgreSQL's own normal
    #: form, or None for an ordinary column. Filled in by `Registry.compile()`,
    #: because the expression names other columns and their database names and
    #: types are not known until then.
    generated_sql: str | None = None

    @property
    def index(self) -> int:
        return self.column.index

    @property
    def oid(self) -> int:
        """This column's PostgreSQL type OID.

        Read through the type rather than copied at compile time, because an
        extension type's OID is not known until startup resolves it against the
        live catalog -- a snapshot taken here would be 0 forever. Every built-in
        type answers the same constant it always did.
        """
        return self.pg_type.oid

    @property
    def index_method(self) -> str | None:
        """The access method for this column's index, if any."""
        return self.column.index_method

    @property
    def index_ops(self) -> str | None:
        """The operator class this column's index uses, if it names one."""
        return self.column.index_ops

    @property
    def index_with(self) -> tuple[tuple[str, str], ...]:
        """This column's index-method options, as ordered `(name, value)` pairs."""
        return self.column.index_with


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
    """The fixed layout dimensions for one model."""

    field_count: int
    relation_count: int
    basicsize: int


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
    #: declared with `unique(...)` / `index(...)` in the model body.
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
        # `fingerprint_oid`, not `oid`: an extension type's real OID is assigned
        # by CREATE EXTENSION and differs between databases, so recording it
        # here would make the same models fingerprint differently against two
        # databases and report drift that is not there. Identical to `oid` for
        # every built-in type, so no existing fingerprint moves.
        str(spec.pg_type.fingerprint_oid).encode("ascii"),
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
    # Appended only when there is one, so an ordinary column fingerprints
    # exactly as it always did. It has to be here at all because a `tsvector`
    # column's type name and OID are the same whatever it analyses: without the
    # expression, changing a TsVector's config or its sources would move no
    # fingerprint and the change would be invisible to drift detection.
    if spec.generated_sql is not None:
        parts.append(spec.generated_sql.encode("utf-8"))
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

    The encoding is versioned and canonical: it never uses `repr()` or
    Python's randomized `hash()`, so a fingerprint is comparable across
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
            # See `_encode_column`: the database-assigned OID would move this
            # digest between databases, the name-derived one will not.
            digest.update(column.pg_type.fingerprint_oid.to_bytes(4, "big"))
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
    "CENTRAL_SCHEMA",
    "FINGERPRINT_VERSION",
    "TENANT_SCHEMA",
    "ColumnRef",
    "ColumnSpec",
    "ModelSpec",
    "RelationshipSpec",
    "SchemaMode",
    "SchemaRef",
    "StorageSpec",
    "fingerprint_model",
    "fingerprint_registry",
]
