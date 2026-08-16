"""Named, compiled reads over one model.

Every application grows a hand-written data-access layer: a module of functions
that build the same five queries, with the filtering inlined at each call site
and no shared name for *the query that fetches a paddock's llamas*. Wreath
already owns the hard half of doing better -- a plan cache keyed by query shape
-- and until now used it only internally.

A `Queries` class gives those reads names:

```python
class Llamas(Queries[Llama]):
    by_paddock = query(Llama.paddock_id == Param("paddock"))
    overdue = query(Llama.checked_at < Param("before")).order_by(Llama.checked_at)

herd = await Llamas(session).by_paddock(paddock=7)
```

Two properties follow from declaring rather than building:

* **The shape is fixed at class-definition time and only values vary**, so each
  declaration compiles exactly once through the registry's existing plan cache,
  however many times it runs and whatever it is called with. There is no second
  cache here; a bound declaration is an ordinary `Select`
  whose plan-cache key is byte-identical to the hand-written query's.
* **Mistakes move to import time.** A column that belongs to another model, a
  parameter written somewhere a value cannot be bound -- those fail when the
  class is defined, not on the request that first runs them.

Declared searches also **fuse**. A vector distance and a relevance score are
numbers on different scales, and no weighting between them survives a change of
embedding model — but a rank is a rank, so `fuse` merges two named searches by
where each row *placed* rather than by what either scored it:

```python
class Documents(Queries[Document]):
    nearest = query().order_by(D.embedding.cosine_distance(Param("q"))).limit(50)
    matching = query(D.search.matches(Param("terms"))).order_by(...).limit(50)
    hybrid = fuse(nearest, matching).limit(10)
```

**Reads only, deliberately.** Writes already have an owner: the `Session`,
whose one-way direction is an ORM invariant. A layer that also wrote would
duplicate that or quietly work around it, which is why this is `Queries` and
not a repository -- the smaller surface is the correct one, not a subset of one.
"""

from __future__ import annotations

from typing import Any, TypeVar, get_args, get_origin

from ._native import _core
from .orm.compiler import (
    _bind_cached_plan,
    check_predicate_columns,
    compile_declared_values,
    compile_rebind,
    compile_select,
)
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
    OrderExpr,
    Predicate,
    RelatedColumnExpr,
    ValueExpr,
)
from .orm.model import Model
from .orm.query import Select

__all__ = [
    "BoundFusion",
    "BoundQuery",
    "Fusion",
    "Param",
    "Placeholder",
    "Queries",
    "QueryDeclaration",
    "fuse",
    "query",
]


