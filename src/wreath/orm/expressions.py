"""Immutable SQL expression nodes.

Operator overloads only build nodes. Nothing here inspects a database, holds a
connection, or renders SQL text; the compiler owns rendering and the session
owns execution.

Nodes compare by identity, because `==` is overloaded to *build* an equality
predicate rather than answer one.
"""

from __future__ import annotations

from typing import Any

from .errors import DeclarationError
from .types import Json, Jsonb, Text, TextArray, _ArrayType

# Operator tokens are matched against an allowlist in the compiler, so a node
# can never inject SQL even if one is constructed directly.
EQ = "="
NE = "<>"
LT = "<"
LE = "<="
GT = ">"
GE = ">="
LIKE = "LIKE"
ILIKE = "ILIKE"
AND = "AND"
OR = "OR"
NOT = "NOT"
IS_NULL = "IS NULL"
IS_NOT_NULL = "IS NOT NULL"
IN = "IN"
NOT_IN = "NOT IN"

# JSONB and array operators. The two-word "= ANY"/"= ALL" tokens are rendered
# specially by the compiler (`value = ANY(column)`); the rest render as an
# ordinary `left <op> right` and only widen the operator allowlist.
CONTAINS = "@>"
CONTAINED_BY = "<@"
#: Geospatial tokens. Each is a `BinaryExpr` operator rather than a node type of
#: its own, for the reason `BinaryExpr` already documents for the distance and
#: text-search nodes: every walker in the compiler dispatches on that type and
#: recurses through `left`/`right`, so a new token costs three renderer cases and
#: no change in either language. `point` and `box` are function calls the
#: renderer builds from both operands, as `ts_rank` already is.
GEO_POINT = "point"
GEO_BOX = "box"
GEO_DISTANCE = "geo_distance"
#: PostGIS tier-2 tokens: the second rendering `within()` and `nearest()` gain
#: for a `Geography` column, and the containment tier 1 has no answer for.
#:
#: `GEO_KNN` is a token of its own rather than pgvector's `<->`, and the reason
#: is the refusal rather than the SQL -- both render the same two characters. An
#: unbounded `ORDER BY embedding <-> $1` is allowed today for vector search and
#: must stay allowed, while an unbounded geography KNN is refused, so the guard
#: has to be able to tell them apart *after* the node is built. A shared token
#: would make the two indistinguishable at exactly that point.
GEO_KNN = "geo_knn"
GEO_DWITHIN = "geo_dwithin"
GEO_COVERS = "geo_covers"
HAS_KEY = "?"
HAS_ANY = "?|"
HAS_ALL = "?&"
PATH_TEXT = "#>>"
PATH_JSON = "#>"
OVERLAPS = "&&"
ANY_EQ = "= ANY"
ALL_EQ = "= ALL"

# pgvector distance operators. Each yields a *number*, not a boolean, so the
# node they build is only usable in an ORDER BY or as the left side of a
# comparison -- `where()` refuses one on its own rather than letting PostgreSQL
# refuse it later with a message about the argument of WHERE.
L2_DISTANCE = "<->"
COSINE_DISTANCE = "<=>"
INNER_PRODUCT = "<#>"
L1_DISTANCE = "<+>"

# The two pgvector distances over PostgreSQL's built-in `bit` -- the query half
# of binary quantization. They are grouped apart from the four above because
# they apply to a different set of column types, not because they render
# differently: `where()` refuses all six alike.
HAMMING_DISTANCE = "<~>"
JACCARD_DISTANCE = "<%>"

DISTANCE_OPERATORS = frozenset(
    {
        L2_DISTANCE,
        COSINE_DISTANCE,
        INNER_PRODUCT,
        L1_DISTANCE,
        HAMMING_DISTANCE,
        JACCARD_DISTANCE,
        # Great-circle metres. A distance like the pgvector ones, and ordered by
        # the same way -- smallest first, with a ceiling, which is the only
        # shape an index answers.
        GEO_DISTANCE,
        # Spheroidal metres, measured by PostGIS rather than by the renderer.
        # In here for the same reason: `where()` refuses a distance on its own,
        # and tier 2 earns that refusal by being one rather than by declaring a
        # second copy of it.
        GEO_KNN,
    }
)
#: Orderings that need a `LIMIT`. Both are proximity searches, which an index
#: answers only as `ORDER BY ... LIMIT n`; every other distance in
#: `DISTANCE_OPERATORS` may be ordered by without one, and pgvector's has been
#: able to since it shipped.
BOUNDED_ORDER_OPERATORS = frozenset({GEO_DISTANCE, GEO_KNN})

# Full-text search. `@@` answers a boolean and `ts_rank` answers a relevance
# score, and both take a *tsquery* built by a parser function on the right-hand
# side -- so the token names the parser as well as the operation, the way
# "= ANY" names a quantifier. The compiler's allowlist therefore still decides
# every byte that reaches SQL, and the choice of parser is a declaration rather
# than an operand.
#
# `websearch_to_tsquery` is the default because it is the one that does not
# raise on user input: quotes, `&`, `!` and a lone `:` are all just characters
# to it. `to_tsquery` accepts operator syntax and raises on a syntax error,
# which is a fine trade when the query is written by the application and a
# 500 when it comes from a search box.
MATCHES_WEBSEARCH = "@@ websearch_to_tsquery"
MATCHES_TSQUERY = "@@ to_tsquery"
RANK_WEBSEARCH = "ts_rank websearch_to_tsquery"
RANK_TSQUERY = "ts_rank to_tsquery"
TEXT_MATCH_OPERATORS = frozenset({MATCHES_WEBSEARCH, MATCHES_TSQUERY})
TEXT_RANK_OPERATORS = frozenset({RANK_WEBSEARCH, RANK_TSQUERY})
#: The tsquery parsers `.matches()` and `.rank()` accept.
TSQUERY_PARSERS = ("websearch_to_tsquery", "to_tsquery")

ASC = "ASC"
DESC = "DESC"


class Expression:
    """Base class for everything that renders into a SQL fragment."""

    __slots__ = ()

    __hash__ = object.__hash__


