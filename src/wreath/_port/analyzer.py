"""Static (never-import-the-target) FastAPI/Pydantic/ormar/SQLModel analyzer.

Design 07's load-bearing constraint: the source cannot be imported (private deps,
import-time side effects), so this walks `ast` only. Two passes: (1) index every
module's classes by framework base across the whole tree so body-params and query
targets resolve cross-module; (2) classify constructs into findings.
"""
from __future__ import annotations

import ast
import builtins
import os
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from .._conditional import STATUS_WITHOUT_BODY as _STATUS_WITHOUT_BODY
from .ir import Finding, Report, SkippedFile
from .rules import RULES

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
_MARKER_RULE = {
    "Query": "param.query",
    "Path": "param.path",
    "Header": "param.header",
    "Cookie": "param.cookie",
    "Form": "param.form",
    "File": "param.file",
}
_STR_CONSTRAINTS = frozenset({"min_length", "max_length", "regex", "pattern"})
#: Every constraint keyword a `Field(...)` can carry. Derived from
#: `_STR_CONSTRAINTS` rather than spelled out again, so the string constraints
#: cannot be extended in one place and missed in the other.
_FIELD_CONSTRAINTS = _STR_CONSTRAINTS | frozenset({"ge", "le", "gt", "lt", "multiple_of"})
#: `Field(...)` keywords that carry the field's *default*, which a dataclass has
#: a slot for: `= <value>` or `= field(default_factory=...)`.
_FIELD_DEFAULTS = frozenset({"default", "default_factory"})
#: `Field(...)` keywords that describe the field for a schema generator rather
#: than change its value. Wreath's OpenAPI has no per-field description slot
#: (`openapi.py` documents operations, not properties), so these are dropped —
#: which is a documentation loss, never a runtime one, and so does not hold the
#: field back. Every *other* keyword does: see `pydantic_field_rule`.
_FIELD_DOC_ONLY = frozenset({
    "description", "title", "examples", "example", "json_schema_extra", "deprecated",
})

# ormar QuerySet verb -> rule. The verb immediately after `.objects.` is the one
# that names the shape of the rewrite; a trailing `.all()`/`.get_or_none()` is
# just how ormar spells "now run it", which wreath spells `session.fetch(...)`.
_QUERY_RULE = {
    "filter": "orm.query.filter",
    "exclude": "orm.query.filter",
    "get_or_none": "orm.query.get_or_none",
    "get": "orm.query.get",
    "create": "orm.query.create",
    "all": "orm.query.all",
    "select_related": "orm.query.eager",
    "select_all": "orm.query.select_all",
    "prefetch_related": "orm.query.eager",
    "values": "orm.query.values",
    "values_list": "orm.query.values",
    "fields": "orm.query.values",
    "bulk_create": "orm.query.bulk",
    "bulk_update": "orm.query.bulk",
    "count": "orm.query.count",
    "exists": "orm.query.exists",
    "delete": "orm.query.delete",
    "first": "orm.query.first",
    "last": "orm.query.first",
    "get_or_create": "orm.query.get_or_create",
    "update_or_create": "orm.query.get_or_create",
    # `order_by` heads a chain often enough to deserve its own verdict. Without
    # one it fell through to the generic `orm.query`, which is *unsupported* —
    # so the one shape wreath handles best (an explicitly ordered read) was
    # reported as the shape it cannot do at all.
    "order_by": "orm.query.order",
    "limit": "orm.query",
    "offset": "orm.query",
}


# ormar keyword lookup -> the wreath comparison it becomes, as an operator.
LOOKUP_OPERATOR: dict[str, str] = {
    "": "==", "exact": "==", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
}
# ormar keyword lookup -> the column method it becomes, and how the value is
# spelled inside it. `%s` is the value as written.
#
# The pattern lookups were held back for a long time on the grounds that
# wrapping a value in wildcards is "a decision about what the author meant".
# It is not: ormar's own `icontains` compiles to `ILIKE '%' || value || '%'`,
# so writing the same thing is a translation of what they wrote and nothing
# more. Holding them back left 170 ordinary searches for a human to redo.
LOOKUP_METHOD: dict[str, tuple[str, str]] = {
    "in": ("in_", "%s"),
    "iexact": ("ilike", "%s"),
    "contains": ("like", 'f"%%{%s}%%"'),
    "icontains": ("ilike", 'f"%%{%s}%%"'),
    "startswith": ("like", 'f"{%s}%%"'),
    "istartswith": ("ilike", 'f"{%s}%%"'),
    "endswith": ("like", 'f"%%{%s}"'),
    "iendswith": ("ilike", 'f"%%{%s}"'),
    "jsonb_contains": ("contains", "%s"),
    "jsonb_contained_by": ("contained_by", "%s"),
    "jsonb_has_key": ("has_key", "%s"),
    "jsonb_has_any": ("has_any", "%s"),
    "jsonb_has_all": ("has_all", "%s"),
}
# `__isnull` is the odd one: which method it becomes depends on the *value*,
# so it only translates when that value is written out.
_NULL_METHOD = {True: "is_null", False: "is_not_null"}
_MECHANICAL_LOOKUPS = frozenset(LOOKUP_OPERATOR) | frozenset(LOOKUP_METHOD) | {"isnull"}

# Chain verbs that keep a mechanical head mechanical, and how their own
# arguments have to look. `first`/`last` are absent because wreath makes the
# ordering explicit and the source has none to carry over; `delete` because a
# bulk delete has no query form; `values` because the rows come back as models
# rather than dicts, which the caller sees; `update` because it is a write.
#
#   "kwargs"   — keyword filters, checked the same way the head's are
#   "value"    — one argument carried across untouched (limit/offset)
#   "columns"  — string literals naming columns, which resolve to `Model.<col>`
#   "relations" — string literals naming relations, one `.include(...)` each
_MECHANICAL_TAIL: dict[str, str] = {
    "all": "kwargs",
    "count": "kwargs",
    "exists": "kwargs",
    "get_or_none": "kwargs",
    # A second `filter` is another `.where(...)`; wreath ands them together the
    # same way ormar does. `exclude` is deliberately absent — it negates, which
    # is a different call, and the translated message does not describe it.
    "filter": "kwargs",
    "limit": "value",
    "offset": "value",
    "order_by": "columns",
    # `filter(a=1).select_related("owner")` is two rewrites that are each
    # already determined on their own — a `.where(...)` and an `.include(...)`.
    # Leaving them off this table meant that putting two translatable calls next
    # to each other produced a verdict neither of them earns, and putting them
    # next to each other is how a query chain is usually written.
    "select_related": "relations",
    "prefetch_related": "relations",
    "delete": "empty",
    "update": "write_values",
}

# Head verb -> the rule to use when the arguments are mechanical. A verb absent
# here is never auto-translated whatever its arguments look like.
_QUERY_EXACT_RULE = {
    "filter": "orm.query.filter_exact",
    "get_or_none": "orm.query.get_or_none_exact",
    "get": "orm.query.get_exact",
    "create": "orm.query.create_exact",
    "all": "orm.query.all",
    "count": "orm.query.count",
    "exists": "orm.query.exists",
    "order_by": "orm.query.order_exact",
    "select_related": "orm.query.eager_exact",
    "prefetch_related": "orm.query.eager_exact",
    "limit": "orm.query.page_exact",
    "offset": "orm.query.page_exact",
}


def _eager_names_are_literal(call: ast.Call | None) -> bool:
    """Whether `select_related(...)` names relations this analyzer can resolve.

    `select_related("llama")` is `.include(Model.llama.selectin())` — a
    rename. `select_all()` is not: it means "every relation", and wreath has
    no such switch, so the set has to be written out by someone who knows which
    ones the caller actually reads. A `a__b` trail is a nested include and a
    non-literal is a runtime name; neither is resolved here.
    """
    if call is None or not call.args or call.keywords:
        return False
    return all(
        isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and "__" not in argument.value
        for argument in call.args
    )

# Tail verbs that only a specific head makes mechanical. `first` is the case:
# `orm.query.first` is held back because "first without an order is not
# deterministic" — an objection that does not apply when the head *is* the
# order. Keyed by head so `filter(a=1).first()` keeps the old verdict.
_TAIL_NEEDS_HEAD: dict[str, frozenset[str]] = {
    "first": frozenset({"order_by"}),
    "last": frozenset(),                  # reversing the declared order is a decision
}


def split_lookup(keyword: str) -> tuple[str, str]:
    """`("name", "icontains")` for `name__icontains`; `("name", "")` for `name`."""
    column, separator, suffix = keyword.rpartition("__")
    if not separator or suffix not in _MECHANICAL_LOOKUPS:
        return keyword, ""
    return column, suffix


def _lookup_is_mechanical(
    keyword: str,
    value: ast.expr | None = None,
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
) -> bool:
    """Whether `filter(<keyword>=v)` becomes a wreath predicate on its own.

    A bare column name is an equality test. A name with `__` is a column plus
    a lookup *only* when the trailing segment is one wreath has a spelling for;
    otherwise it is a relation traversal (`owner__name`) or a container lookup
    (`tags__jsonb_has_any`). The container lookup needs an operator someone
    chooses. The relation does *not* need a join chosen — `Model.owner.name`
    is a `RelatedColumnExpr` and the compiler emits the INNER JOIN itself — it
    needs the relation *resolved*, and `owner`'s target model is usually
    declared in another module. Reading the suffix is what separates them, and
    getting it wrong in the permissive direction would emit an attribute chain
    against a column that is not a relation at all.

    `__isnull` is the one lookup whose *value* decides the answer: `is_null()`
    and `is_not_null()` are different calls, so a variable there is unreadable.
    """
    column, suffix = split_lookup(keyword)
    if not suffix:
        return "__" not in keyword or _resolved_column_path(
            model, keyword, relations or {}, columns or {}
        )
    if not column:
        return False
    if "__" in column and not _resolved_column_path(
        model, column, relations or {}, columns or {}
    ):
        return False
    if suffix == "isnull":
        return isinstance(value, ast.Constant) and value.value in _NULL_METHOD
    return True


def _resolved_column_path(
    model: str,
    path: str,
    relations: dict[str, dict[str, str]],
    columns: dict[str, set[str]],
) -> bool:
    """Whether ``owner__name`` is a tree-proven relationship column path."""
    parts = path.split("__")
    current = model
    if not current or len(parts) < 2:
        return False
    for relationship in parts[:-1]:
        target = relations.get(current, {}).get(relationship)
        if target is None:
            return False
        current = target
    return parts[-1] in columns.get(current, set())


