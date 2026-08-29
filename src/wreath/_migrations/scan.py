"""Which reads survive a re-encode, and which cannot.

A deferred migration converts a column's *values* while the application keeps
serving. During that window some rows hold the old encoding and some hold the
new, and the failure this module exists to prevent is silent: a comparison
against the wrong encoding does not raise, it returns `False`. A filter
quietly drops rows, a join quietly matches nothing, a chart quietly reads low.

So every read that touches a converting column is classified before the
migration is allowed to start:

`rewritable`
    The predicate means the same thing in both encodings once it is widened to
    accept both -- and because the mapping is finite and total, this module can
    say what the widened form *is* rather than merely permitting it.
`refused`
    No correct transitional form exists. Ordered comparison is the headline
    case (`1 < 2` but `'gentle' > 'rolling'`), and grouping, ordering,
    aggregating and joining are worse, because they are not filters and a scan
    that only read `where` clauses would pass an application about to lose
    rows from a join.
`undecidable`
    The operator is visible but the value is not -- a bound parameter, a value
    that arrives with the request. Refused for the same reason: the supplied
    value could be in either encoding, so equality is no safer than ordering.

The default is refusal, and the asymmetry is the argument: a false refusal
costs an argument with the tool, and a false permission costs a data incident
that surfaces a week later. The escape is a written waiver, never a flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..orm.expressions import (
    ALL_EQ,
    ANY_EQ,
    CONTAINED_BY,
    CONTAINS,
    EQ,
    GE,
    GT,
    HAS_ALL,
    HAS_ANY,
    HAS_KEY,
    ILIKE,
    IN,
    IS_NOT_NULL,
    IS_NULL,
    LE,
    LIKE,
    LT,
    NE,
    NOT_IN,
    OVERLAPS,
    PATH_JSON,
    PATH_TEXT,
    BinaryExpr,
    BooleanExpr,
    ColumnExpr,
    InExpr,
    UnaryExpr,
    ValueExpr,
)

#: Operators whose meaning depends on the *order* of the encoding. A mapping
#: that happens to preserve order would make these safe, but "preserves order
#: over the values in the mapping" is not "preserves order over the values in
#: the table" -- see `_monotone_note`.
ORDERED = frozenset({LT, LE, GT, GE})

#: Operators that match an *encoding* rather than a value, so no widening is
#: possible in general.
PATTERN = frozenset({LIKE, ILIKE})

#: Container and path operators. Safe unless they address the declared column.
STRUCTURAL = frozenset(
    {
        CONTAINS,
        CONTAINED_BY,
        HAS_KEY,
        HAS_ANY,
        HAS_ALL,
        PATH_TEXT,
        PATH_JSON,
        OVERLAPS,
        ANY_EQ,
        ALL_EQ,
    }
)

REWRITABLE = frozenset({EQ, NE, IN, NOT_IN})

#: How a calculated view's `declared_columns` roles read in a finding.
_ROLE_NAMES = {"aggregate": "aggregate", "group": "GROUP BY", "time": "time axis"}


@dataclass(frozen=True, slots=True)
class TransitionalHazard:
    """One read that cannot be proven safe for the conversion window."""

    site: str
    column: str
    operation: str
    verdict: str
    detail: str
    rewrite: str | None = None
    waiver: str | None = None

    @property
    def waived(self) -> bool:
        return self.waiver is not None

    def explain(self) -> str:
        head = f"{self.site}: {self.operation} on {self.column}"
        if self.waived:
            return f"{head} -- waived: {self.waiver}"
        return f"{head} -- {self.detail}"


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Everything the scan looked at, and what it concluded.

    `examined` distinguishes a clean result from a scan that inspected no
    declarations. `scanned_nothing` makes that absence of evidence explicit.
    """

    column: str
    examined: int
    hazards: tuple[TransitionalHazard, ...] = ()
    rewrites: tuple[TransitionalHazard, ...] = ()
    scanned_nothing: bool = False
    #: `"recode"` scans; `"retype"` has no re-encode window to scan.
    shape: str = "recode"

    @property
    def blocking(self) -> tuple[TransitionalHazard, ...]:
        return tuple(item for item in self.hazards if not item.waived)

    @property
    def waived(self) -> tuple[TransitionalHazard, ...]:
        return tuple(item for item in self.hazards if item.waived)

    def explain(self) -> str:
        if self.shape == "retype":
            return (
                f"{self.column}: a retype has no re-encode window -- both columns "
                f"are separately typed throughout, an unconverted row reads NULL, "
                f"and the gate proves those gone before anything narrows."
            )
        if self.scanned_nothing:
            return (
                f"{self.column}: nothing was scanned -- no declared query, model "
                f"check or calculated view names this column. That is not the "
                f"same as safe: an inline predicate in a handler is invisible "
                f"here until the source analyser lands."
            )
        parts = [f"{self.column}: examined {self.examined} read(s)"]
        if self.blocking:
            parts.append(f"{len(self.blocking)} unsafe")
        if self.waived:
            parts.append(f"{len(self.waived)} waived")
        if self.rewrites:
            parts.append(f"{len(self.rewrites)} rewritable")
        return ", ".join(parts)