class Predicate(Expression):
    """An expression that renders where a boolean is expected."""

    __slots__ = ()

    def __and__(self, other: Predicate) -> BooleanExpr:
        return BooleanExpr(AND, (self, _as_predicate(other)))

    def __or__(self, other: Predicate) -> BooleanExpr:
        return BooleanExpr(OR, (self, _as_predicate(other)))

    def __invert__(self) -> UnaryExpr:
        return UnaryExpr(NOT, self)

    def __bool__(self) -> bool:
        raise TypeError(
            "SQL predicates cannot be used in Python boolean context; "
            "combine them with & and | rather than and/or"
        )


def _ts_operator(head: str, parser: str, method: str) -> str:
    """The operator token for one text-search call, with its parser checked."""
    if parser not in TSQUERY_PARSERS:
        raise DeclarationError(
            f".{method}(parser={parser!r}) must be one of "
            f"{list(TSQUERY_PARSERS)}; websearch_to_tsquery is the default because "
            "it is the one that does not raise on user input"
        )
    return f"{head} {parser}"


def _as_predicate(value: Any) -> Predicate:
    if not isinstance(value, Predicate):
        raise TypeError(f"expected a SQL predicate, got {type(value).__name__}")
    return value


class ColumnExpr(Expression):
    """A reference to one model column."""

    __slots__ = ("column",)

    def __init__(self, column: Any) -> None:
        self.column = column

    @property
    def model(self) -> type:
        return self.column.owner

    def _compare(self, operator: str, other: Any) -> BinaryExpr:
        return BinaryExpr(operator, self, _bind(self.column, other))

    def __eq__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        if other is None:
            raise TypeError(
                "comparing a column to None is ambiguous in SQL; use .is_null()"
            )
        return self._compare(EQ, other)

    def __ne__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        if other is None:
            raise TypeError(
                "comparing a column to None is ambiguous in SQL; use .is_not_null()"
            )
        return self._compare(NE, other)

    def __lt__(self, other: Any) -> BinaryExpr:
        return self._compare(LT, other)

    def __le__(self, other: Any) -> BinaryExpr:
        return self._compare(LE, other)

    def __gt__(self, other: Any) -> BinaryExpr:
        return self._compare(GT, other)

    def __ge__(self, other: Any) -> BinaryExpr:
        return self._compare(GE, other)

    __hash__ = object.__hash__

    def like(self, pattern: str) -> BinaryExpr:
        return self._compare(LIKE, pattern)

    def ilike(self, pattern: str) -> BinaryExpr:
        return self._compare(ILIKE, pattern)

    def is_null(self) -> UnaryExpr:
        return UnaryExpr(IS_NULL, self)

    def is_not_null(self) -> UnaryExpr:
        return UnaryExpr(IS_NOT_NULL, self)

    def in_(self, values: Any) -> InExpr | InSubqueryExpr:
        """`self IN (...)` against a list of values or a one-column `Select`.

        Args:
            values: An iterable of values, rendered as `IN ($1, $2, ...)`; or a
                `Select` projecting exactly one column, rendered as
                `IN (SELECT ...)` so the filter stays in the database.

        Returns:
            The membership predicate, ready to pass to `.where()`.

        Raises:
            TypeError: `values` is neither an iterable nor a `Select`.
            ValueError: `values` is an empty list. A subquery may legitimately
                return no rows, so this applies to the list form only.
            DeclarationError: The subquery is not a shape `IN` accepts -- see
                `_check_subquery` for the four refusals and why each exists.
        """
        subquery = _as_subquery(values)
        if subquery is not None:
            return InSubqueryExpr(IN, self, _check_subquery(self.column, subquery, "in_"))
        return InExpr(IN, self, _bind_many(self.column, values))

    def not_in(self, values: Any) -> InExpr | InSubqueryExpr:
        """`self NOT IN (...)` against a list of values or a one-column `Select`.

        Takes the same operands as `in_`, with one extra refusal: a subquery
        projecting a *nullable* column. SQL's three-valued logic makes
        `NOT IN (SELECT ...)` return **no rows at all** the moment the subquery
        yields a single NULL, because `x <> NULL` is unknown rather than true.
        That passes every test written against data without NULLs and empties a
        page in production, so it is refused at the call site instead.

        Args:
            values: An iterable of values, or a `Select` projecting exactly one
                NOT NULL column.

        Returns:
            The negated membership predicate.

        Raises:
            DeclarationError: The subquery projects a nullable column, or any of
                the refusals `in_` also makes.
        """
        subquery = _as_subquery(values)
        if subquery is not None:
            checked = _check_subquery(self.column, subquery, "not_in")
            projected = checked.projection[0].column
            if projected.nullable:
                raise DeclarationError(
                    f"not_in() refuses a subquery projecting the nullable column "
                    f"{projected.owner.__name__}.{projected.python_name}: in SQL, "
                    "NOT IN (SELECT ...) matches nothing at all once the subquery "
                    "yields one NULL, so this would silently return an empty result "
                    "rather than an error -- add .where(column.is_not_null()) to the "
                    "subquery, or project a NOT NULL column"
                )
            return InSubqueryExpr(NOT_IN, self, checked)
        return InExpr(NOT_IN, self, _bind_many(self.column, values))

    # -- JSONB and array operators ----------------------------------------
    #
    # `contains`/`contained_by` work on both jsonb and array columns (the
    # operand takes the column's own type); the jsonb key operators require a
    # `Jsonb` column and the array operators require an `Array` column, both
    # checked at build time so a mistyped query fails at the call site.

    def contains(self, other: Any) -> BinaryExpr:
        """`self @> other` -- jsonb/array containment."""
        return BinaryExpr(CONTAINS, self, _bind(self.column, other))

    def contained_by(self, other: Any) -> BinaryExpr:
        """`self <@ other` -- reverse jsonb/array containment."""
        return BinaryExpr(CONTAINED_BY, self, _bind(self.column, other))

    def has_key(self, key: str) -> BinaryExpr:
        """`self ? key` -- the jsonb object has this top-level key."""
        self._require_jsonb("has_key")
        return BinaryExpr(HAS_KEY, self, _bind_as(key, Text))

    def has_any(self, keys: Any) -> BinaryExpr:
        """`self ?| keys` -- the jsonb object has any of these keys."""
        self._require_jsonb("has_any")
        return BinaryExpr(HAS_ANY, self, _bind_as(_nonempty(keys, "has_any"), TextArray))

    def has_all(self, keys: Any) -> BinaryExpr:
        """`self ?& keys` -- the jsonb object has all of these keys."""
        self._require_jsonb("has_all")
        return BinaryExpr(HAS_ALL, self, _bind_as(_nonempty(keys, "has_all"), TextArray))

    def path(self, elements: Any, *, as_json: bool = False) -> _JsonPath:
        """A jsonb path extraction completed by a comparison.

        `Model.data.path(["a", "b"]) == "x"` renders `(data #>> $1) = $2`.
        `as_json=True` extracts a sub-document with `#>` instead, usable with
        the jsonb operators.
        """
        if self.column.pg_type is not Json and self.column.pg_type is not Jsonb:
            raise DeclarationError(
                f".path() requires a json or jsonb column, not {self.column.pg_type.name}"
            )
        return _JsonPath(self, tuple(elements), as_json)

    def overlaps(self, other: Any) -> BinaryExpr:
        """`self && other` -- the arrays share at least one element."""
        self._require_array("overlaps")
        return BinaryExpr(
            OVERLAPS, self, _bind_as(_nonempty(other, "overlaps"), self.column.pg_type)
        )

    def any_eq(self, value: Any) -> BinaryExpr:
        """`value = ANY(self)` -- `value` is an element of the array."""
        element = self._require_array("any_eq")
        return BinaryExpr(ANY_EQ, _bind_as(value, element), self)

    def all_eq(self, value: Any) -> BinaryExpr:
        """`value = ALL(self)` -- every array element equals `value`."""
        element = self._require_array("all_eq")
        return BinaryExpr(ALL_EQ, _bind_as(value, element), self)

    # -- pgvector distance operators --------------------------------------
    #
    # Each renders `column <op> $n` and evaluates to a number, so it is used as
    # an ORDER BY key (the similarity search) or compared against a threshold
    # (`.cosine_distance(q) < 0.3`). Named for what they compute rather than for
    # the symbol, because `<#>` is not a thing anyone reads twice.

    def l2_distance(self, other: Any) -> BinaryExpr:
        """`self <-> other` -- Euclidean distance. Orderable."""
        return self._distance(L2_DISTANCE, "l2_distance", other)

    def cosine_distance(self, other: Any) -> BinaryExpr:
        """`self <=> other` -- cosine distance, the common default. Orderable."""
        return self._distance(COSINE_DISTANCE, "cosine_distance", other)

    def inner_product(self, other: Any) -> BinaryExpr:
        """`self <#> other` -- *negative* inner product, as pgvector defines it.

        Negative so that ordering ascending still puts the most similar row
        first, which is the whole reason pgvector spells it that way. Read a
        result of `-0.9` as an inner product of `0.9`.
        """
        return self._distance(INNER_PRODUCT, "inner_product", other)

    def l1_distance(self, other: Any) -> BinaryExpr:
        """`self <+> other` -- taxicab (L1) distance. Orderable."""
        return self._distance(L1_DISTANCE, "l1_distance", other)

    def hamming_distance(self, other: Any) -> BinaryExpr:
        """`self <~> other` -- how many bits differ. Orderable.

        The query half of binary quantization: over a `Bit` column, this counts
        the positions where two signatures disagree, which is the cheap
        stand-in for distance that lets a 32x smaller index shortlist
        candidates before the real vectors re-score them.
        """
        return self._bit_distance(HAMMING_DISTANCE, "hamming_distance", other)

    def jaccard_distance(self, other: Any) -> BinaryExpr:
        """`self <%> other` -- one minus the Jaccard similarity. Orderable.

        Set overlap rather than positional agreement: it counts the bits set in
        both against the bits set in either, so two sparse signatures that share
        their few set bits are close even though most positions agree trivially
        by being zero in both. That is the difference from
        `hamming_distance`, which those shared zeros dominate.
        """
        return self._bit_distance(JACCARD_DISTANCE, "jaccard_distance", other)

    def _distance(self, operator: str, method: str, other: Any) -> BinaryExpr:
        from .types import ExtensionType

        if not isinstance(self.column.pg_type, ExtensionType):
            raise DeclarationError(
                f".{method}() requires a Vector, Halfvec or Sparsevec column, not "
                f"{self.column.pg_type.name}"
            )
        return BinaryExpr(operator, self, _bind(self.column, other))

    def _bit_distance(self, operator: str, method: str, other: Any) -> BinaryExpr:
        from .types import BIT_OID

        # By OID rather than by class: `Bit` is a plain built-in `PgType`, not an
        # `ExtensionType`, because `bit` is PostgreSQL's own type and only these
        # two operators over it are pgvector's.
        if self.column.pg_type.oid != BIT_OID:
            raise DeclarationError(
                f".{method}() requires a Bit column, not {self.column.pg_type.name}. "
                "It is pgvector's distance over PostgreSQL's `bit`, so a `vector` "
                "column takes .l2_distance()/.cosine_distance() instead"
            )
        return BinaryExpr(operator, self, _bind(self.column, other))

    # -- full-text search --------------------------------------------------
    #
    # `matches` is the predicate and `rank` is the score, and they are separate
    # calls on purpose: PostgreSQL evaluates `ts_rank` per surviving row and it
    # cannot use the index, so a search filters with `@@` and *then* orders by
    # the rank of what survived. Writing it as one call would hide which half
    # costs what.

    def matches(self, terms: Any, *, parser: str = "websearch_to_tsquery") -> BinaryExpr:
        """`self @@ websearch_to_tsquery('<config>', terms)` -- a text-search predicate.

        The configuration comes from the column's own `TsVector` declaration, so
        the query is analysed exactly as the stored vector was; a query analysed
        under a different configuration matches nothing and looks like missing
        data.

        Args:
            terms: The search text, bound as a parameter. A `wreath.queries.Param`
                works here as it does for the distance operators.
            parser: `"websearch_to_tsquery"` (the default) or `"to_tsquery"`.
                The default never raises on user input -- `"`, `&`, `!` and a
                lone `:` are just characters to it. `to_tsquery` understands
                `&`/`|`/`!`/`<->` and raises a syntax error on malformed input,
                so choose it only where the query is the application's and the
                error is handled.

        Returns:
            The predicate, ready for `.where()`.

        Raises:
            DeclarationError: This is not a `TsVector` column, or `parser` is
                not one of the two.
        """
        self._require_tsvector("matches")
        return BinaryExpr(
            _ts_operator("@@", parser, "matches"), self, _bind_as(terms, Text)
        )

    def rank(self, terms: Any, *, parser: str = "websearch_to_tsquery") -> BinaryExpr:
        """`ts_rank(self, websearch_to_tsquery('<config>', terms))` -- a relevance score.

        A number, not a predicate: order by it (`.rank(t).desc()`) or compare it
        against a threshold. `where()` refuses one on its own, because
        PostgreSQL would refuse it later with a message about the argument of
        WHERE rather than about the line that wrote it.

        Takes the same arguments as `matches`, and should be given the same
        `terms` and `parser` -- ranking one query while filtering by another is
        a bug that looks like bad relevance.
        """
        self._require_tsvector("rank")
        return BinaryExpr(
            _ts_operator("ts_rank", parser, "rank"), self, _bind_as(terms, Text)
        )

    def _require_tsvector(self, method: str) -> None:
        from .types import TsVectorType

        if not isinstance(self.column.pg_type, TsVectorType):
            raise DeclarationError(
                f".{method}() requires a TsVector column, not "
                f"{self.column.pg_type.name}"
            )

    def _require_jsonb(self, method: str) -> None:
        if self.column.pg_type is not Jsonb:
            raise DeclarationError(
                f".{method}() requires a Jsonb column, not {self.column.pg_type.name}"
            )

    def _require_array(self, method: str) -> Any:
        pg_type = self.column.pg_type
        if not isinstance(pg_type, _ArrayType):
            raise DeclarationError(
                f".{method}() requires an Array column, not {pg_type.name}"
            )
        return pg_type.element

    def _require_point(self, method: str) -> None:
        from .types import Point

        if self.column.pg_type is not Point:
            raise DeclarationError(
                f".{method}() requires a Point column, not {self.column.pg_type.name}"
            )

    def _geography(self) -> Any:
        """The column's PostGIS `geography` type, or None for a tier-1 column.

        The dispatch point for the whole second rendering. It asks the *column*
        what it is rather than asking the caller which tier they meant, which is
        what lets a model move from `Point` to `Geography` without its queries
        changing -- the promise `Geography`'s own docstring already makes.
        """
        from .types import ExtensionType

        pg_type = self.column.pg_type
        if isinstance(pg_type, ExtensionType) and pg_type.type_name == "geography":
            return pg_type
        return None

    def covered_by(self, region: Any) -> BinaryExpr:
        """`ST_Covers(ST_GeogFromText($1), self)` — rows inside a region.

        The question tier 1 structurally cannot answer: a catchment, a council
        boundary, an exclusion zone. `region` is a `wreath.geospatial.Polygon`,
        which is built from `Coordinate`s and therefore cannot arrive with its
        longitude and latitude the wrong way round.

        `ST_Covers` rather than `ST_Contains`, and not as a preference:
        **`ST_Contains(geography, geography)` does not exist**, so a query
        reaching for it fails at the database. `ST_Covers` also answers "yes"
        for a point exactly on the boundary, which is the reading a catchment
        wants -- an address on the line is in the district.

        The polygon travels out as `text` and PostGIS lifts it, so there is no
        bind-only column type and no OID to resolve. Storing polygons is a
        different feature; this one only queries against them.

        Args:
            region: The `Polygon` to test containment in.

        Returns:
            The predicate, ready for `.where()`.

        Raises:
            DeclarationError: This is not a `Geography` column.
            TypeError: `region` is not a `Polygon`.
        """
        from ..geospatial import Polygon
        from .types import Text

        if self._geography() is None:
            raise DeclarationError(
                f".covered_by() requires a Geography column, not "
                f"{self.column.pg_type.name}; containment against a shape is the "
                "operation tier 1 does not have, because PostgreSQL's own "
                "geometry has no polygon operator a GiST index answers -- declare "
                "the column as Geography() and CREATE EXTENSION postgis"
            )
        if not isinstance(region, Polygon):
            raise TypeError(
                f"covered_by() takes a wreath.geospatial.Polygon, got "
                f"{type(region).__name__}; a BoundingBox is a rectangle in "
                "degrees and .within() already covers a circle, so a region with "
                "a shape is the only thing this adds"
            )
        return BinaryExpr(GEO_COVERS, self, _bind_as(region.wkt, Text))

    def within(self, centre: Any, metres: Any) -> Predicate:
        """Rows whose point is within `metres` of `centre`, great-circle.

        On a **`Geography`** column this is `ST_DWithin(col, $1, $2)`, which
        PostGIS plans as an `&&` index condition with the exact spheroidal test
        filtered over what survives -- literally the shape the tier-1 rendering
        below hand-builds, which is why both are spelled `within()`.

        On a `Point` column it renders as two things ANDed, and both halves are
        load-bearing:

        * `self <@ box(point(...), point(...))` -- the degree-aligned bounding
          box, which is the only form a GiST `point_ops` index can answer, and
          therefore the only reason this does not read the whole table.
        * the exact haversine distance, compared against `metres`.

        The box is a *superset* of the circle -- its corners reach about 40%
        further than its edges -- so dropping the second half would return
        rows that are not within the radius. Dropping the first half would be
        correct and would scan everything. Neither is optional.

        A circle crossing the antimeridian produces two boxes, ORed, because
        `<@` has no notion of a wrapped edge; one reaching a pole produces a
        box spanning every longitude. `wreath.geospatial.bounding_boxes` makes
        that decision and this method only renders it.
        """
        from ..geospatial import _metres, bounding_boxes
        from .types import Float64

        geography = self._geography()
        if geography is not None:
            # Two leaves under one node, because `BinaryExpr` carries two
            # operands and `ST_DWithin` takes three arguments. The renderer
            # reads this shape directly, and it emits the centre before the
            # radius -- the order the tree is walked in, which is what keeps the
            # placeholder numbering and the bind program in step.
            return BinaryExpr(
                GEO_DWITHIN,
                self,
                BinaryExpr(
                    GEO_DWITHIN,
                    _bind_as(centre, geography),
                    _bind_as(_metres(metres), Float64),
                ),
            )
        self._require_point("within")
        boxes = bounding_boxes(centre, metres)
        contained = [
            BinaryExpr(
                CONTAINED_BY,
                self,
                BinaryExpr(
                    GEO_BOX,
                    BinaryExpr(GEO_POINT, _bind_as(box.lon_min, Float64),
                               _bind_as(box.lat_min, Float64)),
                    BinaryExpr(GEO_POINT, _bind_as(box.lon_max, Float64),
                               _bind_as(box.lat_max, Float64)),
                ),
            )
            for box in boxes
        ]
        # `or_` of one predicate is that predicate, so the single-box case needs
        # no branch of its own. A mutant that always took `or_` survived, which
        # is the tool reporting a redundant condition rather than a missing test.
        box_test = or_(*contained)
        exact = BinaryExpr(
            "<=", self._distance_to(centre), _bind_as(float(metres), Float64)
        )
        return and_(box_test, exact)

    def nearest(self, centre: Any) -> BinaryExpr:
        """Distance to `centre` in metres -- an order key, not a predicate.

        `order_by(Model.at.nearest(here))` with a `limit` is the shape an
        index answers. A number rather than a boolean, exactly as the pgvector
        distances and `ts_rank` are, so `where()` refuses it on its own.

        On a **`Geography`** column this is PostGIS's `<->`, which the GiST
        index answers directly as an `Index Scan ... Order By:` -- a true
        nearest-neighbour search rather than tier 1's bounding-box
        approximation, and measured on the spheroid rather than on a sphere.
        Both answer metres, so the two tiers are comparable; they differ by the
        ~0.5% the guide states.

        On a `Point` column it renders the great-circle haversine, unchanged.
        """
        geography = self._geography()
        if geography is not None:
            return BinaryExpr(GEO_KNN, self, _bind_as(centre, geography))
        self._require_point("nearest")
        return self._distance_to(centre)

    def _distance_to(self, centre: Any) -> BinaryExpr:
        from ..geospatial import Coordinate
        from .types import Float64

        if type(centre) is not Coordinate:
            raise TypeError(
                "a geospatial query takes a Coordinate centre; build one with "
                "Coordinate(lat=..., lon=...)"
            )
        # Three leaves, not two, and the count is load-bearing. The bind
        # *program* walks this tree once and emits one value per ValueExpr
        # node, while the renderer emits one placeholder per render -- so a
        # node rendered twice would desynchronise them and the statement would
        # be prepared with more parameters than the plan supplies. The haversine
        # needs the centre's latitude twice (once in the delta, once in
        # `cos phi2`) and its longitude once, so the tree carries exactly that,
        # in exactly the order `_render_geo_distance` emits them.
        return BinaryExpr(
            GEO_DISTANCE,
            self,
            BinaryExpr(
                GEO_POINT,
                _bind_as(centre.lat, Float64),
                BinaryExpr(
                    GEO_POINT,
                    _bind_as(centre.lat, Float64),
                    _bind_as(centre.lon, Float64),
                ),
            ),
        )

    def asc(self) -> OrderExpr:
        return OrderExpr(self, ASC)

    def desc(self) -> OrderExpr:
        return OrderExpr(self, DESC)

    def __repr__(self) -> str:
        owner = getattr(self.column.owner, "__name__", "?")
        return f"<ColumnExpr {owner}.{self.column.python_name}>"


