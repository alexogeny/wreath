"""One-pass validation from a request body straight into a model.

Without this there are two validation engines on the write path: request
binding checks a JSON payload against a dataclass, and then every assignment to
a model re-checks the same value through its column's `PgType`. The value is
proven twice and copied twice.

Here a body is validated *by the columns themselves*, once, directly into the
model's cells. The column type stays the single source of the type rules -- this
does not add a second set -- but it runs exactly once per field, and the value
that survives it is the value that gets bound to SQL.

The validator is **generated per model**, at route-compile time. A model's
columns are fixed the moment the class exists, so the loop that walked them per
request was re-deciding, on every field of every body, facts that were settled
at import: what the field is called, whether it is nullable, whether it has a
default. Generating the source unrolls all of that into straight-line code, and
lets a cross-field rule read the values out of local variables instead of
loading them back out of the cells they were just written to. See
`constraints.py` for the same technique applied to a single column's checks.

The database still enforces its own structural constraints (types, NOT NULL,
keys). That is deliberate: it is the backstop, not the validator. Application
rules such as enums stay in Python, so the schema stays portable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..binding import ValidationError, _error
from .constraints import CheckViolation, _compile, coercer
from .fields import MISSING
from .model import Model


def compile_model_validator(
    model: type[Model],
) -> Callable[[Any, tuple[Any, ...]], Any]:
    """Build the validator for `model`; generated once, run per request."""
    if not isinstance(model, type) or not issubclass(model, Model):
        raise TypeError(f"expected a wreath.orm Model, got {model!r}")
    if model.__wreath_table__ is None:
        raise TypeError(f"{model.__name__} is not a mapped model")

    columns = model.__wreath_columns__
    rules = model.__wreath_compiled_rules__
    # Only the fields a rule actually reads need to survive in a local; every
    # other field goes straight into its cell and is never looked at again.
    wanted = {index for rule in rules for index in rule[0]}

    ns: dict[str, Any] = {
        "_MISS": MISSING,
        "_new": model._orm_new,
        "_error": _error,
        "_kind": _kind,
        "_ValidationError": ValidationError,
        "_known": frozenset(column.python_name for column in columns),
        "_isinstance": isinstance,
        "_sorted": sorted,
        "_len": len,
        "_str": str,
    }
    body = [
        "def validate_body(payload, loc=()):",
        "    if not _isinstance(payload, dict):",
        "        raise _ValidationError(",
        "            [_error(loc, 'value is not an object', 'dict')]",
        "        )",
        "    errors = []",
        "    instance = _new()",
    ]
    for column in columns:
        body.extend(_field(column, ns, column.index in wanted))
    body.extend(_extras(len(columns)))
    body.extend(_rules(rules, ns))
    body.append("    return instance")
    body.append("")

    validate = _compile("\n".join(body), "validate_body", ns, model.__name__)
    validate.__name__ = f"validate_{model.__name__}_body"
    validate.__qualname__ = validate.__name__
    validate.__doc__ = f"Validate a request body into a {model.__name__}."
    return validate


def _field(column: Any, ns: dict[str, Any], keep: bool) -> list[str]:
    """The generated block that proves one column out of the payload."""
    index = column.index
    name = column.python_name
    ns[f"_coerce{index}"] = coercer(column.pg_type)
    ns[f"_column{index}"] = column
    where = f"(*loc, {name!r})"
    lines = []
    if keep:
        # The sentinel is what lets a rule tell "absent" from any real value,
        # including None, with an identity check rather than a lookup.
        lines.append(f"    _f{index} = _MISS")
    lines.append(f"    _value = payload.get({name!r}, _MISS)")
    lines.append("    if _value is not _MISS:")
    lines.append("        if _value is None:")
    if column.nullable:
        lines.append(f"            instance._orm_set_loaded({index}, None)")
        if keep:
            lines.append(f"            _f{index} = None")
    else:
        lines.append(
            f"            errors.append(_error({where}, 'value must not be null', 'null'))"
        )
    lines.append("        else:")
    lines.append("            try:")
    lines.append(f"                _proven = _coerce{index}(_value)")
    lines.append("            except (TypeError, ValueError, OverflowError) as error:")
    lines.append(
        f"                errors.append(_error({where}, _str(error), "
        f"_kind(error, _column{index})))"
    )
    lines.append("            else:")
    lines.extend(_checks(column, ns, where, keep, "                "))
    lines.extend(_absent(column, ns, keep))
    return lines


def _checks(
    column: Any, ns: dict[str, Any], where: str, keep: bool, indent: str
) -> list[str]:
    """The column's business rules, inlined here rather than called.

    `Column.validate` fuses the same checks into a callable that *raises*,
    because that is what an assignment needs: `intern.salary = 60_000` has one
    value and one way to refuse it. A body is the other case -- it reports every
    bad field at once, so a violation here is an expected outcome rather than an
    exceptional one, and raising through it means building an exception and a
    traceback only to call `str()` on a message that was a constant all along.

    Both are emitted from the same `Check.source`, so this is one set of
    rules with two ways of answering, not two sets that can disagree. The tests
    pin them together: what a body is refused for, an assignment is refused for.

    The first failing check wins, matching assignment -- which can only raise
    once -- so the two paths report the same violation for the same value.
    """
    index = column.index
    lines = []
    for number, check in enumerate(column.checks):
        message = f"_message{index}_{number}"
        ns[message] = check.message
        test = check.source("_proven", ns)
        branch = "if" if number == 0 else "elif"
        lines.append(f"{indent}{branch} not ({test}):")
        lines.append(f"{indent}    errors.append(_error({where}, {message}, {check.kind!r}))")
    if lines:
        lines.append(f"{indent}else:")
        indent += "    "
    lines.append(f"{indent}instance._orm_set_loaded({index}, _proven)")
    if keep:
        lines.append(f"{indent}_f{index} = _proven")
    return lines


def _absent(column: Any, ns: dict[str, Any], keep: bool) -> list[str]:
    """The generated block for a column the payload does not mention."""
    index = column.index
    name = column.python_name
    lines = ["    else:"]
    if column.default is not MISSING:
        if column.default is None:
            # A None default is the value NULL, not something to coerce: no
            # column type accepts None, so coercing it would reject the very
            # default the column declares.
            lines.append(f"        instance._orm_set_loaded({index}, None)")
            if keep:
                lines.append(f"        _f{index} = None")
            return lines
        ns[f"_default{index}"] = column.default
        ns[f"_validate{index}"] = column.validate
        produce = f"_default{index}()" if callable(column.default) else f"_default{index}"
        # The raising validator, not the inlined checks: a default that breaks
        # the column's own rules is a mistake in the declaration, not something
        # a client sent, so it is not reported as a field error against a body
        # that never mentioned the field.
        lines.append(f"        _proven = _validate{index}({produce})")
        lines.append(f"        instance._orm_set_loaded({index}, _proven)")
        if keep:
            lines.append(f"        _f{index} = _proven")
        return lines
    # A column the database fills may be omitted; anything else that is absent
    # would insert a NULL the column forbids.
    if column.nullable or column.server_default or column.primary_key:
        lines.append("        pass")
        return lines
    lines.append(
        f"        errors.append(_error((*loc, {name!r}), 'field is required', 'missing'))"
    )
    return lines


def _extras(count: int) -> list[str]:
    return [
        f"    if _len(payload) > {count} or not payload.keys() <= _known:",
        "        for extra in _sorted(payload.keys() - _known):",
        "            errors.append(_error((*loc, extra), 'unexpected field', 'extra'))",
        "    if errors:",
        "        raise _ValidationError(errors)",
    ]


def _rules(rules: tuple[Any, ...], ns: dict[str, Any]) -> list[str]:
    """The generated block for rules that span fields.

    They run only once every field is present and sound: a rule has nothing
    useful to say about a value that was already rejected, and reporting it
    anyway buries the error that matters. A rule whose columns are not all
    loaded is skipped rather than guessed at.
    """
    lines: list[str] = []
    for number, (indexes, test, message, kind, at) in enumerate(rules):
        ns[f"_rule{number}"] = test
        ns[f"_message{number}"] = message
        ns[f"_rulekind{number}"] = kind
        guard = " and ".join(f"_f{index} is not _MISS" for index in indexes)
        arguments = ", ".join(f"_f{index}" for index in indexes)
        where = f"(*loc, {at!r})" if at else "loc"
        lines.append(f"    if {guard} and not _rule{number}({arguments}):")
        lines.append(
            f"        errors.append(_error({where}, _message{number}, _rulekind{number}))"
        )
    if lines:
        lines.append("    if errors:")
        lines.append("        raise _ValidationError(errors)")
    return lines


def _kind(error: Exception, column: Any) -> str:
    """The error `type` tag: the broken rule's name, or the column's type.

    A business rule names itself (`le`, `length`, `one_of`) so a client
    can tell "that is not an integer" from "that integer is too large" without
    parsing the message.
    """
    if isinstance(error, CheckViolation):
        return error.kind
    return column.pg_type.name


__all__ = ["compile_model_validator"]
