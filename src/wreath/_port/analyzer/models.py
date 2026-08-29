"""Pydantic models and dataclasses: what a `Field(...)` carries, whether a
dataclass needs `kw_only`, and which validators say nothing wreath does not."""

from __future__ import annotations

import ast

from .imports import _Imports

_STR_CONSTRAINTS = frozenset({"min_length", "max_length", "regex", "pattern"})
#: Every constraint keyword a `Field(...)` can carry. Derived from
#: `_STR_CONSTRAINTS` rather than spelled out again, so the string constraints
#: cannot be extended in one place and missed in the other.
_FIELD_CONSTRAINTS = _STR_CONSTRAINTS | frozenset({"ge", "le", "gt", "lt", "multiple_of"})
#: `Field(...)` keywords that carry the field's *default*, which a dataclass has
#: a slot for: `= <value>` or `= field(default_factory=...)`.
_FIELD_DEFAULTS = frozenset({"default", "default_factory"})
#: `Field(...)` schema keywords. Metadata with a Wreath ``Field`` home is kept
#: by the exact rule below; the remainder is documentation-only and may be
#: dropped without changing runtime behaviour.
_FIELD_DOC_ONLY = frozenset(
    {
        "description",
        "title",
        "examples",
        "example",
        "json_schema_extra",
        "deprecated",
    }
)
# Pydantic metadata with the same runtime meaning on ``wreath.binding.Field``.
# ``regex`` is the Pydantic-v1 spelling of Wreath's ``pattern``. Deliberately
# absent: ``multiple_of``, decimal digit constraints, strictness and exclusion;
# those do not have the same contract and keep the generic review verdict.
_WREATH_FIELD_METADATA = frozenset(
    {
        "alias",
        "description",
        "gt",
        "ge",
        "lt",
        "le",
        "min_length",
        "max_length",
        "pattern",
        "regex",
    }
)


#: pydantic v1 `con*` factory names, which are a constraint wearing a type's
#: clothes. Shared with `_Analyzer._annotation_is_constrained` and the emitter.
_CONSTRAINED_TYPES = frozenset(
    {
        "confloat",
        "conint",
        "constr",
        "condecimal",
        "conbytes",
        "conlist",
        "conset",
        "condate",
    }
)


# The analyzer decides a verdict and the emitter performs the rewrite, so a
# predicate the two disagree about is the worst kind of codemod bug: the report
# says "translated" and the output is not. They are defined once here and
# imported by `emit`, which already draws its other primitives from this module.


def _is_field_constraint(value: ast.AST | None, imports: _Imports) -> bool:
    """A `Field(...)` carrying at least one value or string constraint."""
    if isinstance(value, ast.Call) and imports.origin(value.func).split(".")[-1] == "Field":
        return any(k.arg in _FIELD_CONSTRAINTS for k in value.keywords)
    return False


def _is_constrained_annotation(annotation: ast.AST | None, imports: _Imports) -> bool:
    """A pydantic v1 constrained-type annotation (`confloat`, `constr`, ...)."""
    if isinstance(annotation, ast.Call):
        return imports.origin(annotation.func).split(".")[-1] in _CONSTRAINED_TYPES
    return False