# Shaped after passes' `Declared`: a written reason, no `strict=False`, no
# global off switch. The waiver attaches to the read that needs it rather than
# to the migration, because a blanket waiver on a migration is indistinguishable
# from not having the feature.

#: Waivers declared against a *site name* rather than an object, keyed
#: `(column, site)`. Both halves are stable strings, which is the whole
#: reason this exists -- see `waive_transitional`.
_SITE_WAIVERS: dict[tuple[str, str], str] = {}


def _require_reason(reason: str) -> str:
    if not reason or not reason.strip():
        raise ValueError(
            "a transitional waiver requires a reason explaining why this read is "
            "safe across the conversion window; there is no unreasoned waiver"
        )
    return reason.strip()


def transitional_read(column: Any, *, reason: str) -> Any:
    """Waive the transitional check for one read, with a written reason.

    The reason is not decoration. It appears in `wreath migrations check`
    output as a count and in `--json` in full, so "we waived everything" is
    visible in review rather than discovered afterwards.

    This form decorates a function. A *declared* query is a slotted value with
    nowhere to hang an attribute, so it takes `waive_transitional`
    instead; the error below says so rather than failing obscurely.
    """
    written = _require_reason(reason)
    name = _column_key(column)

    def decorate(target: Any) -> Any:
        existing = getattr(target, "__wreath_transitional__", ())
        try:
            target.__wreath_transitional__ = (*existing, (name, written))
        except AttributeError as error:
            raise TypeError(
                f"{type(target).__name__} cannot carry a waiver as an attribute; "
                f"declare it by name instead -- "
                f"waive_transitional({name!r}, site='Class.attribute', reason=...)"
            ) from error
        return target

    return decorate


def waive_transitional(column: Any, *, site: str, reason: str) -> None:
    """Waive one read named by its declared site, for reads that are values.

    A `QueryDeclaration` is immutable and slotted, so there is nothing to
    decorate. Keying on the declared *name* rather than on object identity is
    deliberate: the name is the thing the declaration sells, it is stable across
    a reload, and `id()` is not an identity -- a lesson this codebase has paid
    for twice.
    """
    key = _column_key(column) if not isinstance(column, str) else column
    _SITE_WAIVERS[(key, site)] = _require_reason(reason)


def clear_waivers() -> None:
    """Drop every site-named waiver. For tests."""
    _SITE_WAIVERS.clear()


def waiver_for(target: Any, column: str, site: str | None = None) -> str | None:
    """The reason waiving *column* on *target* or at *site*, if one was declared."""
    for name, reason in getattr(target, "__wreath_transitional__", ()) or ():
        if name == column:
            return reason
    if site is not None:
        return _SITE_WAIVERS.get((column, site))
    return None


