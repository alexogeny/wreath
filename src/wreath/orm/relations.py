"""Relationship declarations and explicit load options."""

from __future__ import annotations

from typing import Any, Literal

from .errors import DeclarationError

LoadStrategy = Literal["raise", "selectin", "joined"]
LOAD_STRATEGIES = frozenset({"raise", "selectin", "joined"})

_ORDER = 0


class LoadOption:
    """An explicit request to load one relationship with one strategy."""

    __slots__ = ("nested", "relationship", "strategy")

    def __init__(
        self, relationship: Relationship, strategy: str, nested: tuple[LoadOption, ...]
    ) -> None:
        self.relationship = relationship
        self.strategy = strategy
        self.nested = nested

    def __repr__(self) -> str:
        owner = getattr(self.relationship.owner, "__name__", "?")
        return f"<LoadOption {owner}.{self.relationship.python_name} {self.strategy}>"


class RelationshipExpr:
    """Class-level access to a relationship; builds load options and predicates."""

    __slots__ = ("path", "relationship")

    def __init__(self, relationship: Relationship, path: tuple[Relationship, ...] = ()) -> None:
        self.relationship = relationship
        self.path = path or (relationship,)

    def selectin(self, *nested: LoadOption) -> LoadOption:
        """Load this relationship with a second batched statement."""
        return LoadOption(self.relationship, "selectin", _check_nested(nested))

    def joined(self, *nested: LoadOption) -> LoadOption:
        """Load this relationship in the same statement with a LEFT JOIN."""
        return LoadOption(self.relationship, "joined", _check_nested(nested))

    def __getattr__(self, name: str) -> Any:
        """Reach a column or a further relationship on the target model.

        ``Book.author.name`` is a predicate operand, not a load: see
        :class:`~wreath.orm.expressions.RelatedColumnExpr`.
        """
        # Dunder and private lookups must miss normally, or copy/pickle
        # protocols see a relationship where they expect absence.
        if name.startswith("_"):
            raise AttributeError(name)
        from .expressions import ColumnExpr, RelatedColumnExpr

        target = self.relationship.resolved_target
        if target is None:
            raise DeclarationError(
                f"{self._label()} cannot be traversed yet: it targets "
                f"{self.relationship.target!r} by name, which is resolved when the "
                "models are registered. Build this query after app.orm(...)/Registry(...)."
            )
        try:
            attribute = getattr(target, name)
        except AttributeError:
            raise AttributeError(
                f"{target.__name__} has no column or relationship {name!r}, so "
                f"{self._label()}.{name} is not a valid predicate"
            ) from None
        if isinstance(attribute, RelatedColumnExpr):  # pragma: no cover - defensive
            raise DeclarationError(f"{self._label()}.{name} is already a joined column")
        if isinstance(attribute, ColumnExpr):
            return RelatedColumnExpr(attribute.column, self.path)
        if isinstance(attribute, RelationshipExpr):
            return RelationshipExpr(attribute.relationship, (*self.path, attribute.relationship))
        raise TypeError(
            f"{self._label()}.{name} is not a column or relationship of "
            f"{target.__name__}"
        )

    def _label(self) -> str:
        owner = getattr(self.relationship.owner, "__name__", "?")
        return f"{owner}.{'.'.join(item.python_name for item in self.path)}"

    def __repr__(self) -> str:
        owner = getattr(self.relationship.owner, "__name__", "?")
        return f"<RelationshipExpr {owner}.{self.relationship.python_name}>"


def _check_nested(nested: tuple[Any, ...]) -> tuple[LoadOption, ...]:
    for item in nested:
        if not isinstance(item, LoadOption):
            raise TypeError(
                f"nested load options must come from .selectin()/.joined(), got {item!r}"
            )
    return nested


class Relationship:
    """A declared relationship, and the descriptor that reads it."""

    __slots__ = (
        "_expression",
        "back_populates",
        "foreign_key",
        "index",
        "load",
        "order",
        "owner",
        "prototype",
        "python_name",
        "resolved_target",
        "shape_ref",
        "target",
    )

    def __init__(
        self,
        target: Any,
        *,
        foreign_key: Any,
        back_populates: str | None,
        load: LoadStrategy,
    ) -> None:
        global _ORDER
        _ORDER += 1
        self.target = target
        # `target` may name a model that is not defined yet; the registry writes
        # the class back here once it resolves the graph, which is what lets
        # `Book.author.name` be traversed for a predicate.
        self.resolved_target: Any = target if isinstance(target, type) else None
        self.foreign_key = foreign_key
        self.back_populates = back_populates
        self.load = load
        self.order = _ORDER
        self.python_name: str = ""
        # This relationship's contribution to a plan-cache key, encoded once.
        # Filled in by `_clone`, once the relationship is attached and named.
        self.shape_ref: bytes = b""
        self.owner: type | None = None
        self.index: int = -1
        self.prototype: Relationship | None = None
        self._expression: RelationshipExpr | None = None

    def _clone(self, owner: type | None, python_name: str, index: int) -> Relationship:
        copy = Relationship(
            self.target,
            foreign_key=self.foreign_key,
            back_populates=self.back_populates,
            load=self.load,
        )
        copy.order = self.order
        copy.owner = owner
        copy.python_name = python_name
        copy.shape_ref = b"R" + python_name.encode("utf-8")
        copy.index = index
        copy.prototype = self.prototype or self
        copy._expression = RelationshipExpr(copy)
        return copy

    @property
    def expression(self) -> RelationshipExpr:
        if self._expression is None:
            raise DeclarationError(
                f"relationship {self.python_name or '<unnamed>'!r} is not attached to a model"
            )
        return self._expression

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self.expression
        return obj._orm_get_relation(self.index)

    def __set__(self, obj: Any, value: Any) -> None:
        obj._orm_set_relation(self.index, value)

    def __delete__(self, obj: Any) -> None:
        raise AttributeError(f"cannot delete relationship {self.python_name!r}")

    def __repr__(self) -> str:
        owner = getattr(self.owner, "__name__", "?")
        return f"<Relationship {owner}.{self.python_name} -> {self.target!r}>"


def relationship(
    target: Any,
    *,
    foreign_key: Any,
    back_populates: str | None = None,
    load: LoadStrategy = "raise",
) -> Any:
    """Declare a relationship to ``target``.

    ``target`` is a model class or its name, resolved only inside the registry
    that owns both models. ``foreign_key`` names the column holding the key: a
    column on this model makes the relationship to-one, and a column on the
    target makes it to-many.

    ``load`` defaults to ``"raise"``, so reading the attribute without loading
    it raises instead of emitting a query. ``"selectin"`` and ``"joined"`` load
    it with every query for this model.
    """
    if load not in LOAD_STRATEGIES:
        raise DeclarationError(
            f"unknown load strategy {load!r}; expected one of "
            f"{', '.join(sorted(LOAD_STRATEGIES))}"
        )
    if not isinstance(target, (str, type)):
        raise DeclarationError(
            f"relationship target must be a model class or its name, got {target!r}"
        )
    if foreign_key is None:
        raise DeclarationError("relationship() requires foreign_key=")
    if back_populates is not None and not isinstance(back_populates, str):
        raise DeclarationError("back_populates= must be an attribute name")
    return Relationship(
        target,
        foreign_key=foreign_key,
        back_populates=back_populates,
        load=load,
    )


__all__ = [
    "LOAD_STRATEGIES",
    "LoadOption",
    "LoadStrategy",
    "Relationship",
    "RelationshipExpr",
    "relationship",
]