class Placeholder(Expression):
    """The gap a declaration leaves in its predicate tree.

    Not a `ValueExpr`: a placeholder must never
    be mistaken for a bound value. Neither the cache key nor the SQL renderer
    knows this node, so one that reached them by another route fails loudly
    rather than compiling a query with a missing value in it.

    Public because it is half of a contract rather than an implementation
    detail: `compile_rebind` takes the marker class
    to look for, so any module that lets a caller write `Param` in a
    predicate and binds it later has to name this type. `wreath.series` is the
    second such module. What it is *not* is something to construct — a
    `Param` compared against a column builds one, and that is the only route
    that gives it a type to coerce with.
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

    `Param("paddock")` stands where a literal would:

    ```python
    by_paddock = query(Llama.paddock_id == Param("paddock"))
    ```

    It subclasses the column expression rather than the value expression for a
    reason worth knowing: Python gives the *right* operand of a comparison first
    refusal only when its type is a proper subclass of the left operand's, and
    the left operand here is a column. Inheriting from `RelatedColumnExpr` --
    the deepest column type a declaration can compare against -- is what lets
    both `Llama.paddock_id == Param(...)` and `Llama.paddock.name == Param(...)`
    build a placeholder instead of trying to coerce one into a
    `bigint`. A `Param` is bait for an operator, never a node in a tree; the
    tree gets a `Placeholder`.

    Because interception happens in the operator, a parameter works with the six
    comparisons (`==`, `!=`, `<`, `<=`, `>`, `>=`) in either order.

    Operators spelled as methods -- `like`, the jsonb and array operators, the
    vector distances -- bind their operand before this class can intercept
    anything, so they reach it the other way round: `_bind` recognises the
    `_as_placeholder` seam and asks for a placeholder of the type that position
    takes. `in_` is the exception, because its operand is a *list* of values
    rather than one, and the list's length is part of the query's shape.
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

    def _as_placeholder(self, pg_type: Any) -> Placeholder:
        """The placeholder this parameter stands for, typed by its position.

        The seam `wreath.orm.expressions._bind` uses for operators spelled as
        *methods*, which never reach the reflected-comparison machinery below --
        `Document.embedding.cosine_distance(Param("q"))` binds its operand
        directly, so this is where the parameter becomes a placeholder instead.
        """
        return Placeholder(self.name, pg_type)

    def _against(self, other: Any, operator: str) -> BinaryExpr:
        if not isinstance(other, ColumnExpr) or isinstance(other, Param):
            raise TypeError(
                f"a parameter compares against a model column such as "
                f"Llama.paddock_id, got {other!r}"
            )
        return BinaryExpr(operator, other, Placeholder(self.name, other.column.pg_type))

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

    Created by `query` in a class body, where the model is not known yet,
    so it starts as a recording of builder calls and becomes a real
    `Select` when `Queries` names its model. Both
    states are immutable -- a builder method returns a new declaration, and
    resolving one produces a new one -- so a declaration written once at module
    level can be reused by more than one class.
    """

    __slots__ = (
        "_binders",
        "_ordering_binders",
        "_execution_select",
        "_select",
        "_single",
        "_steps",
        "_value_program",
        "name",
        "parameters",
    )

    def __init__(
        self,
        steps: tuple[tuple[str, tuple[Any, ...]], ...],
        *,
        single: bool = False,
        select: Select | None = None,
        binders: tuple[Any, ...] = (),
        ordering_binders: tuple[Any, ...] = (),
        execution_select: Select | None = None,
        value_program: Any = None,
        name: str = "query",
        parameters: tuple[str, ...] = (),
    ) -> None:
        self._steps = steps
        self._single = single
        self._select = select
        self._binders = binders
        self._ordering_binders = ordering_binders
        self._execution_select = execution_select
        self._value_program = value_program
        #: `Class.attribute`, once a `Queries` class has claimed it.
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
        """Order this query's rows. Part of the shape, so no `Param` here.

        A sort column supplied per call would be a different query shape each
        time, which is the one thing a declared query is not. For request-driven
        sorting, `wreath.pagination.apply_sort` checks an allow-list instead.

        Raises:
            DeclarationError: if given a `Param`.
        """
        _reject_params("order_by", expressions)
        return self._step("order_by", expressions)

    def include(self, *load_options: Any) -> QueryDeclaration:
        """Load relationships with this query."""
        return self._step("include", load_options)

    def limit(self, value: int) -> QueryDeclaration:
        """Cap the rows this query returns. Fixed at declaration time.

        A per-call limit belongs to `wreath.pagination.paginate`, which bounds it.

        Raises:
            DeclarationError: if given a `Param`.
        """
        _reject_params("limit", (value,))
        return self._step("limit", (value,))

    def offset(self, value: int) -> QueryDeclaration:
        """Skip the first `value` rows. Fixed at declaration time.

        Raises:
            DeclarationError: if given a `Param`.
        """
        _reject_params("offset", (value,))
        return self._step("offset", (value,))

    def one(self) -> QueryDeclaration:
        """Return a single object (or `None`) rather than a list.

        Raises `MultipleResultsError` if the query matches more than one row,
        exactly as `Session.fetch_one` does.
        """
        self._check_open()
        return QueryDeclaration(self._steps, single=True)

    def _resolve(self, owner: str, attribute: str, model: type) -> QueryDeclaration:
        """Turn the recording into a Select, and check what can be checked."""
        select = Select.build(model, ())
        for method, args in self._steps:
            select = getattr(select, method)(*args)
        found: list[Placeholder] = []
        binders = []
        for predicate in select.predicates:
            check_predicate_columns(model, predicate)
            binders.append(compile_rebind(predicate, Placeholder, found))
        # Orderings are walked after the predicates and in declaration order,
        # because that is the order the renderer emits their placeholders in and
        # `parameters` has to name them in bind order.
        ordering_binders = tuple(
            compile_rebind(item.expression, Placeholder, found)
            for item in select.orderings
        )
        execution_select = select
        if self._single and (
            execution_select.limit_ is None or execution_select.limit_ > 2
        ):
            execution_select = execution_select.limit(2)
        return QueryDeclaration(
            self._steps,
            single=self._single,
            select=select,
            binders=tuple(binders),
            ordering_binders=ordering_binders,
            execution_select=execution_select,
            value_program=compile_declared_values(execution_select, Placeholder),
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

        The result is an ordinary `Select`, so anything that takes one --
        `Session.fetch`, `Session.count`, `wreath.pagination` -- takes a
        declared query too.
        """
        select = self._select
        if select is None:
            raise DeclarationError(
                "a query() declaration is only usable as an attribute of a "
                "Queries subclass, which is what gives it a model"
            )
        self._check_values(values)
        return self._bind_validated(values)

    def _check_values(self, values: dict[str, Any]) -> None:
        names = self.parameters
        if not names:
            if values:
                raise TypeError(f"{self.name}() takes no parameters")
            return
        for name in names:
            if name not in values:
                raise TypeError(f"{self.name}() is missing parameter {name!r}")
        if len(values) != len(names):
            known = frozenset(names)
            for name in values:
                if name not in known:
                    raise TypeError(f"{self.name}() got an unexpected parameter {name!r}")

    def _bind_validated(self, values: dict[str, Any]) -> Select:
        select = self._select
        if select is None:
            raise DeclarationError(
                "a query() declaration is only usable as an attribute of a "
                "Queries subclass, which is what gives it a model"
            )
        if not self.parameters:
            return select
        bound = select.rebound(
            tuple(
                predicate if binder is None else binder(values)
                for predicate, binder in zip(select.predicates, self._binders, strict=True)
            )
        )
        if any(self._ordering_binders):
            # Only rebuilt when an ordering actually holds a parameter: a
            # declared vector search binds its query vector here, and every
            # other declaration keeps the tuple it was compiled with.
            bound = bound.rebound_orderings(
                tuple(
                    item if binder is None else OrderExpr(binder(values), item.direction)
                    for item, binder in zip(
                        select.orderings, self._ordering_binders, strict=True
                    )
                )
            )
        return bound

    def _compile(self, registry: Any, values: dict[str, Any]) -> tuple[Any, Select]:
        """Bind against this registry, bypassing Select construction on hits."""
        self._check_values(values)
        execution_select = self._execution_select
        value_program = self._value_program
        if execution_select is None or value_program is None:
            raise DeclarationError(
                "a query() declaration is only usable as an attribute of a "
                "Queries subclass, which is what gives it a model"
            )
        cached = registry.cached_prepared_plan(self)
        if cached is not None:
            shape_key, plan = cached
            return _bind_cached_plan(plan, shape_key, value_program(values)), execution_select

        bound = self._bind_validated(values)
        if self._single and (bound.limit_ is None or bound.limit_ > 2):
            bound = bound.limit(2)
        compiled = compile_select(registry, bound)
        registry.remember_prepared_shape(self, compiled.shape_key)
        return compiled, execution_select

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
        compiled, execution_select = declaration._compile(
            self.session.registry, values
        )
        if declaration.single:
            return await self.session._fetch_one_compiled(execution_select, compiled)
        return await self.session._fetch_compiled(execution_select, compiled)

    async def count(self, **values: Any) -> int:
        """How many rows this query matches, without hydrating them."""
        return await self.session.count(self.declaration.bind(**values))

    def __repr__(self) -> str:
        return f"<{self.declaration.name} bound>"


def query(*predicates: Predicate) -> QueryDeclaration:
    """Declare one named read, to be finished by the builder methods.

    Written in the body of a `Queries` subclass, where the model comes
    from the class rather than from this call:

    ```python
    class Llamas(Queries[Llama]):
        overdue = query(Llama.checked_at < Param("before")).order_by(Llama.checked_at)
    ```

    """
    return QueryDeclaration((("where", predicates),) if predicates else ())


class Fusion:
    """Two or more declared searches, merged into one ranked answer.

    Built by `fuse`, and used exactly as a declared query is -- an attribute of
    a `Queries` subclass, called with the parameters its searches need:

    ```python
    class Documents(Queries[Document]):
        nearest = (
            query()
            .order_by(Document.embedding.cosine_distance(Param("q")))
            .limit(50)
        )
        matching = (
            query(Document.search.matches(Param("terms")))
            .order_by(Document.search.rank(Param("terms")).desc())
            .limit(50)
        )
        hybrid = fuse(nearest, matching).limit(10)

    found = await Documents(session).hybrid(q=vector, terms="llama husbandry")
    ```

    **The merge is by rank, not by score.** A cosine distance of `0.18` and a
    `ts_rank` of `0.06` cannot be added, subtracted, or weighted against each
    other: they are different units, and any constant that reconciles them is
    wrong again the next time the embedding model changes. Reciprocal-rank
    fusion throws the numbers away and keeps the *positions* --
    `score = sum(1 / (k + rank))` over the searches that returned the row, ranks
    counted from 1. A row both searches placed mid-table beats a row one of them
    placed first, which is the behaviour hybrid retrieval is wanted for.

    `k` damps how much a first place is worth; `60` is the conventional default
    from the paper the technique comes from, and lowering it makes the top of
    each search count for more.

    **Every search in a fusion is named, ordered, and bounded**, and all three
    are checked when the class is defined. The rank *is* the position, so a
    query with no `order_by` has no ranking to contribute; and the bound is what
    keeps a fusion a merge of two shortlists rather than of two whole tables --
    for a vector search it is also the `LIMIT` that lets the approximate index
    answer at all.

    Named means `fuse` takes the *attributes*, never a `query(...)` written
    inside the call: a search on no class is in no `declarations()` listing, and
    the tools that walk a query set by name -- the transitional-column scanner
    among them -- would skip it in silence. A search declared on another
    `Queries` class is named, and fusing one of those is fine.

    Ties in the fused score are broken by primary key ascending, so the same
    data returns the same order.
    """

    __slots__ = ("_halves", "_k", "_limit", "_settled", "name", "parameters")

    def __init__(
        self,
        halves: tuple[QueryDeclaration, ...],
        *,
        k: int,
        limit: int | None = None,
        settled: bool = False,
        name: str = "fusion",
        parameters: tuple[str, ...] = (),
    ) -> None:
        self._halves = halves
        self._k = k
        self._limit = limit
        self._settled = settled
        #: `Class.attribute`, once a `Queries` class has claimed it.
        self.name = name
        #: Every parameter its searches take, de-duplicated, in bind order.
        self.parameters = parameters

    @property
    def k(self) -> int:
        """The reciprocal-rank constant this fusion scores with."""
        return self._k

    @property
    def limit_(self) -> int | None:
        """How many rows the fusion returns, or `None` for all of them."""
        return self._limit

    def limit(self, value: int) -> Fusion:
        """Cap the fused rows. Fixed at declaration time, as `query()`'s is."""
        if self._settled:
            raise DeclarationError(
                f"{self.name} is already declared; build the whole fusion in the "
                f"class body rather than adding to it afterwards"
            )
        _reject_params("limit", (value,))
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"limit must be an integer >= 1, got {value!r}")
        return Fusion(self._halves, k=self._k, limit=value)

    def _resolve(
        self,
        owner: str,
        attribute: str,
        model: type,
        resolved: dict[QueryDeclaration, QueryDeclaration],
    ) -> Fusion:
        """Bind this fusion to a model, reusing each search's resolved twin.

        `resolved` maps the declarations written in the class body to what they
        became. Reusing those objects rather than resolving private copies is
        load-bearing: the registry associates a prepared plan with the
        declaration *object*, so a copy would compile the same SQL a second time
        and hold a second cache entry for it.

        A half that is in neither `resolved` nor already settled was written
        inside the `fuse(...)` call and belongs to no class, which is what
        `_check_named` refuses.
        """
        name = f"{owner}.{attribute}"
        halves: list[QueryDeclaration] = []
        for half in self._halves:
            # A half this class body names resolved a moment ago; one named on
            # another query set arrived already resolved. Anything else was
            # written inside the `fuse(...)` call, which is what `_check_named`
            # refuses -- and returning the `Select` is how the checks below get
            # one at all.
            settled = resolved.get(half, half)
            select = _check_named(name, owner, attribute, settled)
            _check_half(name, settled, select, model)
            halves.append(settled)
        names: dict[str, None] = {}
        for settled in halves:
            names.update(dict.fromkeys(settled.parameters))
        return Fusion(
            tuple(halves),
            k=self._k,
            limit=self._limit,
            settled=True,
            name=name,
            parameters=tuple(names),
        )

    def _check_values(self, values: dict[str, Any]) -> None:
        names = self.parameters
        for name in names:
            if name not in values:
                raise TypeError(f"{self.name}() is missing parameter {name!r}")
        if len(values) != len(names):
            known = frozenset(names)
            for name in values:
                if name not in known:
                    raise TypeError(f"{self.name}() got an unexpected parameter {name!r}")

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return BoundFusion(self, instance.session)

    def __repr__(self) -> str:
        return f"<fusion {self.name}({', '.join(self.parameters)})>"


