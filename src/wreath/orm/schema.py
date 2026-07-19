"""Frozen metadata produced by registry compilation.

These objects are the only model description the request path reads. They are
immutable: once ``Registry.compile()` returns, nothing re-derives them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .fields import Column, encode_default
from .types import PgType

if TYPE_CHECKING:
    from .model import Model

#: Bumped whenever the canonical fingerprint encoding changes, so fingerprints
#: from different Wreath versions can never collide.
FINGERPRINT_VERSION = b"wreath-orm-fingerprint-1"


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A resolved foreign-key target."""

    schema: str
    table: str
    column: str
    position: int
    model_type: type[Model]


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
    default: Any
    server_default: str | None
    reference: ColumnRef | None
    column: Column

    @property
    def index(self) -> int:
        return self.column.index


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
    table: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[ColumnSpec, ...]
    relationships: tuple[RelationshipSpec, ...]
    storage: StorageSpec
    by_name: dict[str, ColumnSpec]
    by_database_name: dict[str, ColumnSpec]
    #: Filled in by Registry.compile() once relationships resolve.
    by_relationship_name: dict[str, RelationshipSpec] = field(default_factory=dict)
    fingerprint: bytes = b""

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}"

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
) -> bytes:
    """A stable SHA-256 fingerprint of one compiled model.

    The encoding is versioned and canonical: it never uses ``repr()`` or
    Python's randomized ``hash()``, so a fingerprint is comparable across
    processes and runs.
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
    return digest.digest()


def fingerprint_registry(specs: tuple[ModelSpec, ...]) -> bytes:
    """A stable fingerprint over every model a registry owns, in order."""
    digest = hashlib.sha256()
    digest.update(FINGERPRINT_VERSION)
    for spec in specs:
        digest.update(b"\x1e")
        digest.update(spec.qualified_name.encode("utf-8"))
        digest.update(spec.fingerprint)
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
