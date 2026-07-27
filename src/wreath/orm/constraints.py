"""Business rules, layered onto the column types inside the same single pass.

A column type answers *what a value is*: an `int8` is a 64-bit integer, and
nothing else gets in. It cannot answer *what a value is allowed to be here*: an
intern's salary is an `int8`, but an intern's salary above 50,000 is a
business mistake, not a type error.

This module adds that second question without adding a second engine. A check
is **fused into the column's own coercion**, so a column with rules is still one
call on the write path -- the same one call a column without rules costs today.
`compile_column_validator` generates that fused function; the loop in
`validation.py` never learns that constraints exist.

Three layers, in the order they run:

1. the column's `PgType.coerce` -- the type, unchanged and still the single
   source of the type rules;
2. the column's `Check` chain -- per-field business rules;
3. the model's `Rule` set -- whole-object rules over several fields.

Layers 1 and 2 are fused together and run on *every* write: the constructor,
attribute assignment, and the request body validator. Layer 3 needs an object
that is finished, so it runs where a whole object is proven at once.

Rules only ever accumulate. `narrow` appends checks to a column inherited
from a base class and cannot remove one, so every `Intern` that validates is
by construction a valid `Employee`. Widening is not expressible.
"""

from __future__ import annotations

import linecache
import math
import re
from collections.abc import Callable, Iterable
from itertools import count
from typing import Any

from .errors import DeclarationError
from .types import PgType

#: PostgreSQL type names that support `<` and friends in Python.
_ORDERED = frozenset(
    {"int2", "int4", "int8", "float4", "float8", "date", "timestamp", "timestamptz"}
)
#: Type names whose Python value has a length.
_SIZED = frozenset({"text", "varchar", "bytea"})
#: Type names whose Python value is a string.
_TEXTUAL = frozenset({"text", "varchar"})


class CheckViolation(ValueError):
    """A value of the right type that breaks a business rule.

    This is a `ValueError` on purpose. Every seam that already handles a
    rejected assignment -- the body validator's per-field `except`, a
    constructor call, a plain `obj.field = value` -- handles a broken business
    rule the same way, with no new except clause anywhere.
    """

    __slots__ = ("kind",)

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        #: The error `type` tag reported for this failure, e.g. `"le"`.
        self.kind = kind


# -- checks --------------------------------------------------------------------


class Check:
    """One business rule over a single value that has already passed its type.

    A check never sees a value of the wrong type: coercion runs first, so
    `Le(50_000)` on an `Int64` column compares two integers and can assume
    it. That is why a check can compile down to a bare comparison.

    Subclasses implement `source`, which returns a Python *expression* that
    is true when the value is acceptable. Returning source rather than a
    predicate is what lets the whole chain fuse into one function with no call
    per rule.
    """

    __slots__ = ("kind", "message")

    #: The error `type` tag this check reports, e.g. `"le"`. Set by each
    #: subclass; annotated rather than assigned, so it stays a slot.
    kind: str
    #: The message reported when the check fails. A constant, which is why the
    #: generated code can inline it.
    message: str

    #: PostgreSQL type names this check can be applied to; None accepts any.
    supports: frozenset[str] | None = None

    def source(self, var: str, ns: dict[str, Any]) -> str:
        """Python source for an expression that is true when `var` is valid.

        `ns` is the namespace the generated function closes over; a check that
        needs a value it cannot write as a literal binds it there with
        `_bind` and refers to it by the returned name.
        """
        raise NotImplementedError

    def check_type(self, pg_type: Any, where: str) -> None:
        """Reject a check applied to a type it cannot mean anything for."""
        if self.supports is not None and pg_type.name not in self.supports:
            raise DeclarationError(
                f"{where}: {type(self).__name__} cannot apply to a "
                f"{pg_type.name} column"
            )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.message}>"