def _call_is_mechanical(
    call: ast.Call | None,
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
    plain_mappings: frozenset[str] = frozenset(),
) -> bool:
    """Whether every argument of a `.objects.<verb>(...)` call maps across.

    Positional arguments are never mechanical: in ormar they are `Q` objects
    or raw clauses, and neither has a form this analyzer can read. `**kwargs`
    likewise — the keys are a runtime value, so there is nothing to check.
    """
    if call is None:
        return True                       # `.objects.all` with no call at all
    if call.args:
        return False
    return all(
        (
            keyword.arg is None
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id in plain_mappings
        )
        or (
            keyword.arg is not None
            and _lookup_is_mechanical(
                keyword.arg,
                keyword.value,
                model=model,
                relations=relations,
                columns=columns,
            )
        )
        for keyword in call.keywords
    )


def plain_filter_mappings(
    call: ast.Call | None, parents: dict[int, ast.AST]
) -> frozenset[str]:
    """Names of ``**mapping`` arguments proven to contain plain field keys.

    This is deliberately a tiny data-flow proof, not an ormar dictionary
    compatibility layer. A mapping starts from a literal/dict constructor and
    may gain literal subscript keys or literal ``update`` keys in the same
    function. A parameter, dynamic key, aliasing call, or unknown assignment
    makes it unreadable and keeps the query under review.
    """
    if call is None:
        return frozenset()
    wanted = {
        keyword.value.id
        for keyword in call.keywords
        if keyword.arg is None and isinstance(keyword.value, ast.Name)
    }
    if not wanted:
        return frozenset()
    ancestor: ast.AST | None = call
    while ancestor is not None and not isinstance(
        ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        ancestor = parents.get(id(ancestor))
    if not isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return frozenset()
    parameters = {
        argument.arg
        for argument in (
            *ancestor.args.posonlyargs,
            *ancestor.args.args,
            *ancestor.args.kwonlyargs,
        )
    }
    safe: set[str] = set()
    unsafe = wanted & parameters

    def plain_key(value: ast.AST | None) -> bool:
        return (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and "__" not in value.value
        )

    def plain_dict(value: ast.AST | None) -> bool:
        if isinstance(value, ast.Dict):
            return all(plain_key(key) for key in value.keys)
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "dict"
            and not value.args
            and all(
                keyword.arg is not None and "__" not in keyword.arg
                for keyword in value.keywords
            )
        )

    for node in ast.walk(ancestor):
        target: ast.AST | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id in wanted:
            (safe if plain_dict(value) else unsafe).add(target.id)
            continue
        if isinstance(node, ast.AugAssign):
            augmented = node.target
            if isinstance(augmented, ast.Name) and augmented.id in wanted:
                unsafe.add(augmented.id)
            elif (
                isinstance(augmented, ast.Subscript)
                and isinstance(augmented.value, ast.Name)
                and augmented.value.id in wanted
            ):
                unsafe.add(augmented.value.id)
            continue
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id in wanted
        ):
            (safe if plain_key(target.slice) else unsafe).add(target.value.id)
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in wanted
        ):
            valid = (
                not node.args
                and all(
                    keyword.arg is not None and "__" not in keyword.arg
                    for keyword in node.keywords
                )
            ) or (
                len(node.args) == 1
                and not node.keywords
                and plain_dict(node.args[0])
            )
            (safe if valid else unsafe).add(node.func.value.id)
            continue
        if isinstance(node, ast.Call) and node is not call:
            escaped = {
                argument.id
                for argument in node.args
                if isinstance(argument, ast.Name) and argument.id in wanted
            }
            escaped.update(
                keyword.value.id
                for keyword in node.keywords
                if isinstance(keyword.value, ast.Name) and keyword.value.id in wanted
            )
            unsafe.update(escaped)
    return frozenset((safe & wanted) - unsafe)


def query_rule(
    verb: str | None,
    call: ast.Call | None = None,
    tail: tuple[tuple[str, ast.Call | None], ...] = (),
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
    plain_mappings: frozenset[str] = frozenset(),
) -> str:
    """The rule for a `.objects.<verb>` chain, or the generic one.

    Shared with the emitter so the `# TODO(wreath-port: …)` it writes into the
    source says exactly what the report said — a porter grepping one and reading
    the other must not find two different verdicts for the same line.

    `call` and `tail` are what promote a verb from "here is the shape of the
    rewrite" to "here is the rewrite": the arguments have to carry across
    unchanged, and every verb layered on top has to as well. Called without them
    the answer is the conservative one, so a caller that cannot see the chain
    never gets a translated verdict by omission.
    """
    base = _QUERY_RULE.get(verb or "", "orm.query")
    exact = _QUERY_EXACT_RULE.get(verb or "")
    if exact is None:
        return base
    if verb == "order_by":
        # The head's own arguments are column names, not filters.
        if not _tail_step_is_mechanical("order_by", call):
            return base
    elif verb in ("limit", "offset"):
        if not _tail_step_is_mechanical(verb, call):
            return base
    elif verb in ("select_related", "prefetch_related"):
        if not _eager_names_are_literal(call):
            return base
    elif verb == "create":
        if call is None or call.args:
            return base
    elif not _call_is_mechanical(
        call,
        model=model,
        relations=relations,
        columns=columns,
        plain_mappings=plain_mappings,
    ):
        return base
    if not all(
        _tail_step_is_mechanical(
            step,
            node,
            verb or "",
            model=model,
            relations=relations,
            columns=columns,
            plain_mappings=plain_mappings,
        )
        for step, node in tail
    ):
        return base
    return exact


def _tail_step_is_mechanical(
    verb: str,
    call: ast.Call | None,
    head: str = "",
    *,
    model: str = "",
    relations: dict[str, dict[str, str]] | None = None,
    columns: dict[str, set[str]] | None = None,
    plain_mappings: frozenset[str] = frozenset(),
) -> bool:
    """Whether a verb chained after the head carries across with its arguments.

    Checked rather than assumed: `.all(name__icontains=x)` is a filter wearing
    a terminal's name, and treating the verb alone as safe would let the lookup
    the head check exists to catch through the back door.

    `head` is what lets `first` be mechanical after `order_by` and nowhere
    else — the verb alone does not decide it.
    """
    if verb in _TAIL_NEEDS_HEAD:
        return head in _TAIL_NEEDS_HEAD[verb]
    kind = _MECHANICAL_TAIL.get(verb)
    if kind is None:
        return False
    if call is None:
        return True                       # referenced, not called
    if kind == "kwargs":
        return _call_is_mechanical(
            call,
            model=model,
            relations=relations,
            columns=columns,
            plain_mappings=plain_mappings,
        )
    if kind == "value":
        # `limit(n)`/`offset(n)`: one argument, whatever it is, carried across.
        return len(call.args) == 1 and not call.keywords
    if kind == "empty":
        return not call.args and not call.keywords
    if kind == "write_values":
        return not call.args and bool(call.keywords)
    if kind == "relations":
        return _eager_names_are_literal(call)
    # `order_by("name")` / `order_by("-created")` resolve to `Model.<col>` and
    # `.desc()`. A non-literal is a runtime column name, which is a lookup this
    # analyzer cannot do.
    return bool(call.args) and not call.keywords and all(
        isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        for argument in call.args
    )


def chain_tail(
    head: ast.AST, parents: dict[int, ast.AST]
) -> tuple[tuple[str, ast.Call | None], ...]:
    """The verbs applied after the head of a `.objects.<head>(...)` chain.

    `Model.objects.filter(x=1).all()` bills once, at `filter` — so whether
    that finding is honest depends on what came after it, and the head node
    cannot see downstream without walking back up the tree. Each verb comes back
    with its own call, because a tail verb's arguments decide as much as the
    head's do.
    """
    steps: list[tuple[str, ast.Call | None]] = []
    current = parents.get(id(head))
    while isinstance(current, ast.Call):
        parent = parents.get(id(current))
        if not isinstance(parent, ast.Attribute):
            break
        outer = parents.get(id(parent))
        steps.append((parent.attr, outer if isinstance(outer, ast.Call) else None))
        current = outer
    return tuple(steps)


def parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """`id(child) -> parent` for one module, built in a single walk."""
    return {
        id(child): parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

# arrow module-level constructors that are a straight rename onto
# `wreath.temporal`. Anything else on the module (`Arrow(...)`, `interval`,
# `Arrow.range`) needs a look, so it bills separately rather than riding on
# these.
_ARROW_RENAMES = frozenset({"utcnow", "now", "get", "fromtimestamp", "fromdatetime"})

# cachetools stores that map onto a wreath cache.
_CACHE_STORES = frozenset({"TTLCache", "LRUCache", "LFUCache", "Cache", "FIFOCache"})

# fastapi.responses / starlette.responses classes wreath ships an equivalent of.
_RESPONSE_CLASSES = frozenset({
    "JSONResponse", "HTMLResponse", "PlainTextResponse", "RedirectResponse",
    "StreamingResponse", "FileResponse", "ORJSONResponse", "UJSONResponse",
})

# Response classes wreath ships that are not in the fastapi.responses set above.
# A handler already returning one of these carries its own status, so a route
# `status_code=` in front of it was dead before the port.
_EXTRA_RESPONSE_CLASSES = frozenset({"Response", "TextResponse", "SSEResponse"})


#: HTTP status literal -> the `wreath.exceptions` class a `HTTPException(...)`
#: with that status becomes. Shared with the emitter so the report cannot call a
#: status translated that the emitter then annotates: 502/503/501 and the rest
#: have no class, and earn `exc.http_unmapped` instead.
#:
#: 500 maps to the *base* class deliberately — `wreath.exceptions.HTTPException`
#: declares `status = 500`, so `HTTPException(status_code=500, detail=x)` is
#: `HTTPException(x)` and nothing is left to decide. It is the single most common
#: spelling of a 500 by some distance, and it used to fall through to a
#: needs-review annotation over a name whose import the emitter had dropped.
STATUS_EXCEPTION: dict[int, str] = {
    400: "BadRequest", 401: "Unauthorized", 403: "Forbidden", 404: "NotFound",
    405: "MethodNotAllowed", 409: "Conflict", 413: "PayloadTooLarge",
    422: "UnprocessableEntity", 429: "TooManyRequests",
    431: "RequestHeaderFieldsTooLarge", 500: "HTTPException",
}

# Declarative fastapi.security schemes, all of which become an auth backend.
_SECURITY_SCHEMES = frozenset({
    "HTTPBearer", "HTTPBasic", "HTTPDigest", "APIKeyHeader", "APIKeyQuery",
    "APIKeyCookie", "OAuth2PasswordBearer", "OAuth2AuthorizationCodeBearer",
})

# Alembic operations shaped like something `wreath migrations detect` derives from
# the ORM image. Scoped to what detection actually covers — tables, columns,
# primary keys, unique constraints, foreign keys and btree indexes
# (docs/from-fastapi/alembic.md, "What `detect` sees — and what it doesn't yet").
# Being in this set only makes an operation a *candidate*; the arguments decide.
_MIG_DERIVED_OPS = frozenset({
    "add_column", "drop_column", "create_table", "drop_table",
    "create_index", "drop_index", "alter_column",
    "create_unique_constraint", "create_primary_key", "create_foreign_key",
})
# A rename reads as drop+create to an image differ, which would move no data.
_MIG_RENAME_OPS = frozenset({"rename_table"})
# Operations naming an object the ORM cannot declare, or whose kind the call does
# not say (`drop_constraint("uq_x", "t")` — unique? check? exclusion?).
_MIG_REVIEW_OPS = frozenset({
    "create_check_constraint", "drop_constraint", "create_exclude_constraint",
})
# `sa.<T>` / `postgresql.<T>` column types that have a wreath PgType
# (wreath/orm/types.py). Time, Interval, Enum, INET, TSVECTOR, HSTORE, MONEY and
# a bare `CHAR(n)` are absent on purpose: there is no PgType to derive them from.
#
# Numeric/DECIMAL used to be on that absent list and no longer belong there —
# `wreath.orm.types.Numeric` ships, so every money column was being told to stay
# in Alembic over a type wreath has had all along. That is the specific way this
# table goes stale, and it is why each entry is a name that was checked against
# `orm/types.py` rather than remembered.
_SA_MODELLED_TYPES = frozenset({
    "Integer", "INTEGER", "BigInteger", "BIGINT", "SmallInteger", "SMALLINT",
    "String", "VARCHAR", "Text", "TEXT", "Unicode", "UnicodeText",
    "Boolean", "BOOLEAN", "Float", "REAL", "DOUBLE_PRECISION",
    "Numeric", "NUMERIC", "DECIMAL",
    "Date", "DATE", "DateTime", "TIMESTAMP", "LargeBinary", "BYTEA",
    "UUID", "JSON", "JSONB", "ARRAY",
})
# Fully-qualified column types that are a wreath type wearing a foreign name.
# `ormar.fields.sqlalchemy_uuid.CHAR` is ormar's own UUID column — the module
# exists only to store a UUID as text on backends without a uuid type — and it
# is how every Alembic revision generated from an ormar model spells a UUID
# primary key, so it is by far the most common column type in a generated
# revision. Reading it as "an unmodelled CHAR" keeps a large share of the
# migrations in Alembic over what is really a `Uuid` column. A plain `sa.CHAR`
# is *not* here: `character(n)` pads, and wreath has no type for that.
_MODELLED_TYPE_ORIGINS = frozenset({
    "ormar.fields.sqlalchemy_uuid.CHAR",
})
# Table-level constraint objects inside a `create_table(...)` that detection
# reads. CheckConstraint/Index/ExcludeConstraint are deliberately absent.
_SA_TABLE_CONSTRAINTS = frozenset({
    "Column", "PrimaryKeyConstraint", "UniqueConstraint", "ForeignKeyConstraint",
})
# Index kwargs that take the index outside "btree over plain columns".
_MIG_INDEX_MANUAL_KWARGS = frozenset({
    "postgresql_where", "postgresql_using", "postgresql_include",
    "postgresql_ops", "postgresql_concurrently", "mysql_using",
})
# alter_column kwargs whose whole effect lives in the column signature detect
# reads. `comment=` is absent: wreath does not model column comments.
_MIG_ALTER_KWARGS = frozenset({
    "nullable", "type_", "server_default", "new_column_name", "schema",
    "existing_type", "existing_nullable", "existing_server_default",
})
# Referential actions belong to the constraint, not to a column the ORM declares.
_FK_ACTION_KWARGS = frozenset({"ondelete", "onupdate", "deferrable", "initially"})

# The four annotations `load_env`'s `dict[str, str]` converts to without a
# decision. `list`/`dict`/`Optional`/`Literal` are absent: pydantic-settings
# JSON-decodes those from the variable, and wreath hands over the raw string.
_ENV_SCALARS = frozenset({"str", "int", "float", "bool"})
_SETTINGS_FIELD_RULE = {
    "scalar": "settings.field",
    "nested": "settings.nested",
    "complex": "settings.field_complex",
}
# SettingsConfigDict keys whose effect on the ported dataclass is still fully
# determined: a literal prefix goes in front of every variable name, `extra` has
# no counterpart (reading named variables ignores the rest by construction), and
# case sensitivity only picks which spelling to look up. Anything else —
# `env_nested_delimiter`, `secrets_dir`, `env_file_encoding` on a computed path —
# changes where values come from, so the class waits for a human.
_SETTINGS_CONFIG_KEYS = frozenset({
    "env_prefix", "extra", "case_sensitive", "env_file", "populate_by_name",
})
# How many env names a message will spell out before it says "...".
_MAX_NAMED_ENV = 8

# ormar PK column type -> wreath PgType name, for FK type inference from the referenced model.
_PK_PGTYPE = {
    "UUID": "Uuid", "Integer": "Int64", "BigInteger": "Int64",
    "SmallInteger": "Int16", "String": "Varchar", "Text": "Text",
}


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def module_pk_types(tree: ast.Module, imports: _Imports) -> dict[str, str]:
    """{ORM model name -> wreath PgType of its primary key}, resolved within one module.

    Used to give a ForeignKey its real column type instead of a guess. See
    `tree_pk_types` for the whole-tree version, which is what actually resolves
    most of them — a model is usually declared in a different file from the one
    that points at it.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _base_kind(imports, node) in ("ormar", "sqlmodel"):
            for stmt in node.body:
                value = stmt.value if isinstance(stmt, (ast.AnnAssign, ast.Assign)) else None
                if not isinstance(value, ast.Call):
                    continue
                if any(kw.arg == "primary_key" and _is_true(kw.value) for kw in value.keywords):
                    pg = _PK_PGTYPE.get(imports.origin(value.func).split(".")[-1])
                    if pg:
                        out[node.name] = pg
                    break
    return out


def tree_pk_types(files: list[Path], on_skip=None) -> dict[str, str]:
    """{ORM model name -> primary key type} across every file in the tree.

    A foreign key names a model, and that model almost never lives in the same
    file — almost every foreign key points out of its own module — and each one
    was emitted with a guessed `Uuid` column and a note asking a human to look up
    a type this walk can just read.

    **A name declared twice with two different key types resolves to neither.**
    Two apps in one tree can both own a `Llama`, and picking whichever was
    walked first would silently give one of them the other's key type. Dropping
    the name puts it back where it was: flagged, with the type left to a human.
    """
    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for path in files:
        try:
            tree = _parse_file(path)
        except _SKIPPABLE as exc:
            if on_skip is not None:
                on_skip(path, exc)
            continue
        for name, pg in module_pk_types(tree, _Imports().visit(tree)).items():
            if out.setdefault(name, pg) != pg:
                ambiguous.add(name)
    for name in ambiguous:
        del out[name]
    return out


# What a real tree throws at a reader, and the stable code each is reported under.
# Order matters: UnicodeDecodeError is a ValueError, so it must be tested first.
_SKIP_REASONS: tuple[tuple[type[BaseException], str], ...] = (
    (RecursionError, "too-deep"),          # nesting past the parser's stack budget
    (MemoryError, "out-of-memory"),        # a generated or pathological module
    (SyntaxError, "syntax-error"),         # py2, a template, a partial checkout
    (UnicodeDecodeError, "undecodable"),   # not UTF-8 (latin-1 source, or binary)
    (OSError, "unreadable"),               # broken symlink, permissions, deleted mid-walk
    (ValueError, "invalid-source"),        # e.g. embedded NUL bytes
)
# Everything above, as one except-clause. Deliberately *not* BaseException:
# KeyboardInterrupt and SystemExit must end the run.
_SKIPPABLE = tuple({cls for cls, _ in _SKIP_REASONS})


def _skip_reason(exc: BaseException) -> str:
    for cls, reason in _SKIP_REASONS:
        if isinstance(exc, cls):
            return reason
    return "error"  # pragma: no cover - unreachable while _SKIPPABLE mirrors the table


def _skip_detail(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _parse_file(path: Path) -> ast.Module:
    """Read and parse one module. Raises; callers decide whether that is fatal."""
    return ast.parse(path.read_text(encoding="utf-8"))


def _relative_to(path: Path, root: Path) -> str:
    """How a path is spelled in the report: relative to the root when it is under it."""
    return str(path.relative_to(root)) if root == path or root in path.parents else str(path)


# Directory names that are never the application being ported. Everything whose
# name begins with "." is pruned as well — the convention ruff, black and pytest
# already use — which is what removes `.git`, `.tox`, `.nox`, `.venv`, `.eggs`,
# `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.direnv` and `.idea` without
# enumerating them. So this list only carries the undotted names.
_PRUNED_DIRS = frozenset({
    "__pycache__",     # compiled bytecode; never source
    "node_modules",    # a JS dependency tree, frequently vendored beside a Python app
    "site-packages",   # installed third-party code, venv marker present or not
    "venv",            # the undotted spelling of the convention below
    "build",           # a *copy* of the source tree; counting it double-counts
    "dist",            # unpacked sdists/wheels, same problem
})
_PRUNED_SUFFIXES = (".egg-info",)


def _is_pruned_dir(dirpath: str, name: str) -> bool:
    """Is `dirpath/name` infrastructure rather than application source?

    A virtualenv is detected by its **marker**, `pyvenv.cfg`, not by its name:
    `.venv` is a convention and nothing more, and a venv walked as app code both
    inflates the coverage denominator with libraries the user is not porting and
    drags a few thousand unrelated files into a run they did not ask for.
    """
    if name.startswith(".") or name in _PRUNED_DIRS or name.endswith(_PRUNED_SUFFIXES):
        return True
    try:
        return (Path(dirpath) / name / "pyvenv.cfg").is_file()
    except OSError:  # pragma: no cover - unreadable directory; the walk reports it
        return False


def _iter_py(root: Path, on_error=None):
    """Yield every application `.py` under `root`, pruning infrastructure.

    `on_error` receives the `OSError` for any directory that could not be
    listed (`os.walk` swallows those by default, which would silently shrink
    the tree). Symlinked directories are not followed — `os.walk`'s default —
    so a link out of the tree cannot widen the walk beyond what was named.
    """
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = sorted(d for d in dirnames if not _is_pruned_dir(dirpath, d))
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


class _Imports:
    """Resolves local names to their dotted framework origin (honors `as`)."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.has_star = False

    def visit(self, tree: ast.AST) -> _Imports:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.names[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        self.has_star = True
                        continue
                    qualified = f"{mod}.{alias.name}" if mod else alias.name
                    self.names[alias.asname or alias.name] = qualified
        return self

    def origin(self, node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return self.names.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value)
            return f"{base}.{node.attr}"
        return ""


def _boto3_service(node: ast.Call) -> str | None:
    """The AWS service named by `boto3.client("s3")` / `.resource("s3")`.

    `None` when the name is not a literal — a service chosen at runtime is not
    one this analyzer can route, and guessing would put an S3 verdict on a call
    that talks to something else.
    """
    argument = node.args[0] if node.args else None
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value.lower()
    return None


def _literal_name_collection(node: ast.AST) -> bool:
    """Whether a projection names every selected column statically."""
    return isinstance(node, (ast.Set, ast.List, ast.Tuple)) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in node.elts
    )


def pydantic_projection_rule(
    node: ast.Attribute, parents: dict[int, ast.AST]
) -> str:
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


def _base_kind(imports: _Imports, cls: ast.ClassDef) -> str | None:
    """Which framework model kind (if any) this class subclasses."""
    for base in cls.bases:
        origin = imports.origin(base)
        if origin == "pydantic.BaseModel":
            return "pydantic"
        # A BaseHTTPMiddleware subclass is the middleware itself — the construct
        # a porter rewrites. `mw.custom` used to fire only where one was *wired
        # up* (`add_middleware(...)`), so a class defined in its own module and
        # imported elsewhere went unreported entirely.
        if origin in (
            "starlette.middleware.base.BaseHTTPMiddleware",
            "fastapi.middleware.base.BaseHTTPMiddleware",
        ):
            return "middleware"
        # pydantic-settings (v2) and the legacy pydantic v1 BaseSettings both appear
        # in real apps — recognize either.
        if origin in ("pydantic_settings.BaseSettings", "pydantic.BaseSettings"):
            return "settings"
        if origin == "ormar.Model":
            return "ormar"
        if origin == "sqlmodel.SQLModel":
            return "sqlmodel"
    return None


def _index_tree(
    files: list[Path], on_skip=None
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, dict[str, str]],
    set[str],
]:
    """Pass 1: model/settings class names, and each ORM model's declared columns.

    The columns are what let a GraphQL type be compared with the model it claims
    to mirror; the names alone can only say the model exists.
    """
    index: dict[str, set[str]] = {"pydantic": set(), "settings": set(), "orm": set()}
    orm_columns: dict[str, set[str]] = {}
    orm_relations: dict[str, dict[str, str]] = {}
    positional_calls: set[str] = set()
    for path in files:
        try:
            tree = _parse_file(path)
        except _SKIPPABLE as exc:
            if on_skip is not None:
                on_skip(path, exc)
            continue
        imports = _Imports().visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                if isinstance(node.func, ast.Name):
                    positional_calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    positional_calls.add(node.func.attr)
            if isinstance(node, ast.ClassDef):
                kind = _base_kind(imports, node)
                if kind == "pydantic":
                    index["pydantic"].add(node.name)
                elif kind == "settings":
                    index["settings"].add(node.name)
                elif kind in ("ormar", "sqlmodel"):
                    index["orm"].add(node.name)
                    orm_columns[node.name] = _declared_columns(node)
                    relations: dict[str, str] = {}
                    for statement in node.body:
                        if not (
                            isinstance(statement, ast.AnnAssign)
                            and isinstance(statement.target, ast.Name)
                            and isinstance(statement.value, ast.Call)
                            and statement.value.args
                            and imports.origin(statement.value.func)
                            in ("ormar.ForeignKey", "sqlmodel.Relationship")
                        ):
                            continue
                        target = statement.value.args[0]
                        if isinstance(target, ast.Name):
                            relations[statement.target.id] = target.id
                        elif isinstance(target, ast.Attribute):
                            relations[statement.target.id] = target.attr
                        elif isinstance(target, ast.Constant) and isinstance(
                            target.value, str
                        ):
                            relations[statement.target.id] = target.value
                    orm_relations[node.name] = relations
    return index, orm_columns, orm_relations, positional_calls


