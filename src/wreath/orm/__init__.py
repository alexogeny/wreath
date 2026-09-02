"""A dependency-free PostgreSQL ORM for Wreath.

Models are declared explicitly and compiled once, into immutable metadata owned
by an application registry:

```python
from wreath.orm import Mapped, Model, column, relationship
from wreath.orm.types import Int64, Text

class User(Model, table="users"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    posts = relationship("Post", foreign_key="author_id", load="selectin")

registry = app.orm(database="main", models=[User, Post])
```
Two rules shape everything here:

* **Attribute access never performs I/O.** Reading a column that was not
  selected, or a relationship that was not loaded, raises. Loading is always a
  visible `await`.
* **Raw SQL stays first class.** `Session.raw()` and the `wreath.postgres`
  driver are unchanged and undeprecated; the ORM never rewrites the SQL you
  write yourself.

The ORM does not manage schema. It validates that the database matches the
models at startup and never creates, alters, or drops anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
    from .dto import model_dataclass
    from .errors import (
        DeclarationError,
        DetachedInstanceError,
        ExtensionNotInstalledError,
        MappingError,
        MultipleResultsError,
        NoResultError,
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
    from .query import Select, where_fields
    from .registry import Registry
    from .relations import relationship
    from .schema import CENTRAL_SCHEMA, TENANT_SCHEMA, SchemaMode, SchemaRef
    from .session import FromORM, RawQuery, Session, TenantContext
    from .table import (
        AllOf,
        Eq,
        Facet,
        Index,
        InValues,
        IsNull,
        Unique,
        all_of,
        eq,
        facet,
        index,
        is_not_null,
        is_null,
        one_of,
        unique,
    )

__all__ = [
    "CENTRAL_SCHEMA",
    "MISSING",
    "TENANT_SCHEMA",
    "Check",
    "CheckViolation",
    "DeclarationError",
    "DetachedInstanceError",
    "ExtensionNotInstalledError",
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
    "NoResultError",
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
    "AllOf",
    "Eq",
    "InValues",
    "IsNull",
    "all_of",
    "eq",
    "Facet",
    "facet",
    "index",
    "model_dataclass",
    "narrow",
    "not_",
    "or_",
    "rule",
    "relationship",
    "is_not_null",
    "is_null",
    "one_of",
    "unique",
    "where_fields",
]

_EXPORTS = {
    "CENTRAL_SCHEMA": "schema",
    "MISSING": "fields",
    "TENANT_SCHEMA": "schema",
    "Check": "constraints",
    "CheckViolation": "constraints",
    "DeclarationError": "errors",
    "DetachedInstanceError": "errors",
    "ExtensionNotInstalledError": "errors",
    "FromORM": "session",
    "Ge": "constraints",
    "Gt": "constraints",
    "Index": "table",
    "Le": "constraints",
    "Length": "constraints",
    "Lt": "constraints",
    "Mapped": "fields",
    "MappingError": "errors",
    "Model": "model",
    "MultipleResultsError": "errors",
    "NoResultError": "errors",
    "ORMError": "errors",
    "OneOf": "constraints",
    "Pattern": "constraints",
    "Predicate": "constraints",
    "RawQuery": "session",
    "Registry": "registry",
    "RegistryError": "errors",
    "SchemaMismatchError": "errors",
    "SchemaMode": "schema",
    "SchemaRef": "schema",
    "Select": "query",
    "Session": "session",
    "SessionClosedError": "errors",
    "SessionError": "errors",
    "TenantContext": "session",
    "Unique": "table",
    "UnloadedAttributeError": "errors",
    "UnloadedRelationshipError": "errors",
    "and_": "expressions",
    "column": "fields",
    "AllOf": "table",
    "Eq": "table",
    "InValues": "table",
    "IsNull": "table",
    "all_of": "table",
    "eq": "table",
    "Facet": "table",
    "facet": "table",
    "index": "table",
    "model_dataclass": "dto",
    "narrow": "constraints",
    "not_": "expressions",
    "or_": "expressions",
    "rule": "constraints",
    "relationship": "relations",
    "is_not_null": "table",
    "is_null": "table",
    "one_of": "table",
    "unique": "table",
    "where_fields": "query",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