class RelatedColumnExpr(ColumnExpr):
    """A column reached by traversing relationships from the queried model.

    `Book.author.name` is one of these. It compares against `authors.name`
    while the query still selects books, so the compiler turns `path` into an
    INNER JOIN and renders this operand against that join's alias.

    Filtering is not loading: a query that only mentions `Book.author.name`
    joins `authors` to constrain rows and selects nothing from it, so
    `book.author` still raises unless the query also `.include()`s it. That
    keeps the rule that attribute access never performs I/O.

    A subclass of `ColumnExpr`, so every comparison, `in_`, and ordering
    method is inherited. Code that branches on `isinstance(node, ColumnExpr)`
    must therefore test for this type *first*.
    """

    __slots__ = ("path",)
    #: The relationship trail to this column. Declared so the type checker sees
    #: an attribute here, not the `path()` jsonb method inherited from ColumnExpr.
    path: tuple[Any, ...]

    def __init__(self, column: Any, path: tuple[Any, ...]) -> None:
        super().__init__(column)
        self.path = path

    def __repr__(self) -> str:
        trail = ".".join(item.python_name for item in self.path)
        return f"<RelatedColumnExpr {trail}.{self.column.python_name}>"


class ValueExpr(Expression):
    """One bound parameter. The value never reaches SQL text or a cache key."""

    __slots__ = ("pg_type", "value")

    def __init__(self, value: Any, pg_type: Any) -> None:
        self.value = value
        self.pg_type = pg_type

    def __repr__(self) -> str:
        return f"<ValueExpr {self.pg_type.name}>"