def _declared_columns(cls: ast.ClassDef) -> set[str]:
    """The attribute names an ORM model class declares as columns."""
    names: set[str] = set()
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            target = stmt.target.id
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name):
            target = stmt.targets[0].id
        else:
            continue
        if target in ("ormar_config", "__tablename__", "model_config") or not isinstance(
            stmt.value, ast.Call
        ):
            continue
        names.add(target)
    return names


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


class _Analyzer(ast.NodeVisitor):
    def __init__(self, path: Path, root: Path, imports: _Imports, index: dict[str, set[str]],
                 pk_types: dict[str, str] | None = None,
                 orm_columns: dict[str, set[str]] | None = None,
                 orm_relations: dict[str, dict[str, str]] | None = None,
                 positional_model_calls: set[str] | frozenset[str] = frozenset()) -> None:
        self.rel = _relative_to(path, root)
        self.imports = imports
        self.index = index
        self.pk_types = pk_types or {}
        # {ORM model name -> its declared attribute names}, tree-wide. A GraphQL
        # type only mirrors a model when its fields *are* the model's columns, and
        # that comparison needs the columns, not just the class name.
        self.orm_columns = orm_columns or {}
        self.orm_relations = orm_relations or {}
        self.positional_model_calls = set(positional_model_calls)
        # Names the module hands to an application as `lifespan=`; filled by
        # `visit_Module`, because the `FastAPI(lifespan=...)` call sits below the
        # `def` it names.
        self.lifespan_names: frozenset[str] = frozenset()
        self.findings: list[Finding] = []
        self._loop_depth = 0
        self._once: set[str] = set()
        # `Model.objects` nodes already accounted for by the verb that follows
        # them, so the chain bills as one rewrite rather than two findings.
        self._claimed_objects: set[int] = set()
        # `id(child) -> parent`, so a query head can see the verbs chained after
        # it. Filled by `visit_Module`; empty until then, which makes
        # `query_rule` fall back to its conservative answer rather than guess.
        self._parents: dict[int, ast.AST] = {}

    def visit_Module(self, node: ast.Module) -> None:
        self._parents = parent_map(node)
        self.lifespan_names = lifespan_names(node)
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call) and item.args):
            if isinstance(call.func, ast.Name):
                self.positional_model_calls.add(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                self.positional_model_calls.add(call.func.attr)
        self.generic_visit(node)

    # -- emit -----------------------------------------------------------------
    def _emit(self, rule_id: str, line: int, extra: str = "") -> None:
        construct, category, tag, message = RULES[rule_id]
        if extra:
            message = f"{message} ({extra})"
        self.findings.append(Finding(self.rel, line, construct, tag, rule_id, message, category))

    def _once_emit(self, key: str, rule_id: str, line: int) -> None:
        if key not in self._once:
            self._once.add(key)
            self._emit(rule_id, line)

    # -- loops (dynamic include_router) ---------------------------------------
    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    # -- classes (models / settings) ------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            origin = self.imports.origin(dec.func if isinstance(dec, ast.Call) else dec)
            if origin.split(".")[-1] == "as_form":
                self._emit("form.as_form", node.lineno)
            elif origin.startswith("strawberry.") and origin.split(".")[-1] in (
                "type", "input", "interface", "federation",
            ):
                # One finding per GraphQL type, not per field: wreath derives
                # fields from the ORM model, so `strawberry.auto` (the single
                # most common GraphQL token there is) is deleted rather
                # than ported. A finding per field for a no-op would bury the rest.
                rule_id, reason = self._graphql_type_shape(node, origin.split(".")[-1])
                self._emit(rule_id, node.lineno, reason)
        kind = _base_kind(self.imports, node)
        if kind == "middleware":
            self._emit("mw.custom", node.lineno)
        elif kind == "pydantic":
            self._emit("pydantic.model", node.lineno)
            offenders = dataclass_needs_kw_only(self.imports, node)
            if offenders:
                self._emit(
                    (
                        "pydantic.model_kw_only"
                        if node.name in self.positional_model_calls
                        else "pydantic.model_kw_only_exact"
                    ),
                    node.lineno,
                    "required after a defaulted field: " + ", ".join(offenders),
                )
            self._scan_pydantic_body(node)
        elif kind == "settings":
            # The class verdict follows its fields, so bill the fields first.
            self._scan_settings_body(node)
            rule_id = settings_class_rule(self.imports, node, self.index["settings"])
            self._emit(
                rule_id, node.lineno,
                settings_required(node) if rule_id == "settings.class_env" else "",
            )
        elif kind in ("ormar", "sqlmodel"):
            self._emit("orm.model", node.lineno)
            self._scan_orm_body(node, kind)
        # descend for validators, nested calls, etc.
        self.generic_visit(node)

    def _graphql_type_shape(self, node: ast.ClassDef, decorator: str) -> tuple[str, str]:
        """Whether deleting this strawberry class is provably equivalent.

        Wreath derives the object type from the ORM model, so a class that is
        nothing but `strawberry.auto` over a model's columns has no counterpart
        to write — the same argument that makes an `auto` field emit nothing
        makes the enclosing class a deletion. It only holds when the class really
        is that model's full column set, and two things break it:

        * **a subset is a narrowing.** Exposure in wreath is per model, not per
          field, so deleting a type that lists four of eight columns publishes the
          other four. That is a schema widening, and it must not happen quietly.
        * **snake_case is a rename on the wire.** Strawberry camel-cases field
          names by default; wreath emits `column.python_name` verbatim
          (`_graphql/schema.py`). `fleece_kg` is `fleeceKg` in the old
          schema and `fleece_kg` in the new one, so every client sees it.
        """
        if decorator != "type":
            return "graphql.type", f"a strawberry.{decorator} is not a derived object type"
        if _plain_graphql_dataclass(self.imports, node):
            return "graphql.type_dataclass", "register it in GraphQL(dataclasses=[...])"
        fields: list[str] = []
        auto: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return "graphql.type", "the class carries resolvers, so it is not just a mirror"
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.append(stmt.target.id)
                if self.imports.origin(stmt.annotation) == "strawberry.auto":
                    auto.append(stmt.target.id)
        if not fields or len(auto) != len(fields):
            return "graphql.type", (
                "some fields are declared rather than `strawberry.auto`, so they are not "
                "provably the model's columns"
            )
        columns = self.orm_columns.get(node.name)
        if columns is None:
            return "graphql.type", f"no ORM model named {node.name} in the tree to derive from"
        hidden = sorted(columns - set(fields))
        if hidden:
            return "graphql.type", (
                "the derived type would also expose " + ", ".join(hidden)
                + " — wreath's exposure is per model, not per field, so deleting this class "
                "widens the public schema"
            )
        renamed = sorted(name for name in fields if "_" in name)
        if renamed:
            return "graphql.type", (
                "strawberry camel-cases field names and wreath does not, so "
                + ", ".join(renamed) + " would change on the wire"
            )
        return "graphql.type_mirror", ""

    def _scan_pydantic_body(self, node: ast.ClassDef) -> None:
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
                # pydantic v1 nested config class
                self._emit("pydantic.config_class", stmt.lineno)
                continue
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "model_config":
                    continue
                self._emit(pydantic_field_rule(self.imports, stmt), stmt.lineno)
            elif isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
            ):
                extra = _config_extra(stmt.value)
                if extra == "ignore":
                    self._emit("pydantic.config_ignore", stmt.lineno)
                elif extra == "forbid":
                    self._emit("pydantic.config_forbid", stmt.lineno)

    def _scan_settings_body(self, node: ast.ClassDef) -> list[str]:
        """Bill each env field by its shape, and report the shapes for the class verdict."""
        shapes: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "model_config":
                    continue
                shape = settings_field_shape(self.imports, stmt, self.index["settings"])
                shapes.append(shape)
                self._emit(_SETTINGS_FIELD_RULE[shape], stmt.lineno)
        return shapes

    def _scan_orm_body(self, node: ast.ClassDef, kind: str) -> None:
        for stmt in node.body:
            targets = []
            value = None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                targets = [stmt.target.id]
                value = stmt.value
            elif isinstance(stmt, ast.Assign):
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                value = stmt.value
            if not targets or targets[0] in ("ormar_config", "__tablename__", "model_config"):
                continue
            if not isinstance(value, ast.Call):
                continue
            origin = self.imports.origin(value.func)
            if origin == "ormar.ForeignKey" or self._has_kw(value, "foreign_key"):
                target = value.args[0] if value.args else None
                target_name = target.id if isinstance(target, ast.Name) else (
                    target.attr if isinstance(target, ast.Attribute) else None)
                typed = target_name in self.pk_types
                self._emit("orm.fk_typed" if typed else "orm.fk", stmt.lineno)
            elif origin.startswith("ormar.") or origin.endswith(".ARRAY") or (
                kind == "sqlmodel" and origin.split(".")[-1] == "Field"
            ):
                self._emit("orm.column", stmt.lineno)

    # -- functions (routes / deps / validators / lifespan) --------------------
    def visit_FunctionDef(self, node) -> None:
        self._handle_function(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _handle_function(self, node) -> None:
        is_route = False
        for dec in node.decorator_list:
            origin = self.imports.origin(dec.func if isinstance(dec, ast.Call) else dec)
            tail = origin.split(".")[-1]
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                attr = dec.func.attr
                if attr in HTTP_METHODS:
                    is_route = True
                    self._emit("route.method", dec.lineno)
                    self._scan_route_options(dec, node)
                elif attr == "websocket":
                    self._emit("route.websocket", dec.lineno)
                elif attr == "exception_handler":
                    self._emit("exc.handler", dec.lineno)
            if tail in ("field_validator", "model_validator", "validator", "root_validator"):
                self._emit("pydantic.validator", getattr(dec, "lineno", node.lineno))
            elif tail == "asynccontextmanager":
                self._scan_lifespan(node)
            elif tail == "shared_task" or (tail == "task" and "celery" in origin.lower()):
                # Both spellings, deliberately: `@celery_app.task(bind=True)` is a
                # Call and `@celery_app.task` is a bare Attribute. Checking only the
                # Call form missed every undecorated-argument task -- and the
                # emitter (`emit.py`) has always matched on `tail`, so the report
                # under-counted exactly the sites whose ported source carried a TODO.
                self._emit("bg.celery", getattr(dec, "lineno", node.lineno))
            elif tail == "field" and origin.startswith("strawberry."):
                self._emit("graphql.resolver", getattr(dec, "lineno", node.lineno))
            elif tail == "cached" and origin.startswith("cachetools"):
                self._emit("cache.decorator", getattr(dec, "lineno", node.lineno))
        if is_route:
            self._scan_params(node)

    def _scan_lifespan(self, node) -> None:
        """Bill an `@asynccontextmanager` only if it is the app's lifespan.

        `contextlib.asynccontextmanager` is stdlib and wreath has no opinion
        about it: an advisory-lock or connection helper written with it needs no
        porting at all, and telling its author to "split at the yield into
        on_startup/on_shutdown" would be advice about a function that has no
        startup. So the decorator alone is not the signal — being handed to the
        application as `lifespan=` is.
        """
        if not _is_lifespan(node, self.lifespan_names, self.imports):
            return
        rule_id, reason = lifespan_shape(node)
        self._emit(rule_id, node.lineno, reason)

    def _scan_route_options(self, dec: ast.Call, node) -> None:
        for kw in dec.keywords:
            if kw.arg == "response_model":
                self._emit("route.response_model", dec.lineno)
            elif kw.arg == "status_code":
                self._emit(status_code_rule(self.imports, kw.value, node), dec.lineno)
            elif kw.arg == "include_in_schema" and _is_false(kw.value):
                self._emit("route.include_in_schema", dec.lineno)
            elif kw.arg == "response_class":
                self._emit("route.response_class", dec.lineno)

    def _scan_params(self, node) -> None:
        args = node.args
        # The tail of `args.args` is exactly as long as `args.defaults`.
        defaulted = args.args[len(args.args) - len(args.defaults):]
        defaults = dict(zip([a.arg for a in defaulted], args.defaults, strict=True))
        for arg in list(args.args) + list(args.kwonlyargs):
            default = defaults.get(arg.arg)
            ann_origin = self.imports.origin(arg.annotation) if arg.annotation else ""
            if isinstance(default, ast.Call):
                marker = self.imports.origin(default.func).split(".")[-1]
                if marker == "Body":
                    # A plain `Body(...)` is the explicit spelling of what an
                    # annotated model parameter already says. `embed=True` is
                    # not: it wraps the value in a single-key object, and wreath
                    # has no switch for that — the DTO has to gain the wrapper.
                    self._emit(
                        "param.body_embed"
                        if any(k.arg == "embed" and _is_true(k.value)
                               for k in default.keywords)
                        else "param.body",
                        arg.lineno,
                    )
                    continue
                rule_id = _MARKER_RULE.get(marker)
                if rule_id == "param.query" and any(k.arg in _STR_CONSTRAINTS
                                                    for k in default.keywords):
                    self._emit("param.query_strconstraint", arg.lineno)
                    continue
                if rule_id:
                    self._emit(rule_id, arg.lineno)
                    continue
            if ann_origin.split(".")[-1] == "UploadFile":
                self._emit("param.file", arg.lineno)
            elif arg.annotation is not None and self._annotation_is_model(arg.annotation):
                self._emit("param.body", arg.lineno)

    # -- calls / attributes (queries, deps, middleware, infra) ----------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        origin = self.imports.origin(func)
        tail = origin.split(".")[-1]
        if isinstance(func, ast.Attribute):
            attr = func.attr
            if attr == "include_router":
                self._emit("route.include_dynamic" if self._loop_depth else "route.include_static",
                           node.lineno)
            elif attr == "add_middleware":
                self._scan_add_middleware(node)
            elif attr in ("delay", "apply_async"):
                self._emit("bg.celery", node.lineno)
            elif attr in ("send_json", "receive_json"):
                self._emit("ws.json_method", node.lineno)
            elif attr == "create_task" and self.imports.origin(func.value) == "asyncio":
                if self._created_task_is_joined(node):
                    self._emit("bg.asyncio_joined", node.lineno)
                else:
                    self._once_emit("asyncio_loop", "bg.asyncio_loop", node.lineno)
        if tail == "Process" and origin.startswith("multiprocessing"):
            # One finding per spawn, the way `bg.celery` bills the decorator
            # rather than every call. `.start()`/`.join()`/`.is_alive()` around it
            # are the same worker, and billing them would report one port as four.
            self._emit("bg.multiprocessing", node.lineno)
        if tail == "FastAPI":
            self._emit("route.app", node.lineno)
        elif tail == "APIRouter":
            self._emit("route.router", node.lineno)
            self._scan_router_deps(node)
        elif tail == "HTTPException":
            self._scan_http_exception(node)
        elif tail == "Depends":
            self._emit("depends.use", node.lineno)
        elif tail in ("GraphQL", "GraphQLRouter", "GraphQLApp"):
            self._emit("graphql.mount", node.lineno)
        elif tail == "Security":
            self._emit("auth.security", node.lineno)
        elif tail in _SECURITY_SCHEMES and self._from_framework(origin):
            self._emit("auth.security_scheme", node.lineno)
        elif tail == "jsonable_encoder":
            self._emit("resp.jsonable", node.lineno)
        elif tail in _RESPONSE_CLASSES and self._from_framework(origin):
            self._emit("resp.class", node.lineno)
        elif tail == "TestClient" and self._from_framework(origin):
            parent = self._parents.get(id(node))
            direct_assignment = (
                isinstance(parent, ast.Assign)
                and len(parent.targets) == 1
                and isinstance(parent.targets[0], ast.Name)
            )
            ancestor = parent
            while ancestor is not None and not isinstance(
                ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                ancestor = self._parents.get(id(ancestor))
            self._emit(
                "test.client_local"
                if direct_assignment and ancestor is not None
                else "test.client",
                node.lineno,
            )
        elif tail in _CACHE_STORES and origin.startswith("cachetools"):
            self._emit("cache.store", node.lineno)
        elif origin.startswith("arrow."):
            # Split by constructor rather than billing the module once: the
            # clock and parsing calls are a rename, and lumping them in with the
            # rest would keep reporting shipped work as outstanding.
            if tail in _ARROW_RENAMES:
                self._once_emit("arrow", "time.arrow", node.lineno)
            else:
                self._once_emit("arrow_other", "time.arrow_other", node.lineno)
        elif origin.startswith("httpx."):
            self._once_emit("httpx", "ext.httpx", node.lineno)
        elif origin.startswith(("pandas", "numpy")):
            self._once_emit("pandas", "ext.pandas", node.lineno)
        elif origin.startswith("alembic.op."):
            self._scan_migration_op(node, tail)
        elif origin.startswith("boto3") or origin.startswith("aioboto3"):
            # `boto3.client("s3")` names its service in the first argument, and
            # that argument is what decides the verdict: S3 has a wreath target
            # now, every other service still has none. A module that talks to
            # both bills both, so the once-key carries the service.
            service = _boto3_service(node)
            if service == "s3":
                self._once_emit("boto3-s3", "ext.boto3_s3", node.lineno)
            else:
                self._once_emit("boto3", "ext.boto3", node.lineno)
        elif origin in ("hmac.new", "hmac.compare_digest"):
            # A digest compared against a header is a signature check. One
            # finding per module: `hmac.new(...)` and the `compare_digest` that
            # reads it are two halves of one verify, not two ports.
            self._once_emit("hmac", "webhook.hmac", node.lineno)
        elif "dlock" in origin:
            self._once_emit("dlock", "lock.dlock", node.lineno)
        elif tail in ("decode", "get_unverified_header") and "jwt" in origin.lower():
            self._emit("auth.jwt", node.lineno)
        elif tail == "OAuth2Session" or "authlib" in origin:
            self._emit("auth.oauth", node.lineno)
        elif origin.startswith("aiometer"):
            self._once_emit("aiometer", "ext.aiometer", node.lineno)
        elif origin.startswith("gql") and tail in {"Client", "gql"}:
            self._once_emit("gql", "ext.gql", node.lineno)
        elif origin.startswith("s3path"):
            self._once_emit("s3path", "ext.s3path", node.lineno)
        # migration MANUAL cast
        if any(kw.arg == "postgresql_using" for kw in node.keywords):
            self._emit("mig.manual", node.lineno)
        self.generic_visit(node)

    def _created_task_is_joined(self, node: ast.Call) -> bool:
        """Whether this task's exception is observed in its defining function."""
        parent = self._parents.get(id(node))
        if isinstance(parent, ast.Await):
            return True
        if not (
            isinstance(parent, (ast.Assign, ast.AnnAssign))
            and (
                isinstance(getattr(parent, "target", None), ast.Name)
                or (
                    isinstance(parent, ast.Assign)
                    and len(parent.targets) == 1
                    and isinstance(parent.targets[0], ast.Name)
                )
            )
        ):
            return False
        target = (
            parent.target
            if isinstance(parent, ast.AnnAssign)
            else parent.targets[0]
        )
        if not isinstance(target, ast.Name):
            return False
        ancestor: ast.AST | None = parent
        while ancestor is not None and not isinstance(
            ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            ancestor = self._parents.get(id(ancestor))
        if not isinstance(ancestor, ast.AsyncFunctionDef):
            return False
        for candidate in ast.walk(ancestor):
            if not isinstance(candidate, ast.Await):
                continue
            if isinstance(candidate.value, ast.Name) and candidate.value.id == target.id:
                return True
            if isinstance(candidate.value, ast.Call):
                awaited = candidate.value
                if (
                    self.imports.origin(awaited.func) in ("asyncio.gather", "asyncio.wait")
                    and any(
                        isinstance(argument, ast.Name) and argument.id == target.id
                        for argument in awaited.args
                    )
                ):
                    return True
        return False

    def visit_Attribute(self, node: ast.Attribute) -> None:
        value = node.value
        # `Model.objects.<verb>` — the verb is what names the rewrite, so claim
        # the `.objects` underneath it. Visiting is top-down, so this node is
        # always reached before the one it claims.
        if isinstance(value, ast.Attribute) and value.attr == "objects":
            self._claimed_objects.add(id(value))
            call = self._parents.get(id(node))
            model = ast.unparse(value.value)
            self._emit(
                query_rule(
                    node.attr,
                    call if isinstance(call, ast.Call) else None,
                    chain_tail(node, self._parents),
                    model=model,
                    relations=self.orm_relations,
                    columns=self.orm_columns,
                    plain_mappings=plain_filter_mappings(
                        call if isinstance(call, ast.Call) else None, self._parents
                    ),
                ),
                value.lineno,
            )
        elif node.attr == "objects" and id(node) not in self._claimed_objects:
            # `.objects` used as a value rather than called through: still a
            # query surface, but there is no verb to be specific about.
            self._emit("orm.query", node.lineno)
        elif node.attr == "get_pydantic":
            self._emit(pydantic_projection_rule(node, self._parents), node.lineno)
        elif node.attr == "dependency_overrides":
            self._once_emit("dep_override", "test.dependency_override", node.lineno)
        elif node.attr.startswith("HTTP_") and self.imports.origin(value) in (
            "fastapi.status", "starlette.status",
        ):
            self._emit("resp.status_const", node.lineno)
        self.generic_visit(node)

    def _from_framework(self, origin: str) -> bool:
        """Whether a resolved name came from FastAPI/Starlette rather than a local.

        Applications routinely have a class sharing a name with a framework one
        — their own `TestClient` wrapper, for instance — and reporting those
        would be noise a porter has to dismiss by hand.
        """
        return origin.startswith(("fastapi", "starlette"))

    def _scan_migration_op(self, node: ast.Call, tail: str) -> None:
        """Sort one Alembic operation into derivable, manual, or data-rewriting.

        Most revisions in a mature app are ordinary DDL that
        `wreath migrations generate` produces from the model change, and for
        those there is nothing to hand-write at all. The ones that are not are
        the ones worth a porter's attention: raw SQL and a row rewrite, which no
        generator can derive, and a **rename**, which is the operation an image
        differ reads as a drop plus a create — the ordinary-looking op that
        would silently move no data.
        """
        if tail == "execute":
            self._emit("mig.raw_sql", node.lineno)
        elif tail == "get_bind":
            self._emit("mig.data", node.lineno)
        elif tail in _MIG_RENAME_OPS:
            self._emit("mig.rename", node.lineno)
        elif tail in _MIG_REVIEW_OPS:
            self._emit("mig.schema_op", node.lineno)
        elif tail in _MIG_DERIVED_OPS:
            # `postgresql_using` already earns the stronger MANUAL verdict below;
            # emitting both would double-count one operation.
            if not self._has_kw(node, "postgresql_using"):
                self._emit(self._migration_verdict(node, tail), node.lineno)

    def _migration_verdict(self, node: ast.Call, tail: str) -> str:
        """The rule a derivable-*shaped* operation actually earns.

        The verb narrows the candidates; the arguments decide. An index is
        derivable only while it stays btree-over-columns, an `alter_column`
        only while every kwarg it sets is part of the column signature detection
        reads, and a table only while every column type has a wreath PgType.
        """
        if tail in ("create_index", "drop_index"):
            if any(kw.arg in _MIG_INDEX_MANUAL_KWARGS for kw in node.keywords):
                return "mig.index_manual"
            if tail == "create_index" and not _index_is_over_columns(node):
                return "mig.index_manual"
            return "mig.derived"
        if tail == "alter_column":
            if self._has_kw(node, "new_column_name"):
                return "mig.rename"
            if any(kw.arg not in _MIG_ALTER_KWARGS for kw in node.keywords):
                return "mig.schema_op"
            for kw in node.keywords:
                if kw.arg in ("type_", "existing_type") and not self._sa_type_is_modelled(kw.value):
                    return "mig.unmodelled_type"
            return "mig.derived"
        if tail in ("create_table", "add_column"):
            return self._table_body_verdict(node)
        return "mig.derived"

    def _table_body_verdict(self, node: ast.Call) -> str:
        """Whether every column/constraint in a `create_table`/`add_column` is modelled."""
        verdict = "mig.derived"
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                continue                      # the table name
            if not isinstance(argument, ast.Call):
                return "mig.schema_op"        # a splat or a name: nothing to read
            kind = self.imports.origin(argument.func).split(".")[-1]
            if kind not in _SA_TABLE_CONSTRAINTS:
                return "mig.schema_op"
            if any(kw.arg in _FK_ACTION_KWARGS for kw in argument.keywords):
                return "mig.schema_op"
            if kind != "Column":
                continue
            inner = self._column_verdict(argument)
            if inner == "mig.schema_op":
                return inner
            if inner != "mig.derived":
                verdict = inner
        return verdict

    def _column_verdict(self, call: ast.Call) -> str:
        """One `sa.Column(...)`: modelled type, no referential action of its own."""
        for argument in call.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                continue                      # the column name
            if isinstance(argument, ast.Call):
                kind = self.imports.origin(argument.func).split(".")[-1]
                if kind == "ForeignKey":
                    if any(kw.arg in _FK_ACTION_KWARGS for kw in argument.keywords):
                        return "mig.schema_op"
                    continue
            if not self._sa_type_is_modelled(argument):
                return "mig.unmodelled_type"
        return "mig.derived"

    def _sa_type_is_modelled(self, node: ast.AST | None) -> bool:
        """Whether `sa.String(length=80)` / `postgresql.JSONB()` has a wreath PgType."""
        if isinstance(node, ast.Call):
            node = node.func
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return False
        origin = self.imports.origin(node)
        return origin in _MODELLED_TYPE_ORIGINS or origin.split(".")[-1] in _SA_MODELLED_TYPES

    def _scan_add_middleware(self, node: ast.Call) -> None:
        first = node.args[0] if node.args else None
        origin = self.imports.origin(first) if first else ""
        tail = origin.split(".")[-1]
        if tail == "CORSMiddleware":
            self._emit("mw.cors", node.lineno)
        elif tail == "TrustedHostMiddleware":
            self._emit("mw.trustedhost", node.lineno)
        else:
            self._emit("mw.custom", node.lineno)

    def _scan_router_deps(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "dependencies" and not isinstance(kw.value, (ast.List, ast.Tuple)):
                self._emit("depends.router_call", node.lineno)

    def _scan_http_exception(self, node: ast.Call) -> None:
        self._emit(http_exception_rule(self.imports, node), node.lineno)

    # -- small predicates -----------------------------------------------------
    def _annotation_is_model(self, ann: ast.AST) -> bool:
        name = ""
        if isinstance(ann, ast.Name):
            name = ann.id
        elif isinstance(ann, ast.Attribute):
            name = ann.attr
        return name in self.index["pydantic"] or name in self.index["orm"]

    def _has_kw(self, call: ast.Call, name: str) -> bool:
        return any(kw.arg == name for kw in call.keywords)


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


# --- predicates shared with the emitter -------------------------------------
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


#: pydantic v1 `con*` factory names, which are a constraint wearing a type's
#: clothes. Shared with `_Analyzer._annotation_is_constrained` and the emitter.
_CONSTRAINED_TYPES = frozenset({
    "confloat", "conint", "constr", "condecimal", "conbytes", "conlist", "conset", "condate",
})


def pydantic_field_rule(imports: _Imports, stmt: ast.AnnAssign) -> str:
    """The verdict one `BaseModel` field earns, by the *shape* of its `Field(...)`.

    Shared with the emitter, for the reason `query_rule` and `status_code_rule`
    are: the report and the `# TODO(wreath-port: …)` written into the source must
    not disagree about one line, and the emitter must only rewrite what the
    report calls determined.

    A dataclass field has exactly one slot — the default — so a `Field(...)`
    translates when everything it carries either *is* the default or is
    documentation wreath has nowhere to put:

    * `pydantic.field` — a bare annotation, a plain default, or a `Field(...)`
      holding only `default=`/`default_factory=` and doc keywords. The marker is
      deleted and the default written as an ordinary Python default.
    * `pydantic.field_constraint` — `ge=`/`max_length=`/a `con*` annotation. The
      constraint has three possible homes and they do not behave alike, so it
      stays for a human.
    * `pydantic.field_marker` — anything else the marker carries. `alias=` is
      the one that matters: wreath binds a body field by its own name, so
      dropping the alias silently renames it on the wire.
    """
    if _is_field_constraint(stmt.value, imports) or _is_constrained_annotation(
        stmt.annotation, imports
    ):
        return "pydantic.field_constraint"
    value = stmt.value
    if not (
        isinstance(value, ast.Call)
        and imports.origin(value.func).split(".")[-1] == "Field"
    ):
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
        if pydantic_field_rule(imports, stmt) != "pydantic.field":
            return True                       # left in place, `=` and all
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


def _is_lifespan(node, lifespan_names, imports: _Imports) -> bool:
    """Whether this function is *the app's* lifespan, and not merely an
    `@asynccontextmanager`.

    `contextlib.asynccontextmanager` is stdlib, and an advisory-lock or
    connection helper written with it needs no porting at all. Recognized three
    ways: registered as a lifespan on the app, named `lifespan` by convention,
    or taking exactly one `FastAPI`/`Starlette`-annotated parameter.
    """
    if node.name in lifespan_names or node.name == "lifespan":
        return True
    parameters = list(node.args.args) + list(node.args.posonlyargs)
    return (
        len(parameters) == 1
        and parameters[0].annotation is not None
        and imports.origin(parameters[0].annotation).split(".")[-1]
        in ("FastAPI", "Starlette")
    )


def settings_field_shape(
    imports: _Imports, stmt: ast.AnnAssign, settings_names: set[str] | None = None
) -> str:
    """`"nested"` | `"scalar"` | `"complex"` for one `BaseSettings` field.

    `scalar` is the shape whose whole translation is decided by the source: one
    of the four types `load_env`'s `dict[str, str]` converts to, and either no
    default (a required variable) or a literal one. Anything else — a container,
    an optional, a `Field(...)` marker, a computed default — needs someone to
    decide how the raw string becomes the value.

    `settings_names` is what separates `nested` from `complex`. A caller
    without a tree index may omit it: a sub-group then reads as `complex`, which
    keeps the *class* verdict identical, since neither shape is `scalar`.
    """
    annotation = imports.origin(stmt.annotation).split(".")[-1]
    known = settings_names or set()
    value_is_group = (
        isinstance(stmt.value, ast.Call)
        and imports.origin(stmt.value.func).split(".")[-1] in known
    )
    if annotation in known or value_is_group:
        return "nested"
    if annotation not in _ENV_SCALARS:
        return "complex"
    if stmt.value is None:
        return "scalar"                       # no default: a required variable
    if isinstance(stmt.value, ast.Constant) and not isinstance(stmt.value.value, bytes):
        return "scalar"
    return "complex"


def settings_class_rule(
    imports: _Imports, node: ast.ClassDef, settings_names: set[str] | None = None
) -> str:
    """Whether a whole `BaseSettings` class is a field-by-field mechanical rewrite.

    Shared with the emitter, so the report and the annotation written into the
    source cannot disagree about one class. A class earns the translated verdict
    only when every field does and its configuration says nothing this analyzer
    cannot read: `env_prefix` and `extra` change the target in a way that is
    still fully determined, while an `env_nested_delimiter`, a `secrets_dir`
    or a pydantic-v1 `class Config` do not, so they hold the class back rather
    than being quietly ignored.
    """
    shapes = [
        settings_field_shape(imports, stmt, settings_names)
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        and stmt.target.id != "model_config"
    ]
    if not shapes or any(shape != "scalar" for shape in shapes):
        return "settings.class"
    for stmt in node.body:
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return "settings.class"           # a v1 `class Config`, or a validator
        config = _model_config_value(stmt)
        if config is None:
            continue
        if not isinstance(config, ast.Call):
            return "settings.class"
        for kw in config.keywords:
            if kw.arg not in _SETTINGS_CONFIG_KEYS or not isinstance(kw.value, ast.Constant):
                return "settings.class"
    return "settings.class_env"


def settings_required(node: ast.ClassDef) -> str:
    """The `required_env=[...]` list a settings class implies: fields with no default."""
    prefix = _env_prefix(node)
    names = [
        f"{prefix}{stmt.target.id.upper()}"
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.target.id != "model_config"
        and stmt.value is None
    ]
    note = f"every variable is read as {prefix}<FIELD>; " if prefix else ""
    if not names:
        return f"{note}every field has a default, so nothing is required at boot"
    shown = names[:_MAX_NAMED_ENV]
    listing = ", ".join(shown) + (", ..." if len(names) > len(shown) else "")
    return f"{note}required_env=[{listing}]"


def _env_prefix(node: ast.ClassDef) -> str:
    for stmt in node.body:
        config = _model_config_value(stmt)
        if isinstance(config, ast.Call):
            for kw in config.keywords:
                if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                    value = kw.value.value
                    if isinstance(value, str):
                        return value.upper()
    return ""


def lifespan_names(tree: ast.Module) -> frozenset[str]:
    """Names handed to an application as `lifespan=<name>` anywhere in the module."""
    return frozenset(
        keyword.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "lifespan" and isinstance(keyword.value, ast.Name)
    )


def lifespan_shape(node) -> tuple[str, str]:
    """`(rule_id, reason)` for one lifespan body.

    The split into `@app.on_startup`/`@app.on_shutdown` is determined exactly
    when the body *is* a split: one bare `yield` as a top-level statement, with
    the halves independent. Three things break that, and each is worth naming
    rather than lumping together, because they need different fixes:

    * the yield hands a value to the framework (FastAPI's lifespan-state dict),
      which has to find a home on `app.state`;
    * the yield sits inside a `try`/`async with`, so the shutdown half is
      that block's exit rather than a suffix of the body;
    * a name made before the yield is used after it — the halves are separate
      functions, so that name needs somewhere to live.
    """
    yields = [
        (index, statement) for index, statement in enumerate(node.body)
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield)
    ]
    if len(yields) != 1:
        nested = any(isinstance(n, ast.Yield) for n in ast.walk(node))
        return "lifespan.ctx", (
            "the yield is inside a try/async with, so the shutdown half is that block's exit"
            if nested else "no top-level yield to split at"
        )
    index, statement = yields[0]
    if statement.value.value is not None:  # type: ignore[union-attr]
        return "lifespan.ctx", "the yield hands a value to the framework; put it on app.state"
    crossing = _names_crossing(node.body[:index], node.body[index + 1:])
    if crossing:
        return "lifespan.ctx", (
            "startup makes " + ", ".join(crossing) + " and shutdown uses them, so they need a "
            "home the two hooks share (app.state)"
        )
    return "lifespan.split", ""


def _names_crossing(before: list[ast.stmt], after: list[ast.stmt]) -> list[str]:
    """Names bound in `before` and read in `after`, in binding order."""
    bound: list[str] = []
    for statement in before:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                if node.id not in bound:
                    bound.append(node.id)
    read = {
        node.id for statement in after for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return [name for name in bound if name in read]


def status_int(imports: _Imports, node: ast.AST | None) -> int | None:
    """The integer a status expression denotes, or `None` if it is not a literal.

    `status.HTTP_404_NOT_FOUND` is a literal wearing a name, and applications
    spell the status that way far more often than as a bare integer.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if not isinstance(node, ast.Attribute) or not node.attr.startswith("HTTP_"):
        return None
    if imports.origin(node.value) not in ("fastapi.status", "starlette.status"):
        return None
    digits = node.attr.split("_")[1]
    return int(digits) if digits.isdigit() else None


def http_exception_status(node: ast.Call) -> ast.expr | None:
    """The `status_code` expression of an `HTTPException(...)`, however spelled."""
    for keyword in node.keywords:
        if keyword.arg == "status_code":
            return keyword.value
    return node.args[0] if node.args else None


def http_exception_rule(imports: _Imports, node: ast.Call) -> str:
    """The verdict one `HTTPException(...)` earns.

    Shared with the emitter so a status the emitter cannot rewrite is never
    reported as translated. Three outcomes:

    * `exc.http_literal` — the status resolves to an int `STATUS_EXCEPTION` has
      a class for, so the call becomes that class. `status.HTTP_404_NOT_FOUND`
      counts: it is a literal wearing a name, and applications spell the status
      that way far more often than as a bare integer.
    * `exc.http_unmapped` — the status is a literal wreath ships no class for
      (502/503/501/…), or the call carries `headers=`, whose wreath spelling is
      a sequence of lowercase byte pairs rather than fastapi's `dict[str, str]`.
    * `exc.http_variable` — the status is not readable here at all.
    """
    status = status_int(imports, http_exception_status(node))
    if status is None:
        return "exc.http_variable"
    if status not in STATUS_EXCEPTION:
        return "exc.http_unmapped"
    if any(keyword.arg == "headers" for keyword in node.keywords):
        return "exc.http_unmapped"
    return "exc.http_literal"


def status_code_rule(imports: _Imports, value: ast.expr, node) -> str:
    """Which verdict `status_code=` earns on this handler.

    Shared with the emitter for the reason `query_rule` is: the report and the
    `# TODO(wreath-port: …)` written into the source have to agree about one
    line, and the emitter must only perform the rewrite the report calls
    determined.

    Wreath has no `status_code` slot on the decorator — the status lives on the
    response the handler returns. So the question is which response class this
    return becomes, and for a *literal* return wreath's own coercion answers it
    (`app._to_response`: dict/list/tuple/number -> JSONResponse, str ->
    TextResponse). Wrapping such a return in the class wreath would have chosen
    anyway changes the status and nothing else.

    A `return some_name` is where that stops. The runtime type picks the class,
    and a dataclass is not JSON-serializable at all in wreath (`_json.dumps`
    raises; `dataclasses.asdict` is the documented step) — so wrapping an
    unknown value would emit code that fails on the first request, which is the
    silent conversion this tool exists to avoid.
    """
    status = status_int(imports, value)
    if status is None:
        return "route.status_code"
    returns = _returns_in(node)
    if status in _STATUS_WITHOUT_BODY:
        return "route.status_code_empty" if not returns else "route.status_code_empty_body"
    if len(returns) != 1 or returns[0].value is None:
        return "route.status_code"
    returned = returns[0].value
    if isinstance(returned, ast.Call) and _is_response_construction(imports, returned):
        return "route.status_code_response"
    if isinstance(returned, (ast.Dict, ast.List, ast.Tuple, ast.DictComp, ast.ListComp)):
        return "route.status_code_return"
    if isinstance(returned, ast.Constant):
        if isinstance(returned.value, str):
            return "route.status_code_text"
        if isinstance(returned.value, (bool, int, float)) or returned.value is None:
            return "route.status_code_return"
    return "route.status_code"


def _is_response_construction(imports: _Imports, call: ast.Call) -> bool:
    """Whether this call builds a response object that carries its own status."""
    tail = imports.origin(call.func).split(".")[-1]
    return tail in _RESPONSE_CLASSES or tail in _EXTRA_RESPONSE_CLASSES


def _returns_in(node) -> list[ast.Return]:
    """Every `return` belonging to this function, not to one nested inside it.

    A nested `def`/`lambda` has its own returns (the streaming-generator
    pattern puts one right inside a handler), and counting those would make a
    one-return handler look like several.
    """
    found: list[ast.Return] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(current, ast.Return):
            found.append(current)
        stack.extend(ast.iter_child_nodes(current))
    return found


def _model_config_value(stmt: ast.stmt) -> ast.expr | None:
    """The right-hand side of `model_config = SettingsConfigDict(...)`, if that is this."""
    if isinstance(stmt, ast.Assign):
        if any(isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets):
            return stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        if isinstance(stmt.target, ast.Name) and stmt.target.id == "model_config":
            return stmt.value
    return None


def _index_is_over_columns(node: ast.Call) -> bool:
    """`create_index(name, table, ["a", "b"])` — plain columns, not an expression.

    `[sa.text("lower(name)")]` and a runtime column list are both outside what
    detection reads, and both look the same from the verb alone.
    """
    columns = node.args[2] if len(node.args) > 2 else next(
        (kw.value for kw in node.keywords if kw.arg == "columns"), None
    )
    if not isinstance(columns, (ast.List, ast.Tuple)):
        return False
    return bool(columns.elts) and all(
        isinstance(element, ast.Constant) and isinstance(element.value, str)
        for element in columns.elts
    )


def query_chain_runs(head: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Whether a `Model.objects.…` chain *executes*, rather than only building.

    A chain that ends at `filter(...)` is a query object and needs nothing to
    make it; one that ends at `all()`, `count()`, `exists()` or `get_or_none()`
    has to be run, and in wreath that means a session. Only the second kind
    forces a session onto the function it sits in.
    """
    verbs = {head.attr if isinstance(head, ast.Attribute) else ""}
    verbs.update(verb for verb, _ in chain_tail(head, parents))
    return bool(verbs & _RUNNING_VERBS)


#: The chain verbs that execute a query.
_RUNNING_VERBS = frozenset({
    "all",
    "get_or_none",
    "get",
    "create",
    "count",
    "exists",
    "update",
    "delete",
})


def _function_query_names(
    tree: ast.Module,
    imports: _Imports,
    orm_columns: dict[str, set[str]],
    orm_relations: dict[str, dict[str, str]],
) -> tuple[set[str], set[str]]:
    """`(functions that run a query, every function name defined here)`.

    Only the *determined* queries count: a chain this tool would not rewrite
    anyway is no reason to change a signature.
    """
    parents = parent_map(tree)
    runs: set[str] = set()
    defined: set[str] = set()
    enclosing: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            if isinstance(node, ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    enclosing.setdefault(id(child), node.name)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "objects"):
            continue
        call = parents.get(id(node))
        rule_id = query_rule(
            node.attr, call if isinstance(call, ast.Call) else None,
            chain_tail(node, parents),
            model=ast.unparse(node.value.value),
            relations=orm_relations,
            columns=orm_columns,
            plain_mappings=plain_filter_mappings(
                call if isinstance(call, ast.Call) else None, parents
            ),
        )
        owner = enclosing.get(id(node))
        if owner is not None and rule_id in QUERY_TRANSLATED and query_chain_runs(node, parents):
            runs.add(owner)
    return runs, defined


def _called_names(tree: ast.Module) -> dict[str, set[str]]:
    """`{async function name -> the plain function/method names it calls}`."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        called: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        out.setdefault(node.name, set()).update(called)
    return out


#: Query verdicts the emitter writes out in full. Defined here rather than in
#: `emit` because `TreeContext` has to agree with it while deciding which
#: functions need a session, and a second copy of the list would drift.
QUERY_TRANSLATED = frozenset({
    "orm.query.filter_exact", "orm.query.get_or_none_exact", "orm.query.get_exact",
    "orm.query.create_exact", "orm.query.all",
    "orm.query.page_exact",
    "orm.query.count", "orm.query.exists", "orm.query.order_exact",
    "orm.query.eager_exact",
})


#: Names a call site can carry without meaning the function of that name: every
#: builtin, plus the methods `dict`, `list`, `str`, `set` and a file answer to.
#: A repository is very likely to have its own `get`, `count` or `update`, and
#: matching by name would then rewrite every `payload.get(...)` in the tree.
_AMBIGUOUS_CALL_NAMES = frozenset(dir(builtins)) | frozenset({
    "get", "keys", "values", "items", "update", "copy", "pop", "setdefault",
    "append", "extend", "insert", "remove", "discard", "clear", "add",
    "count", "index", "sort", "reverse", "join", "split", "strip", "format",
    "encode", "decode", "read", "write", "close", "flush", "send", "seek",
    "startswith", "endswith", "replace", "lower", "upper", "title",
    "first", "last", "all", "any", "one", "run", "execute", "save", "delete",
})


def session_functions(
    files: list[Path],
    on_skip=None,
    *,
    orm_columns: dict[str, set[str]] | None = None,
    orm_relations: dict[str, dict[str, str]] | None = None,
) -> frozenset[str]:
    """Every function name that has to take a session, once it has spread.

    A function that runs a query needs one. So does anything that calls such a
    function, and anything that calls *that* — the requirement climbs the call
    graph until it reaches a route handler, where wreath supplies it. This is
    what `--opinionated` needs in order to finish the job: adding the parameter
    without updating the callers leaves a tree that imports and then fails on
    the first call.

    **Names, not resolved targets.** Working out that `repo.by_herd(...)` is
    `LlamaRepository.by_herd` needs type inference this tool does not have, so a
    method name is matched by name across the tree. The over-approximation is
    deliberate and it is one-directional: a name that matches gains a keyword
    argument, and since *every* definition of that name gains the parameter too,
    the pair stays consistent. What it cannot know is a same-named method on a
    third-party object, which is why this is opt-in and why the report says which
    functions were changed.
    """
    runs: set[str] = set()
    calls: dict[str, set[str]] = {}
    definitions: dict[str, int] = {}
    for path in files:
        try:
            tree = _parse_file(path)
        except _SKIPPABLE as exc:
            if on_skip is not None:
                on_skip(path, exc)
            continue
        imports = _Imports().visit(tree)
        module_runs, module_defined = _function_query_names(
            tree, imports, orm_columns or {}, orm_relations or {}
        )
        runs |= module_runs
        for name in module_defined:
            definitions[name] = definitions.get(name, 0) + 1
        for name, called in _called_names(tree).items():
            calls.setdefault(name, set()).update(called)
    # A name is only followed when it can only mean one thing: defined exactly
    # once in the tree, and not something a built-in type also answers to. The
    # tree has an `async def all(self)` in a repository, and without this guard
    # every `all(...)` in it — the built-in — was handed a session.
    def usable(name: str) -> bool:
        return definitions.get(name) == 1 and name not in _AMBIGUOUS_CALL_NAMES
    needs = {name for name in runs if usable(name)}
    changed = True
    while changed:                            # climb the call graph to a fixed point
        changed = False
        for name, called in calls.items():
            if name not in needs and usable(name) and called & needs:
                needs.add(name)
                changed = True
    return frozenset(needs)


@dataclass(frozen=True)
class TreeContext:
    """What one module needs to know about the rest of the tree to be ported well.

    A module on its own cannot see that `Llama`'s primary key is a UUID, that
    `NewLlama` is a body model rather than a query parameter, or that a GraphQL
    type lists exactly the columns of the model it shadows. Every one of those
    changes the verdict, so `port_tree` reads the tree once and hands the answers
    to each file.
    """

    pk_types: dict[str, str] = dataclass_field(default_factory=dict)
    index: dict[str, set[str]] = dataclass_field(
        default_factory=lambda: {"pydantic": set(), "settings": set(), "orm": set()}
    )
    orm_columns: dict[str, set[str]] = dataclass_field(default_factory=dict)
    orm_relations: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)
    positional_model_calls: frozenset[str] = frozenset()
    #: Function names that have to take a session once the requirement has
    #: climbed the call graph. Only `--opinionated` acts on it, because acting on
    #: it changes signatures and call sites across the whole tree.
    session_functions: frozenset[str] = frozenset()

    @classmethod
    def of(cls, files: list[Path], on_skip=None, *, opinionated: bool = False) -> TreeContext:
        index, orm_columns, orm_relations, positional_calls = _index_tree(
            files, on_skip=on_skip
        )
        return cls(
            tree_pk_types(files, on_skip=on_skip),
            index,
            orm_columns,
            orm_relations,
            frozenset(positional_calls),
            session_functions(
                files,
                on_skip=on_skip,
                orm_columns=orm_columns,
                orm_relations=orm_relations,
            )
            if opinionated
            else frozenset(),
        )


def module_findings(
    path: Path, root: Path, tree: ast.Module, imports: _Imports, context: TreeContext
) -> list[Finding]:
    """Every finding for one already-parsed module.

    Shared with the emitter. The emitter used to carry its own copy of each
    detector, and the two drifted apart exactly as you would expect: 23 rules
    and 794 findings appeared in the report and nowhere in the ported files, so
    a porter reading their own code saw no sign of 160 hand-written SQL
    migrations or 87 pandas modules. Deriving both from this makes that class of
    gap impossible rather than merely fixed.
    """
    analyzer = _Analyzer(
        path, root, imports, context.index,
        {**context.pk_types, **module_pk_types(tree, imports)},
        context.orm_columns,
        context.orm_relations,
        context.positional_model_calls,
    )
    if imports.has_star:
        analyzer._emit("resolve.star_import", 1)
    analyzer.visit(tree)
    return analyzer.findings


def analyze(root) -> Report:
    """Analyze a single app root (directory or file) and return its Report.

    **One bad file is recorded and skipped, never fatal.** A 3000-file tree
    reliably contains a broken symlink, a file whose permission bit says no, a
    file deleted between the walk and the read, a null byte in a "`.py`" that
    is really a fixture, and an expression nested past the parser's limit. Each
    of those takes its own file out of the run and leaves the rest in, and each
    lands in `Report.skipped` with a reason — a silently dropped file is
    indistinguishable from a file with nothing in it, and the coverage number is
    computed from exactly this population.

    `KeyboardInterrupt` and `SystemExit` derive from `BaseException` and
    are deliberately *not* caught: a run the user asked to stop must stop.
    """
    root = Path(root)
    skipped: dict[str, SkippedFile] = {}

    def record(target, exc: BaseException) -> None:
        key = _relative_to(Path(target), root)  # same spelling Findings use
        # First reason wins: the same file is read twice (index pass, then
        # analysis pass) and would otherwise be reported twice.
        skipped.setdefault(key, SkippedFile(key, _skip_reason(exc), _skip_detail(exc)))

    files = list(_iter_py(root, on_error=lambda exc: record(exc.filename or root, exc)))
    context = TreeContext.of(files, on_skip=record)
    findings: list[Finding] = []
    analyzed = 0
    for path in files:
        try:
            tree = _parse_file(path)
            found = module_findings(path, root, tree, _Imports().visit(tree), context)
        except _SKIPPABLE as exc:
            # Partial findings from a half-visited file are discarded with it:
            # half a module's constructs is a worse denominator than none.
            record(path, exc)
            continue
        analyzed += 1
        findings.extend(found)
    return Report(findings, roots=[str(root)], skipped=list(skipped.values()),
                  files_analyzed=analyzed)


def analyze_all(roots) -> Report:
    """Analyze several app roots (a glob of apps, design 07 §5) into one Report."""
    return Report.merge([analyze(r) for r in roots])