def _column_key(column: Any) -> str:
    """`schema.table.column` for a model column or a column expression."""
    inner = getattr(column, "column", column)
    owner = getattr(inner, "owner", None)
    if owner is None:
        raise TypeError(f"expected a model column, got {column!r}")
    table = getattr(owner, "__wreath_table__", None) or owner.__name__.lower()
    schema = getattr(owner, "__wreath_schema__", None) or "public"
    return f"{schema}.{table}.{inner.database_name}"


def _is_target(expression: Any, column: Any) -> bool:
    """Does this expression name the column being converted?"""
    if not isinstance(expression, ColumnExpr):
        return False
    return expression.column is column


def _literals(node: Any) -> tuple[list[Any], bool]:
    """Every literal in *node*, and whether any operand was dynamic.

    A `Placeholder` from a declared query's `Param` is dynamic: the operator
    is visible but the value is not, and a value that arrives with a request
    could be in either encoding.
    """
    values: list[Any] = []
    dynamic = False
    for item in node if isinstance(node, (list, tuple)) else (node,):
        if isinstance(item, ValueExpr):
            values.append(item.value)
        else:
            dynamic = True
    return values, dynamic


def _monotone_note(mapping: dict[Any, Any]) -> str:
    """Why an order-preserving mapping still does not license a comparison.

    The check itself is easy -- sort the keys, sort the values, compare orders.
    What it cannot establish is *totality*: a row holding a value the mapping
    does not mention breaks the ordering regardless, and finding rows the
    mapping does not cover is much of why a re-encode is being run at all.

    So the exemption is deliberately not taken. The honest escape is the
    waiver, which costs one line, states the claim in words, and is counted in
    review -- strictly better than a permission inferred from an unproven
    premise.
    """
    keys = list(mapping)
    values = [mapping[key] for key in keys]
    try:
        monotone = sorted(range(len(keys)), key=lambda i: keys[i]) == sorted(
            range(len(keys)), key=lambda i: values[i]
        )
    except TypeError:
        monotone = False
    if not monotone:
        return "the mapping does not preserve order, so no ordered comparison survives"
    return (
        "the mapping happens to preserve order, but that is only proven over the "
        "values it names -- a row holding an unmapped value breaks it, and "
        "unmapped rows are much of why a re-encode is run. Waive this read with "
        "a reason if you know the mapping is total"
    )


def _classify_binary(
    node: BinaryExpr, column: Any, mapping: dict[Any, Any], site: str, name: str
) -> TransitionalHazard | None:
    operator = node.operator
    left_is_target = _is_target(node.left, column)
    right_is_target = _is_target(node.right, column)
    if not (left_is_target or right_is_target):
        return None

    if operator in ORDERED:
        return TransitionalHazard(
            site=site,
            column=name,
            operation=f"ordered comparison ({operator})",
            verdict="refused",
            detail=_monotone_note(mapping),
        )
    if operator in PATTERN:
        return TransitionalHazard(
            site=site,
            column=name,
            operation=f"pattern match ({operator})",
            verdict="refused",
            detail=(
                "a pattern matches an encoding, and there is no general widening "
                "of one pattern into two"
            ),
        )
    if operator in STRUCTURAL:
        return TransitionalHazard(
            site=site,
            column=name,
            operation=f"structural operator ({operator})",
            verdict="refused",
            detail=(
                "the operator addresses the value's structure, which the conversion is changing"
            ),
        )
    if operator in (EQ, NE):
        operand = node.right if left_is_target else node.left
        values, dynamic = _literals(operand)
        if dynamic:
            return TransitionalHazard(
                site=site,
                column=name,
                operation=f"comparison against a bound value ({operator})",
                verdict="undecidable",
                detail=(
                    "the operator is visible but the value is not, and a value "
                    "supplied at request time could be in either encoding"
                ),
            )
        return _widen(values, operator, mapping, site, name)
    return None


