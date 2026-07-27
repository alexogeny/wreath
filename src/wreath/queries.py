"""Named, compiled reads over one model.

Every application grows a hand-written data-access layer: a module of functions
that build the same five queries, with the filtering inlined at each call site
and no shared name for *the query that fetches a paddock's llamas*. Wreath
already owns the hard half of doing better -- a plan cache keyed by query shape
-- and until now used it only internally.

A ``Queries`` class gives those reads names::

    class Llamas(Queries[Llama]):
        by_paddock = query(Llama.paddock_id == Param("paddock"))
        overdue = query(Llama.checked_at < Param("before")).order_by(Llama.checked_at)

    herd = await Llamas(session).by_paddock(paddock=7)

Two properties follow from declaring rather than building:

* **The shape is fixed at class-definition time and only values vary**, so each
  declaration compiles exactly once through the registry's existing plan cache,
  however many times it runs and whatever it is called with. There is no second
  cache here; a bound declaration is an ordinary :class:`~wreath.orm.Select`
  whose plan-cache key is byte-identical to the hand-written query's.
* **Mistakes move to import time.** A column that belongs to another model, a
  parameter written somewhere a value cannot be bound -- those fail when the
  class is defined, not on the request that first runs them.

**Reads only, deliberately.** Writes already have an owner: the ``Session``,
whose one-way direction is an ORM invariant. A layer that also wrote would
duplicate that or quietly work around it, which is why this is ``Queries`` and
not a repository -- the smaller surface is the correct one, not a subset of one.
"""

from __future__ import annotations

from typing import Any, TypeVar, get_args, get_origin

from .orm.compiler import check_predicate_columns, compile_rebind
from .orm.errors import DeclarationError
from .orm.expressions import (
    EQ,
    GE,
    GT,
    LE,
    LT,
    NE,
    BinaryExpr,
    ColumnExpr,
    Expression,
    Predicate,
    RelatedColumnExpr,
    ValueExpr,
)
from .orm.model import Model
from .orm.query import Select

__all__ = ["BoundQuery", "Param", "Queries", "QueryDeclaration", "query"]


class _Placeholder(Expression):
    """The gap a declaration leaves in its predicate tree.

    Not a :class:`~wreath.orm.expressions.ValueExpr`: a placeholder must never
    be mistaken for a bound value. Neither the cache key nor the SQL renderer
    knows this node, so one that reached them by another route fails loudly
    rather than compiling a query with a missing value in it.
    """

    __slots__ = ("name", "pg_type")

    def __init__(self, name: str, pg_type: Any) -> None:
        self.name = name
        #: Taken from the column it was compared against, so the bound value is
        #: coerced and typed exactly as a literal in the same position would be.
        self.pg_type = pg_type

    def bind(self, values: Any) -> ValueExpr:
        """The bound value for this call, coerced to the column's type."""
        try:
            return ValueExpr(self.pg_type.coerce(values[self.name]), self.pg_type)
        except (TypeError, ValueError, OverflowError) as error:
            # The type error is about a parameter, and a caller looking at it
            # needs to know which one before knowing what was wrong with it.
            raise type(error)(f"parameter {self.name!r}: {error}") from error

    def __repr__(self) -> str:
        return f"<Placeholder {self.name} {self.pg_type.name}>"


class Param(RelatedColumnExpr):
    """One named value a declared query binds per call.

    ``Param("paddock")`` stands where a literal would::

        by_paddock = query(Llama.paddock_id == Param("paddock"))

    It subclasses the column expression rather than the value expression for a
    reason worth knowing: Python gives the *right* operand of a comparison first
    refusal only when its type is a proper subclass of the left operand's, and
    the left operand here is a column. Inheriting from ``RelatedColumnExpr`` --
    the deepest column type a declaration can compare against -- is what lets
    both ``Llama.paddock_id == Param(...)`` and ``Llama.paddock.name ==
    Param(...)`` build a placeholder instead of trying to coerce one into a
    ``bigint``. A ``Param`` is bait for an operator, never a node in a tree; the
    tree gets a ``_Placeholder``.

    Because interception happens in the operator, a parameter works with the six
    comparisons (``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``) in either order.
    Operators spelled as methods -- ``like``, ``in_``, the jsonb and array
    operators -- bind their operand before a ``Param`` can be seen, so those
    take literals for now.
    """

    __slots__ = ("name",)

    __hash__ = object.__hash__

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name.isidentifier():
            raise DeclarationError(
                f"a parameter name must be a Python identifier, got {name!r}"
            )
        # No column and no path: a Param is resolved by whatever it is compared
        # against, and every inherited method that reads those is unreachable
        # from a declaration.
        super().__init__(None, ())
        self.name = name

    def _against(self, other: Any, operator: str) -> BinaryExpr:
        if not isinstance(other, ColumnExpr) or isinstance(other, Param):
            raise TypeError(
                f"a parameter compares against a model column such as "
                f"Llama.paddock_id, got {other!r}"
            )
        return BinaryExpr(operator, other, _Placeholder(self.name, other.column.pg_type))

    # The operator that reaches these is the *reflected* one -- `column < param`
    # arrives as `param.__gt__(column)` -- so each builds the predicate read
    # from the column's side. Writing the parameter first means the same thing
    # and lands on the same method, which is why the two orders agree.

    def __eq__(self, other: Any) -> BinaryExpr:
        return self._against(other, EQ)

    def __ne__(self, other: Any) -> BinaryExpr:
        return self._against(other, NE)

    def __lt__(self, other: Any) -> BinaryExpr:
        return self._against(other, GT)

    def __le__(self, other: Any) -> BinaryExpr:
        return self._against(other, GE)

    def __gt__(self, other: Any) -> BinaryExpr:
        return self._against(other, LT)

    def __ge__(self, other: Any) -> BinaryExpr:
        return self._against(other, LE)

    def __repr__(self) -> str:
        return f"<Param {self.name}>"


