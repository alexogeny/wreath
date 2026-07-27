"""Static (never-import-the-target) FastAPI/Pydantic/ormar/SQLModel analyzer.

Design 07's load-bearing constraint: the source cannot be imported (private deps,
import-time side effects), so this walks ``ast`` only. Two passes: (1) index every
module's classes by framework base across the whole tree so body-params and query
targets resolve cross-module; (2) classify constructs into findings.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

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

# ormar QuerySet verb -> rule. The verb immediately after `.objects.` is the one
# that names the shape of the rewrite; a trailing `.all()`/`.get_or_none()` is
# just how ormar spells "now run it", which wreath spells `session.fetch(...)`.
# Frequencies in the corpus this was measured against are in the rule messages.
_QUERY_RULE = {
    "filter": "orm.query.filter",
    "exclude": "orm.query.filter",
    "get_or_none": "orm.query.get_or_none",
    "get": "orm.query.get",
    "create": "orm.query.create",
    "all": "orm.query.all",
    "select_related": "orm.query.eager",
    "select_all": "orm.query.eager",
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
}


# ormar keyword lookups whose wreath predicate is a plain operator over the
# value as written. `__icontains` and friends are deliberately absent: they
# rewrite the *value* (wrapping it in wildcards), which is a decision about what
# the author meant by "contains", not a translation of what they wrote.
_MECHANICAL_LOOKUPS = frozenset({"exact", "gt", "gte", "lt", "lte", "in"})

# Chain verbs that keep a mechanical head mechanical, and how their own
# arguments have to look. `first`/`last` are absent because wreath makes the
# ordering explicit and the source has none to carry over; `delete` because a
# bulk delete has no query form; `values` because the rows come back as models
# rather than dicts, which the caller sees; `update` because it is a write.
#
#   "kwargs"  — keyword filters, checked the same way the head's are
#   "value"   — one argument carried across untouched (limit/offset)
#   "columns" — string literals naming columns, which resolve to `Model.<col>`
_MECHANICAL_TAIL: dict[str, str] = {
    "all": "kwargs",
    "count": "kwargs",
    "exists": "kwargs",
    "get_or_none": "kwargs",
    "limit": "value",
    "offset": "value",
    "order_by": "columns",
}

# Head verb -> the rule to use when the arguments are mechanical. A verb absent
# here is never auto-translated whatever its arguments look like.
_QUERY_EXACT_RULE = {
    "filter": "orm.query.filter_exact",
    "get_or_none": "orm.query.get_or_none_exact",
    "all": "orm.query.all",
    "count": "orm.query.count",
    "exists": "orm.query.exists",
    "order_by": "orm.query.order_exact",
    "select_related": "orm.query.eager_exact",
    "prefetch_related": "orm.query.eager_exact",
}


def _eager_names_are_literal(call: ast.Call | None) -> bool:
    """Whether ``select_related(...)`` names relations this analyzer can resolve.

    ``select_related("llama")`` is ``.include(Model.llama.selectin())`` — a
    rename. ``select_all()`` is not: it means "every relation", and wreath has
    no such switch, so the set has to be written out by someone who knows which
    ones the caller actually reads. A ``a__b`` trail is a nested include and a
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


def _lookup_is_mechanical(keyword: str) -> bool:
    """Whether ``filter(<keyword>=v)`` becomes a predicate with ``v`` unchanged.

    A bare column name is an equality test. A name with ``__`` is a column plus
    a lookup *only* when the trailing segment is one wreath has an operator for;
    otherwise it is a relation traversal (``owner__name``) or a container lookup
    (``tags__jsonb_has_any``). The container lookup needs an operator someone
    chooses. The relation does *not* need a join chosen — ``Model.owner.name``
    is a ``RelatedColumnExpr`` and the compiler emits the INNER JOIN itself — it
    needs the relation *resolved*, and ``owner``'s target model is usually
    declared in another module. Reading the suffix is what separates them, and
    getting it wrong in the permissive direction would emit an attribute chain
    against a column that is not a relation at all.
    """
    column, separator, suffix = keyword.rpartition("__")
    if not separator:
        return True                       # a plain column: equality
    return bool(column) and suffix in _MECHANICAL_LOOKUPS