class BoundFusion:
    """A declared fusion with a session behind it; call it to run it."""

    __slots__ = ("fusion", "session")

    def __init__(self, fusion: Fusion, session: Any) -> None:
        self.fusion = fusion
        self.session = session

    async def __call__(self, **values: Any) -> Any:
        """The fused rows, best first, as ordinary hydrated objects.

        Each search runs against the session and contributes a ranking of
        primary keys; the keys are scored and sorted, and the objects come back
        in that order. A row both searches returned is one object, because the
        session's identity map already guarantees that.
        """
        fusion = self.fusion
        fusion._check_values(values)
        session = self.session
        registry = session.registry
        rankings: list[tuple[Any, ...]] = []
        rows: dict[Any, Any] = {}
        for half in fusion._halves:
            compiled, execution_select = half._compile(
                registry, {name: values[name] for name in half.parameters}
            )
            ranking: list[Any] = []
            for row in await session._fetch_compiled(execution_select, compiled):
                key = row._orm_primary_key()
                if key is None:
                    # Not reachable through a declared query, which always
                    # selects every column -- but a merge keyed on identity has
                    # to say so rather than quietly dropping the row.
                    raise DeclarationError(
                        f"{fusion.name}() cannot merge a "
                        f"{type(row).__name__} whose primary key is not loaded"
                    )
                if key not in rows:
                    rows[key] = row
                ranking.append(key)
            rankings.append(tuple(ranking))
        order = _fused_order(tuple(rankings), fusion._k)
        if fusion._limit is not None:
            order = order[: fusion._limit]
        return [rows[key] for key in order]

    def __repr__(self) -> str:
        return f"<{self.fusion.name} bound>"