class QueryDeclaration:
    """One named read: a query shape, its parameters, and how to bind them.

    Created by :func:`query` in a class body, where the model is not known yet,
    so it starts as a recording of builder calls and becomes a real
    :class:`~wreath.orm.Select` when :class:`Queries` names its model. Both
    states are immutable -- a builder method returns a new declaration, and
    resolving one produces a new one -- so a declaration written once at module
    level can be reused by more than one class.
    """

    __slots__ = ("_binders", "_select", "_single", "_steps", "name", "parameters")

    def __init__(
        self,
        steps: tuple[tuple[str, tuple[Any, ...]], ...],
        *,
        single: bool = False,
        select: Select | None = None,
        binders: tuple[Any, ...] = (),
        name: str = "query",
        parameters: tuple[str, ...] = (),
    ) -> None:
        self._steps = steps
        self._single = single
        self._select = select
        self._binders = binders
        #: ``Class.attribute``, once a ``Queries`` class has claimed it.
        self.name = name
        #: Parameter names, in the order they bind.
        self.parameters = parameters

    # -- declaration ------------------------------------------------------

    def _check_open(self) -> None:
        """A resolved declaration is a fixed shape, and stays one.

        Letting one be extended afterwards would produce a second shape wearing
        the first one's name -- and the name is the thing this module sells.
        """
        if self._select is not None:
            raise DeclarationError(
                f"{self.name} is already declared; build the whole query in the "
                f"class body rather than adding to it afterwards"
            )

    def _step(self, method: str, args: tuple[Any, ...]) -> QueryDeclaration:
        self._check_open()
        return QueryDeclaration((*self._steps, (method, args)), single=self._single)

    def where(self, *predicates: Predicate) -> QueryDeclaration:
        """Narrow this query; predicates combine with AND."""
        return self._step("where", predicates)

    def order_by(self, *expressions: Any) -> QueryDeclaration:
        _reject_params("order_by", expressions)
        return self._step("order_by", expressions)

    def include(self, *load_options: Any) -> QueryDeclaration:
        """Load relationships with this query."""
        return self._step("include", load_options)

    def limit(self, value: int) -> QueryDeclaration:
        _reject_params("limit", (value,))
        return self._step("limit", (value,))

    def offset(self, value: int) -> QueryDeclaration:
        _reject_params("offset", (value,))
        return self._step("offset", (value,))

    def one(self) -> QueryDeclaration:
        """Return a single object (or ``None``) rather than a list.

        Raises ``MultipleResultsError`` if the query matches more than one row,
        exactly as ``Session.fetch_one`` does.
        """
        self._check_open()
        return QueryDeclaration(self._steps, single=True)

    def _resolve(self, owner: str, attribute: str, model: type) -> QueryDeclaration:
        """Turn the recording into a Select, and check what can be checked."""
        select = Select.build(model, ())
        for method, args in self._steps:
            select = getattr(select, method)(*args)
        found: list[_Placeholder] = []
        binders = []
        for predicate in select.predicates:
            check_predicate_columns(model, predicate)
            binders.append(compile_rebind(predicate, _Placeholder, found))
        return QueryDeclaration(
            self._steps,
            single=self._single,
            select=select,
            binders=tuple(binders),
            name=f"{owner}.{attribute}",
            # A name used twice binds both sites from one argument, so the
            # parameter list is de-duplicated while keeping bind order.
            parameters=tuple(dict.fromkeys(item.name for item in found)),
        )

    # -- use --------------------------------------------------------------

    @property
    def single(self) -> bool:
        """Whether this declaration returns one object rather than a list."""
        return self._single

    def bind(self, **values: Any) -> Select:
        """The query this declaration runs for one set of parameter values.

        The result is an ordinary ``Select``, so anything that takes one --
        ``Session.fetch``, ``Session.count``, ``wreath.pagination`` -- takes a
        declared query too.
        """
        select = self._select
        if select is None:
            raise DeclarationError(
                "a query() declaration is only usable as an attribute of a "
                "Queries subclass, which is what gives it a model"
            )
        names = self.parameters
        if not names:
            if values:
                raise TypeError(f"{self.name}() takes no parameters")
            return select
        for name in names:
            if name not in values:
                raise TypeError(f"{self.name}() is missing parameter {name!r}")
        if len(values) != len(names):
            for name in values:
                if name not in names:
                    raise TypeError(f"{self.name}() got an unexpected parameter {name!r}")
        return select.rebound(
            tuple(
                predicate if binder is None else binder(values)
                for predicate, binder in zip(select.predicates, self._binders, strict=True)
            )
        )

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        # Reached on the class, this is the declaration itself -- which is what
        # makes the name usable by things that are not a call: an authorization
        # rule, a typegen pass, a test that inspects the shape.
        if instance is None:
            return self
        return BoundQuery(self, instance.session)

    def __repr__(self) -> str:
        return f"<query {self.name}({', '.join(self.parameters)})>"