def pydantic_field_rule(imports: _Imports, stmt: ast.AnnAssign) -> str:
    """The verdict one `BaseModel` field earns, by the *shape* of its `Field(...)`.

    Shared with the emitter, for the reason `query_rule` and `status_code_rule`
    are: the report and the `# TODO(wreath-port: …)` written into the source must
    not disagree about one line, and the emitter must only rewrite what the
    report calls determined.

    A dataclass field has one default slot and Wreath's ``Field`` carries its
    wire metadata, so both pieces translate when every keyword has a known home:

    * `pydantic.field` — a bare annotation, a plain default, or a `Field(...)`
      holding only `default=`/`default_factory=` and doc keywords. The marker is
      deleted and the default written as an ordinary Python default.
    * `pydantic.field_metadata_exact` — aliases and supported constraints move
      to ``Annotated[T, wreath.binding.Field(...)]`` with the default outside.
    * `pydantic.field_constraint` — an unsupported constraint such as
      ``multiple_of=`` or a v1 ``con*`` annotation stays for a human.
    * `pydantic.field_marker` — anything else the marker carries.
    """
    value = stmt.value
    if (
        isinstance(value, ast.Call)
        and imports.origin(value.func).startswith("pydantic.")
        and imports.origin(value.func).split(".")[-1] == "Field"
        and len(value.args) <= 1
        and any(keyword.arg in _WREATH_FIELD_METADATA for keyword in value.keywords)
        and all(
            keyword.arg is not None
            and keyword.arg in _FIELD_DEFAULTS | _FIELD_DOC_ONLY | _WREATH_FIELD_METADATA
            for keyword in value.keywords
        )
    ):
        return "pydantic.field_metadata_exact"
    if _is_field_constraint(value, imports) or _is_constrained_annotation(stmt.annotation, imports):
        return "pydantic.field_constraint"
    if not (isinstance(value, ast.Call) and imports.origin(value.func).split(".")[-1] == "Field"):
        return "pydantic.field"
    # `Field(default, ...)` takes the default positionally and nothing else; a
    # second positional is pydantic v1's `default_factory` slot, which this does
    # not read rather than guess at.
    if len(value.args) > 1:
        return "pydantic.field_marker"
    if any(
        keyword.arg is None or keyword.arg not in _FIELD_DEFAULTS | _FIELD_DOC_ONLY
        for keyword in value.keywords
    ):
        return "pydantic.field_marker"
    return "pydantic.field"


def field_has_default(imports: _Imports, stmt: ast.AnnAssign) -> bool:
    """Whether the *ported* field still has an `=` after it.

    Pydantic does not care what order defaulted and required fields are declared
    in; a dataclass does, and `@dataclass` raises `TypeError` at class-creation
    time for a required field that follows a defaulted one. So the order has to
    be read before the class header is written — see `dataclass_needs_kw_only`.

    What counts is the text that comes out, not what pydantic meant. A marker
    the emitter leaves alone (a constraint, an `alias=`) is still written as
    `= Field(...)`, so it occupies the defaulted position even though pydantic
    read it as required.
    """
    value = stmt.value
    if value is None:
        return False
    if isinstance(value, ast.Call) and imports.origin(value.func).split(".")[-1] == "Field":
        if pydantic_field_rule(imports, stmt) not in {
            "pydantic.field",
            "pydantic.field_metadata_exact",
        }:
            return True  # left in place, `=` and all
        if any(keyword.arg in _FIELD_DEFAULTS for keyword in value.keywords):
            return True
        # `Field(...)` with an Ellipsis first argument is pydantic's spelling of
        # "required"; `Field(0)` is a default.
        return bool(value.args) and not (
            isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis
        )
    return True


def dataclass_needs_kw_only(imports: _Imports, node: ast.ClassDef) -> list[str]:
    """The fields that would make this class an illegal `@dataclass`, in order.

    Empty when the declaration order is already legal. Non-empty means a
    required field follows a defaulted one, which pydantic accepts and
    `@dataclass` refuses — `TypeError: non-default argument 'x' follows default
    argument`, raised when the module is *imported*, so neither `ast.parse` nor
    `compile` sees it, and writing the fields in that order is perfectly
    ordinary as long as pydantic is the one reading them.
    """
    offenders: list[str] = []
    defaulted = False
    for stmt in node.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
            continue
        if stmt.target.id == "model_config":
            continue
        if field_has_default(imports, stmt):
            defaulted = True
        elif defaulted:
            offenders.append(stmt.target.id)
    return offenders


def _config_extra(value: ast.AST | None) -> str | None:
    """The `extra=` setting on a `model_config`/`Config` call, if any."""
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg == "extra" and isinstance(kw.value, ast.Constant):
                # A Constant holds any literal; only a string is an `extra=`
                # setting. Callers compare against "forbid"/"ignore", so a
                # non-string was already as good as absent.
                extra = kw.value.value
                return extra if isinstance(extra, str) else None
    return None


def _model_config_value(stmt: ast.stmt) -> ast.expr | None:
    """The right-hand side of `model_config = SettingsConfigDict(...)`, if that is this."""
    if isinstance(stmt, ast.Assign):
        if any(isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets):
            return stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        if isinstance(stmt.target, ast.Name) and stmt.target.id == "model_config":
            return stmt.value
    return None