def _call_is_mechanical(call: ast.Call | None) -> bool:
    """Whether every argument of a ``.objects.<verb>(...)`` call maps across.

    Positional arguments are never mechanical: in ormar they are ``Q`` objects
    or raw clauses, and neither has a form this analyzer can read. ``**kwargs``
    likewise — the keys are a runtime value, so there is nothing to check.
    """
    if call is None:
        return True                       # `.objects.all` with no call at all
    if call.args:
        return False
    return all(
        keyword.arg is not None and _lookup_is_mechanical(keyword.arg)
        for keyword in call.keywords
    )


def query_rule(
    verb: str | None,
    call: ast.Call | None = None,
    tail: tuple[tuple[str, ast.Call | None], ...] = (),
) -> str:
    """The rule for a ``.objects.<verb>`` chain, or the generic one.

    Shared with the emitter so the ``# TODO(wreath-port: …)`` it writes into the
    source says exactly what the report said — a porter grepping one and reading
    the other must not find two different verdicts for the same line.

    ``call`` and ``tail`` are what promote a verb from "here is the shape of the
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
    elif verb in ("select_related", "prefetch_related"):
        if not _eager_names_are_literal(call):
            return base
    elif not _call_is_mechanical(call):
        return base
    if not all(_tail_step_is_mechanical(step, node, verb or "") for step, node in tail):
        return base
    return exact


def _tail_step_is_mechanical(verb: str, call: ast.Call | None, head: str = "") -> bool:
    """Whether a verb chained after the head carries across with its arguments.

    Checked rather than assumed: ``.all(name__icontains=x)`` is a filter wearing
    a terminal's name, and treating the verb alone as safe would let the lookup
    the head check exists to catch through the back door.

    ``head`` is what lets ``first`` be mechanical after ``order_by`` and nowhere
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
        return _call_is_mechanical(call)
    if kind == "value":
        # `limit(n)`/`offset(n)`: one argument, whatever it is, carried across.
        return len(call.args) == 1 and not call.keywords
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
    """The verbs applied after the head of a ``.objects.<head>(...)`` chain.

    ``Model.objects.filter(x=1).all()`` bills once, at ``filter`` — so whether
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
    """``id(child) -> parent`` for one module, built in a single walk."""
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

# Statuses that must not carry a body (wreath.response._STATUS_WITHOUT_BODY).
_STATUS_WITHOUT_BODY = frozenset({204, 304})

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
# (wreath/orm/types.py). Numeric/Decimal, Time, Interval, Enum, INET, TSVECTOR,
# HSTORE and MONEY are absent on purpose: there is no PgType to derive them from.
_SA_MODELLED_TYPES = frozenset({
    "Integer", "INTEGER", "BigInteger", "BIGINT", "SmallInteger", "SMALLINT",
    "String", "VARCHAR", "Text", "TEXT", "Unicode", "UnicodeText",
    "Boolean", "BOOLEAN", "Float", "REAL", "DOUBLE_PRECISION",
    "Date", "DATE", "DateTime", "TIMESTAMP", "LargeBinary", "BYTEA",
    "UUID", "JSON", "JSONB", "ARRAY",
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

    Used to give a ForeignKey its real column type instead of a guess. Cross-module
    references (target not in this module) resolve to nothing -> the FK stays needs-review.
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
    """Is ``dirpath/name`` infrastructure rather than application source?

    A virtualenv is detected by its **marker**, ``pyvenv.cfg``, not by its name:
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
    """Yield every application ``.py`` under ``root``, pruning infrastructure.

    ``on_error`` receives the ``OSError`` for any directory that could not be
    listed (``os.walk`` swallows those by default, which would silently shrink
    the tree). Symlinked directories are not followed — ``os.walk``'s default —
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
    """Resolves local names to their dotted framework origin (honors ``as``)."""

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
    """The AWS service named by ``boto3.client("s3")`` / ``.resource("s3")``.

    ``None`` when the name is not a literal — a service chosen at runtime is not
    one this analyzer can route, and guessing would put an S3 verdict on a call
    that talks to something else.
    """
    argument = node.args[0] if node.args else None
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value.lower()
    return None


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
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Pass 1: model/settings class names, and each ORM model's declared columns.

    The columns are what let a GraphQL type be compared with the model it claims
    to mirror; the names alone can only say the model exists.
    """
    index: dict[str, set[str]] = {"pydantic": set(), "settings": set(), "orm": set()}
    orm_columns: dict[str, set[str]] = {}
    for path in files:
        try:
            tree = _parse_file(path)
        except _SKIPPABLE as exc:
            if on_skip is not None:
                on_skip(path, exc)
            continue
        imports = _Imports().visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                kind = _base_kind(imports, node)
                if kind == "pydantic":
                    index["pydantic"].add(node.name)
                elif kind == "settings":
                    index["settings"].add(node.name)
                elif kind in ("ormar", "sqlmodel"):
                    index["orm"].add(node.name)
                    orm_columns[node.name] = _declared_columns(node)
    return index, orm_columns


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