def fuse(*queries: QueryDeclaration, k: int = 60) -> Fusion:
    """Merge two or more declared searches into one ranked answer.

    ```python
    hybrid = fuse(nearest, matching).limit(10)
    ```

    Each row is scored by where it placed -- `sum(1 / (k + rank))` over the
    searches that returned it -- so a vector distance and a relevance score
    become comparable without inventing a weighting between them. See `Fusion`
    for what each search must supply and what `k` does.
    """
    if len(queries) < 2:
        raise DeclarationError(
            "fuse() merges at least two declared queries; there is nothing to "
            "fuse a single query with"
        )
    for item in queries:
        if not isinstance(item, QueryDeclaration):
            raise DeclarationError(
                f"fuse() takes declared queries built with query(), got {item!r}"
            )
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError(f"k must be an integer >= 0, got {k!r}")
    return Fusion(tuple(queries), k=k)


def _fused_order(rankings: tuple[tuple[Any, ...], ...], k: int) -> list[Any]:
    """Reciprocal-rank fusion over rankings of keys, best first.

    `score = sum(1 / (k + rank))`, ranks counted from 1, summed over every
    ranking a key appears in. Ties are broken by the key itself, so an ordering
    this produces is a function of its input and nothing else -- two rows that
    placed identically must not come back in whatever order a dict happened to
    hold them.

    Separated from execution because this is the part that can be wrong without
    looking wrong: a scoring mistake still returns plausible rows in a plausible
    order, and only a hand-computed expectation catches it.
    """
    return _core.fused_order(rankings, k)


