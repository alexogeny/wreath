"""A dependency-free PostgreSQL ORM for Wreath.

Models are declared explicitly and compiled once, into immutable metadata owned
by an application registry::

    from wreath.orm import Mapped, Model, column, relationship
    from wreath.orm.types import Int64, Text

    class User(Model, table="users"):
        id: Mapped[int] = column(Int64, primary_key=True)
        email: Mapped[str] = column(Text, unique=True)
        posts = relationship("Post", foreign_key="author_id", load="selectin")

    registry = app.orm(database="main", models=[User, Post])

Two rules shape everything here:

* **Attribute access never performs I/O.** Reading a column that was not
  selected, or a relationship that was not loaded, raises. Loading is always a
  visible ``await``.
* **Raw SQL stays first class.** ``Session.raw()`` and the ``wreath.postgres``
  driver are unchanged and undeprecated; the ORM never rewrites the SQL you
  write yourself.

The ORM does not manage schema. It validates that the database matches the
models at startup and never creates, alters, or drops anything.
"""

from __future__ import annotations

from .constraints import (
    Check,
    CheckViolation,
    Ge,
    Gt,
    Le,
    Length,
    Lt,
    OneOf,
    Pattern,
    Predicate,
    narrow,
    rule,
)
from .errors import (
    DeclarationError,
    DetachedInstanceError,
    MappingError,
    MultipleResultsError,
    ORMError,
    RegistryError,
    SchemaMismatchError,
    SessionClosedError,
    SessionError,
    UnloadedAttributeError,
    UnloadedRelationshipError,
)
from .expressions import and_, not_, or_
from .fields import MISSING, Mapped, column
from .model import Model
from .query import Select
from .registry import Registry
from .relations import relationship
from .schema import CENTRAL_SCHEMA, TENANT_SCHEMA, SchemaMode, SchemaRef
from .session import FromORM, RawQuery, Session, TenantContext
from .table import Index, Unique, index, unique

__all__ = [
    "CENTRAL_SCHEMA",
    "MISSING",
    "TENANT_SCHEMA",
    "Check",
    "CheckViolation",
    "DeclarationError",
    "DetachedInstanceError",
    "FromORM",
    "Ge",
    "Gt",
    "Index",
    "Le",
    "Length",
    "Lt",
    "Mapped",
    "MappingError",
    "Model",
    "MultipleResultsError",
    "ORMError",
    "OneOf",
    "Pattern",
    "Predicate",
    "RawQuery",
    "Registry",
    "RegistryError",
    "SchemaMismatchError",
    "SchemaMode",
    "SchemaRef",
    "Select",
    "Session",
    "SessionClosedError",
    "SessionError",
    "TenantContext",
    "Unique",
    "UnloadedAttributeError",
    "UnloadedRelationshipError",
    "and_",
    "column",
    "index",
    "narrow",
    "not_",
    "or_",
    "rule",
    "relationship",
    "unique",
]