class BinaryExpr(Predicate):
    __slots__ = ("left", "operator", "right")

    def __init__(self, operator: str, left: Expression, right: Expression) -> None:
        self.operator = operator
        self.left = left
        self.right = right

    # A distance node -- and a text-search one -- is a `BinaryExpr` on purpose
    # rather than a class of its own: every walker in the compiler -- bind
    # collection, the generated bind program, the plan-cache key, and their C
    # counterparts -- already dispatches on this exact type and recurses through
    # `left`/`right`. A new node type would have to be taught to each of them, in
    # two languages, for a tree shape they already handle. `ts_rank(a, b)` is a
    # function call rather than an infix operator, which the *renderer* special-
    # cases by its token, exactly as it already does for "= ANY".

    @property
    def is_distance(self) -> bool:
        """Whether this node evaluates to a distance rather than a boolean."""
        return self.operator in DISTANCE_OPERATORS

    @property
    def is_rank(self) -> bool:
        """Whether this node evaluates to a text-search relevance score."""
        return self.operator in TEXT_RANK_OPERATORS

    def _require_distance(self, method: str) -> None:
        if not (self.is_distance or self.is_rank):
            raise DeclarationError(
                f".{method}() orders by a distance or a rank; {self.operator!r} "
                "yields a boolean, and ordering by one is almost never what was "
                "meant"
            )

    def asc(self) -> OrderExpr:
        """Order by this distance or rank, smallest first."""
        self._require_distance("asc")
        return OrderExpr(self, ASC)

    def desc(self) -> OrderExpr:
        """Order by this distance or rank, largest first.

        The one to reach for with `.rank()`: a relevance score is *higher* when
        the row is a better match, which is the opposite of a distance.
        """
        self._require_distance("desc")
        return OrderExpr(self, DESC)

    def __lt__(self, other: Any) -> BinaryExpr:
        return self._threshold(LT, other)

    def __le__(self, other: Any) -> BinaryExpr:
        return self._threshold(LE, other)

    def __gt__(self, other: Any) -> BinaryExpr:
        return self._threshold(GT, other)

    def __ge__(self, other: Any) -> BinaryExpr:
        return self._threshold(GE, other)

    def _threshold(self, operator: str, other: Any) -> BinaryExpr:
        from .types import Float64

        if not (self.is_distance or self.is_rank):
            raise TypeError(
                f"cannot compare a {self.operator!r} predicate against a value"
            )
        return BinaryExpr(operator, self, _bind_as(other, Float64))

    def __repr__(self) -> str:
        return f"<BinaryExpr {self.left!r} {self.operator} {self.right!r}>"