def _check_named(
    name: str, owner: str, attribute: str, declaration: QueryDeclaration
) -> Select:
    """The `Select` behind a fused search, refusing one that no class names.

    A fused search must be a query some class body binds to a name.

    Everything that walks a query set walks it *by name*:
    `Queries.declarations()` is how the transitional-column scanner reaches each
    search's `Select`, and it can only report attributes. A search written
    inside the `fuse(...)` call is on no class, so it appears in no listing and
    is skipped without a word -- and a silently skipped query is exactly the
    migration bug nobody attributes to the fusion that hid it.

    Refusing it here (refuse rather than half-wire) makes the gap structurally impossible rather
    than documented, and it is the direction that stays open: a later release
    may accept an inline search once the scanner sees one, where a later release
    could not start rejecting a spelling applications had shipped.

    Only an *unresolved* declaration can fail this. Resolving one happens in
    `Queries.__init_subclass__` and nowhere else, so a settled declaration --
    including one named on a different query set -- is an attribute of some
    class by construction. That is also why this returns the `Select`: being
    named is exactly what makes one exist, so the checks that need it take it
    from here rather than re-testing for `None` where no `None` can arrive.
    """
    select = declaration._select
    if select is not None:
        return select
    raise DeclarationError(
        f"{name} fuses a search written inline, which no class names. Declare "
        f"each search as a named attribute of {owner} and fuse those names: "
        f"nearest = query()...limit(50); matching = query(...)...limit(50); "
        f"{attribute} = fuse(nearest, matching). A fusion's searches have to be "
        f"reachable by name, because that is how the transitional-column "
        f"scanner and typegen find the query behind each one."
    )