def _literal_name_collection(node: ast.AST) -> bool:
    """Whether a projection names every selected column statically."""
    return isinstance(node, (ast.Set, ast.List, ast.Tuple)) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
    )


def pydantic_projection_rule(node: ast.Attribute, parents: dict[int, ast.AST]) -> str:
    """Classify an ormar ``get_pydantic`` projection by what can be emitted.

    A bare call or literal ``include``/``exclude`` is a declaration Wreath's
    ``model_dataclass`` represents directly. A runtime set, extra option, or a
    call nested inside another model transformer is not: the outer transform
    can change requiredness or validators after the projection was built.
    """
    call = parents.get(id(node))
    if not isinstance(call, ast.Call) or call.func is not node or call.args:
        return "pydantic.get_pydantic"
    seen: set[str] = set()
    for keyword in call.keywords:
        if (
            keyword.arg not in {"include", "exclude"}
            or keyword.arg in seen
            or not _literal_name_collection(keyword.value)
        ):
            return "pydantic.get_pydantic"
        seen.add(keyword.arg)
    if seen == {"include", "exclude"}:
        return "pydantic.get_pydantic"
    outer = parents.get(id(call))
    if isinstance(outer, ast.Call):
        return "pydantic.get_pydantic"
    return "pydantic.get_pydantic_exact"


def _literal_members(annotation: ast.AST, imports: _Imports) -> frozenset[object] | None:
    """The constant members of one ``Literal[...]`` annotation."""
    if not (
        isinstance(annotation, ast.Subscript)
        and imports.origin(annotation.value) in {"typing.Literal", "typing_extensions.Literal"}
    ):
        return None
    values = (
        annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    )
    if not all(isinstance(value, ast.Constant) for value in values):
        return None
    return frozenset(value.value for value in values if isinstance(value, ast.Constant))


def redundant_literal_validator(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[int, ast.AST],
    imports: _Imports,
) -> bool:
    """Whether a field validator only restates a ``Literal`` membership test."""
    decorator = next(
        (
            item
            for item in node.decorator_list
            if isinstance(item, ast.Call)
            and imports.origin(item.func).split(".")[-1] == "field_validator"
        ),
        None,
    )
    if decorator is None or len(decorator.args) != 1 or decorator.keywords:
        return False
    field_arg = decorator.args[0]
    if not isinstance(field_arg, ast.Constant) or not isinstance(field_arg.value, str):
        return False
    owner: ast.AST | None = parents.get(id(node))
    if not isinstance(owner, ast.ClassDef):
        return False
    field = next(
        (
            statement
            for statement in owner.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == field_arg.value
        ),
        None,
    )
    if field is None or (members := _literal_members(field.annotation, imports)) is None:
        return False
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) != 2 or not isinstance(body[0], ast.If) or not isinstance(body[1], ast.Return):
        return False
    check = body[0].test
    returned = body[1].value
    if not (
        isinstance(check, ast.Compare)
        and isinstance(check.left, ast.Name)
        and len(check.ops) == 1
        and isinstance(check.ops[0], ast.NotIn)
        and len(check.comparators) == 1
        and isinstance(check.comparators[0], (ast.Set, ast.Tuple, ast.List))
        and isinstance(returned, ast.Name)
        and returned.id == check.left.id
        and len(body[0].body) == 1
        and isinstance(body[0].body[0], ast.Raise)
        and not body[0].orelse
    ):
        return False
    compared = check.comparators[0].elts
    return (
        all(isinstance(value, ast.Constant) for value in compared)
        and frozenset(value.value for value in compared if isinstance(value, ast.Constant))
        == members
    )


def _plain_graphql_dataclass(imports: _Imports, node: ast.ClassDef) -> bool:
    """Whether a Strawberry output class is already an ordinary dataclass shape."""
    if node.bases:
        return False
    found = False
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            found = True
            if imports.origin(statement.annotation) == "strawberry.auto":
                return False
            if statement.value is not None and not isinstance(statement.value, ast.Constant):
                return False
            continue
        if isinstance(statement, ast.Pass):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        return False
    return found