class InExpr(Predicate):
    """Membership against an explicit value list.

    Rendered as `IN ($1, $2, ...)` because the driver codecs exchange scalars
    rather than arrays; the operand count is therefore part of the query shape.
    """

    __slots__ = ("operator", "left", "values")

    def __init__(self, operator: str, left: Expression, values: tuple[ValueExpr, ...]) -> None:
        self.operator = operator
        self.left = left
        self.values = values

    def __repr__(self) -> str:
        return f"<InExpr {self.left!r} {self.operator} {len(self.values)} values>"


class InSubqueryExpr(Predicate):
    """Membership against a one-column subquery: `left IN (SELECT ...)`.

    Distinct from `InExpr` rather than a mode of it, because the two differ in
    every respect the compiler cares about: the operand count is not part of the
    query shape (the subquery's own shape is), the bound values live inside a
    nested `Select` rather than beside the operator, and the rendered text is a
    statement rather than a parenthesised list. Sharing one class would mean a
    branch at each of those points and an attribute that is meaningless in one
    of the two states.

    `select` is validated at construction by `_check_subquery`, so a node that
    exists is one the compiler can render.
    """

    __slots__ = ("left", "operator", "select")

    def __init__(self, operator: str, left: Expression, select: Any) -> None:
        self.operator = operator
        self.left = left
        self.select = select

    def __repr__(self) -> str:
        model = getattr(self.select.model, "__name__", "?")
        return f"<InSubqueryExpr {self.left!r} {self.operator} (SELECT ... FROM {model})>"