def _classify_in(
    node: InExpr, column: Any, mapping: dict[Any, Any], site: str, name: str
) -> TransitionalHazard | None:
    if not _is_target(node.left, column):
        return None
    values, dynamic = _literals(list(node.values))
    if dynamic:
        return TransitionalHazard(
            site=site,
            column=name,
            operation=f"membership against bound values ({node.operator})",
            verdict="undecidable",
            detail="a value supplied at request time could be in either encoding",
        )
    return _widen(values, node.operator, mapping, site, name)


def _widen(
    values: list[Any], operator: str, mapping: dict[Any, Any], site: str, name: str
) -> TransitionalHazard:
    """Both encodings of every literal, or a refusal naming the one that is missing."""
    inverse = {new: old for old, new in mapping.items()}
    widened: list[Any] = []
    unknown: list[Any] = []
    for value in values:
        if value in mapping:
            widened.extend((value, mapping[value]))
        elif value in inverse:
            widened.extend((inverse[value], value))
        else:
            unknown.append(value)
    if unknown:
        return TransitionalHazard(
            site=site,
            column=name,
            operation=f"comparison against an unmapped value ({operator})",
            verdict="refused",
            detail=(
                f"the mapping does not mention {unknown[0]!r}, so this read "
                f"cannot be widened to cover both encodings"
            ),
        )
    ordered = list(dict.fromkeys(widened))
    verb = "NOT IN" if operator in (NE, NOT_IN) else "IN"
    return TransitionalHazard(
        site=site,
        column=name,
        operation=f"comparison ({operator})",
        verdict="rewritable",
        detail="widens to accept both encodings",
        rewrite=f"{name.rsplit('.', 1)[-1]} {verb} ({', '.join(repr(v) for v in ordered)})",
    )


def _walk(
    node: Any, column: Any, mapping: dict[Any, Any], site: str, name: str
) -> list[TransitionalHazard]:
    found: list[TransitionalHazard] = []
    if isinstance(node, BooleanExpr):
        for operand in node.operands:
            found.extend(_walk(operand, column, mapping, site, name))
    elif isinstance(node, UnaryExpr):
        if node.operator in (IS_NULL, IS_NOT_NULL):
            if _is_target(node.operand, column) and (None in mapping or None in mapping.values()):
                found.append(
                    TransitionalHazard(
                        site=site,
                        column=name,
                        operation=f"null test ({node.operator})",
                        verdict="refused",
                        detail="the mapping introduces or removes nulls",
                    )
                )
        else:
            found.extend(_walk(node.operand, column, mapping, site, name))
    elif isinstance(node, InExpr):
        hazard = _classify_in(node, column, mapping, site, name)
        if hazard is not None:
            found.append(hazard)
    elif isinstance(node, BinaryExpr):
        hazard = _classify_binary(node, column, mapping, site, name)
        if hazard is not None:
            found.append(hazard)
        else:
            for side in (node.left, node.right):
                if isinstance(side, (BinaryExpr, BooleanExpr, UnaryExpr, InExpr)):
                    found.extend(_walk(side, column, mapping, site, name))
    return found


def _ordering_hazards(
    orderings: Any, column: Any, site: str, name: str
) -> list[TransitionalHazard]:
    found = []
    for ordering in orderings or ():
        if _is_target(getattr(ordering, "expression", None), column):
            found.append(
                TransitionalHazard(
                    site=site,
                    column=name,
                    operation="ORDER BY",
                    verdict="refused",
                    detail=(
                        "a half-converted sort is not merely wrong, it is unstable "
                        "between requests as the pass advances"
                    ),
                )
            )
    return found