class _Comparison(Check):
    """The four ordering bounds, which differ only in their operator."""

    __slots__ = ("bound",)

    supports = _ORDERED
    _operator = ""
    _phrasing = ""

    def __init__(self, bound: Any) -> None:
        self.bound = bound
        self.kind = type(self).__name__.lower()
        self.message = f"value must be {self._phrasing} {bound}"

    def source(self, var: str, ns: dict[str, Any]) -> str:
        return f"{var} {self._operator} {_constant(ns, self.bound, 'bound')}"


class Ge(_Comparison):
    """`value >= bound`."""

    __slots__ = ()
    _operator = ">="
    _phrasing = "at least"


class Gt(_Comparison):
    """`value > bound`."""

    __slots__ = ()
    _operator = ">"
    _phrasing = "greater than"


class Le(_Comparison):
    """`value <= bound`."""

    __slots__ = ()
    _operator = "<="
    _phrasing = "at most"


class Lt(_Comparison):
    """`value < bound`."""

    __slots__ = ()
    _operator = "<"
    _phrasing = "less than"


class Length(Check):
    """Bound the length of a string or `bytes` value."""

    __slots__ = ("maximum", "minimum")

    supports = _SIZED

    def __init__(self, minimum: int | None = None, maximum: int | None = None) -> None:
        if minimum is None and maximum is None:
            raise DeclarationError("Length() needs a minimum, a maximum, or both")
        for name, value in (("minimum", minimum), ("maximum", maximum)):
            if value is not None and (type(value) is not int or value < 0):
                raise DeclarationError(f"Length({name}=) must be a non-negative int")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise DeclarationError(
                f"Length(minimum={minimum}, maximum={maximum}) can never hold"
            )
        self.minimum = minimum
        self.maximum = maximum
        self.kind = "length"
        if minimum is None:
            self.message = f"length must be at most {maximum}"
        elif maximum is None:
            self.message = f"length must be at least {minimum}"
        elif minimum == maximum:
            self.message = f"length must be exactly {minimum}"
        else:
            self.message = f"length must be between {minimum} and {maximum}"

    def source(self, var: str, ns: dict[str, Any]) -> str:
        ns["_len"] = len
        size = f"_len({var})"
        if self.minimum is None:
            return f"{size} <= {self.maximum}"
        if self.maximum is None:
            return f"{size} >= {self.minimum}"
        # One chained comparison, which evaluates len() once.
        return f"{self.minimum} <= {size} <= {self.maximum}"


class Pattern(Check):
    """Require a string to contain a match for a regular expression.

    The pattern is searched, not anchored: anchor it yourself with `^` and
    `$` when that is what you mean.
    """

    __slots__ = ("regex",)

    supports = _TEXTUAL

    def __init__(self, pattern: str | re.Pattern[str]) -> None:
        try:
            self.regex = re.compile(pattern)
        except re.error as error:
            raise DeclarationError(f"Pattern({pattern!r}) is not a regex: {error}") from None
        self.kind = "pattern"
        self.message = f"value must match {self.regex.pattern!r}"

    def source(self, var: str, ns: dict[str, Any]) -> str:
        search = _bind(ns, "search", self.regex.search)
        return f"{search}({var}) is not None"


class OneOf(Check):
    """Restrict a column to a fixed set of values.

    This is how an enum stays in Python. The database keeps enforcing the type;
    which of its values are meaningful is an application rule, so the schema
    stays portable.
    """

    __slots__ = ("allowed",)

    def __init__(self, *values: Any) -> None:
        if not values:
            raise DeclarationError("OneOf() needs at least one allowed value")
        try:
            self.allowed = frozenset(values)
        except TypeError:
            raise DeclarationError(
                f"OneOf() values must be hashable; got {values!r}"
            ) from None
        self.kind = "one_of"
        self.message = f"value must be one of {', '.join(sorted(map(repr, values)))}"

    def check_type(self, pg_type: Any, where: str) -> None:
        for value in self.allowed:
            try:
                pg_type.coerce(value)
            except (TypeError, ValueError, OverflowError) as error:
                raise DeclarationError(
                    f"{where}: OneOf value {value!r} is not a valid "
                    f"{pg_type.name}: {error}"
                ) from None

    def source(self, var: str, ns: dict[str, Any]) -> str:
        return f"{var} in {_constant(ns, self.allowed, 'allowed')}"