class BooleanExpr(Predicate):
    __slots__ = ("operands", "operator")

    def __init__(self, operator: str, operands: tuple[Predicate, ...]) -> None:
        self.operator = operator
        self.operands = operands

    def __repr__(self) -> str:
        return f"<BooleanExpr {self.operator} {len(self.operands)}>"


class UnaryExpr(Predicate):
    __slots__ = ("operand", "operator")

    def __init__(self, operator: str, operand: Expression) -> None:
        # Unconditionally, with no `operator == NOT` guard: the other two unary
        # operators are `IS NULL` and `IS NOT NULL`, and both are built by
        # `ColumnExpr` over a column, so the only way this walk reaches a
        # containment at all is through a negation. A mutation pass forced the
        # guard always-true and nothing objected, which is the tool reporting
        # exactly that -- and two spellings of one rule is how they drift apart.
        _refuse_negated_containment(operand)
        self.operator = operator
        self.operand = operand

    def __repr__(self) -> str:
        return f"<UnaryExpr {self.operator} {self.operand!r}>"


class OrderExpr:
    """One ORDER BY key: a column, or an expression that evaluates to one.

    `expression` is a bare `ColumnExpr` for an ordinary sort, and a
    `BinaryExpr` for the two kinds of ordering that are computed rather than
    stored -- a vector distance and a text-search rank. The compiler branches on
    which, because a computed key carries a bound value and so has to render
    through the operand renderer rather than as a quoted column name.
    """

    __slots__ = ("direction", "expression")

    def __init__(self, expression: Expression, direction: str) -> None:
        self.expression = expression
        self.direction = direction

    def __repr__(self) -> str:
        return f"<OrderExpr {self.expression!r} {self.direction}>"


def _bind(column: Any, value: Any) -> Any:
    if isinstance(value, Expression):
        return _placeholder_or_refuse(value, column.pg_type)
    # Coercing here rejects a mistyped comparison at the call site rather than
    # at execution, and guarantees the bound value matches the column's OID.
    return ValueExpr(column.pg_type.coerce(value), column.pg_type)