def _check_half(
    name: str, declaration: QueryDeclaration, select: Select, model: type
) -> None:
    """What a query must be before a fusion can rank it.

    Takes the `Select` from `_check_named` rather than reading it back off the
    declaration, so there is no unresolved case left here to test for.
    """
    if select.model is not model:
        raise DeclarationError(
            f"{name} fuses a query over {select.model.__name__} with one over "
            f"{model.__name__}; a fusion merges rows by primary key, so every "
            f"query in one reads one model"
        )
    if declaration.single:
        raise DeclarationError(
            f"{name} cannot fuse {declaration.name}: .one() returns one object "
            f"rather than a ranking, and a fusion ranks rows"
        )
    if not select.orderings:
        raise DeclarationError(
            f"{name} cannot fuse {declaration.name}: a fused row's score comes "
            f"from where it placed, so each query needs an order_by(...)"
        )
    if select.limit_ is None:
        raise DeclarationError(
            f"{name} cannot fuse {declaration.name}: each query needs a "
            f"limit(...), so the fusion merges two shortlists rather than two "
            f"whole tables -- and for a vector search that bound is also what "
            f"lets the approximate index answer at all"
        )


class Queries[ModelT]:
    """A named set of reads over one model.

    Subclass it with the model as its type argument and declare the reads as
    class attributes; construct it with a session to run them:

    ```python
    class Llamas(Queries[Llama]):
        by_paddock = query(Llama.paddock_id == Param("paddock"))

    herd = await Llamas(session).by_paddock(paddock=7)
    ```

    A subclass with no declarations of its own may leave the model out, so a
    base class can hold whatever a family of query sets shares.
    """

    __slots__ = ("session",)

    #: The model these queries read, filled in from `Queries[Model]`.
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
        fused = [
            (name, value)
            for name, value in vars(cls).items()
            if isinstance(value, Fusion)
        ]
        if (declared or fused) and model is None:
            raise DeclarationError(
                f"{cls.__name__} declares queries but names no model; write "
                f"class {cls.__name__}(Queries[YourModel])"
            )
        resolved: dict[QueryDeclaration, QueryDeclaration] = {}
        for name, declaration in declared:
            # Resolving produces a new declaration, so the same query() object
            # can be shared by two classes with two different models.
            settled = declaration._resolve(cls.__name__, name, model)
            resolved[declaration] = settled
            setattr(cls, name, settled)
        # Fusions second, and with what the queries above became: a fusion names
        # the declarations written beside it, and both must end up as the same
        # object or the registry holds two prepared plans for one shape.
        for name, fusion in fused:
            setattr(cls, name, fusion._resolve(cls.__name__, name, model, resolved))
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

    @classmethod
    def fusions(cls) -> dict[str, Fusion]:
        """Every declared fusion on this class, by attribute name.

        Separate from `declarations` because a fusion is not one query: the
        things that walk declarations -- the transitional-column scanner, a
        typegen pass -- want the `Select` behind each, and a fusion has one per
        search. Its searches are ordinary declarations and appear there.
        """
        found: dict[str, Fusion] = {}
        for klass in reversed(cls.__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, Fusion):
                    found[name] = value
        return found

    def __repr__(self) -> str:
        model = getattr(self.model, "__name__", "?")
        return f"<{type(self).__name__} {model} queries={len(self.declarations())}>"


def _declared_model(cls: type) -> Any:
    """The model named by `Queries[Model]`, or inherited from a base."""
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
    `Param` in an ORDER BY or a LIMIT could never be supplied by a caller, and
    a declaration nobody can call should not survive its own class body.
    """
    for value in values:
        if isinstance(value, Param):
            raise DeclarationError(
                f"{method}() cannot take a parameter: it is part of the query's "
                f"shape, and only values are bound per call"
            )
