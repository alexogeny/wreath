"""ORM error types.

Declaration mistakes surface as :class:`DeclarationError` while a registry
compiles, never at request time. Everything else is raised by a session.
"""

from __future__ import annotations


class ORMError(Exception):
    """Base class for every ORM error."""


class DeclarationError(ORMError):
    """An invalid model, column, or relationship declaration.

    Always raised while ``app.orm()`` compiles a registry, so a running
    application never discovers a broken declaration mid-request.
    """


class RegistryError(ORMError):
    """Invalid registry configuration or lookup."""


class UnloadedAttributeError(ORMError, AttributeError):
    """A scalar column was read on an object that never loaded it.

    Attribute access never issues SQL, so an unprojected column raises rather
    than silently returning ``None`` or fetching a row.
    """


class UnloadedRelationshipError(ORMError, AttributeError):
    """A relationship was read without being loaded.

    Load it explicitly with ``.include(...)`` on the query or
    ``await session.load(instance, Model.relationship)``.
    """


class MappingError(ORMError):
    """A result set does not match the model it was asked to hydrate."""


class MultipleResultsError(ORMError):
    """``fetch_one()`` matched more than one row."""


class SchemaMismatchError(ORMError):
    """The database schema disagrees with the compiled models."""

    def __init__(self, message: str, diff: tuple[object, ...] = ()) -> None:
        super().__init__(message)
        self.diff = diff


class SessionError(ORMError):
    """Invalid session use."""


class SessionClosedError(SessionError):
    """The session was used after it was closed."""


class DetachedInstanceError(ORMError):
    """An operation required an object that a session still owns."""


__all__ = [
    "DeclarationError",
    "DetachedInstanceError",
    "MappingError",
    "MultipleResultsError",
    "ORMError",
    "RegistryError",
    "SchemaMismatchError",
    "SessionClosedError",
    "SessionError",
    "UnloadedAttributeError",
    "UnloadedRelationshipError",
]