class Predicate(Check):
    """An arbitrary rule over one value: the escape hatch.

    The function is called with the coerced value and returns true when it is
    acceptable. Prefer the specific checks where they fit -- they compile to a
    comparison, while this compiles to a call.
    """

    __slots__ = ("function",)

    def __init__(
        self,
        function: Callable[[Any], bool],
        message: str,
        *,
        kind: str = "predicate",
    ) -> None:
        if not callable(function):
            raise DeclarationError(f"Predicate() needs a callable, got {function!r}")
        self.function = function
        self.kind = kind
        self.message = message

    def source(self, var: str, ns: dict[str, Any]) -> str:
        return f"{_bind(ns, 'predicate', self.function)}({var})"


# -- declarations collected by ModelMeta ---------------------------------------


class Narrow:
    """A subclass tightening a rule on a column it inherited.

    Declared in a class body with `narrow`. The base's checks still run,
    first; these are appended. There is deliberately no way to drop an inherited
    check, so a narrowed model always satisfies the model it narrows.
    """

    __slots__ = ("checks", "field")

    def __init__(self, field: str, checks: tuple[Check, ...]) -> None:
        self.field = field
        self.checks = checks

    def __repr__(self) -> str:
        return f"<Narrow {self.field} {list(self.checks)}>"


def narrow(field: str, *checks: Check) -> Narrow:
    """Add checks to an inherited column, for this model only:

    ```python
    class Intern(Employee, table="interns"):
        salary_cap = narrow("salary", Le(50_000))
    ```
    The attribute name is documentation; the metaclass finds these by type. The
    base's own checks are unaffected and still run first.
    """
    if not isinstance(field, str) or not field:
        raise DeclarationError(f"narrow() needs a column name, got {field!r}")
    if not checks:
        raise DeclarationError(f"narrow({field!r}) declares no checks")
    for item in checks:
        if not isinstance(item, Check):
            raise DeclarationError(
                f"narrow({field!r}) takes checks from wreath.orm.constraints, got {item!r}"
            )
    return Narrow(field, checks)


class Rule:
    """A business rule spanning more than one column.

    Declared in a class body with `rule`. Unlike a check, a rule needs an
    object that is finished, so it runs once the fields are all in -- not on
    assignment, where the other fields may not have values yet.
    """

    __slots__ = ("at", "fields", "kind", "message", "test")

    def __init__(
        self,
        fields: tuple[str, ...],
        test: Callable[..., bool],
        message: str,
        kind: str,
        at: str | None,
    ) -> None:
        self.fields = fields
        self.test = test
        self.message = message
        self.kind = kind
        self.at = at

    def __repr__(self) -> str:
        return f"<Rule {self.kind} over {', '.join(self.fields)}>"