def _join_hazard(column: Any, registry: Any, name: str) -> TransitionalHazard | None:
    """A join key mid-conversion silently fails to match, so rows vanish."""
    if getattr(column, "references", None) is None and not _is_referenced(column, registry):
        return None
    return TransitionalHazard(
        site="model declaration",
        column=name,
        operation="join key",
        verdict="refused",
        detail=(
            "this column is a foreign key or the target of one; a join key "
            "mid-conversion silently fails to match and rows vanish from an "
            "inner join, which no where-clause scan would catch"
        ),
    )


def _is_referenced(column: Any, registry: Any) -> bool:
    for spec in getattr(registry, "specs", ()) or ():
        for item in getattr(spec, "columns", ()) or ():
            reference = getattr(item, "references", None)
            if reference is not None and getattr(reference, "column", None) is column:
                return True
    return False


def _check_hazard(column: Any, name: str) -> TransitionalHazard | None:
    """A `check=` constraint validates one encoding and will reject the other."""
    if not getattr(column, "checks", ()):
        return None
    return TransitionalHazard(
        site="model declaration",
        column=name,
        operation="check constraint",
        verdict="refused",
        detail=(
            "the column carries a check that validates assignments; it accepts "
            "one encoding and will reject the other for the length of the window"
        ),
    )


def scan_predicates(
    predicates: Any, column: Any, mapping: dict[Any, Any], *, site: str
) -> list[TransitionalHazard]:
    """Classify every read of *column* inside *predicates*."""
    name = _column_key(column)
    found: list[TransitionalHazard] = []
    for predicate in predicates or ():
        found.extend(_walk(predicate, column, mapping, site, name))
    return found


def scan_select(
    select: Any, column: Any, mapping: dict[Any, Any], *, site: str
) -> list[TransitionalHazard]:
    """A declared query: its predicates and its ordering."""
    name = _column_key(column)
    found = scan_predicates(getattr(select, "predicates", ()), column, mapping, site=site)
    found.extend(_ordering_hazards(getattr(select, "orderings", ()), column, site, name))
    return found


def scan_view(
    view: Any, column: Any, mapping: dict[Any, Any], *, site: str
) -> list[TransitionalHazard]:
    """A calculated view: its filters, and what it does to the column besides filter.

    `wreath.series` exposes `declared_columns` for exactly this reader,
    tagging each column `time` / `aggregate` / `group`. A grouped chart
    over a half-converted column shows one category forking into two, with
    nothing raising.
    """
    name = _column_key(column)
    found = scan_predicates(getattr(view, "predicates", ()), column, mapping, site=site)
    reasons = {
        "aggregate": ("arithmetic over two encodings is meaningless even when both are numeric"),
        "group": (
            "one logical group splits into two, so counts halve and a category "
            "appears to fork, with nothing raising"
        ),
        "time": "the bucketing column is being converted underneath the spine",
    }
    for role, expression in getattr(view, "declared_columns", ()) or ():
        if _is_target(expression, column):
            found.append(
                TransitionalHazard(
                    site=site,
                    column=name,
                    operation=_ROLE_NAMES[role],
                    verdict="refused",
                    detail=reasons[role],
                )
            )
    return found


def scan(
    column: Any,
    mapping: dict[Any, Any],
    *,
    registry: Any = None,
    queries: Any = (),
    views: Any = (),
) -> ScanReport:
    """Every population, against one converting column.

    *queries* are `Queries` subclasses, *views* are `Series`/`Aggregate`
    declarations, and *registry* supplies the model declarations. Discovery is
    deliberately a separate concern -- see `collect_populations` -- so the
    lattice can be tested with no global state at all.
    """
    inner = getattr(column, "column", column)
    name = _column_key(inner)
    hazards: list[TransitionalHazard] = []
    examined = 0

    check = _check_hazard(inner, name)
    if check is not None:
        hazards.append(check)
        examined += 1
    if registry is not None:
        join = _join_hazard(inner, registry, name)
        if join is not None:
            hazards.append(join)
            examined += 1

    for holder in queries or ():
        declarations = holder.declarations() if hasattr(holder, "declarations") else {}
        for attribute, declaration in declarations.items():
            select = getattr(declaration, "_select", None)
            if select is None:
                continue
            examined += 1
            site = f"{holder.__name__}.{attribute}"
            found = scan_select(select, inner, mapping, site=site)
            hazards.extend(_apply_waivers(found, declaration, name, site))

    for view in views or ():
        examined += 1
        site = getattr(view, "name", None) or repr(view)
        hazards.extend(scan_view(view, inner, mapping, site=site))

    rewrites = tuple(item for item in hazards if item.verdict == "rewritable")
    unsafe = tuple(item for item in hazards if item.verdict != "rewritable")
    return ScanReport(
        column=name,
        examined=examined,
        hazards=unsafe,
        rewrites=rewrites,
        scanned_nothing=examined == 0,
    )