def _bind_many(column: Any, values: Any) -> tuple[ValueExpr, ...]:
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise TypeError("in_() requires an iterable of values, or a Select")
    # Deliberately `_bind`'s refusal rather than its placeholder seam: `in_`
    # renders one placeholder per operand, so a declared query would have to
    # promise how *many* values a parameter stands for, and that count is part
    # of the query shape rather than of the call. Kept out until there is a
    # spelling for it -- see `wreath.queries.Param`.
    bound = tuple(_bind_element(column, item) for item in values)
    if not bound:
        raise ValueError("in_() requires at least one value")
    return bound


def _bind_element(column: Any, value: Any) -> ValueExpr:
    if isinstance(value, Expression):
        raise TypeError(
            "column-to-column comparison is not supported; compare against a value"
        )
    return ValueExpr(column.pg_type.coerce(value), column.pg_type)


def _as_subquery(values: Any) -> Any:
    """`values` if it is a `Select`, else None.

    Imported here rather than at module scope because `wreath.orm.query` imports
    this module; the import is resolved once and cached by `sys.modules`.
    """
    from .query import Select

    return values if isinstance(values, Select) else None


def _check_subquery(column: Any, select: Any, method: str) -> Any:
    """Refuse every subquery shape `IN (SELECT ...)` cannot render correctly.

    Four refusals, each for a failure that would otherwise surface far from its
    cause -- as a driver error at prepare time, or as a wrong answer:

    * **Not exactly one projected column.** `IN` compares one value against one
      column. A bare `Model.select()` projects every column, which is the easy
      mistake to make, so it is named separately from a multi-column projection.
    * **Ordering, paging, or row locking.** They change which rows the subquery
      yields without changing the *set* `IN` tests against, except for `limit`,
      which does -- and whose bound value cannot be threaded into the outer
      statement's placeholder sequence at the position `IN` needs it. Refusing
      the whole group keeps the rule one sentence long instead of three.
    * **Eager loads.** A subquery projects one column; there is nothing for a
      load to attach to.
    * **Relationship traversal in the subquery's predicates.** Filtering through
      a relationship needs a JOIN planned against the registry, which is not
      available where the subquery renders.

    Args:
        column: The outer column being compared, used only in messages.
        select: The candidate subquery.
        method: `"in_"` or `"not_in"`, so the message names what the caller wrote.

    Returns:
        `select`, unchanged, once every refusal has passed.

    Raises:
        DeclarationError: The subquery is one of the four shapes above.
    """
    model = getattr(select.model, "__name__", "?")
    if not select.projection:
        raise DeclarationError(
            f"{method}() needs a subquery projecting exactly one column, but "
            f"{model}.select() projects every column -- name the one to compare "
            f"against, e.g. {model}.select({model}.id)"
        )
    if len(select.projection) != 1:
        names = ", ".join(item.column.python_name for item in select.projection)
        raise DeclarationError(
            f"{method}() needs a subquery projecting exactly one column, but this "
            f"one projects {len(select.projection)} ({names})"
        )
    if select.orderings or select.limit_ is not None or select.offset_ is not None:
        raise DeclarationError(
            f"{method}() refuses a subquery with order_by/limit/offset: `IN` tests "
            "set membership, so ordering and offset cannot change the answer, and a "
            "subquery LIMIT cannot bind its value at the position the outer "
            "statement needs it -- resolve the rows first and pass a list"
        )
    if select.for_update_:
        raise DeclarationError(
            f"{method}() refuses a subquery with for_update(): a row lock inside a "
            "membership test locks rows the outer query never returns"
        )
    if select.includes:
        raise DeclarationError(
            f"{method}() refuses a subquery with include(): it projects one column, "
            "so there is nothing for an eager load to attach to"
        )
    for predicate in select.predicates:
        _refuse_related(predicate, method)
    return select


def _refuse_negated_containment(node: Any) -> None:
    """Raise if `node` negates a `covered_by()`.

    `NOT ST_Covers(region, col)` is "everywhere else", which is every row the
    index would have excluded -- so the predicate that made the query indexable
    is the one being inverted, and PostGIS answers it with a sequential scan
    over the whole table. Refused here rather than at render time because it is
    a property of the query as written, and the caller who wrote it is who can
    fix it: filter positively, or describe the complement as a region.

    Written on the *node* rather than on `__invert__`, so `not_(...)` and a
    directly constructed `UnaryExpr` are refused by the same rule.

    Two arms, not `_refuse_related`'s five: a containment is a top-level
    predicate, so the only way one hides under a negation is inside an `AND`/
    `OR`. It cannot be the operand of a comparison -- `_threshold` refuses that,
    because a containment is not a distance -- and it cannot be under a nested
    negation, because the inner `UnaryExpr` was checked when it was built. A
    mutation pass showed both of the wider arms surviving, which is the tool
    saying the same thing.
    """
    if isinstance(node, BooleanExpr):
        for operand in node.operands:
            _refuse_negated_containment(operand)
        return
    if getattr(node, "operator", None) == GEO_COVERS:
        raise DeclarationError(
            "a containment cannot be negated: NOT ST_Covers(region, column) asks "
            "for everywhere else, which is every row the index would have "
            "excluded, so PostGIS answers it by reading the whole table. Filter "
            "positively, or describe the complement as a region of its own"
        )


def _refuse_related(node: Any, method: str) -> None:
    """Raise if `node` filters through a relationship. See `_check_subquery`."""
    if isinstance(node, RelatedColumnExpr):
        raise DeclarationError(
            f"{method}() refuses a subquery filtering through a relationship "
            f"({node.column.python_name}): that needs a JOIN planned against the "
            "registry, which a subquery predicate cannot reach -- filter on the "
            "subquery model's own columns, or resolve the rows first"
        )
    for slot in ("left", "right", "operand"):
        child = getattr(node, slot, None)
        if child is not None:
            _refuse_related(child, method)
    for slot in ("operands", "values"):
        for child in getattr(node, slot, ()) or ():
            _refuse_related(child, method)


def _bind_as(value: Any, pg_type: Any) -> Any:
    """Bind `value` against an explicit type rather than the column's own.

    The jsonb key operators take `text`/`text[]` operands and `any_eq`
    takes the array's element type, so those bind through this rather than
    `_bind` (which would coerce against the column type).
    """
    if isinstance(value, Expression):
        return _placeholder_or_refuse(value, pg_type)
    return ValueExpr(pg_type.coerce(value), pg_type)