def rule(
    *fields: str,
    message: str | None = None,
    at: str | None = None,
    name: str | None = None,
) -> Callable[[Callable[..., bool]], Rule]:
    """Declare a whole-object rule over several columns:

    ```python
    class Intern(Employee, table="interns"):
        @rule("salary", "tenure_months")
        def pay_band(salary: int, tenure_months: int) -> bool:
            "an intern past six months cannot be paid more than 40k"
            return not (tenure_months > 6 and salary > 40_000)
    ```
    The function takes the named columns' validated values, in the order named,
    and returns true when the object is acceptable. It is a plain function, not
    a method: it receives values rather than an instance, so it never touches a
    descriptor and cannot read a column it did not declare.

    The docstring is the error message unless `message` is given. By default
    the error is reported against the object; `at="salary"` reports it against
    one field instead, which is usually what a form wants.
    """
    if not fields:
        raise DeclarationError("rule() needs at least one column name")
    for field in fields:
        if not isinstance(field, str) or not field:
            raise DeclarationError(f"rule() takes column names, got {field!r}")
    if at is not None and at not in fields:
        raise DeclarationError(
            f"rule(at={at!r}) must name one of the rule's own columns: "
            f"{', '.join(fields)}"
        )

    def declare(function: Callable[..., bool]) -> Rule:
        if not callable(function):
            raise DeclarationError(f"rule() decorates a function, got {function!r}")
        # A callable need not be a function: a lambda has no useful __name__ and
        # a callable object has none at all.
        given = getattr(function, "__name__", None) or "rule"
        text = message or (getattr(function, "__doc__", None) or "").strip().split("\n")[0].strip()
        if not text:
            raise DeclarationError(
                f"rule {given!r} needs a message: give it a docstring or pass message="
            )
        return Rule(fields, function, text, name or given, at)

    return declare


# -- compilation ---------------------------------------------------------------

_SEQUENCE = count()


def _literal(value: Any) -> str | None:
    """Source for `value` as an inline constant, or None if it needs a name."""
    kind = type(value)
    if kind is bool or kind is int or kind is str or kind is bytes:
        return repr(value)
    # repr() of a float round-trips exactly, but 'nan' and 'inf' are names
    # rather than literals and would compile to a NameError.
    if kind is float and math.isfinite(value):
        return repr(value)
    return None


def _bind(ns: dict[str, Any], label: str, value: Any) -> str:
    """Put `value` in the generated function's namespace under a fresh name."""
    name = f"_{label}_{len(ns)}"
    ns[name] = value
    return name


def _constant(ns: dict[str, Any], value: Any, label: str) -> str:
    """Source for `value`: a literal where possible, otherwise a bound name.

    A literal becomes a `LOAD_CONST`, which is why the common case -- an
    integer bound like `Le(50_000)` -- costs nothing beyond the comparison.
    """
    return _literal(value) or _bind(ns, label, value)


def _compile(source: str, name: str, ns: dict[str, Any], origin: str) -> Any:
    """Compile generated source, keeping it readable in a traceback.

    Registering the source with `linecache` under a unique pseudo-filename is
    what makes a failure inside a generated validator show the generated line
    rather than a bare `<string>`.
    """
    filename = f"<wreath.orm.constraints:{origin}:{next(_SEQUENCE)}>"
    linecache.cache[filename] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        filename,
    )
    exec(compile(source, filename, "exec"), ns)  # noqa: S102 - see module docstring
    function = ns[name]
    function.__wreath_source__ = source
    return function


def coercer(pg_type: PgType) -> Callable[[Any], Any]:
    """The type's coercion as one call rather than two.

    `PgType.coerce` is a wrapper whose whole body is `self._coerce(value)`.
    On the write path that wrapper is a Python call per field that does no work,
    and it was the single biggest cost in validating a body -- more than the
    checks it wraps. When a type has not overridden `coerce`, the wrapper is
    provably equivalent to the function inside it, so the inner one is used and
    the rules are unchanged. A subclass that *does* override `coerce` keeps
    its override, because then the wrapper is no longer just a wrapper.
    """
    if type(pg_type).coerce is PgType.coerce:
        return pg_type._coerce
    return pg_type.coerce