class _Analyzer(ast.NodeVisitor):
    def __init__(self, path: Path, root: Path, imports: _Imports, index: dict[str, set[str]],
                 pk_types: dict[str, str] | None = None,
                 orm_columns: dict[str, set[str]] | None = None) -> None:
        self.rel = _relative_to(path, root)
        self.imports = imports
        self.index = index
        self.pk_types = pk_types or {}
        # {ORM model name -> its declared attribute names}, tree-wide. A GraphQL
        # type only mirrors a model when its fields *are* the model's columns, and
        # that comparison needs the columns, not just the class name.
        self.orm_columns = orm_columns or {}
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
                # most common GraphQL token in the corpus) is deleted rather
                # than ported. 300 findings for a no-op would bury the rest.
                rule_id, reason = self._graphql_type_shape(node, origin.split(".")[-1])
                self._emit(rule_id, node.lineno, reason)
        kind = _base_kind(self.imports, node)
        if kind == "middleware":
            self._emit("mw.custom", node.lineno)
        elif kind == "pydantic":
            self._emit("pydantic.model", node.lineno)
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
        nothing but ``strawberry.auto`` over a model's columns has no counterpart
        to write — the same argument that makes an ``auto`` field emit nothing
        makes the enclosing class a deletion. It only holds when the class really
        is that model's full column set, and two things break it:

        * **a subset is a narrowing.** Exposure in wreath is per model, not per
          field, so deleting a type that lists four of eight columns publishes the
          other four. That is a schema widening, and it must not happen quietly.
        * **snake_case is a rename on the wire.** Strawberry camel-cases field
          names by default; wreath emits ``column.python_name`` verbatim
          (``_graphql/schema.py``). ``fleece_kg`` is ``fleeceKg`` in the old
          schema and ``fleece_kg`` in the new one, so every client sees it.
        """
        if decorator != "type":
            return "graphql.type", f"a strawberry.{decorator} is not a derived object type"
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
                if (_is_field_constraint(stmt.value, self.imports)
                        or self._annotation_is_constrained(stmt.annotation)):
                    self._emit("pydantic.field_constraint", stmt.lineno)
                else:
                    self._emit("pydantic.field", stmt.lineno)
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
        """Bill an ``@asynccontextmanager`` only if it is the app's lifespan.

        ``contextlib.asynccontextmanager`` is stdlib and wreath has no opinion
        about it: an advisory-lock or connection helper written with it needs no
        porting at all, and telling its author to "split at the yield into
        on_startup/on_shutdown" would be advice about a function that has no
        startup. So the decorator alone is not the signal — being handed to the
        application as ``lifespan=`` is.
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
            self._emit("test.client", node.lineno)
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
        elif origin.startswith("gql"):
            self._once_emit("gql", "ext.gql", node.lineno)
        elif origin.startswith("s3path"):
            self._once_emit("s3path", "ext.s3path", node.lineno)
        # migration MANUAL cast
        if any(kw.arg == "postgresql_using" for kw in node.keywords):
            self._emit("mig.manual", node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        value = node.value
        # `Model.objects.<verb>` — the verb is what names the rewrite, so claim
        # the `.objects` underneath it. Visiting is top-down, so this node is
        # always reached before the one it claims.
        if isinstance(value, ast.Attribute) and value.attr == "objects":
            self._claimed_objects.add(id(value))
            call = self._parents.get(id(node))
            self._emit(
                query_rule(
                    node.attr,
                    call if isinstance(call, ast.Call) else None,
                    chain_tail(node, self._parents),
                ),
                value.lineno,
            )
        elif node.attr == "objects" and id(node) not in self._claimed_objects:
            # `.objects` used as a value rather than called through: still a
            # query surface, but there is no verb to be specific about.
            self._emit("orm.query", node.lineno)
        elif node.attr == "get_pydantic":
            self._emit("pydantic.get_pydantic", node.lineno)
        elif node.attr == "dependency_overrides":
            self._once_emit("dep_override", "test.dependency_override", node.lineno)
        elif node.attr.startswith("HTTP_") and self.imports.origin(value) in (
            "fastapi.status", "starlette.status",
        ):
            self._emit("resp.status_const", node.lineno)
        self.generic_visit(node)

    def _from_framework(self, origin: str) -> bool:
        """Whether a resolved name came from FastAPI/Starlette rather than a local.

        The corpus has application classes that share a name with a framework one
        (its own ``TestClient`` wrapper, for instance), and reporting those would
        be noise a porter has to dismiss by hand.
        """
        return origin.startswith(("fastapi", "starlette"))

    def _scan_migration_op(self, node: ast.Call, tail: str) -> None:
        """Sort one Alembic operation into derivable, manual, or data-rewriting.

        Most revisions in a mature app are ordinary DDL that
        ``wreath migrations generate`` produces from the model change, and for
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
        derivable only while it stays btree-over-columns, an ``alter_column``
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
        """Whether every column/constraint in a ``create_table``/``add_column`` is modelled."""
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
        """One ``sa.Column(...)``: modelled type, no referential action of its own."""
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
        """Whether ``sa.String(length=80)`` / ``postgresql.JSONB()`` has a wreath PgType."""
        if isinstance(node, ast.Call):
            node = node.func
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return False
        return self.imports.origin(node).split(".")[-1] in _SA_MODELLED_TYPES

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
        status = None
        for kw in node.keywords:
            if kw.arg == "status_code":
                status = kw.value
        if status is None and node.args:
            status = node.args[0]
        # `status.HTTP_404_NOT_FOUND` is a literal wearing a name. Treating it as
        # unresolvable would push the most common spelling in a real codebase (72
        # sites in the corpus) into needs-review for no reason.
        resolved = self._status_constant(status) is not None
        self._emit("exc.http_literal" if resolved else "exc.http_variable", node.lineno)

    def _status_constant(self, node: ast.AST | None) -> int | None:
        """The integer behind ``fastapi.status.HTTP_404_NOT_FOUND``, if that is what this is."""
        return status_int(self.imports, node)

    # -- small predicates -----------------------------------------------------
    def _annotation_is_model(self, ann: ast.AST) -> bool:
        name = ""
        if isinstance(ann, ast.Name):
            name = ann.id
        elif isinstance(ann, ast.Attribute):
            name = ann.attr
        return name in self.index["pydantic"] or name in self.index["orm"]

    def _annotation_is_constrained(self, ann: ast.AST | None) -> bool:
        """pydantic v1 constrained-type annotation (confloat/conint/constr/...)."""
        if isinstance(ann, ast.Call):
            return self.imports.origin(ann.func).split(".")[-1] in (
                "confloat", "conint", "constr", "condecimal", "conbytes", "conlist",
                "conset", "condate",
            )
        return False

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
    """A ``Field(...)`` carrying at least one value or string constraint."""
    if isinstance(value, ast.Call) and imports.origin(value.func).split(".")[-1] == "Field":
        return any(k.arg in _FIELD_CONSTRAINTS for k in value.keywords)
    return False


def _config_extra(value: ast.AST | None) -> str | None:
    """The ``extra=`` setting on a ``model_config``/``Config`` call, if any."""
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
    ``@asynccontextmanager``.

    ``contextlib.asynccontextmanager`` is stdlib, and an advisory-lock or
    connection helper written with it needs no porting at all. Recognized three
    ways: registered as a lifespan on the app, named ``lifespan`` by convention,
    or taking exactly one ``FastAPI``/``Starlette``-annotated parameter.
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
    """``"nested"`` | ``"scalar"`` | ``"complex"`` for one ``BaseSettings`` field.

    ``scalar`` is the shape whose whole translation is decided by the source: one
    of the four types ``load_env``'s ``dict[str, str]`` converts to, and either no
    default (a required variable) or a literal one. Anything else — a container,
    an optional, a ``Field(...)`` marker, a computed default — needs someone to
    decide how the raw string becomes the value.

    ``settings_names`` is what separates ``nested`` from ``complex``. A caller
    without a tree index may omit it: a sub-group then reads as ``complex``, which
    keeps the *class* verdict identical, since neither shape is ``scalar``.
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
    """Whether a whole ``BaseSettings`` class is a field-by-field mechanical rewrite.

    Shared with the emitter, so the report and the annotation written into the
    source cannot disagree about one class. A class earns the translated verdict
    only when every field does and its configuration says nothing this analyzer
    cannot read: ``env_prefix`` and ``extra`` change the target in a way that is
    still fully determined, while an ``env_nested_delimiter``, a ``secrets_dir``
    or a pydantic-v1 ``class Config`` do not, so they hold the class back rather
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
    """The ``required_env=[...]`` list a settings class implies: fields with no default."""
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
    """Names handed to an application as ``lifespan=<name>`` anywhere in the module."""
    return frozenset(
        keyword.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "lifespan" and isinstance(keyword.value, ast.Name)
    )


def lifespan_shape(node) -> tuple[str, str]:
    """``(rule_id, reason)`` for one lifespan body.

    The split into ``@app.on_startup``/``@app.on_shutdown`` is determined exactly
    when the body *is* a split: one bare ``yield`` as a top-level statement, with
    the halves independent. Three things break that, and each is worth naming
    rather than lumping together, because they need different fixes:

    * the yield hands a value to the framework (FastAPI's lifespan-state dict),
      which has to find a home on ``app.state``;
    * the yield sits inside a ``try``/``async with``, so the shutdown half is
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
    """Names bound in ``before`` and read in ``after``, in binding order."""
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
    """The integer a status expression denotes, or ``None`` if it is not a literal.

    ``status.HTTP_404_NOT_FOUND`` is a literal wearing a name, and it is how a
    real codebase spells the status far more often than a bare integer.
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


def status_code_rule(imports: _Imports, value: ast.expr, node) -> str:
    """Which verdict ``status_code=`` earns on this handler.

    Shared with the emitter for the reason ``query_rule`` is: the report and the
    ``# TODO(wreath-port: …)`` written into the source have to agree about one
    line, and the emitter must only perform the rewrite the report calls
    determined.

    Wreath has no ``status_code`` slot on the decorator — the status lives on the
    response the handler returns. So the question is which response class this
    return becomes, and for a *literal* return wreath's own coercion answers it
    (``app._to_response``: dict/list/tuple/number -> JSONResponse, str ->
    TextResponse). Wrapping such a return in the class wreath would have chosen
    anyway changes the status and nothing else.

    A ``return some_name`` is where that stops. The runtime type picks the class,
    and a dataclass is not JSON-serializable at all in wreath (``_json.dumps``
    raises; ``dataclasses.asdict`` is the documented step) — so wrapping an
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
    """Every ``return`` belonging to this function, not to one nested inside it.

    A nested ``def``/``lambda`` has its own returns (the streaming-generator
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
    """The right-hand side of ``model_config = SettingsConfigDict(...)``, if that is this."""
    if isinstance(stmt, ast.Assign):
        if any(isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets):
            return stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        if isinstance(stmt.target, ast.Name) and stmt.target.id == "model_config":
            return stmt.value
    return None


def _index_is_over_columns(node: ast.Call) -> bool:
    """``create_index(name, table, ["a", "b"])`` — plain columns, not an expression.

    ``[sa.text("lower(name)")]`` and a runtime column list are both outside what
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


def analyze(root) -> Report:
    """Analyze a single app root (directory or file) and return its Report.

    **One bad file is recorded and skipped, never fatal.** A 3000-file tree
    reliably contains a broken symlink, a file whose permission bit says no, a
    file deleted between the walk and the read, a null byte in a "``.py``" that
    is really a fixture, and an expression nested past the parser's limit. Each
    of those takes its own file out of the run and leaves the rest in, and each
    lands in ``Report.skipped`` with a reason — a silently dropped file is
    indistinguishable from a file with nothing in it, and the coverage number is
    computed from exactly this population.

    ``KeyboardInterrupt`` and ``SystemExit`` derive from ``BaseException`` and
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
    index, orm_columns = _index_tree(files, on_skip=record)
    findings: list[Finding] = []
    analyzed = 0
    for path in files:
        try:
            tree = _parse_file(path)
            imports = _Imports().visit(tree)
            analyzer = _Analyzer(path, root, imports, index, module_pk_types(tree, imports),
                                 orm_columns)
            if imports.has_star:
                analyzer._emit("resolve.star_import", 1)
            analyzer.visit(tree)
        except _SKIPPABLE as exc:
            # Partial findings from a half-visited file are discarded with it:
            # half a module's constructs is a worse denominator than none.
            record(path, exc)
            continue
        analyzed += 1
        findings.extend(analyzer.findings)
    return Report(findings, roots=[str(root)], skipped=list(skipped.values()),
                  files_analyzed=analyzed)


def analyze_all(roots) -> Report:
    """Analyze several app roots (a glob of apps, design 07 §5) into one Report."""
    return Report.merge([analyze(r) for r in roots])