def _apply_waivers(
    found: list[TransitionalHazard], target: Any, column: str, site: str | None = None
) -> list[TransitionalHazard]:
    reason = waiver_for(target, column, site) if target is not None else None
    if reason is None:
        return found
    return [item if item.verdict == "rewritable" else _waive(item, reason) for item in found]


def _waive(hazard: TransitionalHazard, reason: str) -> TransitionalHazard:
    return TransitionalHazard(
        site=hazard.site,
        column=hazard.column,
        operation=hazard.operation,
        verdict=hazard.verdict,
        detail=hazard.detail,
        rewrite=hazard.rewrite,
        waiver=reason,
    )


def collect_declarations(modules: Any) -> tuple[Any, ...]:
    """Every deferred-migration declaration reachable from *modules*.

    Declarations live beside the model rather than in a migration file, because
    this repository generates artifacts from the catalog and has no authored
    migration to hang a decorator on. So finding them means looking where they
    are written.
    """
    from .deferred import Recode, Retype

    found: list[Any] = []
    seen: set[int] = set()
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, (Recode, Retype)) and id(value) not in seen:
                seen.add(id(value))
                found.append(value)
    return tuple(found)


def collect_populations(modules: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Declared query sets and calculated views reachable from *modules*.

    Discovery is separated from the lattice on purpose: walking loaded modules
    is a heuristic, and a heuristic that silently returns nothing is exactly the
    failure `ScanReport` reports as `scanned_nothing`.
    """
    from ..queries import Queries

    query_sets: list[Any] = []
    views: list[Any] = []
    try:
        from ..series import Aggregate, Series

        view_types: tuple[type, ...] = (Series, Aggregate)
    except ImportError:  # pragma: no cover - deferred so a cycle degrades rather than fails
        # Only an import failure means "there are no views to look for". A
        # NameError or TypeError from `series`'s module body is a bug, and
        # swallowing it would make every view invisible to the scan while
        # `scanned_nothing` stayed silent about why.
        view_types = ()

    seen: set[int] = set()
    for module in modules:
        for value in vars(module).values():
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, type) and issubclass(value, Queries) and value is not Queries:
                query_sets.append(value)
            elif view_types and isinstance(value, view_types):
                views.append(value)
    return tuple(query_sets), tuple(views)


def scan_application(registry: Any = None, modules: Any = None) -> list[ScanReport]:
    """One report per deferred declaration reachable from the loaded application.

    This is the `wreath migrations check` reader. It needs no database: the
    scan is startup work over declarations that are already values, which is why
    it can run in CI against an application that has never connected to
    anything.
    """
    import sys  # only this reader walks loaded modules

    loaded = (
        tuple(
            module
            for name, module in list(sys.modules.items())
            if module is not None and not name.startswith(("wreath.", "_", "test"))
        )
        if modules is None
        else tuple(modules)
    )
    declarations = collect_declarations(loaded)
    queries, views = collect_populations(loaded)
    return [
        declaration.scan(registry=registry, queries=queries, views=views)
        for declaration in declarations
    ]