def compile_column_validator(column: Any, owner: str) -> Callable[[Any], Any]:
    """Fuse a column's type and its checks into one callable.

    Returns `pg_type.coerce` itself when the column has no checks, so a plain
    column keeps costing exactly one call and this whole module stays off its
    path. With checks, the generated function is still one call: the checks
    become comparisons inside it rather than calls of their own.

    The result is the *only* thing that proves a value for this column. Both the
    native and the pure storage assign through it, and the body validator runs
    it, so the type rules and the business rules cannot drift apart -- there is
    one function and three callers.
    """
    coerce = coercer(column.pg_type)
    checks: tuple[Check, ...] = column.checks
    if not checks:
        return coerce

    where = f"{owner}.{column.python_name}"
    ns: dict[str, Any] = {"_coerce": coerce, "_CheckViolation": CheckViolation}
    lines = ["def _validate(value):", "    value = _coerce(value)"]
    for check in checks:
        check.check_type(column.pg_type, where)
        test = check.source("value", ns)
        message = _constant(ns, check.message, "message")
        kind = _constant(ns, check.kind, "kind")
        lines.append(f"    if not ({test}):")
        lines.append(f"        raise _CheckViolation({message}, {kind})")
    lines.append("    return value")
    lines.append("")

    validate = _compile("\n".join(lines), "_validate", ns, where)
    validate.__name__ = f"validate_{owner}_{column.python_name}"
    validate.__qualname__ = validate.__name__
    validate.__doc__ = f"Coerce and check a value for {where}."
    return validate


#: A compiled rule: the values to gather, the test, and how to report a failure.
CompiledRule = tuple[tuple[int, ...], Callable[..., bool], str, str, str | None]


def compile_rules(model: Any) -> tuple[CompiledRule, ...]:
    """Resolve each rule's column names to storage indexes, once.

    Names are looked up here, at class-creation time, so a rule over a column
    that does not exist is a declaration error rather than a surprise on the
    first request that happens to hit it.
    """
    columns = model.__wreath_column_map__
    compiled: list[CompiledRule] = []
    for item in model.__wreath_rules__:
        indexes = []
        for field in item.fields:
            column = columns.get(field)
            if column is None:
                raise DeclarationError(
                    f"{model.__name__}: rule {item.kind!r} names column {field!r}, "
                    f"which {model.__name__} does not declare"
                )
            indexes.append(column.index)
        compiled.append((tuple(indexes), item.test, item.message, item.kind, item.at))
    return tuple(compiled)


def check_rules(instance: Any) -> list[tuple[str, str, str | None]]:
    """Run a model's rules against a finished object.

    Returns one `(message, kind, at)` per broken rule, empty when the object
    is acceptable. A rule whose columns are not all loaded is skipped rather
    than guessed at: a field that is absent has already been reported by
    whoever was proving the fields, and a rule cannot say anything useful about
    a value that is not there.
    """
    broken: list[tuple[str, str, str | None]] = []
    for indexes, test, message, kind, at in type(instance).__wreath_compiled_rules__:
        values = []
        for index in indexes:
            if not instance._orm_is_loaded(index):
                break
            values.append(instance._orm_get(index))
        else:
            if not test(*values):
                broken.append((message, kind, at))
    return broken


def collect_checks(declared: Iterable[Check] | Check | None, where: str) -> tuple[Check, ...]:
    """Normalize a `check=` argument into a tuple, rejecting junk early."""
    if declared is None:
        return ()
    if isinstance(declared, Check):
        return (declared,)
    if isinstance(declared, (str, bytes)) or not isinstance(declared, Iterable):
        raise DeclarationError(
            f"{where}: check= takes a Check or a sequence of them, got {declared!r}"
        )
    checks = tuple(declared)
    for item in checks:
        if not isinstance(item, Check):
            raise DeclarationError(
                f"{where}: check= takes Checks from wreath.orm.constraints, got {item!r}"
            )
    return checks


__all__ = [
    "Check",
    "CheckViolation",
    "Ge",
    "Gt",
    "Le",
    "Length",
    "Lt",
    "Narrow",
    "OneOf",
    "Pattern",
    "Predicate",
    "Rule",
    "check_rules",
    "compile_column_validator",
    "compile_rules",
    "narrow",
    "rule",
]