def _placeholder_or_refuse(value: Any, pg_type: Any) -> Any:
    """A declared query's parameter, or the refusal every other node earns.

    `wreath.queries.Param` intercepts the six comparison operators by being a
    subclass of the column expression, which is why `Llama.id == Param("x")`
    works. An operator spelled as a *method* -- `cosine_distance(...)` -- never
    reaches that machinery, so the parameter arrives here as an ordinary
    expression instead. `_as_placeholder` is the seam that lets it become the
    placeholder it would have become on the operator path; anything without one
    is a column-to-column comparison and is refused exactly as before.

    Reached only when the operand *is* an expression, which is the error path
    for every ordinary call, so nothing pays for it.
    """
    maker = getattr(value, "_as_placeholder", None)
    if maker is None:
        raise TypeError(
            "column-to-column comparison is not supported; compare against a value"
        )
    return maker(pg_type)


def _nonempty(values: Any, label: str) -> list[Any]:
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise TypeError(f"{label}() requires an iterable of values")
    materialized = list(values)
    if not materialized:
        raise ValueError(f"{label}() requires at least one value")
    return materialized


class _JsonPath:
    """A pending jsonb path extraction, completed by a comparison.

    `Model.data.path(["a", "b"])` renders nothing on its own; comparing it
    (or, for `as_json` paths, applying a jsonb operator) builds the predicate.
    Text paths compare against `text` (`#>>`); json paths against `jsonb`
    (`#>`). It is intentionally unhashable and cannot be used in a boolean
    context, for the same reason column expressions cannot.
    """

    __slots__ = ("_as_json", "_column", "_elements")

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, column: ColumnExpr, elements: tuple[Any, ...], as_json: bool) -> None:
        if not elements:
            raise ValueError("path() requires at least one path element")
        self._column = column
        self._elements = elements
        self._as_json = as_json

    def _extract(self) -> BinaryExpr:
        operator = PATH_JSON if self._as_json else PATH_TEXT
        path = _bind_as([str(item) for item in self._elements], TextArray)
        return BinaryExpr(operator, self._column, path)

    def _operand_type(self) -> Any:
        return Jsonb if self._as_json else Text

    def _compare(self, operator: str, other: Any) -> BinaryExpr:
        return BinaryExpr(operator, self._extract(), _bind_as(other, self._operand_type()))

    def __bool__(self) -> bool:
        raise TypeError(
            "SQL predicates cannot be used in Python boolean context; "
            "combine them with & and | rather than and/or"
        )

    def __eq__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        return self._compare(EQ, other)

    def __ne__(self, other: Any) -> BinaryExpr:  # ty: ignore[invalid-method-override]
        return self._compare(NE, other)

    def __lt__(self, other: Any) -> BinaryExpr:
        return self._compare(LT, other)

    def __le__(self, other: Any) -> BinaryExpr:
        return self._compare(LE, other)

    def __gt__(self, other: Any) -> BinaryExpr:
        return self._compare(GT, other)

    def __ge__(self, other: Any) -> BinaryExpr:
        return self._compare(GE, other)

    def like(self, pattern: str) -> BinaryExpr:
        return self._compare(LIKE, pattern)

    def ilike(self, pattern: str) -> BinaryExpr:
        return self._compare(ILIKE, pattern)

    def contains(self, other: Any) -> BinaryExpr:
        """`(data #> path) @> other` -- containment on the sub-document."""
        if not self._as_json:
            raise DeclarationError("contains() on a path requires path(..., as_json=True)")
        return BinaryExpr(CONTAINS, self._extract(), _bind_as(other, Jsonb))

    def has_key(self, key: str) -> BinaryExpr:
        """`(data #> path) ? key` -- key test on the sub-document."""
        if not self._as_json:
            raise DeclarationError("has_key() on a path requires path(..., as_json=True)")
        return BinaryExpr(HAS_KEY, self._extract(), _bind_as(key, Text))


def and_(*predicates: Predicate) -> Predicate:
    """Combine predicates with `AND`; a single predicate passes through."""
    if not predicates:
        raise ValueError("and_() requires at least one predicate")
    if len(predicates) == 1:
        return _as_predicate(predicates[0])
    return BooleanExpr(AND, tuple(_as_predicate(item) for item in predicates))


def or_(*predicates: Predicate) -> Predicate:
    """Combine predicates with `OR`; a single predicate passes through."""
    if not predicates:
        raise ValueError("or_() requires at least one predicate")
    if len(predicates) == 1:
        return _as_predicate(predicates[0])
    return BooleanExpr(OR, tuple(_as_predicate(item) for item in predicates))


def not_(predicate: Predicate) -> UnaryExpr:
    return UnaryExpr(NOT, _as_predicate(predicate))


__all__ = [
    "ALL_EQ",
    "AND",
    "ANY_EQ",
    "ASC",
    "CONTAINED_BY",
    "CONTAINS",
    "DESC",
    "EQ",
    "GE",
    "GT",
    "HAS_ALL",
    "HAS_ANY",
    "HAS_KEY",
    "ILIKE",
    "IN",
    "IS_NOT_NULL",
    "IS_NULL",
    "LE",
    "LIKE",
    "LT",
    "MATCHES_TSQUERY",
    "MATCHES_WEBSEARCH",
    "NE",
    "NOT",
    "NOT_IN",
    "OR",
    "OVERLAPS",
    "PATH_JSON",
    "PATH_TEXT",
    "RANK_TSQUERY",
    "RANK_WEBSEARCH",
    "TEXT_MATCH_OPERATORS",
    "TEXT_RANK_OPERATORS",
    "TSQUERY_PARSERS",
    "BinaryExpr",
    "BooleanExpr",
    "ColumnExpr",
    "Expression",
    "InExpr",
    "InSubqueryExpr",
    "OrderExpr",
    "Predicate",
    "UnaryExpr",
    "ValueExpr",
    "and_",
    "not_",
    "or_",
]