class BoundQuery:
    """A declared query with a session behind it; call it to run it."""

    __slots__ = ("declaration", "session")

    def __init__(self, declaration: QueryDeclaration, session: Any) -> None:
        self.declaration = declaration
        self.session = session

    async def __call__(self, **values: Any) -> Any:
        declaration = self.declaration
        select = declaration.bind(**values)
        if declaration.single:
            return await self.session.fetch_one(select)
        return await self.session.fetch(select)

    async def count(self, **values: Any) -> int:
        """How many rows this query matches, without hydrating them."""
        return await self.session.count(self.declaration.bind(**values))

    def __repr__(self) -> str:
        return f"<{self.declaration.name} bound>"


def query(*predicates: Predicate) -> QueryDeclaration:
    """Declare one named read, to be finished by the builder methods.

    Written in the body of a :class:`Queries` subclass, where the model comes
    from the class rather than from this call::

        class Llamas(Queries[Llama]):
            overdue = query(Llama.checked_at < Param("before")).order_by(Llama.checked_at)
    """
    return QueryDeclaration((("where", predicates),) if predicates else ())


class Queries[ModelT]:
    """A named set of reads over one model.

    Subclass it with the model as its type argument and declare the reads as
    class attributes; construct it with a session to run them::

        class Llamas(Queries[Llama]):
            by_paddock = query(Llama.paddock_id == Param("paddock"))

        herd = await Llamas(session).by_paddock(paddock=7)

    A subclass with no declarations of its own may leave the model out, so a
    base class can hold whatever a family of query sets shares.
    """

    __slots__ = ("session",)

    #: The model these queries read, filled in from ``Queries[Model]``.
    model: Any = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = _declared_model(cls)
        if model is not None and not (isinstance(model, type) and issubclass(model, Model)):
            raise DeclarationError(
                f"{cls.__name__} names {model!r}, which is not a model class; "
                f"Queries[...] takes a Model subclass"
            )
        declared = [
            (name, value)
            for name, value in vars(cls).items()
            if isinstance(value, QueryDeclaration)
        ]
        if declared and model is None:
            raise DeclarationError(
                f"{cls.__name__} declares queries but names no model; write "
                f"class {cls.__name__}(Queries[YourModel])"
            )
        for name, declaration in declared:
            # Resolving produces a new declaration, so the same query() object
            # can be shared by two classes with two different models.
            setattr(cls, name, declaration._resolve(cls.__name__, name, model))
        if model is not None:
            cls.model = model

    def __init__(self, session: Any) -> None:
        self.session = session

    @classmethod
    def declarations(cls) -> dict[str, QueryDeclaration]:
        """Every declared read on this class, by attribute name.

        A declared query is a stable name, and this is where anything that wants
        to work from those names -- authorization, generated clients -- starts.
        """
        found: dict[str, QueryDeclaration] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, QueryDeclaration):
                    found[name] = value
        return found

    def __repr__(self) -> str:
        model = getattr(self.model, "__name__", "?")
        return f"<{type(self).__name__} {model} queries={len(self.declarations())}>"


def _declared_model(cls: type) -> Any:
    """The model named by ``Queries[Model]``, or inherited from a base."""
    for base in getattr(cls, "__orig_bases__", ()):
        origin = get_origin(base)
        if isinstance(origin, type) and issubclass(origin, Queries):
            arguments = get_args(base)
            if arguments and not isinstance(arguments[0], TypeVar):
                return arguments[0]
    return getattr(cls, "model", None)


def _reject_params(method: str, values: tuple[Any, ...]) -> None:
    """A parameter is only ever a *value*, and these positions are not values.

    Caught here because it is the last moment the mistake is still local: a
    ``Param`` in an ORDER BY or a LIMIT could never be supplied by a caller, and
    a declaration nobody can call should not survive its own class body.
    """
    for value in values:
        if isinstance(value, Param):
            raise DeclarationError(
                f"{method}() cannot take a parameter: it is part of the query's "
                f"shape, and only values are bound per call"
            )
