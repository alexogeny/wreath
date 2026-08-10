"""Phase 1 declarative emitter (design 07 §3/§7).

Source-to-source translation by **pure `ast` + position-based text splicing** —
no `ast.unparse` (loses comments/formatting) and no third-party CST. The rule is
design 07's contract: **transpile declarations, copy logic**. Only declarative spans
(imports, class headers, decorators, parameter markers, exception constructors,
middleware registration) are rewritten in the original source text; every function
body is preserved byte-for-byte, with `# TODO(wreath-port: ...)` annotation lines
inserted above anything the analyzer tagged needs-review / unsupported (and above any
construct Phase 1 does not yet rewrite, e.g. ORM models — nothing is silently skipped).

Every emitted file is re-`ast.parse`d as a round-trip guard: a structurally broken
emit is a tool bug and raises rather than being written.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .analyzer import (
    _NULL_METHOD,
    _SKIPPABLE,
    HTTP_METHODS,
    LOOKUP_METHOD,
    LOOKUP_OPERATOR,
    STATUS_EXCEPTION,
    TreeContext,
    _base_kind,
    _config_extra,
    _eager_paths,
    _Imports,
    _is_false,
    _is_true,
    _iter_py,
    _order_argument_is_mechanical,
    _pagination_values,
    _plain_graphql_dataclass,
    _projection_names,
    _relative_to,
    _resolved_column_path,
    _returns_in,
    _skip_detail,
    _skip_reason,
    chain_tail,
    dataclass_needs_kw_only,
    http_exception_rule,
    http_exception_status,
    lifespan_names,
    module_findings,
    module_pk_types,
    parent_map,
    plain_filter_mappings,
    pydantic_field_rule,
    pydantic_projection_rule,
    query_rule,
    redundant_literal_validator,
    settings_class_rule,
    split_lookup,
    status_code_rule,
    status_int,
)
from .ir import TRANSLATED, SkippedFile
from .rules import RULES

_PHASE1_ONLY = (
    "wreath port Phase 1 emits the declarative surface (imports, routes, Pydantic "
    "DTOs, exceptions, CORS); ORM bodies, queries, auth, lifespan and settings are "
    "annotated for manual review, not rewritten (design 07 §7)."
)

# fastapi name -> wreath name, imported `from wreath import <name>`. Names whose value
# is the SAME need no call-site rewrite; FastAPI/APIRouter change name, so their call
# sites are rewritten too (see _Emitter.visit_Call).
_FASTAPI_TO_WREATH = {
    "FastAPI": "Wreath",
    "APIRouter": "Router",
    "Depends": "Depends",
    "Request": "Request",
    "Query": "Query",
    "Path": "Path",
    "Header": "Header",
    "Cookie": "Cookie",
    "Form": "Form",
    "File": "File",
    "WebSocket": "WebSocket",
    "WebSocketDisconnect": "WebSocketDisconnect",
    "BackgroundTasks": "BackgroundTasks",
    "Response": "Response",
    # NOTE: FastAPI's UploadFile has no wreath public equivalent — intentionally NOT
    # mapped, so it is left in place + flagged rather than emitted as a broken import.
}
_FASTAPI_RENAMED = {"FastAPI": "Wreath", "APIRouter": "Router"}

# cachetools store -> the wreath cache. Wreath has one bounded cache; what
# changes between the four is the eviction order, not the interface.
_CACHE_RENAME = {
    "TTLCache": "BoundedCache",
    "LRUCache": "BoundedCache",
    "LFUCache": "BoundedCache",
    "FIFOCache": "BoundedCache",
    "Cache": "BoundedCache",
}


# Names whose import the emitter deletes only when nothing still refers to them.
# Spelled as full origins because the same class arrives by several routes:
# `from fastapi import HTTPException` and `from fastapi.exceptions import
# HTTPException` are one class, and both have to keep the import alive.
_RETAINED_ORIGINS = frozenset(
    {
        "fastapi.HTTPException",
        "fastapi.exceptions.HTTPException",
        "starlette.exceptions.HTTPException",
        "pydantic.BaseModel",
        "pydantic.Field",
        # `status` and `jsonable_encoder` both usually disappear entirely — every
        # `status.HTTP_*` becomes its number and `jsonable_encoder(x)` becomes `x`.
        # The import goes with them, unless one use is left that did not.
        "fastapi.status",
        "starlette.status",
        "fastapi.encoders.jsonable_encoder",
        # Same story for the two libraries wreath replaced outright: every call
        # becomes a wreath one, and the import goes unless something is left over
        # (`arrow.Arrow(...)`, a `@cachetools.cached` decorator).
        "arrow",
        "cachetools",
        *(f"cachetools.{name}" for name in _CACHE_RENAME),
    }
)
#: Where a retained name has to keep coming from, when its module was rewritten.
_RETAINED_MODULE = {
    "status": ("fastapi", "starlette"),
    "jsonable_encoder": ("fastapi.encoders",),
}

# A rewritten wreath name -> the module it is actually imported from. Markers live in
# `wreath.binding` and first-class HTTP controls in `wreath.policy`;
# everything else (Wreath, Router, Depends, Request) is top-level `wreath`.
_WREATH_MODULE = {
    "Query": "wreath.binding",
    "Path": "wreath.binding",
    "Header": "wreath.binding",
    "Cookie": "wreath.binding",
    "Form": "wreath.binding",
    "File": "wreath.binding",
    "Field": "wreath.binding",
    "HttpPolicy": "wreath.policy",
    "CorsPolicy": "wreath.policy",
    "TrustedHostPolicy": "wreath.policy",
    "WebSocket": "wreath.websocket",
    "WebSocketDisconnect": "wreath.websocket",
    # `wreath` re-exports JSONResponse and Response; the rest live in wreath.response.
    "TextResponse": "wreath.response",
    "HTMLResponse": "wreath.response",
    "RedirectResponse": "wreath.response",
    "StreamingResponse": "wreath.response",
    "FileResponse": "wreath.response",
    "SSEResponse": "wreath.response",
    "TestClient": "wreath.testing",
    "BoundedCache": "wreath.cache",
    "BadRequest": "wreath.exceptions",
    "Unauthorized": "wreath.exceptions",
    "Forbidden": "wreath.exceptions",
    "NotFound": "wreath.exceptions",
    "MethodNotAllowed": "wreath.exceptions",
    "Conflict": "wreath.exceptions",
    "UnprocessableEntity": "wreath.exceptions",
    "TooManyRequests": "wreath.exceptions",
    # The base class every wreath status exception derives from, and a 500 on
    # its own. It is what a surviving `except HTTPException` / `exception_handler
    # (HTTPException)` / `HTTPException(status_code=500, ...)` becomes.
    "HTTPException": "wreath.exceptions",
    "PayloadTooLarge": "wreath.exceptions",
    "RequestHeaderFieldsTooLarge": "wreath.exceptions",
    # ORM (Phase 2): declarative API from wreath.orm, PgTypes from wreath.orm.types.
    "Model": "wreath.orm",
    "Mapped": "wreath.orm",
    "column": "wreath.orm",
    "relationship": "wreath.orm",
    "Ge": "wreath.orm",
    "Le": "wreath.orm",
    "Gt": "wreath.orm",
    "Lt": "wreath.orm",
    "Length": "wreath.orm",
    "AllOf": "wreath.orm",
    "unique": "wreath.orm",
    "index": "wreath.orm",
    "Session": "wreath.orm",
    "FromORM": "wreath.orm",
    "model_dataclass": "wreath.orm",
    "Environment": "wreath.config",
    "Env": "wreath.config",
    "read_osenv": "wreath.config",
    "HTTPClient": "wreath.http_client",
    "ClientTimeout": "wreath.http_client",
    "ClientResponse": "wreath.http_client",
    "ClientError": "wreath.http_client",
    "RetryPolicy": "wreath.http_client",
    "dumps": "wreath.json",
    "loads": "wreath.json",
    "BackgroundTasks": "wreath.background",
    "Numeric": "wreath.orm.types",
    "Bytea": "wreath.orm.types",
    "Uuid": "wreath.orm.types",
    "Varchar": "wreath.orm.types",
    "Text": "wreath.orm.types",
    "Int64": "wreath.orm.types",
    "Int32": "wreath.orm.types",
    "Int16": "wreath.orm.types",
    "Bool": "wreath.orm.types",
    "Float64": "wreath.orm.types",
    "Date": "wreath.orm.types",
    "Timestamp": "wreath.orm.types",
    "TimestampTz": "wreath.orm.types",
    "Json": "wreath.orm.types",
    "Jsonb": "wreath.orm.types",
    "Array": "wreath.orm.types",
    "TextArray": "wreath.orm.types",
}


def _grouped_imports(names):
    """`from <module> import ...` lines, routing each name to its real wreath module."""
    by_mod: dict[str, list[str]] = {}
    for name in names:
        by_mod.setdefault(_WREATH_MODULE.get(name, "wreath"), []).append(name)
    return [
        f"from {mod} import " + ", ".join(sorted(set(group)))
        for mod, group in sorted(by_mod.items())
    ]


# The status -> wreath exception class table lives in `analyzer` and is imported
# here, for the reason `query_rule` and `status_code_rule` are shared: the report
# decides `exc.http_literal` from exactly the table the emitter rewrites with, so
# a status with no class cannot be reported as translated and then annotated.

# Query/Path/... kwargs that map to a Wreath parameter marker. Dataclass field
# metadata is handled separately by ``_rewrite_field_metadata``.
_KW_RENAME = {"ge": "minimum", "le": "maximum"}
_KW_KEEP = frozenset({"alias"})
_MARKERS = frozenset({"Query", "Path", "Header", "Cookie", "Form", "File"})
# Marker keywords that only wrote prose into the generated API documentation.
# Wreath has no slot for them and nothing behaves differently without them, so
# they go quietly: 70 of the 72 notes this used to write said "description".
_MARKER_DOC_KWARGS = frozenset(
    {
        "description",
        "title",
        "example",
        "examples",
        "openapi_examples",
        "deprecated",
        "include_in_schema",
        "json_schema_extra",
    }
)

# ormar column type -> wreath PgType name (wreath.orm.types). DateTime is resolved by its
# timezone= kwarg; ARRAY (ormar_postgres_extensions) is handled by element type. Types with
# no wreath equivalent (Time, Enum, ...) are intentionally absent -> annotated.
_ORMAR_TYPE = {
    "UUID": "Uuid",
    "String": "Varchar",
    "Text": "Text",
    "Integer": "Int64",
    "BigInteger": "Int64",
    "SmallInteger": "Int16",
    "Boolean": "Bool",
    "Float": "Float64",
    "Date": "Date",
    "JSON": "Jsonb",
    "JSONB": "Jsonb",
    # Both of these have shipped in `wreath.orm.types` the whole time and were
    # simply missing from this table, so a money column and a blob column each
    # came out as "map by hand" over a type that already existed.
    "Decimal": "Numeric",
    "LargeBinary": "Bytea",
}

# ormar column keywords that describe the column for a schema generator. Wreath
# has nowhere to put them and nothing depends on them, so they are dropped
# without a note. A `description=` on nearly every column, each one asking a
# human to look at it, is how a real finding gets buried.
#
# `name=` is deliberately NOT here: on an ormar column it renames the *database*
# column, which is the opposite of documentation.
_ORMAR_DOC_KWARGS = frozenset(
    {
        "description",
        "title",
        "comment",
        "example",
        "examples",
        "overwrite_pydantic_type",
        "represent_as_base_field_type",
    }
)
_SA_ELEM_TYPE = {"String": "Text", "Text": "Text", "Integer": "Int64", "Boolean": "Bool"}
# wreath PgType name -> the Python annotation for a FK column of that PK type.
_PG_PYANN = {
    "Uuid": "uuid.UUID",
    "Int64": "int",
    "Int32": "int",
    "Int16": "int",
    "Varchar": "str",
    "Text": "str",
}

# The two `status_code=` verdicts whose target names a response class the emitter
# can wrap the return in. `route.status_code_response` and `_empty` are determined
# too, but one edits inside the returned call and the other appends a statement, so
# they are annotated rather than rewritten (Phase 1 does spans, not statements).
_STATUS_WRAPPER = {
    "route.status_code_return": "JSONResponse",
    "route.status_code_text": "TextResponse",
}

# rule_ids Phase 1 fully rewrites (or that map 1:1 needing no edit) → no annotation.
_REWRITTEN = frozenset(
    {
        "route.app",
        "route.router",
        "route.method",
        "route.include_static",
        "param.query",
        "param.path",
        "param.header",
        "param.cookie",
        "param.form",
        "param.file",
        "pydantic.model",
        "pydantic.field",
        "pydantic.config_forbid",
        "exc.http_literal",
        "exc.handler",
        "mw.cors",
        "mw.trustedhost",
        "depends.use",
    }
)

_HEADER_PREFIX = "# wreath-port:"

#: The parameter name the emitter gives a route handler that has queries to run.
_SESSION_PARAM = "session"

# fastapi/starlette response class -> the wreath one, for `from
# fastapi.responses import …`. `PlainTextResponse` is `TextResponse` here, and
# the two JSON-encoder variants collapse onto the one `JSONResponse`, because
# wreath's codec is native and there is nothing to choose between.
_RESPONSE_RENAME = {
    "JSONResponse": "JSONResponse",
    "ORJSONResponse": "JSONResponse",
    "UJSONResponse": "JSONResponse",
    "HTMLResponse": "HTMLResponse",
    "PlainTextResponse": "TextResponse",
    "RedirectResponse": "RedirectResponse",
    "StreamingResponse": "StreamingResponse",
    "FileResponse": "FileResponse",
    "Response": "Response",
}
# Module prefixes whose members are renamed by `_RESPONSE_RENAME`.
_RESPONSE_MODULES = ("fastapi.responses", "starlette.responses")
# What each wreath response class calls its first argument, which is what
# fastapi's `content=` becomes. All of them accept it by keyword, so this is a
# rename in place and never a reordering.
_RESPONSE_BODY_ARG = {
    "JSONResponse": "data",
    "HTMLResponse": "body",
    "TextResponse": "body",
    "StreamingResponse": "body",
    "Response": "body",
    "RedirectResponse": "url",
    "FileResponse": "path",
}

# Whole-module import swaps: `from <old> import <name>` becomes
# `from <new module for that name> import <new name>`, and every call site keeps
# working because the name is the same or is rewritten with it.
_TESTCLIENT_MODULES = ("fastapi.testclient", "starlette.testclient")
_TEST_REQUEST_METHODS = frozenset(
    {"request", "get", "post", "put", "patch", "delete", "head", "options"}
)
# arrow constructor -> the `wreath.temporal` function that replaces it.
_ARROW_RENAME = {
    "utcnow": "now",
    "now": "now",
    "get": "parse",
    "fromtimestamp": "from_wall_clock",
    "fromdatetime": "parse",
}

#: Every framework name that becomes a wreath name, by where it came from. One
#: table, read at every mention of the name — an annotation, an `except` clause,
#: an `isinstance` — rather than only where it is called, because that is where
#: most of them appear.
_RENAMED_ORIGINS: dict[str, str] = {
    "fastapi.FastAPI": "Wreath",
    "fastapi.APIRouter": "Router",
    **{
        f"{module}.{old}": new
        for module in _RESPONSE_MODULES
        for old, new in _RESPONSE_RENAME.items()
    },
    **{f"{module}.TestClient": "TestClient" for module in _TESTCLIENT_MODULES},
    "fastapi.Response": "Response",
    "httpx.Response": "ClientResponse",
    "httpx.HTTPError": "ClientError",
    "httpx.TimeoutException": "ClientError",
    "httpx.NetworkError": "ClientError",
    "httpx.ReadError": "ClientError",
}


#: Query verdicts whose target is fully determined, and which the emitter
#: therefore writes out rather than describing. Everything else — a write, a
#: dynamic projection or relation traversal — keeps
#: its note, because a person still has to decide something.
_QUERY_TRANSLATED = frozenset(
    {
        "orm.query.filter_exact",
        "orm.query.get_or_none_exact",
        "orm.query.get_exact",
        "orm.query.create_exact",
        "orm.query.all",
        "orm.query.page_exact",
        "orm.query.count",
        "orm.query.exists",
        "orm.query.order_exact",
        "orm.query.eager_exact",
        "orm.query.select_all_exact",
        "orm.query.get_or_create_exact",
        "orm.query.values_exact",
    }
)

#: How each chain verb contributes to the wreath query, and what runs it.
#: `None` means the verb only builds; a string names the `session` method.
_QUERY_RUNNER = {
    "all": "fetch",
    "get_or_none": "fetch_one",
    "get": "require_one",
    "create": "create",
    "count": "count",
    "exists": "exists",
}


def _snake(name: str) -> str:
    """A stable local-name spelling for one statically known model name."""
    pieces: list[str] = []
    for character in name:
        if character.isupper() and pieces:
            pieces.append("_")
        pieces.append(character.lower() if character.isalnum() else "_")
    return "".join(pieces).strip("_") or "row"


class _QueryPlan:
    """The wreath query one `Model.objects.…` chain becomes, assembled verb by verb."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.wheres: list[str] = []
        self.orders: list[str] = []
        self.includes: list[str] = []
        self.limit: str | None = None
        self.offset: str | None = None
        self.runner: str | None = None
        self.write_values: list[str] = []
        self.primary_key: str | None = None
        self.create_pairs: list[tuple[str, str]] = []
        self.existing_name: str | None = None
        self.projection_pairs: list[tuple[str, str]] = []
        self.row_name: str | None = None

    def step(self, emitter: _Emitter, verb: str, call: ast.Call | None) -> bool:
        """Fold one verb into the plan; `False` if it is not one we can write."""
        if self.runner is not None:
            return False  # nothing chains after the run
        if verb == "get_or_create":
            if call is None or call.args or not call.keywords:
                return False
            pairs = [
                (keyword.arg, emitter._seg(keyword.value))
                for keyword in call.keywords
                if keyword.arg is not None
            ]
            names = {name for name, _value in pairs}
            if (
                len(pairs) != len(call.keywords)
                or not names
                or names & {"_defaults", "defaults"}
                or any("__" in name for name in names)
            ):
                return False
            self.create_pairs = pairs
            self.wheres = [
                f"{self.model}.{name} == {value}" for name, value in pairs
            ]
            self.write_values = [f"{name}={value}" for name, value in pairs]
            self.runner = "get_or_create"
            return True
        if verb == "select_all":
            return call is not None and not call.args and not call.keywords
        if verb == "values":
            names = _projection_names(call)
            if names is not None:
                pairs = [emitter._projection_pair(self.model, name) for name in names]
                if any(pair is None for pair in pairs):
                    return False
                self.projection_pairs = [pair for pair in pairs if pair is not None]
            elif call is None or call.args or call.keywords or not self.projection_pairs:
                return False
            self.runner = "values"
            return True
        if verb == "values_list":
            names = _projection_names(call)
            if names is not None:
                pairs = [emitter._projection_pair(self.model, name) for name in names]
                if any(pair is None for pair in pairs):
                    return False
                self.projection_pairs = [pair for pair in pairs if pair is not None]
            elif call is None or call.args or call.keywords or not self.projection_pairs:
                return False
            self.runner = "values_list"
            return True
        if verb == "fields":
            names = _projection_names(call)
            if names is None:
                return False
            pairs = [emitter._projection_pair(self.model, name) for name in names]
            if any(pair is None for pair in pairs):
                return False
            self.projection_pairs = [pair for pair in pairs if pair is not None]
            return True
        if verb in ("filter", "all", "get_or_none", "get", "count", "exists"):
            if verb == "get" and call is not None and len(call.keywords) == 1:
                keyword = call.keywords[0]
                if keyword.arg in ("id", "pk"):
                    self.primary_key = emitter._seg(keyword.value)
                    self.runner = "require"
                    return not call.args
            for keyword in call.keywords if call else ():
                predicate = emitter._predicate(self.model, keyword)
                if predicate is None:
                    return False
                self.wheres.append(predicate)
            if call is not None and call.args:
                return False
            self.runner = _QUERY_RUNNER.get(verb)
            return True
        if verb == "create":
            if call is None or call.args:
                return False
            self.write_values = [
                f"{keyword.arg}={emitter._seg(keyword.value)}"
                if keyword.arg is not None
                else f"**{emitter._seg(keyword.value)}"
                for keyword in call.keywords
            ]
            self.runner = "create"
            return True
        if verb == "order_by":
            for argument in call.args if call else ():
                if not _order_argument_is_mechanical(argument, self.model):
                    return False
                if isinstance(argument, ast.Constant):
                    name = argument.value
                    if not isinstance(name, str):
                        return False
                    column = f"{self.model}.{name.lstrip('-')}"
                    self.orders.append(
                        f"{column}.desc()" if name.startswith("-") else column
                    )
                else:
                    self.orders.append(emitter._seg(argument))
            return bool(self.orders)
        if verb == "first":
            if call is None or call.args or call.keywords or not self.orders:
                return False
            self.limit = "1"
            self.runner = "fetch_one"
            return True
        if verb in ("select_related", "prefetch_related"):
            paths = _eager_paths(call)
            if paths is None:
                return False
            for path in paths:
                expression = emitter._include_expression(self.model, path)
                if expression is None:
                    return False
                self.includes.append(expression)
            return bool(self.includes)
        if verb in ("limit", "offset"):
            if call is None or len(call.args) != 1 or call.keywords:
                return False
            setattr(self, verb, emitter._seg(call.args[0]))
            return True
        if verb == "paginate":
            values = _pagination_values(call)
            if values is None:
                return False
            page, page_size = (emitter._seg(value) for value in values)
            self.limit = page_size
            self.offset = f"({page} - 1) * {page_size}"
            return True
        if verb == "delete":
            if call is None or call.args or call.keywords:
                return False
            self.runner = "delete_where"
            return True
        if verb == "update":
            if call is None or call.args or not call.keywords:
                return False
            self.write_values = [
                f"{keyword.arg}={emitter._seg(keyword.value)}"
                if keyword.arg is not None
                else f"**{emitter._seg(keyword.value)}"
                for keyword in call.keywords
            ]
            self.runner = "update_where"
            return True
        return False

    def render(self, session: str | None) -> str:
        if self.runner == "get_or_create" and self.existing_name is not None:
            query = f"{self.model}.select().where({', '.join(self.wheres)})"
            values = ", ".join(self.write_values)
            existing = self.existing_name
            return (
                f"(({existing}, False) if "
                f"({existing} := await {session}.fetch_one({query})) is not None "
                f"else (await {session}.create({self.model}, {values}), True))"
            )
        if self.runner == "create":
            suffix = f", {', '.join(self.write_values)}" if self.write_values else ""
            return f"await {session}.create({self.model}{suffix})"
        if self.runner == "require" and self.primary_key is not None:
            return f"await {session}.require({self.model}, {self.primary_key})"
        selected = ", ".join(
            f"{self.model}.{attribute}" for _key, attribute in self.projection_pairs
        )
        query = f"{self.model}.select({selected})" if selected else f"{self.model}.select()"
        if self.wheres:
            query += f".where({', '.join(self.wheres)})"
        for relation in self.includes:
            query += f".include({relation})"
        if self.orders:
            query += f".order_by({', '.join(self.orders)})"
        if self.limit is not None:
            query += f".limit({self.limit})"
        if self.offset is not None:
            query += f".offset({self.offset})"
        if self.runner is None:
            return query
        if self.runner == "values" and self.row_name is not None:
            pairs = ", ".join(
                f"{key!r}: {self.row_name}.{attribute}"
                for key, attribute in self.projection_pairs
            )
            return f"[{{{pairs}}} for {self.row_name} in await {session}.fetch({query})]"
        if self.runner == "values_list" and self.row_name is not None:
            values = ", ".join(
                f"{self.row_name}.{attribute}"
                for _key, attribute in self.projection_pairs
            )
            if len(self.projection_pairs) == 1:
                values += ","
            return f"[({values}) for {self.row_name} in await {session}.fetch({query})]"
        if self.runner == "exists":
            # Wreath has no `exists()`; the count is the same round trip.
            return f"await {session}.count({query}) > 0"
        if self.runner == "update_where":
            values = ", ".join(self.write_values)
            return f"await {session}.update_where({query}, {values})"
        return f"await {session}.{self.runner}({query})"


class EmitError(Exception):
    """A generated file failed the round-trip ast.parse guard (a tool bug)."""


@dataclass(frozen=True)
class PortResult:
    """What one `port_tree` run wrote, left alone, and could not read.

    `skipped` and `failed` are deliberately separate. A *skip* is a success:
    the output was already current, or it had been hand-edited and refusing to
    clobber it is the correct answer. A *failure* is a file that could not be
    read or translated at all, so nothing about it reached the output tree. A
    caller that folded the two together could not tell "your tree is already
    ported" from "a third of your tree never made it".
    """

    written_files: list[Path] = field(default_factory=list)
    regenerated: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[SkippedFile] = field(default_factory=list)


#: AST nodes that carry a source span. `ast.AST` itself declares none of
#: `lineno`/`col_offset`/`end_lineno`/`end_col_offset` -- only statements and
#: expressions do (plus `ast.arg` and `ast.keyword`, for signature and call-site
#: surgery) -- so annotating a span argument as `ast.AST` claims more than the
#: type provides.
_Positioned = ast.stmt | ast.expr | ast.arg | ast.keyword


def _span_end(node: _Positioned) -> tuple[int, int]:
    """The (line, col) just past `node`.

    `end_lineno`/`end_col_offset` are optional on the AST classes because a
    node synthesized by hand need not carry them. Every node here came from
    `ast.parse`, which always populates them; if one somehow has not, the span
    is unknown and rewriting it would silently corrupt the output, so refuse.
    """
    if node.end_lineno is None or node.end_col_offset is None:
        raise ValueError(f"{type(node).__name__} at line {node.lineno} has no end position")
    return node.end_lineno, node.end_col_offset


# --------------------------------------------------------------------------- edits
class _Buffer:
    """Byte-accurate span replacements + line-start insertions over one source."""

    def __init__(self, source: str) -> None:
        self.src = source
        self.b = source.encode("utf-8")
        self._starts = [0]
        for i, byte in enumerate(self.b):
            if byte == 0x0A:
                self._starts.append(i + 1)
        self._edits: list[tuple[int, int, bytes]] = []

    def _off(self, line: int, col: int) -> int:
        return self._starts[line - 1] + col

    def line_indent(self, line: int) -> str:
        start = self._starts[line - 1]
        end = self.b.find(b"\n", start)
        raw = self.b[start : (end if end != -1 else len(self.b))]
        return raw[: len(raw) - len(raw.lstrip(b" \t"))].decode("utf-8")

    def start_of_line(self, line: int) -> int:
        return self._starts[line - 1]

    def start_of(self, node: _Positioned) -> int:
        return self._off(node.lineno, node.col_offset)

    def end_of(self, node: _Positioned) -> int:
        return self._off(*_span_end(node))

    def replace(self, node: _Positioned, text: str) -> None:
        self._edits.append((self.start_of(node), self.end_of(node), text.encode("utf-8")))

    def replace_span(self, s_node: _Positioned, e_node: _Positioned, text: str) -> None:
        self._edits.append((self.start_of(s_node), self.end_of(e_node), text.encode("utf-8")))

    def insert_before_line(self, line: int, text: str) -> None:
        off = self._starts[line - 1]
        self._edits.append((off, off, (text + "\n").encode("utf-8")))

    def render(self) -> str:
        # Apply non-overlapping edits from the end; drop any that would overlap an
        # already-applied region (defensive — declarative spans shouldn't collide).
        b = self.b
        applied_start = len(b) + 1
        for s, e, repl in sorted(self._edits, key=lambda x: (x[0], x[1]), reverse=True):
            if e > applied_start:
                continue  # overlap: skip rather than corrupt
            b = b[:s] + repl + b[e:]
            applied_start = min(applied_start, s)
        return b.decode("utf-8")


# --------------------------------------------------------------------------- walker
class _Emitter(ast.NodeVisitor):
    def __init__(
        self,
        source: str,
        imports: _Imports,
        pk_types: dict[str, str] | None = None,
        *,
        opinionated: bool = False,
    ) -> None:
        self.buf = _Buffer(source)
        self.src = source
        self.imports = imports
        self.opinionated = opinionated
        self.session_functions: frozenset[str] = frozenset()
        self.session_definition_sites: frozenset[tuple[str, int]] = frozenset()
        self.session_call_sites: frozenset[tuple[str, int, int]] = frozenset()
        self.session_sites_resolved = False
        self._source_path = ""
        self.pk_types = pk_types or {}  # ORM model name -> PK PgType (FK inference)
        self.orm_columns: dict[str, set[str]] = {}
        self.orm_relations: dict[str, dict[str, str]] = {}
        self.orm_tables: dict[str, str] = {}
        self.orm_unique_constraints: dict[str, tuple[frozenset[str], ...]] = {}
        self.orm_mixins: frozenset[str] = frozenset()
        self.pydantic_models: frozenset[str] = frozenset()
        self.settings_models: frozenset[str] = frozenset()
        self._settings_custom_init: frozenset[str] = frozenset()
        self._settings_bindings: dict[str, tuple[str | None, str | None]] = {}
        self._http_clients: dict[int, tuple[str, ast.expr | None]] = {}
        self._http_client_calls: dict[int, int] = {}
        self._http_requests: dict[int, int] = {}
        self._http_dynamic_clients: dict[int, ast.expr] = {}
        self._http_url_parts: dict[int, str] = {}
        self._http_request_timeouts: dict[int, ast.expr] = {}
        self._http_retries: dict[int, ast.expr] = {}
        self._http_transport_assignments: set[int] = set()
        self._http_timeout_constants: set[str] = set()
        self._http_responses: set[tuple[int | None, str]] = set()
        self.needs_urlencode = False
        self._global_test_clients: dict[str, ast.Call] = {}
        self._fixture_test_clients: set[str] = set()
        self.needs_fixture = False
        self._pydantic_partial_family: frozenset[str] = frozenset()
        self.needs: set[str] = set()  # extra `from wreath import` names
        self._from_fastapi_wreath: set[str] = set()  # names already on the rewritten fastapi import
        self.needs_annotated = False  # `from typing import Annotated`
        self.needs_dataclass = False  # `from dataclasses import dataclass`
        # `field` is imported separately from the decorator, because it is only
        # needed for a mutable default and it is an ordinary English word: three
        # real modules use `field` as a loop variable, and importing it there
        # shadowed their own name.
        self.needs_field = False  # `field` (a default_factory)
        self.needs_uuid = False  # plain `import uuid` (a Uuid FK annotation)
        self.needs_temporal = False  # `from wreath import temporal`
        # Names the import rewrite must NOT drop, because a reference to them
        # survived the visit. A codemod that deletes an import whose name is
        # still used produces a module that imports nothing and runs nowhere:
        # `Field`, `HTTPException` and `BaseModel` all went missing this way, and
        # every module it happened to parsed, compiled, and then raised
        # `NameError` on import. Filled by `visit_Name`
        # and `visit_Attribute`, which is why `rewrite_imports` now runs *after*
        # the walk rather than before it.
        self._retain: set[str] = set()
        # The name a session is reachable under in the function being walked,
        # and whether a query wanted one and could not have it.
        self._session: str | None = None
        self._session_wanted = False
        # Byte spans replaced whole, so nothing inside one is edited again.
        self._replaced: list[tuple[int, int]] = []
        # `id(node)` of every name node subsumed by a rewritten span, so a
        # reference the emitter already dealt with does not also retain its
        # import. Marked at each rewrite site, read by `visit_Name`.
        self._rewritten: set[int] = set()
        self.annotated_lines: set[tuple[int, str]] = set()  # (line, rule_id) dedupe
        self._dep_targets: set[str] = set()  # function names referenced by Depends(<name>)
        self._claimed_objects: set[int] = set()  # `.objects` billed by its verb
        # Same parent map the analyzer builds, for the same reason: the verdict
        # a query gets depends on its arguments and on the verbs chained after
        # it, and neither is visible from the head node alone.
        self._parents: dict[int, ast.AST] = {}
        # Names handed to an application as `lifespan=`; filled by `visit_Module`,
        # since the `FastAPI(lifespan=...)` call sits below the `def` it names.
        self._lifespan_names: frozenset[str] = frozenset()
        self._test_clients: frozenset[str] = frozenset()
        self._removed_pydantic_imports: set[str] = set()
        self._removed_middleware_imports: set[str] = set()
        self._used_names: set[str] = set()

    def visit_Module(self, node: ast.Module) -> None:
        self._parents = parent_map(node)
        self._used_names = {
            candidate.id for candidate in ast.walk(node) if isinstance(candidate, ast.Name)
        }
        class_bases = {
            candidate.name: {
                self._seg(base.func.value)
                if (
                    isinstance(base, ast.Call)
                    and isinstance(base.func, ast.Attribute)
                    and base.func.attr == "model_as_partial"
                )
                else self._seg(base).split("[", 1)[0]
                for base in candidate.bases
            }
            for candidate in node.body
            if isinstance(candidate, ast.ClassDef)
        }
        partial = {
            name
            for name, bases in class_bases.items()
            if any(
                isinstance(base, ast.Call)
                and isinstance(base.func, ast.Attribute)
                and base.func.attr == "model_as_partial"
                for candidate in node.body
                if isinstance(candidate, ast.ClassDef) and candidate.name == name
                for base in candidate.bases
            )
        }
        for candidate in node.body:
            if not isinstance(candidate, ast.ClassDef):
                continue
            for base in candidate.bases:
                if (
                    isinstance(base, ast.Call)
                    and isinstance(base.func, ast.Attribute)
                    and base.func.attr == "model_as_partial"
                ):
                    partial.add(self._seg(base.func.value))
        changed = True
        while changed:
            changed = False
            for name, bases in class_bases.items():
                if name in partial or not (bases & partial):
                    continue
                partial.add(name)
                changed = True
            for name in tuple(partial):
                for base in class_bases.get(name, ()):
                    if base in class_bases and base not in partial:
                        partial.add(base)
                        changed = True
        self._pydantic_partial_family = frozenset(partial)
        def owner_id(candidate: ast.AST) -> int | None:
            owner = self._parents.get(id(candidate))
            while owner is not None:
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return id(owner)
                owner = self._parents.get(id(owner))
            return None

        def owner_chain(candidate: ast.AST) -> tuple[int, ...]:
            owners: list[int] = []
            owner = self._parents.get(id(candidate))
            while owner is not None:
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owners.append(id(owner))
                owner = self._parents.get(id(owner))
            return tuple(owners)

        transport_definitions: dict[
            str, list[tuple[ast.Assign, ast.Call, int | None]]
        ] = {}
        for candidate in ast.walk(node):
            if (
                isinstance(candidate, ast.Assign)
                and len(candidate.targets) == 1
                and isinstance(candidate.targets[0], ast.Name)
                and isinstance(candidate.value, ast.Call)
                and self.imports.origin(candidate.value.func) == "httpx.AsyncHTTPTransport"
            ):
                transport_definitions.setdefault(candidate.targets[0].id, []).append(
                    (candidate, candidate.value, owner_id(candidate))
                )
            if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                continue
            target = (
                candidate.target
                if isinstance(candidate, ast.AnnAssign)
                else candidate.targets[0] if len(candidate.targets) == 1 else None
            )
            value = candidate.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Call)
                and self._http_timeout_total(value) is not None
            ):
                self._http_timeout_constants.add(target.id)
        self._lifespan_names = lifespan_names(node)
        self._settings_custom_init = frozenset(
            cls.name
            for cls in node.body
            if isinstance(cls, ast.ClassDef)
            and cls.name in self.settings_models
            and any(
                isinstance(statement, ast.FunctionDef)
                and statement.name == "__init__"
                and bool(
                    statement.args.posonlyargs
                    or statement.args.kwonlyargs
                    or len(statement.args.args) > 1
                    or statement.args.vararg
                    or statement.args.kwarg
                )
                for statement in cls.body
            )
        )
        for cls in node.body:
            if not isinstance(cls, ast.ClassDef) or cls.name not in self.settings_models:
                continue
            env_file = prefix = None
            for statement in cls.body:
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if not any(
                    isinstance(target, ast.Name) and target.id == "model_config"
                    for target in targets
                ) or not isinstance(statement.value, ast.Call):
                    continue
                for keyword in statement.value.keywords:
                    if keyword.arg == "env_file":
                        env_file = self._seg(keyword.value)
                    elif keyword.arg == "env_prefix":
                        prefix = self._seg(keyword.value)
            self._settings_bindings[cls.name] = (env_file, prefix)
        for candidate in ast.walk(node):
            if not isinstance(candidate, (ast.With, ast.AsyncWith)):
                continue
            for item in candidate.items:
                call = item.context_expr
                if not (
                    isinstance(call, ast.Call)
                    and self.imports.origin(call.func) == "httpx.AsyncClient"
                    and isinstance(item.optional_vars, ast.Name)
                    and not call.args
                ):
                    continue
                key = id(item)
                options = {keyword.arg for keyword in call.keywords}
                if None in options or not options <= {
                    "base_url",
                    "headers",
                    "timeout",
                    "transport",
                }:
                    continue
                transport = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == "transport"),
                    None,
                )
                if transport is not None:
                    transport_assignment = None
                    if isinstance(transport, ast.Name):
                        scopes = owner_chain(call)
                        candidates = [
                            (assignment, definition)
                            for assignment, definition, scope in transport_definitions.get(
                                transport.id, ()
                            )
                            if assignment.lineno < call.lineno
                            and (scope in scopes or (scope is None and not scopes))
                        ]
                        if candidates:
                            transport_assignment, transport = max(
                                candidates, key=lambda item: item[0].lineno
                            )
                    if not (
                        isinstance(transport, ast.Call)
                        and self.imports.origin(transport.func)
                        == "httpx.AsyncHTTPTransport"
                        and not transport.args
                        and all(keyword.arg == "retries" for keyword in transport.keywords)
                    ):
                        continue
                    retries = next(
                        (
                            keyword.value
                            for keyword in transport.keywords
                            if keyword.arg == "retries"
                        ),
                        None,
                    )
                    if retries is not None:
                        self._http_retries[key] = retries
                        if transport_assignment is not None:
                            self._http_transport_assignments.add(id(transport_assignment))
                requests = [
                    request
                    for request in ast.walk(candidate)
                    if isinstance(request, ast.Call)
                    and isinstance(request.func, ast.Attribute)
                    and isinstance(request.func.value, ast.Name)
                    and request.func.value.id == item.optional_vars.id
                    and request.func.attr in _TEST_REQUEST_METHODS
                    and all(
                        keyword.arg
                        in {"headers", "json", "content", "data", "params", "timeout"}
                        for keyword in request.keywords
                    )
                ]
                if len(requests) == 1:
                    request_timeout = next(
                        (
                            keyword.value
                            for keyword in requests[0].keywords
                            if keyword.arg == "timeout"
                        ),
                        None,
                    )
                    if request_timeout is not None:
                        self._http_request_timeouts[key] = request_timeout
                if "base_url" not in options:
                    if len(requests) != 1:
                        continue
                    request = requests[0]
                    # Re-narrowed: the comprehension above only keeps calls whose
                    # `func` is an Attribute, but `requests` is a list of `ast.Call`
                    # and that guarantee does not survive the binding.
                    method = (
                        request.func.attr
                        if isinstance(request.func, ast.Attribute)
                        else ""
                    )
                    target_index = 1 if method == "request" else 0
                    if len(request.args) <= target_index:
                        continue
                    self._http_dynamic_clients[key] = request.args[target_index]
                headers = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == "headers"),
                    None,
                )
                self._http_clients[key] = (item.optional_vars.id, headers)
                self._http_client_calls[id(call)] = key
                self._http_requests.update((id(request), key) for request in requests)
        for candidate in ast.walk(node):
            if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                continue
            value = candidate.value
            if not (
                isinstance(value, ast.Await)
                and isinstance(value.value, ast.Call)
                and id(value.value) in self._http_requests
            ):
                continue
            targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
            self._http_responses.update(
                (owner_id(candidate), target.id)
                for target in targets
                if isinstance(target, ast.Name)
            )
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and self._is_test_client_call(statement.value)
            ):
                self._global_test_clients[statement.targets[0].id] = statement.value
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fixture = any(
                self.imports.origin(
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                ).endswith("pytest.fixture")
                or (
                    isinstance(decorator, ast.Attribute)
                    and decorator.attr == "fixture"
                )
                for decorator in statement.decorator_list
            )
            if fixture and any(
                isinstance(inner, (ast.Return, ast.Yield))
                and isinstance(inner.value, ast.Call)
                and self._is_test_client_call(inner.value)
                for inner in ast.walk(statement)
            ):
                self._fixture_test_clients.add(statement.name)
        self.generic_visit(node)

    # -- helpers -----------------------------------------------------------------
    def _seg(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.src, node) or ""

    def _enclosing_callable_id(self, node: ast.AST) -> int | None:
        owner = self._parents.get(id(node))
        while owner is not None:
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return id(owner)
            owner = self._parents.get(id(owner))
        return None

    def _fresh_name(self, preferred: str) -> str:
        """Reserve a local temporary without shadowing a source identifier."""
        name = preferred
        suffix = 2
        while name in self._used_names:
            name = f"{preferred}_{suffix}"
            suffix += 1
        self._used_names.add(name)
        return name

    def _session_definition_selected(self, node) -> bool:
        if self.session_sites_resolved:
            return (self._source_path, node.lineno) in self.session_definition_sites
        return node.name in self.session_functions

    def _session_call_selected(self, node: ast.Call, called: str | None) -> bool:
        if self.session_sites_resolved:
            return (
                self._source_path,
                node.lineno,
                node.col_offset,
            ) in self.session_call_sites
        return called in self.session_functions

    def _asgi_test_client_app(self, call: ast.Call) -> ast.expr | None:
        if self.imports.origin(call.func) != "httpx.AsyncClient":
            return None
        options = {keyword.arg for keyword in call.keywords}
        if None in options or not options <= {"transport", "base_url", "headers"}:
            return None
        transport = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "transport"),
            None,
        )
        if not (
            isinstance(transport, ast.Call)
            and self.imports.origin(transport.func) == "httpx.ASGITransport"
        ):
            return None
        app = next(
            (keyword.value for keyword in transport.keywords if keyword.arg == "app"),
            transport.args[0] if transport.args else None,
        )
        return app if isinstance(app, ast.expr) else None

    def _is_test_client_call(self, call: ast.Call) -> bool:
        return (
            self.imports.origin(call.func)
            in {f"{module}.TestClient" for module in _TESTCLIENT_MODULES}
            or self._asgi_test_client_app(call) is not None
        )

    def _test_client_text(self, call: ast.Call) -> str:
        app = self._asgi_test_client_app(call)
        if app is None:
            return self._seg(call)
        self.needs.add("TestClient")
        headers = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "headers"),
            None,
        )
        suffix = "" if headers is None else f", headers={self._seg(headers)}"
        self._resolve(call.lineno, "ext.httpx")
        self._resolve(call.lineno, "test.client")
        self._rewritten.update(id(item) for item in ast.walk(call))
        return f"TestClient({self._seg(app)}{suffix})"

    def _annotate(self, line: int, rule_id: str, extra: str = "") -> None:
        """Write the rule's own wording, with a detail in brackets after it."""
        _c, _cat, tag, message = RULES[rule_id]
        if extra:
            message = f"{message} ({extra})"
        self._annotate_message(line, rule_id, tag, message)

    def _resolve(self, line: int, rule_id: str) -> None:
        """Record that this line's finding was *acted on*, so no note is written.

        The shared pass writes a note for every finding the report calls
        needs-review, which is right until `--opinionated` settles one itself:
        the file would then carry both the decision and the question about it.
        """
        self.annotated_lines.add((line, rule_id))

    def _note(self, line: int, rule_id: str, text: str) -> None:
        """Write `text` *instead of* the rule's wording, under the same rule id.

        For the cases where the emitter knows something specific enough that the
        general sentence in front of it is noise — "this column stored a UUID as
        text" says everything, and prefixing it with the catalog's description of
        every ormar column does not help anyone read it.
        """
        self._annotate_message(line, rule_id, RULES[rule_id][2], text)

    def _annotate_message(self, line: int, rule_id: str, tag: str, message: str) -> None:
        key = (line, rule_id)
        if key in self.annotated_lines:
            return
        self.annotated_lines.add(key)
        indent = self.buf.line_indent(line)
        # A note is one comment line, so it cannot contain a newline. Quoting a
        # multi-line construct back at the reader put one in, and the comment
        # then swallowed the code under it — a file that would not parse.
        self.buf.insert_before_line(
            line, f"{indent}# TODO(wreath-port: [{tag}] {' '.join(message.split())} [{rule_id}])"
        )

    # -- imports -----------------------------------------------------------------
    def rewrite_imports(self, tree: ast.Module) -> None:
        last_import_line = 0
        live_httpx = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and self.imports.origin(node).startswith("httpx")
            and id(node) not in self._rewritten
            and not self._inside_replaced(node)
        }
        # Only the imports at the *top* of the file decide where new ones go. One
        # module that imports something halfway down the file, and following the
        # last import anywhere put `from wreath.orm import Session` below the
        # function that used it.
        in_prologue = True
        for node in tree.body:
            if in_prologue and isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import_line = max(last_import_line, _span_end(node)[0])
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                in_prologue = False
            if isinstance(node, ast.ImportFrom) and node.module == "fastapi" and node.level == 0:
                self._rewrite_from_fastapi(node)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module in ("fastapi.exceptions", "starlette.exceptions")
                and node.level == 0
                and any(a.name == "HTTPException" for a in node.names)
            ):
                # The same class, imported the long way round. Missing this
                # spelling left the module importing fastapi's HTTPException
                # *and* wreath's, under one name.
                self._rewrite_from_fastapi(node)
            elif isinstance(node, ast.ImportFrom) and node.module == "pydantic" and node.level == 0:
                self._rewrite_from_pydantic(node)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "pydantic_settings"
                and node.level == 0
            ):
                gone = {
                    alias.name
                    for alias in node.names
                    if alias.name in {"BaseSettings", "SettingsConfigDict"}
                    and (alias.asname or alias.name) not in self._retain
                }
                self.buf.replace(node, self._keep_leftover(node, gone, node.module))
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "pydantic_partial"
                and node.level == 0
            ):
                gone = {
                    alias.name
                    for alias in node.names
                    if alias.name == "PartialModelMixin"
                    and (alias.asname or alias.name) not in self._retain
                }
                self.buf.replace(node, self._keep_leftover(node, gone, node.module))
            elif (
                isinstance(node, ast.ImportFrom)
                and (module := node.module) is not None
                and module.startswith(("fastapi.middleware", "starlette.middleware"))
                and any(a.name in {"CORSMiddleware", "TrustedHostMiddleware"} for a in node.names)
            ):
                imported = {a.name for a in node.names}
                drop = imported & {"CORSMiddleware", "TrustedHostMiddleware"}
                self.buf.replace(
                    node,
                    self._keep_leftover(node, drop=drop, module=module),
                )
            elif isinstance(node, ast.ImportFrom) and node.module in _RESPONSE_MODULES:
                self._swap_import(node, _RESPONSE_RENAME)
            elif isinstance(node, ast.ImportFrom) and node.module in _TESTCLIENT_MODULES:
                self._swap_import(node, {"TestClient": "TestClient"})
            elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
                keep = [
                    alias
                    for alias in node.names
                    if (alias.asname or alias.name) in live_httpx
                ]
                self.buf.replace(
                    node,
                    "from httpx import " + ", ".join(self._alias_str(alias) for alias in keep)
                    if keep
                    else "",
                )
            elif isinstance(node, ast.Import) and any(
                alias.name == "httpx" for alias in node.names
            ):
                keep = [
                    alias
                    for alias in node.names
                    if alias.name != "httpx" or (alias.asname or alias.name) in live_httpx
                ]
                self.buf.replace(
                    node,
                    "import " + ", ".join(self._alias_str(alias) for alias in keep)
                    if keep
                    else "",
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "cachetools":
                self._drop_replaced(node, set(_CACHE_RENAME))
            elif isinstance(node, ast.ImportFrom) and self._removed_middleware_imports:
                gone = {
                    alias.name
                    for alias in node.names
                    if alias.name in self._removed_middleware_imports
                    and (alias.asname or alias.name) not in self._retain
                }
                if gone:
                    self.buf.replace(
                        node, self._keep_leftover(node, gone, node.module or "")
                    )
            elif (
                isinstance(node, ast.Import)
                and all((a.asname or a.name) not in self._retain for a in node.names)
                and all(a.name in ("arrow",) for a in node.names)
            ):
                self.buf.replace(node, "")
            elif (
                isinstance(node, ast.Import)
                and all((a.asname or a.name) not in self._retain for a in node.names)
                and all(a.name == "strawberry" for a in node.names)
            ):
                self.buf.replace(node, "")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "fastapi.encoders"
                and "jsonable_encoder" not in self._retain
            ):
                self.buf.replace(node, self._keep_leftover(node, {"jsonable_encoder"}, node.module))
        self._last_import_line = last_import_line

    def _drop_replaced(self, node: ast.ImportFrom, names: set[str]) -> None:
        """Drop the imported names whose every call site was rewritten."""
        gone = {
            a.name
            for a in node.names
            if a.name in names and (a.asname or a.name) not in self._retain
        }
        if gone:
            self.buf.replace(node, self._keep_leftover(node, gone, node.module or ""))

    def _swap_import(self, node: ast.ImportFrom, rename: dict[str, str]) -> None:
        """Point the names this import brings in at their wreath equivalents.

        The response classes were the clearest gap: the report has always called
        `fastapi.responses.JSONResponse` a one-to-one rename, and the emitter
        left the import pointing at fastapi — so a "ported" module still needed
        fastapi installed to start.
        """
        moved = [a for a in node.names if a.name in rename and a.asname is None]
        if not moved:
            return
        for alias in moved:
            self.needs.add(rename[alias.name])
        self.buf.replace(
            node, self._keep_leftover(node, {a.name for a in moved}, node.module or "")
        )

    def _rewrite_from_fastapi(self, node: ast.ImportFrom) -> None:
        keep: list[ast.alias] = []
        wreath_names: list[str] = []
        for alias in node.names:
            if alias.name == "HTTPException":
                # Call sites with a mapped status became their own class. Any
                # other reference — `except HTTPException`, an
                # `@app.exception_handler(HTTPException)`, a 502 the table has
                # no class for — is kept, and points at `wreath.exceptions`,
                # whose `HTTPException` is the base of every class in that
                # table and a 500 in its own right.
                if "HTTPException" in self._retain:
                    self.needs.add("HTTPException")
                continue
            if alias.name in _FASTAPI_TO_WREATH:
                wreath_names.append(_FASTAPI_TO_WREATH[alias.name])
            elif alias.name == "status" and (alias.asname or alias.name) not in self._retain:
                continue  # every HTTP_* became its number
            else:
                keep.append(alias)
        self._from_fastapi_wreath.update(wreath_names)
        parts = []
        if wreath_names:
            parts.extend(_grouped_imports(wreath_names))
        if keep:
            parts.append(
                f"from {node.module} import " + ", ".join(self._alias_str(a) for a in keep)
            )
        self.buf.replace(node, "\n".join(parts) if parts else "")

    def _rewrite_from_pydantic(self, node: ast.ImportFrom) -> None:
        # A name is only dropped once every use of it is gone. `BaseModel` on a
        # class with a second base, or `Field` on a field whose marker needed a
        # human, is still written in the file — deleting its import turns a
        # reviewable port into a module that will not import at all.
        dropped = ({"BaseModel", "Field"} - self._retain) | self._removed_pydantic_imports
        keep = [a for a in node.names if a.name not in dropped]
        if any(a.name in dropped for a in node.names):
            self.needs_dataclass = True
        if keep:
            self.buf.replace(
                node, "from pydantic import " + ", ".join(self._alias_str(a) for a in keep)
            )
        else:
            self.buf.replace(node, "")

    def _keep_leftover(self, node: ast.ImportFrom, drop: set[str], module: str) -> str:
        keep = [a for a in node.names if a.name not in drop]
        if not keep:
            return ""
        return f"from {module} import " + ", ".join(self._alias_str(a) for a in keep)

    @staticmethod
    def _alias_str(a: ast.alias) -> str:
        return f"{a.name} as {a.asname}" if a.asname else a.name

    def inject_imports(self) -> None:
        lines: list[str] = []
        extra = self.needs - self._from_fastapi_wreath
        if extra:
            lines.extend(_grouped_imports(extra))
        if self.needs_annotated and "Annotated" not in self.imports.names:
            lines.append("from typing import Annotated")
        # A foreign key onto a UUID primary key is annotated `uuid.UUID`, so the
        # module needs the module. 81 emitted files referred to a `uuid` nothing
        # had imported.
        if self.needs_uuid and "uuid" not in self.imports.names:
            lines.append("import uuid")
        if self.needs_temporal and "temporal" not in self.imports.names:
            lines.append("from wreath import temporal")
        if self.needs_urlencode and "urlencode" not in self.imports.names:
            lines.append("from urllib.parse import urlencode")
        if self.needs_fixture and "fixture" not in self.imports.names:
            lines.append("from pytest import fixture")
        if self._http_dynamic_clients and "urlsplit" not in self.imports.names:
            lines.append("from urllib.parse import urlsplit, urlunsplit")
        wanted = [
            name
            for name, needed in (("dataclass", self.needs_dataclass), ("field", self.needs_field))
            if needed and name not in self.imports.names
        ]
        if wanted:
            lines.append("from dataclasses import " + ", ".join(wanted))
        additions = "\n".join(lines)
        if additions and getattr(self, "_last_import_line", 0):
            self.buf.insert_before_line(self._last_import_line + 1, additions)
        elif additions:
            self.buf.insert_before_line(1, additions + "\n")

    def annotate_findings(self, findings) -> None:
        """Write a note above every construct the report says needs a person.

        The emitter rewrites what it can and this covers the rest, straight from
        the analyzer's own verdicts — so the file a porter opens carries the same
        list the report prints, with each note sitting on the line it is about.

        Only `needs-review` and `unsupported` findings become notes. A translated
        verdict means the answer is already decided, and marking 1,078 derivable
        Alembic operations "done" would bury the 160 that are not.
        """
        for finding in findings:
            if finding.tag == TRANSLATED:
                continue
            self._annotate_message(finding.line, finding.rule_id, finding.tag, finding.message)

    # -- dependencies (Phase 3): a function referenced by Depends(<name>) gains a
    # leading `request: Request` param, exactly like a route handler.
    def collect_dep_targets(self, tree: ast.Module) -> None:
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and self.imports.origin(n.func).split(".")[-1] == "Depends":
                if n.args and isinstance(n.args[0], ast.Name):
                    self._dep_targets.add(n.args[0].id)

    # -- classes (Pydantic DTOs / ORM / custom middleware) -----------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name in self._pydantic_partial_family:
            self._annotate(node.lineno, "pydantic.partial")
            self.generic_visit(node)
            return
        for dec in node.decorator_list:
            decorated = dec.func if isinstance(dec, ast.Call) else dec
            origin = self.imports.origin(decorated)
            if origin.split(".")[-1] == "as_form":
                # translated: whole-model Annotated[Model, Form()] replaces it
                self._delete_decorator(dec)
            elif origin == "strawberry.type" and _plain_graphql_dataclass(
                self.imports, node
            ):
                self._delete_decorator(dec)
                self.needs_dataclass = True
                indent = self.buf.line_indent(node.lineno)
                self.buf.insert_before_line(node.lineno, f"{indent}@dataclass(kw_only=True)")
        projection_base = any(
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Attribute)
            and base.func.attr == "get_pydantic"
            and pydantic_projection_rule(base.func, self._parents)
            == "pydantic.get_pydantic_exact"
            for base in node.bases
        )
        if projection_base:
            self.needs_dataclass = True
            indent = self.buf.line_indent(node.lineno)
            self.buf.insert_before_line(node.lineno, f"{indent}@dataclass(kw_only=True)")
        kind = _base_kind(self.imports, node)
        if kind is None and node.name in self.pydantic_models:
            kind = "pydantic"
        elif kind is None and node.name in self.settings_models:
            kind = "settings"
        if kind == "pydantic":
            self._rewrite_pydantic_class(node)
        elif kind == "settings":
            self._rewrite_settings_class(node)
        elif kind == "ormar":
            self._rewrite_ormar_class(node)
        elif node.name in self.orm_mixins:
            self._rewrite_ormar_mixin(node)
        elif kind == "sqlmodel":
            self._note(
                node.lineno,
                "orm.model",
                "this is a SQLModel class and only ormar models are rewritten "
                "automatically. The shape is the same: Mapped[...] annotations "
                "with column(...) for each field",
            )
        self.generic_visit(node)

    def _rewrite_settings_class(self, node: ast.ClassDef) -> None:
        """Move BaseSettings onto Environment-bound dataclasses."""
        self.needs_dataclass = True
        indent = self.buf.line_indent(node.lineno)
        self.buf.insert_before_line(node.lineno, f"{indent}@dataclass(kw_only=True)")
        origins = [self.imports.origin(base) for base in node.bases]
        settings_origins = {"pydantic_settings.BaseSettings", "pydantic.BaseSettings"}
        kept = [
            self._seg(base)
            for base, origin in zip(node.bases, origins, strict=True)
            if origin not in settings_origins
        ]
        if node.bases:
            if kept:
                self.buf.replace_span(node.bases[0], node.bases[-1], ", ".join(kept))
            else:
                self._strip_all_bases(node)
            for base, origin in zip(node.bases, origins, strict=True):
                if origin in settings_origins:
                    self._rewritten.add(id(base))
        rule_id = settings_class_rule(self.imports, node)
        self._resolve(node.lineno, rule_id)
        self._rewrite_settings_custom_init(node)
        for statement in node.body:
            if isinstance(statement, ast.ClassDef) and statement.name == "Config":
                self._replace_all_of(statement, "# wreath-port: BaseSettings Config removed")
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                target = (
                    statement.target
                    if isinstance(statement, ast.AnnAssign)
                    else statement.targets[0] if len(statement.targets) == 1 else None
                )
                if isinstance(target, ast.Name) and target.id == "model_config":
                    self._replace_all_of(statement, "# wreath-port: legacy settings config removed")
                    continue
            if not (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
            ):
                continue
            value = statement.value
            if (
                isinstance(value, ast.Call)
                and self.imports.origin(value.func).split(".")[-1] == "Field"
            ):
                alias = next(
                    (kw.value for kw in value.keywords if kw.arg in ("alias", "env")),
                    None,
                )
                if alias is not None:
                    self.needs.update({"Env"})
                    self.needs_annotated = True
                    annotation = self._seg(statement.annotation)
                    self.buf.replace(
                        statement.annotation,
                        f"Annotated[{annotation}, Env({self._seg(alias)})]",
                    )
                self._rewrite_field_marker(statement, value)
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in self.settings_models
                and not value.args
                and not value.keywords
            ):
                self.needs_field = True
                self._replace_all_of(value, f"field(default_factory={value.func.id})")
            else:
                factory = _mutable_factory(value) if value is not None else None
                if factory and value is not None:
                    self.needs_field = True
                    self._replace_all_of(value, f"field(default_factory={factory})")
            self._resolve(statement.lineno, "settings.field_complex")
            self._resolve(statement.lineno, "settings.nested")

    def _rewrite_settings_custom_init(self, node: ast.ClassDef) -> None:
        init = next(
            (
                statement
                for statement in node.body
                if isinstance(statement, ast.FunctionDef) and statement.name == "__init__"
            ),
            None,
        )
        if init is None:
            return
        parameterized = bool(
            init.args.posonlyargs
            or init.args.kwonlyargs
            or len(init.args.args) > 1
            or init.args.vararg
            or init.args.kwarg
        )
        # The call is carried out of the comprehension beside its statement: a
        # list of `ast.Expr` would widen `.value` back to `ast.expr` and lose the
        # `isinstance` the filter already established.
        super_calls = [
            (statement, statement.value)
            for statement in init.body
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "__init__"
            and isinstance(statement.value.func.value, ast.Call)
            and isinstance(statement.value.func.value.func, ast.Name)
            and statement.value.func.value.func.id == "super"
        ]
        if len(super_calls) != 1:
            return
        statement, call = super_calls[0]
        if not parameterized and not call.args and not call.keywords:
            header_end = self.buf.b.find(
                b":", self.buf.start_of(init), self.buf.start_of(init.body[0])
            )
            name_at = self.buf.b.find(b"__init__", self.buf.start_of(init), header_end)
            if name_at >= 0:
                self.buf._edits.append((name_at, name_at + len("__init__"), b"__post_init__"))
                self._replace_all_of(statement, "")
            return
        if call.args or any(keyword.arg is None for keyword in call.keywords):
            return
        supplied = {keyword.arg: self._seg(keyword.value) for keyword in call.keywords}
        assignments: list[str] = []
        for field_statement in node.body:
            if not (
                isinstance(field_statement, ast.AnnAssign)
                and isinstance(field_statement.target, ast.Name)
            ):
                continue
            name = field_statement.target.id
            value = supplied.get(name)
            if value is None and field_statement.value is not None:
                value = self._seg(field_statement.value)
            if value is not None:
                assignments.append(f"self.{name} = {value}")
        if assignments:
            indent = self.buf.line_indent(statement.lineno)
            self._replace_all_of(
                statement,
                ("\n" + indent).join(assignments),
            )

    def _rewrite_pydantic_class(self, node: ast.ClassDef) -> None:
        self.needs_dataclass = True
        indent = self.buf.line_indent(node.lineno)
        # Pydantic ignores what order the fields are written in; a dataclass
        # does not, and refuses to be built at all if a required field comes
        # after one with a default. `kw_only=True` removes the ordering rule,
        # and costs nothing at the boundary because wreath builds a body model
        # by keyword (`binding._validate_dataclass` calls `cls(**kwargs)`).
        offenders = dataclass_needs_kw_only(self.imports, node)
        header = "@dataclass(kw_only=True)" if offenders else "@dataclass"
        self.buf.insert_before_line(node.lineno, f"{indent}{header}")
        # Strip Pydantic's runtime bases while preserving ordinary mixins and
        # generic bases. PartialModelMixin itself disappears; a
        # ``model_as_partial()`` base becomes a local stdlib-dataclass helper.
        base_origins = [self.imports.origin(b) for b in node.bases]
        kept: list[str] = []
        for base, origin in zip(node.bases, base_origins, strict=True):
            if origin in {"pydantic.BaseModel", "pydantic_partial.PartialModelMixin"}:
                self._rewritten.update(id(item) for item in ast.walk(base))
                continue
            if (
                isinstance(base, ast.Call)
                and isinstance(base.func, ast.Attribute)
                and base.func.attr == "model_as_partial"
                and not base.args
                and not base.keywords
            ):
                kept.append(self._seg(base))
                continue
            kept.append(self._seg(base))
        if node.bases:
            if kept:
                self.buf.replace_span(node.bases[0], node.bases[-1], ", ".join(kept))
            else:
                self._strip_all_bases(node)
        self._resolve(node.lineno, "pydantic.model")
        for stmt in node.body:
            self._rewrite_pydantic_field(stmt)
        self._rewrite_pydantic_validators(node)

    def _rewrite_pydantic_validators(self, node: ast.ClassDef) -> None:
        """Run ordinary field validators from a dataclass ``__post_init__``."""
        calls: list[str] = []
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if redundant_literal_validator(statement, self._parents, self.imports):
                continue
            marker = next(
                (
                    decorator
                    for decorator in statement.decorator_list
                    if isinstance(decorator, ast.Call)
                    and self.imports.origin(decorator.func).split(".")[-1]
                    in {"validator", "field_validator"}
                ),
                None,
            )
            if marker is None or isinstance(statement, ast.AsyncFunctionDef):
                continue
            fields = [
                argument.value
                for argument in marker.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
            if not fields or len(fields) != len(marker.args):
                continue
            self._delete_decorator(marker)
            for decorator in statement.decorator_list:
                decorated = decorator.func if isinstance(decorator, ast.Call) else decorator
                if (
                    isinstance(decorated, ast.Name) and decorated.id == "classmethod"
                ) or self.imports.origin(decorated) == "builtins.classmethod":
                    self._delete_decorator(decorator)
            self._removed_pydantic_imports.update({"validator", "field_validator"})
            self._resolve(marker.lineno, "pydantic.validator")
            calls.extend(
                f"self.{field} = self.{statement.name}(self.{field})" for field in fields
            )
        if not calls:
            return
        existing = next(
            (
                statement
                for statement in node.body
                if isinstance(statement, ast.FunctionDef)
                and statement.name == "__post_init__"
            ),
            None,
        )
        if existing is not None:
            first = existing.body[0]
            indent = self.buf.line_indent(first.lineno)
            self.buf.insert_before_line(
                first.lineno, "\n".join(f"{indent}{call}" for call in calls)
            )
            return
        indent = self.buf.line_indent(node.lineno) + "    "
        end = self.buf.end_of(node.body[-1])
        body = "\n".join(f"{indent}    {call}" for call in calls)
        self.buf._edits.append(
            (end, end, f"\n\n{indent}def __post_init__(self) -> None:\n{body}".encode())
        )

    def _strip_all_bases(self, node: ast.ClassDef) -> None:
        """Remove the whole `(...)` base list — correct only when it is the sole base."""
        b = self.buf.b
        pstart = self.buf.start_of(node.bases[0])
        pend = self.buf.end_of(node.bases[-1])
        open_i = b.rfind(b"(", 0, pstart)
        close_i = b.find(b")", pend)
        if open_i != -1 and close_i != -1:
            self.buf._edits.append((open_i, close_i + 1, b""))
            # The `BaseModel` mention went with the parentheses, so it must not
            # also keep its import alive.
            self._rewritten.add(id(node.bases[0]))

    def _rewrite_pydantic_field(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            declarative = all(
                isinstance(child, ast.Assign)
                or (
                    isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)
                )
                for child in stmt.body
            )
            settings = {
                child.targets[0].id
                for child in stmt.body
                if isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
            }
            if declarative and settings and settings <= {
                "arbitrary_types_allowed",
                "from_attributes",
                "orm_mode",
                "protected_namespaces",
                "use_enum_values",
                "validate_default",
            }:
                self._replace_all_of(stmt, "# wreath-port: redundant model config removed")
                self._resolve(stmt.lineno, "pydantic.config_class")
            else:
                self._annotate(stmt.lineno, "pydantic.config_class")
            return
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name == "model_config":
                extra = _config_extra(stmt.value)
                if extra == "forbid":
                    # `replace` spans from the node's own col_offset, so the
                    # source indentation in front of it is left alone.
                    self.buf.replace(
                        stmt, "# wreath-port: extra='forbid' is wreath's default (dropped)"
                    )
                elif extra == "ignore" and self.opinionated:
                    self._resolve(stmt.lineno, "pydantic.config_ignore")
                    self.buf.replace(
                        stmt,
                        "# wreath-port: extra='ignore' dropped -- wreath rejects unknown "
                        "fields with a 422. Clients sending extras will start failing.",
                    )
                elif extra == "ignore":
                    self._annotate(stmt.lineno, "pydantic.config_ignore")
                elif self._drop_redundant_model_config(stmt, stmt.value):
                    pass
                return
            rule_id = pydantic_field_rule(self.imports, stmt)
            if (
                rule_id == "pydantic.field_metadata_exact"
                and isinstance(stmt.value, ast.Call)
            ):
                self._rewrite_field_metadata(stmt, stmt.value)
                return
            if rule_id != "pydantic.field":
                # Metadata without an exact Wreath equivalent stays written so
                # the reviewer can see the contract they are deciding about.
                self._annotate(stmt.lineno, rule_id)
                return
            default = stmt.value
            if (
                isinstance(default, ast.Call)
                and self.imports.origin(default.func).split(".")[-1] == "Field"
            ):
                self._rewrite_field_marker(stmt, default)
                return
            # mutable literal defaults -> field(default_factory=...)
            factory = _mutable_factory(default) if default is not None else None
            if factory and default is not None:
                self.needs_field = True
                self.buf.replace(default, f"field(default_factory={factory})")
        elif isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
        ):
            extra = _config_extra(stmt.value)
            if extra == "forbid":
                self.buf.replace(
                    stmt, "# wreath-port: extra='forbid' is wreath's default (dropped)"
                )
            elif extra == "ignore" and self.opinionated:
                self._resolve(stmt.lineno, "pydantic.config_ignore")
                self.buf.replace(
                    stmt,
                    "# wreath-port: extra='ignore' dropped -- wreath rejects unknown "
                    "fields with a 422. Clients sending extras will start failing.",
                )
            elif extra == "ignore":
                self._annotate(stmt.lineno, "pydantic.config_ignore")
            else:
                self._drop_redundant_model_config(stmt, stmt.value)

    def _drop_redundant_model_config(
        self, stmt: ast.stmt, value: ast.expr | None
    ) -> bool:
        # `None` because an `AnnAssign` may carry no value at all (`x: int`);
        # it takes the same answer as any other non-call.
        if not isinstance(value, ast.Call):
            return False
        safe = {
            "arbitrary_types_allowed",
            "from_attributes",
            "protected_namespaces",
            "use_enum_values",
            "validate_default",
        }
        names = {keyword.arg for keyword in value.keywords}
        if None in names or not names or not names <= safe:
            return False
        self._removed_pydantic_imports.add("ConfigDict")
        self._replace_all_of(stmt, "# wreath-port: redundant model config removed")
        return True

    def _rewrite_field_metadata(self, stmt: ast.AnnAssign, call: ast.Call) -> None:
        """Move an exact Pydantic ``Field`` contract into ``Annotated``."""
        metadata: list[str] = []
        for keyword in call.keywords:
            if keyword.arg in {
                "alias", "description", "gt", "ge", "lt", "le",
                "min_length", "max_length", "pattern", "regex",
            }:
                name = "pattern" if keyword.arg == "regex" else keyword.arg
                metadata.append(f"{name}={self._seg(keyword.value)}")
        self.needs.add("Field")
        self.needs_annotated = True
        annotation = self._seg(stmt.annotation)
        self.buf.replace(
            stmt.annotation,
            f"Annotated[{annotation}, Field({', '.join(metadata)})]",
        )
        self._rewrite_field_marker(stmt, call)

    def _rewrite_field_marker(self, stmt: ast.AnnAssign, call: ast.Call) -> None:
        """`x: int = Field(default=3, description="…")` -> `x: int = 3`.

        Reached only for the shape `pydantic_field_rule` calls determined: the
        marker holds a default and, at most, metadata already moved into
        ``Annotated``. A dataclass has one slot per field, so the default is
        written into it.

        `Field(...)` with no default at all is how pydantic spells *required*,
        and a dataclass spells that by having no `=` — so the whole assignment
        goes. Removing a default can put a required field after a defaulted one,
        which is exactly what `_rewrite_pydantic_class` already checked for
        before it chose between `@dataclass` and `@dataclass(kw_only=True)`.
        """
        self._rewritten.add(id(call.func))
        by_name = {kw.arg: kw.value for kw in call.keywords}
        factory = by_name.get("default_factory")
        if factory is not None:
            self.needs_field = True
            self.buf.replace(call, f"field(default_factory={self._seg(factory)})")
            return
        default = by_name.get("default")
        if default is None and call.args:
            positional = call.args[0]
            if not (isinstance(positional, ast.Constant) and positional.value is Ellipsis):
                default = positional
        if default is None:
            # Required: drop " = Field(...)" and leave a bare annotation.
            self.buf._edits.append((self.buf.end_of(stmt.annotation), self.buf.end_of(call), b""))
            return
        mutable = _mutable_factory(default)
        if mutable:
            self.needs_field = True
            self.buf.replace(call, f"field(default_factory={mutable})")
        else:
            self.buf.replace(call, self._seg(default))

    # -- classes (ORM models, Phase 2) -------------------------------------------
    def _rewrite_ormar_class(self, node: ast.ClassDef) -> None:
        self.needs.add("Model")
        table, config_stmt = None, None
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ormar_config" for t in stmt.targets
            ):
                config_stmt, table = stmt, _copy_tablename(stmt.value)
        meta = next(
            (
                statement
                for statement in node.body
                if isinstance(statement, ast.ClassDef) and statement.name == "Meta"
            ),
            None,
        )
        if table is None and meta is not None:
            table = next(
                (
                    statement.value.value
                    for statement in meta.body
                    if isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "tablename"
                        for target in statement.targets
                    )
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ),
                None,
            )
        inherited_mixins = [
            base
            for base in node.bases
            if self.imports.origin(base).split(".")[-1] in self.orm_mixins
        ]
        bases = [
            self._seg(base)
            for base in node.bases
            if self.imports.origin(base) != "ormar.Model"
        ]
        if not inherited_mixins:
            bases.insert(0, "Model")
        if node.bases:
            header = ", ".join(bases)
            if table is not None:
                header += f', table="{table}"'
            self.buf.replace_span(node.bases[0], node.bases[-1], header)
            for base in node.bases:
                if self.imports.origin(base) == "ormar.Model":
                    self._rewritten.add(id(base))
        if table is None:
            self._note(
                node.lineno,
                "orm.model",
                'add `table="<name>"` to the class header; the table name '
                "was not written here as plain text",
            )
        if config_stmt is not None:
            self._rewrite_ormar_config(node, config_stmt)
        if meta is not None:
            self._rewrite_legacy_ormar_meta(meta)
        self._rewrite_ormar_fields(node, config_stmt)

    def _rewrite_legacy_ormar_meta(self, meta: ast.ClassDef) -> None:
        """Move legacy ``Meta`` indexes and unique constraints into the model."""
        declarations: list[str] = []
        for statement in meta.body:
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                continue
            name = statement.targets[0].id
            if name == "indexes" and isinstance(statement.value, (ast.List, ast.Tuple)):
                for value in statement.value.elts:
                    columns = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
                    if not all(
                        isinstance(column, ast.Constant) and isinstance(column.value, str)
                        for column in columns
                    ):
                        continue
                    self.needs.add("index")
                    args = ", ".join(self._seg(column) for column in columns)
                    declarations.append(f"_index_{len(declarations)} = index({args})")
            elif name == "constraints" and isinstance(
                statement.value, (ast.List, ast.Tuple)
            ):
                for value in statement.value.elts:
                    if not isinstance(value, ast.Call):
                        continue
                    kind = self.imports.origin(value.func).split(".")[-1]
                    if kind not in ("UniqueColumns", "IndexColumns") or not all(
                        isinstance(column, ast.Constant) and isinstance(column.value, str)
                        for column in value.args
                    ):
                        continue
                    target = "unique" if kind == "UniqueColumns" else "index"
                    self.needs.add(target)
                    args = ", ".join(self._seg(column) for column in value.args)
                    declarations.append(
                        f"_{target}_{len(declarations)} = {target}({args})"
                    )
        indent = self.buf.line_indent(meta.lineno)
        replacement = (
            f"\n{indent}".join(declarations)
            if declarations
            else "# wreath-port: legacy Ormar Meta moved to the class header"
        )
        self._replace_all_of(meta, replacement)

    def _rewrite_ormar_mixin(self, node: ast.ClassDef) -> None:
        """Turn a plain Ormar column mixin into a table-less Wreath model."""
        self.needs.add("Model")
        if node.bases:
            end = self.buf.end_of(node.bases[-1])
            self.buf._edits.append((end, end, b", Model"))
        else:
            name_end = self.buf.start_of(node) + len(f"class {node.name}".encode())
            self.buf._edits.append((name_end, name_end, b"(Model)"))
        self._rewrite_ormar_fields(node, None)

    def _rewrite_ormar_fields(
        self, node: ast.ClassDef, config_stmt: ast.Assign | None
    ) -> None:
        """Rewrite the column declarations shared by tables and mixins."""
        # Paired rather than filtered: `AnnAssign.value` is Optional, and a list
        # of statements throws away the narrowing every reader below relies on.
        columns: list[tuple[ast.AnnAssign, ast.Call]] = [
            (stmt, stmt.value)
            for stmt in node.body
            if stmt is not config_stmt
            and isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.value, ast.Call)
        ]
        # The nullability reminder used to go on every model, whether or not the
        # model left anything unsaid. It now goes only on models with a column
        # that states neither `nullable=` nor `primary_key=` — the ones where the
        # answer really did change, because ormar defaults a column to nullable
        # and wreath defaults it to NOT NULL.
        unstated = [
            stmt.target.id
            for stmt, call in columns
            if isinstance(stmt.target, ast.Name)
            and not any(kw.arg in ("nullable", "primary_key") for kw in call.keywords)
            and self.imports.origin(call.func).split(".")[-1] != "ForeignKey"
        ]
        if unstated:
            self._note(
                node.lineno,
                "orm.column",
                "check whether these columns should allow NULL. ormar let a column be "
                "empty unless told otherwise; wreath requires a value unless told "
                "otherwise, so the ones that said nothing have just changed meaning: "
                + ", ".join(unstated),
            )
        for stmt, call in columns:
            self._rewrite_ormar_column(stmt, call)
        plain_columns: list[tuple[ast.Assign, ast.Call]] = [
            (stmt, stmt.value)
            for stmt in node.body
            if stmt is not config_stmt
            and isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
            and self.imports.origin(stmt.value.func).startswith("ormar.")
        ]
        for stmt, call in plain_columns:
            self._rewrite_unannotated_ormar_column(stmt, call)

    def _rewrite_ormar_config(self, node: ast.ClassDef, config_stmt: ast.Assign) -> None:
        """Delete `ormar_config = …`, but not the constraints hanging off it.

        `constraints=[ormar.UniqueColumns("name", "ranch")]` is a real UNIQUE
        index in the database, and the whole statement used to be deleted with
        no note at all — the port came out looking complete and quietly dropped
        31 constraints. Each one becomes a wreath declaration of the same shape.
        """
        indent = self.buf.line_indent(config_stmt.lineno)
        lines: list[str] = []
        unread: list[str] = []
        for entry in _config_constraints(config_stmt.value):
            kind = self.imports.origin(entry.func).split(".")[-1]
            columns = [
                arg
                for arg in entry.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if kind not in ("UniqueColumns", "IndexColumns") or len(columns) != len(entry.args):
                unread.append(f"{kind} on line {entry.lineno}")
                continue
            call = "unique" if kind == "UniqueColumns" else "index"
            self.needs.add(call)
            names = ", ".join(self._seg(column) for column in columns)
            lines.append(f"_{call}_{len(lines)} = {call}({names})")
        # `replace` starts at the statement's own column, so the first line keeps
        # the indentation already in the source and the rest supply their own.
        self._replace_all_of(config_stmt, f"\n{indent}".join(lines))
        if unread:
            self._note(
                config_stmt.lineno,
                "orm.model",
                "this table declared constraints that were not carried over ("
                + "; ".join(unread)
                + "). They exist in the database today, so "
                "add the matching `unique(...)` or `index(...)` to the model",
            )

    def _rewrite_ormar_column(self, stmt: ast.AnnAssign, call: ast.Call) -> None:
        tail = self.imports.origin(call.func).split(".")[-1]
        ann_src = self._seg(stmt.annotation)
        if tail == "ForeignKey":
            self._rewrite_ormar_fk(stmt, call, ann_src)
            return
        pgtype = self._ormar_pgtype(tail, call)
        if pgtype is None:
            self._note(
                stmt.lineno,
                "orm.column",
                f"wreath has no column type matching ormar.{tail}; pick the "
                "closest one in wreath.orm.types and check the values still fit",
            )
            return
        self.needs.update({"Mapped", "column"})
        kwargs = self._ormar_kwargs(stmt, call)
        self.buf.replace(stmt.annotation, f"Mapped[{ann_src}]")
        args = "" if not kwargs else ", " + ", ".join(kwargs)
        self._replace_all_of(call, f"column({pgtype}{args})")

    def _rewrite_unannotated_ormar_column(
        self, stmt: ast.Assign, call: ast.Call
    ) -> None:
        """Move an old-style mixin column whose Python type was implicit."""
        tail = self.imports.origin(call.func).split(".")[-1]
        pgtype = self._ormar_pgtype(tail, call)
        if pgtype is None:
            self._note(
                stmt.lineno,
                "orm.column",
                f"wreath has no column type matching ormar.{tail}; pick the "
                "closest one in wreath.orm.types and check the values still fit",
            )
            return
        self.needs.add("column")
        kwargs = self._ormar_kwargs(stmt, call)
        args = "" if not kwargs else ", " + ", ".join(kwargs)
        self._replace_all_of(call, f"column({pgtype}{args})")

    def _rewrite_ormar_fk(self, stmt: ast.AnnAssign, call: ast.Call, ann_src: str) -> None:
        if not isinstance(stmt.target, ast.Name) or not call.args:
            self._annotate(stmt.lineno, "orm.fk")
            return
        name = stmt.target.id
        arg0 = call.args[0]
        target = self._seg(arg0)
        target_name = None
        if isinstance(arg0, ast.Name):
            target_name = arg0.id
        elif isinstance(arg0, ast.Attribute):
            target_name = arg0.attr
        idx = any(kw.arg == "index" and _is_true(kw.value) for kw in call.keywords)
        indent = self.buf.line_indent(stmt.lineno)
        pg = self.pk_types.get(target_name)
        if pg is not None:  # translated: PK type resolved from the referenced model
            pyann = _PG_PYANN.get(pg, "int")
            self.needs_uuid = self.needs_uuid or pyann.startswith("uuid.")
            self.needs.update({"Mapped", "column", "relationship", pg})
            index = ", index=True" if idx else ""
            col = f"{name}_id: Mapped[{pyann}] = column({pg}, references={target}.id{index})"
            self._replace_all_of(
                stmt, f'{col}\n{indent}{name} = relationship({target}, load="raise")'
            )
        else:  # needs-review: referenced PK not resolvable in this module -> Uuid default + flag
            self.needs.update({"Mapped", "column", "relationship", "Uuid"})
            index = ", index=True" if idx else ""
            self.needs_uuid = True
            col = f"{name}_id: Mapped[uuid.UUID] = column(Uuid, references={target}.id{index})"
            self._replace_all_of(
                stmt, f'{col}\n{indent}{name} = relationship({target}, load="raise")'
            )
            self._annotate(stmt.lineno, "orm.fk")

    def _ormar_pgtype(self, tail: str, call: ast.Call) -> str | None:
        if tail == "ARRAY":
            elem = "Text"
            for kw in call.keywords:
                if kw.arg == "item_type":
                    v = kw.value.func if isinstance(kw.value, ast.Call) else kw.value
                    elem = _SA_ELEM_TYPE.get(self.imports.origin(v).split(".")[-1], "Text")
            self.needs.update({"Array", elem})
            return f"Array({elem})"
        if tail == "DateTime":
            name = (
                "TimestampTz"
                if any(kw.arg == "timezone" and _is_true(kw.value) for kw in call.keywords)
                else "Timestamp"
            )
            self.needs.add(name)
            return name
        name = _ORMAR_TYPE.get(tail)
        if name:
            self.needs.add(name)
        return name

    def _ormar_kwargs(self, stmt: ast.AnnAssign | ast.Assign, call: ast.Call) -> list[str]:
        """The `column(...)` arguments one ormar column's keywords become.

        Anything with a wreath home is carried; anything that only described the
        column for a schema is dropped without a word; and whatever is left over
        earns one note that names it. That last list used to include
        `max_length=` and `description=`, which between them accounted for 366
        of the 632 notes this emitter wrote — one on nearly every column in the
        tree, for two keywords that need no decision at all.
        """
        out: list[str] = []
        dropped: list[str] = []
        checks: list[str] = []
        minimum = maximum = min_length = max_length = None
        for kw in call.keywords:
            if kw.arg in (
                "primary_key",
                "nullable",
                "unique",
                "index",
                "server_default",
                "default",
            ):
                out.append(f"{kw.arg}={self._seg(kw.value)}")
            elif kw.arg == "default_factory":
                out.append(f"default={self._seg(kw.value)}")
            elif kw.arg == "minimum":
                minimum = self._seg(kw.value)
            elif kw.arg == "maximum":
                maximum = self._seg(kw.value)
            elif kw.arg == "min_length":
                min_length = self._seg(kw.value)
            elif kw.arg == "max_length":
                # ormar's `max_length` sizes a VARCHAR; wreath spells the same
                # limit as a check, which holds it in the database and returns a
                # 422 at the boundary rather than truncating.
                max_length = self._seg(kw.value)
            elif kw.arg in _ORMAR_DOC_KWARGS or kw.arg in ("timezone", "item_type"):
                continue
            elif kw.arg == "uuid_format":
                # ormar can store a UUID as text; wreath's `Uuid` is the native
                # postgres type. Same values, different storage — which matters
                # only if this port points at a database that already exists.
                self._note(
                    stmt.lineno,
                    "orm.column",
                    "ormar stored this UUID as text, and wreath stores it as postgres's "
                    "own uuid type. Starting from an empty database there is nothing to "
                    "do. Pointing at an existing one, this column has to be converted "
                    "before the model matches it",
                )
            else:
                dropped.append(kw.arg or "**kwargs")
        if minimum is not None and maximum is not None:
            self.needs.update({"AllOf", "Ge", "Le"})
            checks.append(f"AllOf((Ge({minimum}), Le({maximum})))")
        elif minimum is not None:
            self.needs.add("Ge")
            checks.append(f"Ge({minimum})")
        elif maximum is not None:
            self.needs.add("Le")
            checks.append(f"Le({maximum})")
        if min_length is not None or max_length is not None:
            self.needs.add("Length")
            bounds = ", ".join(
                f"{name}={value}"
                for name, value in (("minimum", min_length), ("maximum", max_length))
                if value is not None
            )
            checks.append(f"Length({bounds})")
        if len(checks) == 1:
            out.append(f"check={checks[0]}")
        elif checks:
            self.needs.add("AllOf")
            out.append(f"check=AllOf(({', '.join(checks)}))")
        if dropped:
            self._note(
                stmt.lineno,
                "orm.column",
                "this column set "
                + ", ".join(f"{name}=" for name in dropped)
                + ", which wreath's column() has no setting for. Decide what each one "
                "should become before relying on this model",
            )
        return out

    # -- functions (routes) ------------------------------------------------------
    def visit_FunctionDef(self, node) -> None:
        if redundant_literal_validator(node, self._parents, self.imports):
            start = self.buf.start_of_line(node.decorator_list[0].lineno)
            end = self.buf.end_of(node)
            self.buf._edits.append((start, end, b""))
            self._replaced.append((start, end))
            self._removed_pydantic_imports.add("field_validator")
            return
        outer_test_clients = self._test_clients
        fixture_clients = {
            argument.arg
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if argument.arg in self._fixture_test_clients
        }
        referenced_globals = {
            name
            for name in self._global_test_clients
            if any(isinstance(inner, ast.Name) and inner.id == name for inner in ast.walk(node))
        }
        if node.name.startswith("test_"):
            for name in sorted(referenced_globals):
                self._add_test_fixture_param(node, name)
            fixture_clients |= referenced_globals
        if fixture_clients and isinstance(node, ast.FunctionDef):
            self.buf._edits.append((self.buf.start_of(node), self.buf.start_of(node), b"async "))
        self._rewrite_test_client_fixture(node)
        for inner in ast.walk(node):
            if not isinstance(inner, (ast.With, ast.AsyncWith)):
                continue
            for item in inner.items:
                if not (
                    isinstance(item.context_expr, ast.Call)
                    and self._is_test_client_call(item.context_expr)
                ):
                    continue
                if isinstance(inner, ast.With):
                    self.buf._edits.append(
                        (self.buf.start_of(inner), self.buf.start_of(inner), b"async ")
                    )
                    if isinstance(node, ast.FunctionDef) and not fixture_clients:
                        self.buf._edits.append(
                            (self.buf.start_of(node), self.buf.start_of(node), b"async ")
                        )
                if isinstance(item.optional_vars, ast.Name):
                    fixture_clients.add(item.optional_vars.id)
                self._resolve(item.context_expr.lineno, "test.client")
        self._test_clients |= fixture_clients
        rewritten_test_clients = self._rewrite_test_client_function(node)
        if rewritten_test_clients:
            self._test_clients |= rewritten_test_clients
        route_dec = None
        for dec in node.decorator_list:
            attr = (
                dec.func.attr
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute))
                else None
            )
            if attr in HTTP_METHODS:
                route_dec = dec
                self._rewrite_route_options(dec, node)
            # A websocket route, a pydantic validator, a lifespan and a Celery
            # task all need a human, and all of them are recognized once by the
            # analyzer — `annotate_findings` writes their notes.
        if route_dec is not None:
            self._rewrite_route_background_tasks(node)
            self._rewrite_route_created_tasks(node)
            session = self._route_session_name(node)
            self._ensure_request_param(
                node,
                self._route_needs_keyword_only(node) or session == _SESSION_PARAM,
                session,
            )
            self._split_markers(node)
            self._rewrite_as_form_params(node)
            outer, self._session = self._session, session
            self.generic_visit(node)
            self._session = outer
            self._test_clients = outer_test_clients
            return
        if node.name in self._dep_targets:
            self._ensure_request_param(node)  # Phase 3: dependency callable gains `request`
            self._rewrite_as_form_params(node)
        # Outside a route handler nothing supplies a session. By default the
        # queries are left where they are and the note goes on the *function* —
        # one decision to make ("this needs a session, and every caller has to
        # pass it") rather than the same sentence over every query in the body.
        #
        # `--opinionated` makes that decision instead: the parameter is added and
        # the queries are written out. It is separated because it is the one
        # rewrite whose effect leaves the file — every call to this function now
        # has to pass a session — and a codemod should not change a signature
        # someone else depends on without being asked.
        outer_session, outer_wanted = self._session, self._session_wanted
        self._session, self._session_wanted = None, False
        existing_session = next(
            (
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                if argument.annotation is not None
                and self._is_orm_session(argument.annotation)
            ),
            None,
        )
        if existing_session is not None:
            self._session = existing_session
        elif (
            self.opinionated
            and isinstance(node, ast.AsyncFunctionDef)
            and self._session_definition_selected(node)
            and self._can_take_session(node)
        ):
            self._session = _SESSION_PARAM
            self._add_session_param(node)
            # Opinionated mode owns both ends of this signature change: the
            # tree call graph selected the function and every selected call is
            # updated below. A successful rewrite is not review work.
            self._resolve(node.lineno, "orm.query.session_added")
        self.generic_visit(node)
        if self._session_wanted:
            self._annotate(node.lineno, "orm.query.needs_session")
        self._session, self._session_wanted = outer_session, outer_wanted
        self._test_clients = outer_test_clients

    def _rewrite_route_background_tasks(self, node) -> None:
        parameter = next(
            (
                argument
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                if argument.annotation is not None
                and self.imports.origin(argument.annotation) == "fastapi.BackgroundTasks"
            ),
            None,
        )
        if parameter is None:
            return
        returns: list[ast.Return] = []
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Return) or candidate.value is None:
                continue
            owner = self._parents.get(id(candidate))
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = self._parents.get(id(owner))
            if owner is node:
                returns.append(candidate)
        if not returns or any(
            not isinstance(statement.value, ast.Call)
            or not (
                (
                    statement.value.func.attr
                    if isinstance(statement.value.func, ast.Attribute)
                    else getattr(statement.value.func, "id", "")
                ).endswith("Response")
            )
            for statement in returns
        ):
            return
        self._remove_function_parameter(node, parameter)
        body_index = (
            1
            if node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
            and len(node.body) > 1
            else 0
        )
        first = node.body[body_index]
        indent = self.buf.line_indent(first.lineno)
        self.buf.insert_before_line(
            first.lineno, f"{indent}{parameter.arg} = BackgroundTasks()"
        )
        for statement in returns:
            value = statement.value
            if value is None:  # filtered when `returns` was built; re-narrowed here
                continue
            close = self.buf.end_of(value) - 1
            separator = "" if _ends_argument_list(self.buf.b, close) else ", "
            self.buf._edits.append(
                (close, close, f"{separator}background={parameter.arg}".encode())
            )
        self.needs.update({"BackgroundTasks"})

    def _rewrite_route_created_tasks(self, node) -> None:
        if any(
            argument.annotation is not None
            and self.imports.origin(argument.annotation) == "fastapi.BackgroundTasks"
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        ):
            return
        calls: list[tuple[ast.Call, ast.Call]] = []
        for candidate in ast.walk(node):
            if not (
                isinstance(candidate, ast.Call)
                and self.imports.origin(candidate.func) == "asyncio.create_task"
                and candidate.args
                and isinstance(candidate.args[0], ast.Call)
                and isinstance(self._parents.get(id(candidate)), ast.Expr)
                and all(keyword.arg == "name" for keyword in candidate.keywords)
            ):
                continue
            owner = self._parents.get(id(candidate))
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = self._parents.get(id(owner))
            if owner is node:
                calls.append((candidate, candidate.args[0]))
        if not calls:
            return
        name = "_wreath_background"
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and len(node.body) > 1
        ):
            first = node.body[1]
        indent = self.buf.line_indent(first.lineno)
        self.buf.insert_before_line(first.lineno, f"{indent}{name} = BackgroundTasks()")
        for task, coroutine in calls:
            arguments = [self._seg(argument) for argument in coroutine.args]
            arguments.extend(
                f"{keyword.arg}={self._seg(keyword.value)}"
                for keyword in coroutine.keywords
            )
            suffix = "" if not arguments else ", " + ", ".join(arguments)
            self._replace_all_of(
                task,
                f"{name}.add_task({self._seg(coroutine.func)}{suffix})",
            )
            self._resolve(task.lineno, "bg.asyncio_loop")
        returns: list[ast.Return] = []
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Return):
                continue
            owner = self._parents.get(id(candidate))
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = self._parents.get(id(owner))
            if owner is node:
                returns.append(candidate)
        if not returns:
            return
        for statement in returns:
            value = statement.value
            if value is None:
                self._replace_all_of(
                    statement, f"return Response(status=204, background={name})"
                )
                self.needs.add("Response")
                continue
            response_name = (
                value.func.attr
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                else getattr(value.func, "id", "") if isinstance(value, ast.Call) else ""
            )
            if response_name.endswith("Response"):
                close = self.buf.end_of(value) - 1
                separator = "" if _ends_argument_list(self.buf.b, close) else ", "
                self.buf._edits.append(
                    (close, close, f"{separator}background={name}".encode())
                )
            else:
                self._replace_all_of(
                    value, f"JSONResponse({self._seg(value)}, background={name})"
                )
                self.needs.add("JSONResponse")
        self.needs.add("BackgroundTasks")

    def _remove_function_parameter(self, node, parameter: ast.arg) -> None:
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        index = arguments.index(parameter)
        if index + 1 < len(arguments):
            start = self.buf.start_of(parameter)
            end = self.buf.start_of(arguments[index + 1])
        elif index:
            start = self.buf.end_of(arguments[index - 1])
            end = self.buf.end_of(parameter)
        else:
            start = self.buf.start_of(parameter)
            end = self.buf.end_of(parameter)
        self.buf._edits.append((start, end, b""))
        self._rewritten.update(id(item) for item in ast.walk(parameter))

    def _add_test_fixture_param(self, node, name: str) -> None:
        existing = {
            argument.arg
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        }
        if name in existing:
            return
        body_start = self.buf.start_of(node.body[0])
        close = self.buf.b.rfind(b")", self.buf.start_of(node), body_start)
        if close < 0:
            return
        has_args = bool(node.args.posonlyargs or node.args.args or node.args.kwonlyargs)
        if not has_args and node.args.vararg is None and node.args.kwarg is None:
            text = name
        elif node.args.vararg is not None or node.args.kwonlyargs:
            text = f", {name}"
        elif node.args.kwarg is not None:
            text = f"*, {name}, "
            close = self.buf.start_of(node.args.kwarg) - 2
        else:
            text = f", *, {name}"
        self.buf._edits.append((close, close, text.encode()))

    def _rewrite_test_client_fixture(self, node) -> None:
        if node.name not in self._fixture_test_clients:
            return
        changed = False
        for inner in ast.walk(node):
            if not (
                isinstance(inner, (ast.Return, ast.Yield))
                and isinstance(inner.value, ast.Call)
                and self._is_test_client_call(inner.value)
            ):
                continue
            statement = self._parents.get(id(inner)) if isinstance(inner, ast.Yield) else inner
            if not isinstance(statement, (ast.Expr, ast.Return)):
                continue
            indent = self.buf.line_indent(statement.lineno)
            call = self._test_client_text(inner.value)
            self._replace_all_of(
                statement,
                f"async with {call} as {node.name}:\n{indent}    yield {node.name}",
            )
            self._resolve(inner.value.lineno, "test.client")
            changed = True
        if changed and isinstance(node, ast.FunctionDef):
            self.buf._edits.append((self.buf.start_of(node), self.buf.start_of(node), b"async "))

    def visit_Assign(self, node: ast.Assign) -> None:
        if id(node) in self._http_transport_assignments:
            self._replace_all_of(node, "")
            return
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in self._global_test_clients
            and node.value is self._global_test_clients[node.targets[0].id]
        ):
            name = node.targets[0].id
            call = self._test_client_text(node.value)
            self.needs_fixture = True
            self._replace_all_of(
                node,
                f"@fixture\nasync def {name}():\n"
                f"    async with {call} as {name}:\n"
                f"        yield {name}",
            )
            self._resolve(node.lineno, "test.client")
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        value = node.value
        if (
            self.opinionated
            and isinstance(node.target, ast.Name)
            and isinstance(value, ast.Call)
            and (total := self._http_timeout_total(value)) is not None
        ):
            timeout_annotations = [
                candidate
                for candidate in ast.walk(node.annotation)
                if isinstance(candidate, (ast.Name, ast.Attribute))
                and self.imports.origin(candidate) == "httpx.Timeout"
            ]
            if timeout_annotations:
                self.needs.add("ClientTimeout")
                for annotation in timeout_annotations:
                    self._rewritten.update(id(item) for item in ast.walk(annotation))
                    self.buf.replace(annotation, "ClientTimeout")
                self._replace_all_of(value, f"ClientTimeout(total={total})")
                self._resolve(node.lineno, "ext.httpx")
                return
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _rewrite_test_client_function(self, node) -> frozenset[str]:
        """Make one local FastAPI TestClient an async lifespan context.

        The exact shape is deliberately narrow: one direct assignment in a
        function. Module globals, yielded fixtures and factory-returned clients
        have ownership outside this body, so the porter leaves those for a
        person instead of guessing where their lifespan ends.
        """
        if not self.opinionated:
            return frozenset()
        assignments: list[tuple[ast.Assign, ast.Name, ast.Call]] = []
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
                and self._is_test_client_call(statement.value)
            ):
                assignments.append((statement, statement.targets[0], statement.value))
        if len(assignments) != 1:
            return frozenset()
        statement, target, call = assignments[0]
        if isinstance(node, ast.FunctionDef):
            self.buf._edits.append((self.buf.start_of(node), self.buf.start_of(node), b"async "))
        self._replace_all_of(
            statement,
            f"async with {self._test_client_text(call)} as {target.id}:",
        )
        if node.end_lineno is None or statement.end_lineno is None:
            return frozenset()
        for line in range(statement.end_lineno + 1, node.end_lineno + 1):
            offset = self.buf.start_of_line(line)
            self.buf._edits.append((offset, offset, b"    "))
        self._resolve(call.lineno, "test.client_local")
        return frozenset((target.id,))

    def _route_session_name(self, node) -> str | None:
        """The name a session will be reachable under inside this handler.

        A handler that already takes a wreath session keeps its own name.
        Otherwise wreath can supply one — but only if the name is going spare,
        and only if the body has a query that needs it.

        "Already takes a session" is decided by resolving the annotation, not by
        looking for the word. `session: Session` is just as likely to be a
        pydantic model of an incoming payload as a database handle, and reading it
        the wrong way produced a handler with the parameter declared twice.
        """
        parameters = list(node.args.args) + list(node.args.kwonlyargs)
        for arg in parameters:
            if arg.annotation is not None and self._is_orm_session(arg.annotation):
                return arg.arg
        if any(arg.arg == _SESSION_PARAM for arg in parameters):
            return None  # the name is taken by something else
        if not (self._name_is_free("Session") and self._name_is_free("FromORM")):
            return None  # so is the type's name
        if self._session_definition_selected(node) or self._runs_a_query(node):
            return _SESSION_PARAM
        if self.opinionated and self._calls_a_session_function(node):
            return _SESSION_PARAM
        return None

    def _calls_a_session_function(self, node) -> bool:
        """Whether this body calls something that now needs a session passed in."""
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if self._session_call_selected(
                inner,
                inner.func.attr
                if isinstance(inner.func, ast.Attribute)
                else getattr(inner.func, "id", None),
            ):
                return True
            name = (
                inner.func.attr
                if isinstance(inner.func, ast.Attribute)
                else getattr(inner.func, "id", None)
            )
            if name in self.session_functions:
                return True
        return False

    def _can_take_session(self, node) -> bool:
        """Whether `session: Session` can be added to this signature safely."""
        taken = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        return _SESSION_PARAM not in taken and self._name_is_free("Session")

    def _pass_session(self, node: ast.Call) -> None:
        """Add `session=session` to a call whose target now takes one.

        Written in just before the call's closing parenthesis, not after the last
        argument: an argument's own span stops before any brackets wrapped around
        it, so appending there landed the keyword *inside* a parenthesised
        expression.
        """
        close = self.buf.end_of(node) - 1
        if self.buf.b[close : close + 1] != b")":
            return  # not a plain call span; leave it alone
        # `sum(x for x in xs)` may write its generator bare only while it is the
        # sole argument. Adding a second one means adding its brackets too.
        if len(node.args) == 1 and isinstance(node.args[0], ast.GeneratorExp) and not node.keywords:
            start, end = self.buf.start_of(node.args[0]), self.buf.end_of(node.args[0])
            self.buf._edits.append((start, start, b"("))
            self.buf._edits.append((end, end, b")"))
        separator = "" if _ends_argument_list(self.buf.b, close) else ", "
        self.buf._edits.append(
            (close, close, f"{separator}{_SESSION_PARAM}={self._session}".encode())
        )

    def _add_session_param(self, node) -> None:
        """Add a keyword-only `session: Session` to an ordinary function.

        Keyword-only wherever it can be, so it never has to be threaded through
        positional call sites and never collides with an existing default.
        """
        self.needs.add("Session")
        args = node.args
        if args.kwonlyargs:
            last = args.kwonlyargs[-1]
            end = self.buf.end_of(args.kw_defaults[-1] or last)
            text = f", {_SESSION_PARAM}: Session"
        elif args.vararg is not None:
            end = self.buf.end_of(args.vararg)
            text = f", {_SESSION_PARAM}: Session"
        elif args.args or args.posonlyargs:
            positional = list(args.posonlyargs) + list(args.args)
            last = positional[-1]
            default = args.defaults[-1] if args.defaults else None
            end = self.buf.end_of(default if default is not None else last)
            text = f", *, {_SESSION_PARAM}: Session"
        else:
            open_paren = self.buf.b.find(b"(", self.buf.start_of_line(node.lineno))
            if open_paren == -1:
                return
            end = open_paren + 1
            text = f"{_SESSION_PARAM}: Session"
        self.buf._edits.append((end, end, text.encode()))

    def _is_orm_session(self, annotation: ast.expr) -> bool:
        """Whether this annotation is wreath's `Session`, however it is wrapped."""
        if isinstance(annotation, ast.Subscript):  # Annotated[Session, FromORM()]
            inner = annotation.slice
            annotation = inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner
        return self.imports.origin(annotation).startswith("wreath.orm")

    def _name_is_free(self, name: str) -> bool:
        """Whether the module can be handed `name` without shadowing its own.

        Every injected import is a new global, and a module that already binds
        that name to something of its own would silently get the wrong one.
        """
        bound = self.imports.names.get(name)
        return bound is None or bound.startswith("wreath")

    def _runs_a_query(self, node) -> bool:
        """Whether this body has a `Model.objects.…` chain that *runs*."""
        for inner in ast.walk(node):
            if not (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.value, ast.Attribute)
                and inner.value.attr == "objects"
            ):
                continue
            call = self._parents.get(id(inner))
            rule_id = query_rule(
                inner.attr,
                call if isinstance(call, ast.Call) else None,
                chain_tail(inner, self._parents),
                model=self._seg(inner.value.value),
                relations=self.orm_relations,
                columns=self.orm_columns,
                tables=self.orm_tables,
                unique_constraints=self.orm_unique_constraints,
                plain_mappings=plain_filter_mappings(
                    call if isinstance(call, ast.Call) else None, self._parents
                ),
            )
            if rule_id not in _QUERY_TRANSLATED:
                continue
            plan = _QueryPlan(self._seg(inner.value.value))
            steps, _ = self._query_chain(inner)
            if all(plan.step(self, verb, call) for verb, call in steps) and plan.runner:
                return True
        return False

    def _ensure_request_param(
        self, node, keyword_only: bool = False, session: str | None = None
    ) -> None:
        """Give the handler a leading `request: Request`, as wreath calls it.

        `keyword_only` additionally writes a bare `*` after it, which makes
        every remaining parameter keyword-only. That is what lets a required
        parameter keep its required-ness: a plain parameter with no default
        cannot follow one with a default, but a keyword-only one can, and
        wreath hands every bound value over by name (`handler(request,
        **kwargs)`), so nothing about the call changes.

        `session` names a session parameter to add alongside it, for a handler
        whose body has queries to run. Wreath fills it in from the application's
        registry, so no caller changes.
        """
        args = node.args
        positional = list(args.posonlyargs) + list(args.args)
        # A signature that already has a `*` (or a `*args`) cannot be given a
        # second one, and does not need one — everything after it is already
        # keyword-only.
        star = "*, " if keyword_only and not (args.kwonlyargs or args.vararg) else ""
        extra = ""
        parameters = positional + list(args.kwonlyargs)
        reuses_session = any(
            arg.arg == session
            and arg.annotation is not None
            and self._is_orm_session(arg.annotation)
            for arg in parameters
        )
        if session == _SESSION_PARAM and not reuses_session:
            self.needs_annotated = True
            self.needs.update({"Session", "FromORM"})
            extra = f"{_SESSION_PARAM}: Annotated[Session, FromORM()], "
        # Any parameter already named `request` satisfies the injection. Checking
        # only position 0 produced `async def f(request: Request, x, request: Request)`
        # for a handler that declared `request` second — which `ast.parse` accepts
        # (duplicate arguments are a *compile* error, so the round-trip guard let
        # it through) and CPython then refuses to compile.
        existing = next((a for a in positional if a.arg == "request"), None)
        has_request = existing is not None or any(a.arg == "request" for a in args.kwonlyargs)
        if has_request:
            if not (star or extra):
                return
            if existing is positional[0] and len(positional) > 1:
                s = self.buf.start_of(positional[1])
                self.buf._edits.append((s, s, f"{star}{extra}".encode()))
            elif existing is not None and star + extra:
                end = self.buf.end_of(existing)
                self.buf._edits.append((end, end, f", {star}{extra}".rstrip(" ,").encode()))
            else:
                self._note(
                    node.lineno,
                    "route.method",
                    "move `request` to the front, then add "
                    "`session: Annotated[Session, FromORM()]` after it so the "
                    "queries in this handler have a session to run through",
                )
            return
        self.needs.add("Request")
        if positional:
            first = positional[0]
            s = self.buf.start_of(first)
            self.buf._edits.append((s, s, f"request: Request, {star}{extra}".encode()))
            return
        # A handler with no positional parameters: write into the parentheses,
        # in front of whatever keyword-only ones are already there. Writing
        # `request` used to be left as a note, and it was the single most common
        # thing the emitter asked a human to type, and it is the same fifteen
        # characters every time.
        open_paren = self.buf.b.find(b"(", self.buf.start_of_line(node.lineno))
        if open_paren == -1:
            self._note(
                node.lineno,
                "route.method",
                "add a `request: Request` parameter -- every wreath handler takes one first",
            )
            return
        if args.kwonlyargs or args.vararg or args.kwarg:
            self.buf._edits.append(
                (open_paren + 1, open_paren + 1, f"request: Request, {extra}".encode())
            )
            return
        close_paren = self.buf.b.find(b")", open_paren)
        if close_paren == -1 or self.buf.b[open_paren + 1 : close_paren].strip():
            self._note(
                node.lineno,
                "route.method",
                "add a `request: Request` parameter -- every wreath handler takes one first",
            )
            return
        tail = f", {star}{extra}".rstrip(" ,") if extra else ""
        self.buf._edits.append((open_paren + 1, close_paren, f"request: Request{tail}".encode()))

    def _route_needs_keyword_only(self, node) -> bool:
        """Whether porting this signature leaves a required parameter after a defaulted one.

        `q: str = Query(...)` is FastAPI's spelling of a *required* query
        parameter, and wreath spells it `q: Annotated[str, Query()]` with no
        default at all. Written back in place that is a syntax error whenever
        anything before it has a default, which is why these used to be left
        alone with a note. Marking the parameters keyword-only removes the
        ordering rule entirely.
        """
        args = node.args
        defaults = dict(
            zip(
                [a.arg for a in args.args[len(args.args) - len(args.defaults) :]],
                args.defaults,
                strict=True,
            )
        )
        defaulted = False
        for arg in args.args:
            if arg.arg == "request":
                continue
            default = defaults.get(arg.arg)
            required_marker = (
                isinstance(default, ast.Call)
                and arg.annotation is not None
                and self.imports.origin(default.func).split(".")[-1] in _MARKERS
                and _marker_default(default) is None
            )
            if default is not None and not required_marker:
                defaulted = True
            elif defaulted:
                return True
        return False

    def _delete_decorator(self, dec) -> None:
        """Remove a complete decorator, including a multiline argument list."""
        self._rewritten.update(id(item) for item in ast.walk(dec))
        start = self.buf._starts[dec.lineno - 1]
        end_line = dec.end_lineno or dec.lineno
        line_start = self.buf._starts[end_line - 1]
        nxt = self.buf.b.find(b"\n", line_start)
        end = (nxt + 1) if nxt != -1 else len(self.buf.b)
        self.buf._edits.append((start, end, b""))

    def _rewrite_as_form_params(self, node) -> None:
        """`x: T = Depends(<Model>.as_form)` -> `x: Annotated[T, Form()]`.

        Whole-model Form binding.
        """
        args = node.args
        # Both pairings are equal-length by construction: the tail of `args.args`
        # is sliced to `len(args.defaults)`, and the AST keeps `kw_defaults` the
        # same length as `kwonlyargs`, padding with None.
        defaulted = list(
            zip(args.args[len(args.args) - len(args.defaults) :], args.defaults, strict=True)
        )
        defaulted += [
            (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None
        ]
        for arg, default in defaulted:
            if not (
                isinstance(default, ast.Call)
                and default.args
                and self.imports.origin(default.func).split(".")[-1] == "Depends"
            ):
                continue
            dep = default.args[0]
            if (
                isinstance(dep, ast.Attribute)
                and dep.attr == "as_form"
                and arg.annotation is not None
            ):
                self.needs_annotated = True
                self.needs.add("Form")  # -> wreath.binding via _WREATH_MODULE
                new = f"{arg.arg}: Annotated[{self._seg(arg.annotation)}, Form()]"
                s = self.buf.start_of(arg)
                e = self.buf.end_of(default)
                self.buf._edits.append((s, e, new.encode("utf-8")))

    def _rewrite_route_options(self, dec: ast.Call, node) -> None:
        """Translate `status_code=`/`response_model=`; annotate what can't be done safely.

        The verdict comes from `status_code_rule`, the same function the report
        uses, and only the two verdicts that name a *response class* are rewritten
        here. That boundary is load-bearing: this used to wrap any single-`return`
        body in `JSONResponse(...)`, which produced `JSONResponse(<dataclass>)` for
        a handler returning a DTO — and wreath's JSON encoder raises on a
        dataclass, so the ported handler failed on its first request. A verdict
        that is not rewritten keeps its kwarg *and* gains an annotation naming the
        exact edit, because silently dropping `status_code=` would leave the route
        answering 200.
        """
        drop: set[str] = set()
        sc = next((kw for kw in dec.keywords if kw.arg == "status_code"), None)
        if sc is not None:
            rule_id = status_code_rule(self.imports, sc.value, node)
            wrapper = _STATUS_WRAPPER.get(rule_id)
            returns = _returns_in(node)
            # `returned is not None` is implied by both wrapper verdicts (each
            # requires a single return *of a literal*). Checked rather than
            # asserted so an future rule added to `_STATUS_WRAPPER` without that
            # guarantee degrades to an annotation instead of a bad edit.
            returned = returns[0].value if len(returns) == 1 else None
            if wrapper is not None and returned is not None:
                status = status_int(self.imports, sc.value)
                self.needs.add(wrapper)
                self.buf.replace(returned, f"{wrapper}({self._seg(returned)}, status={status})")
                drop.add("status_code")
            else:
                self._annotate(dec.lineno, rule_id)
        if any(kw.arg == "response_model" for kw in dec.keywords):
            # translated: drop the kwarg (the return annotation is the schema source)
            drop.add("response_model")
        if any(kw.arg == "include_in_schema" and _is_false(kw.value) for kw in dec.keywords):
            self._resolve(dec.lineno, "route.include_in_schema")
        if drop:
            parts = [self._seg(a) for a in dec.args]
            parts += [
                (f"{kw.arg}={self._seg(kw.value)}" if kw.arg else f"**{self._seg(kw.value)}")
                for kw in dec.keywords
                if kw.arg not in drop
            ]
            self.buf.replace(dec, f"{self._seg(dec.func)}({', '.join(parts)})")

    def _split_markers(self, node) -> None:
        args = node.args
        defaulted = args.args[len(args.args) - len(args.defaults) :]
        for arg, default in zip(defaulted, args.defaults, strict=True):
            if not isinstance(default, ast.Call):
                continue
            marker = self.imports.origin(default.func).split(".")[-1]
            if marker not in _MARKERS or arg.annotation is None:
                continue
            self._rewrite_marker_param(arg, default, marker)

    def _rewrite_marker_param(self, arg, call: ast.Call, marker: str) -> None:
        ann = self._seg(arg.annotation)
        default = _marker_default(call)
        default_src = None if default is None else self._seg(default)
        # A required marker becomes a parameter with no default at all. That used
        # to be left alone, because such a parameter cannot follow a defaulted
        # one — but `_ensure_request_param` has already put a `*` in front of the
        # bound parameters when this signature needed one, and keyword-only
        # parameters may be declared in any order.
        kept, dropped = [], []
        for kw in call.keywords:
            if kw.arg == "default" or kw.arg in _MARKER_DOC_KWARGS:
                continue  # already read, or documentation only
            if kw.arg in _KW_RENAME:
                kept.append(f"{_KW_RENAME[kw.arg]}={self._seg(kw.value)}")
            elif kw.arg in _KW_KEEP:
                kept.append(f"{kw.arg}={self._seg(kw.value)}")
            else:
                dropped.append(kw.arg)
        self.needs_annotated = True
        marker_call = f"{marker}({', '.join(kept)})"
        new = f"{arg.arg}: Annotated[{ann}, {marker_call}]"
        if default_src is not None:
            new += f" = {default_src}"
        # replace the whole "arg: T = Marker(...)" span
        s = self.buf.start_of(arg)
        e = self.buf.end_of(call)
        self.buf._edits.append((s, e, new.encode("utf-8")))
        if dropped:
            self._annotate(
                arg.lineno,
                "param.query_strconstraint",
                "dropped from the marker: " + ", ".join(f"{n}=" for n in dropped),
            )

    # -- calls (FastAPI/APIRouter name, HTTPException, CORS, queries, infra) ------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        origin = self.imports.origin(func)
        tail = origin.split(".")[-1]
        # `FastAPI`/`APIRouter` are renamed by `visit_Name`/`visit_Attribute`,
        # which reach every mention — an `app: FastAPI` annotation, a
        # `-> FastAPI` return, an `isinstance` check — and not only the call
        # that constructs the application.
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if (
            self.opinionated
            and isinstance(node, ast.Call)
            and self._asgi_test_client_app(node) is not None
            and not self._inside_replaced(node)
        ):
            self._replace_all_of(node, self._test_client_text(node))
        elif self.opinionated and origin == "httpx.AsyncClient":
            self._rewrite_httpx_client(node)
        elif self.opinionated and origin == "httpx.Timeout":
            total = self._http_timeout_total(node)
            if total is not None and not self._inside_replaced(node):
                self.needs.add("ClientTimeout")
                self._replace_all_of(node, f"ClientTimeout(total={total})")
                self._resolve(node.lineno, "ext.httpx")
        elif (
            self.opinionated
            and isinstance(func, ast.Attribute)
            and id(node) in self._http_requests
            and func.attr in _TEST_REQUEST_METHODS
        ):
            self._rewrite_httpx_request(node, self._http_requests[id(node)], func.attr)
        elif (
            self.opinionated
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (
                self._enclosing_callable_id(node),
                func.value.id,
            ) in self._http_responses
            and func.attr == "json"
            and not node.args
            and not node.keywords
        ):
            self.needs.add("loads")
            self._replace_all_of(node, f"loads({func.value.id}.body)")
        if (
            self.opinionated
            and isinstance(func, ast.Name)
            and called in self.settings_models
            and called not in self._settings_custom_init
            and not node.args
            and not node.keywords
            and not self._inside_replaced(node)
        ):
            env_file, prefix = self._settings_bindings.get(called, (None, None))
            self.needs.add("Environment")
            if env_file is None:
                self.needs.add("read_osenv")
                environment = "Environment(read_osenv())"
            else:
                environment = f"Environment.load({env_file})"
            options = "" if prefix is None else f", prefix={prefix}"
            self._replace_all_of(
                node,
                f"{environment}.bind({called}{options})",
            )
        orm_query_call = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "objects"
        )
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in self._test_clients
            and func.attr in _TEST_REQUEST_METHODS
            and not isinstance(self._parents.get(id(node)), ast.Await)
        ):
            parent = self._parents.get(id(node))
            if isinstance(parent, (ast.Attribute, ast.Subscript)):
                # Attribute and subscription bind before ``await``. Merely
                # prefixing ``client.get(...).status`` produced an await of the
                # response attribute on the still-unawaited coroutine.
                self.buf._edits.append(
                    (self.buf.start_of(node), self.buf.start_of(node), b"(await ")
                )
                self.buf._edits.append((self.buf.end_of(node), self.buf.end_of(node), b")"))
            else:
                self.buf._edits.append(
                    (self.buf.start_of(node), self.buf.start_of(node), b"await ")
                )
        if (
            self.opinionated
            and self._session_call_selected(node, called)
            and self._session is not None
            and not orm_query_call
            and not any(kw.arg == _SESSION_PARAM for kw in node.keywords)
        ):
            # The callee gained a session parameter, so this call has to pass
            # one. Doing the signature and leaving the call is the half-port
            # that fails on its first request; `--opinionated` means both ends.
            # A ``Model.objects.create(...)`` call is not a call to a local
            # function named ``create``. The query rewriter below owns its
            # complete span and turns it into ``session.create(Model, ...)``;
            # inserting a keyword here overlaps that replacement and used to
            # leave the old manager call behind in the emitted tree.
            self._pass_session(node)
        if tail == "HTTPException":
            self._rewrite_http_exception(node)
        elif origin == "fastapi.encoders.jsonable_encoder" and len(node.args) == 1:
            # Wreath's JSON codec already serializes dataclasses, ORM rows,
            # UUIDs and datetimes, so the wrapper is the whole change: it goes,
            # and the value it wrapped stays.
            self._rewritten.add(id(func))
            self._replace_all_of(node, self._seg(node.args[0]))
        elif (
            origin in _RENAMED_ORIGINS
            and _RENAMED_ORIGINS[origin] in _RESPONSE_BODY_ARG
        ):
            self._rewrite_response_call(node, _RENAMED_ORIGINS[origin])
        elif origin.startswith("arrow.") and tail in _ARROW_RENAME:
            # `arrow.utcnow()` is `temporal.now()`. An `Instant` is a datetime
            # subclass, so it stores and serializes without a conversion at the
            # edges, and it refuses to be naive — which is the bug arrow's
            # implicit UTC hides.
            self.needs_temporal = True
            self._rewritten.add(id(func))
            self._replace_all_of(func, f"temporal.{_ARROW_RENAME[tail]}")
        elif origin.startswith("cachetools.") and tail in _CACHE_RENAME:
            self._rewrite_cache_call(node, func)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "get_pydantic"
            and pydantic_projection_rule(func, self._parents)
            == "pydantic.get_pydantic_exact"
        ):
            self._rewrite_model_dataclass(node, func)
        elif isinstance(func, ast.Attribute) and func.attr == "add_middleware":
            self._rewrite_add_middleware(node)
        # Everything else a call can be — a Celery `.delay()`, an `asyncio`
        # background loop, a boto3 client, a JWT decode, an Alembic cast — is
        # recognized by the analyzer and annotated from its findings (see
        # `annotate_findings`). Restating those tests here is how the emitter
        # ended up reporting *every* boto3 call as "keep the library" while the
        # report had already learned to route S3 at `wreath.objects`.
        self.generic_visit(node)

    def _rewrite_httpx_client(self, node: ast.Call) -> None:
        key = self._http_client_calls.get(id(node))
        if key is None:
            return
        parent = self._parents.get(id(node))
        if not isinstance(parent, ast.withitem):
            return
        name, _default_headers = self._http_clients[key]
        by_name = {keyword.arg: keyword.value for keyword in node.keywords}
        base_url = by_name.get("base_url")
        if base_url is None:
            dynamic = self._http_dynamic_clients.get(key)
            if dynamic is None:
                return
            statement = self._parents.get(id(parent))
            if not isinstance(statement, (ast.With, ast.AsyncWith)):
                return
            parts = self._fresh_name(f"_{name}_url")
            self._http_url_parts[key] = parts
            indent = self.buf.line_indent(statement.lineno)
            self.buf.insert_before_line(
                statement.lineno,
                f"{indent}{parts} = urlsplit(str({self._seg(dynamic)}))",
            )
            options = [
                "base_url=urlunsplit("
                f"({parts}.scheme, {parts}.netloc, '', '', ''))"
            ]
        else:
            options = [f"base_url={self._seg(base_url)}"]
        timeout = self._http_request_timeouts.get(key, by_name.get("timeout"))
        if timeout is not None:
            if isinstance(timeout, ast.Name) and timeout.id in self._http_timeout_constants:
                options.append(f"timeout={timeout.id}")
            else:
                total = self._http_timeout_value(timeout)
                if total is None:
                    return
                self.needs.add("ClientTimeout")
                options.append(f"timeout=ClientTimeout(total={total})")
        retries = self._http_retries.get(key)
        if retries is not None:
            self.needs.add("RetryPolicy")
            options.append(
                "retry=RetryPolicy("
                f"attempts=1 + ({self._seg(retries)}), "
                "idempotent_only=False, statuses=frozenset())"
            )
        self.needs.add("HTTPClient")
        self._resolve(node.lineno, "ext.httpx")
        self._replace_all_of(
            node,
            f"HTTPClient({name.replace('_', '-')!r}, {', '.join(options)})",
        )

    def _http_timeout_value(self, timeout: ast.expr) -> str | None:
        if not (
            isinstance(timeout, ast.Call)
            and self.imports.origin(timeout.func) == "httpx.Timeout"
        ):
            return self._seg(timeout)
        if timeout.args:
            return self._seg(timeout.args[0])
        value = next(
            (keyword.value for keyword in timeout.keywords if keyword.arg == "timeout"),
            None,
        )
        return self._seg(value) if value is not None else None

    def _http_timeout_total(self, call: ast.Call) -> str | None:
        """The single total deadline of an exact ``httpx.Timeout`` call."""
        if self.imports.origin(call.func) != "httpx.Timeout":
            return None
        if len(call.args) == 1 and not call.keywords:
            return self._seg(call.args[0])
        if call.args or len(call.keywords) != 1 or call.keywords[0].arg != "timeout":
            return None
        return self._seg(call.keywords[0].value)

    def _http_headers(self, value: ast.expr) -> str:
        source = self._seg(value)
        return (
            "tuple((str(_name).lower().encode('ascii'), "
            "str(_value).encode('latin-1')) "
            f"for _name, _value in ({source}).items())"
        )

    def _rewrite_httpx_request(self, node: ast.Call, key: int, method: str) -> None:
        client, default_headers = self._http_clients[key]
        if not node.args:
            return
        allowed = {"headers", "json", "content", "data", "params", "timeout"}
        if any(keyword.arg not in allowed for keyword in node.keywords):
            return
        target_index = 1 if method == "request" else 0
        if len(node.args) <= target_index:
            return
        args = [self._seg(argument) for argument in node.args[: target_index + 1]]
        if key in self._http_dynamic_clients:
            parts = self._http_url_parts.get(key)
            if parts is None:
                return
            args[target_index] = (
                "urlunsplit(('', '', "
                f"{parts}.path or '/', {parts}.query, ''))"
            )
        by_name = {keyword.arg: keyword.value for keyword in node.keywords}
        params = by_name.get("params")
        if params is not None:
            self.needs_urlencode = True
            args[target_index] = f"f'{{{args[target_index]}}}?{{urlencode({self._seg(params)})}}'"
        headers: list[str] = []
        if default_headers is not None:
            headers.append(f"*{self._http_headers(default_headers)}")
        request_headers = by_name.get("headers")
        if request_headers is not None:
            headers.append(f"*{self._http_headers(request_headers)}")
        body = None
        if (value := by_name.get("json")) is not None:
            self.needs.add("dumps")
            body = f"dumps({self._seg(value)})"
            headers.insert(0, "(b'content-type', b'application/json')")
        elif (value := by_name.get("data")) is not None:
            self.needs_urlencode = True
            body = f"urlencode({self._seg(value)}).encode('ascii')"
            headers.insert(0, "(b'content-type', b'application/x-www-form-urlencoded')")
        elif (value := by_name.get("content")) is not None:
            body = self._seg(value)
        keywords = []
        if headers:
            keywords.append(f"headers=({', '.join(headers)},)")
        if body is not None:
            keywords.append(f"body={body}")
        suffix = "" if not keywords else ", " + ", ".join(keywords)
        if method in {"get", "post", "request"}:
            call = f"{client}.{method}({', '.join(args)}{suffix})"
        else:
            target = args[target_index]
            call_args = [repr(method.upper()), target]
            call = f"{client}.request({', '.join(call_args)}{suffix})"
        self._replace_all_of(node, call)

    def _rewrite_model_dataclass(self, node: ast.Call, func: ast.Attribute) -> None:
        """Replace a statically named ormar projection with Wreath's dataclass."""
        outer = self._parents.get(id(node))
        if isinstance(outer, ast.ClassDef):
            projected_name = f"_{outer.name}Fields"
        elif (
            isinstance(outer, ast.Assign)
            and len(outer.targets) == 1
            and isinstance(outer.targets[0], ast.Name)
        ):
            projected_name = outer.targets[0].id
        elif isinstance(outer, ast.AnnAssign) and isinstance(outer.target, ast.Name):
            projected_name = outer.target.id
        else:
            projected_name = None
        arguments = [self._seg(func.value)]
        arguments.extend(
            f"{keyword.arg}={self._seg(keyword.value)}" for keyword in node.keywords
        )
        if projected_name is not None:
            arguments.append(f"name={projected_name!r}")
        self.needs.add("model_dataclass")
        self._rewritten.add(id(func))
        self._replace_all_of(node, f"model_dataclass({', '.join(arguments)})")

    def visit_Name(self, node: ast.Name) -> None:
        """Rename or retain one bare reference to a framework name.

        Every mention counts, not only the one being called. `FastAPI` appears
        as an annotation far more often than as a constructor, and `HTTPException`
        appears in an `except` clause and an exception-handler registration where
        there is no call to rewrite at all.
        """
        self._track_reference(node, self.imports.origin(node))
        self.generic_visit(node)

    def _track_reference(self, node: _Positioned, origin: str) -> None:
        if id(node) in self._rewritten or self._inside_replaced(node):
            return
        wreath_name = _RENAMED_ORIGINS.get(origin)
        if wreath_name is not None:
            self._rewritten.update(id(item) for item in ast.walk(node))
            self.needs.add(wreath_name)
            if wreath_name != origin.split(".")[-1]:
                self.buf.replace(node, wreath_name)
        elif origin in _RETAINED_ORIGINS:
            # Keyed by the name *this module* uses. `from fastapi import status
            # as fastapistatus` is legal, and retaining "status" would have kept
            # an import nothing referred to while dropping the one that mattered.
            self._retain.add(node.id if isinstance(node, ast.Name) else origin.split(".")[-1])
        elif origin.startswith("strawberry") and isinstance(node, ast.Name):
            self._retain.add(node.id)
        elif (
            status := status_int(self.imports, node if isinstance(node, ast.expr) else None)
        ) is not None and isinstance(node, ast.Attribute):
            # `status.HTTP_404_NOT_FOUND` is an integer with a long name, and
            # wreath has no such module, and the number is what the reader
            # already has in mind anyway.
            self._rewritten.add(id(node))
            self._replace_all_of(node, str(status))

    def _inside_replaced(self, node: _Positioned) -> bool:
        """Whether this node sits inside a span already replaced wholesale.

        Edits are applied from the end of the file backwards and an overlapping
        one is dropped, so an inner edit queued after an outer one *wins* — the
        rewritten `HTTPException(...)` would be thrown away in favour of an
        integer written into the call it replaced. Nothing inside a replaced
        span is worth editing, so nothing inside one is.
        """
        start = self.buf.start_of(node)
        return any(low <= start < high for low, high in self._replaced)

    def _replace_all_of(self, node: _Positioned, text: str) -> None:
        """Replace a whole construct, and remember that its insides are gone."""
        self._replaced.append((self.buf.start_of(node), self.buf.end_of(node)))
        self.buf.replace(node, text)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        value = node.value
        self._track_reference(node, self.imports.origin(node))
        if (
            self.opinionated
            and isinstance(value, ast.Name)
            and (self._enclosing_callable_id(node), value.id) in self._http_responses
        ):
            if node.attr == "status_code":
                end = self.buf.end_of(node)
                self.buf._edits.append((end - len("status_code"), end, b"status"))
            elif node.attr == "content":
                end = self.buf.end_of(node)
                self.buf._edits.append((end - len("content"), end, b"body"))
            elif node.attr == "text":
                self._replace_all_of(node, f"{value.id}.body.decode('utf-8')")
        if node.attr == "status_code" and self._test_clients:
            end = self.buf.end_of(node)
            self.buf._edits.append((end - len("status_code"), end, b"status"))
        # Mirrors the analyzer: the verb after `.objects` names the rewrite, and
        # the `.objects` underneath it is claimed so one chain gets one note.
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "objects"
            and node.attr == "__class__"
        ):
            self._claimed_objects.add(id(value))
            self._annotate(value.lineno, "orm.manager_patch")
        elif isinstance(value, ast.Attribute) and value.attr == "objects":
            self._claimed_objects.add(id(value))
            call = self._parents.get(id(node))
            rule_id = query_rule(
                node.attr,
                call if isinstance(call, ast.Call) else None,
                chain_tail(node, self._parents),
                model=self._seg(value.value),
                relations=self.orm_relations,
                columns=self.orm_columns,
                tables=self.orm_tables,
                unique_constraints=self.orm_unique_constraints,
                plain_mappings=plain_filter_mappings(
                    call if isinstance(call, ast.Call) else None, self._parents
                ),
            )
            if not self._rewrite_query(node, rule_id):
                self._annotate(value.lineno, rule_id)
        elif node.attr == "objects" and id(node) not in self._claimed_objects:
            self._annotate(node.lineno, "orm.manager_value")
        self.generic_visit(node)

    # -- queries -----------------------------------------------------------------
    def _query_chain(self, head: ast.Attribute):
        """`[(verb, call), …]` for a `Model.objects.…` chain, and its last node."""
        steps: list[tuple[str, ast.Call | None]] = []
        current: ast.AST = head
        verb = head.attr
        outer = self._parents.get(id(head))
        while True:
            call = outer if isinstance(outer, ast.Call) else None
            steps.append((verb, call))
            current = call or current
            following = self._parents.get(id(current))
            if not isinstance(following, ast.Attribute):
                return steps, current
            verb = following.attr
            outer = self._parents.get(id(following))
            current = following

    def _rewrite_query(self, head: ast.Attribute, rule_id: str) -> bool:
        """Turn `Llama.objects.filter(id=x).all()` into a real wreath query.

        Returns whether it happened. Three things have to line up, and when any
        of them does not the chain is left exactly as written and gets its note
        instead — a query that runs against the wrong session, or that quietly
        drops a lookup, is worse than one a person still has to move.

        1. Every verb in the chain has to be one of the handful below.
        2. Every lookup has to be one `LOOKUP_OPERATOR`/`LOOKUP_METHOD` spells.
        3. If the chain *runs* the query rather than just building it, a session
           has to be in scope. Inside a route handler wreath supplies one; the
           enclosing function otherwise has to take one, and that is a change to
           every caller, so it is left to the person making it.
        """
        if not rule_id.startswith("orm.query.") or rule_id not in _QUERY_TRANSLATED:
            return False
        objects = head.value
        if not isinstance(objects, ast.Attribute):
            return False  # not `<Model>.objects.<verb>`
        model = self._seg(objects.value)
        if not model:
            return False
        steps, last = self._query_chain(head)
        plan = _QueryPlan(model)
        for verb, call in steps:
            if not plan.step(self, verb, call):
                return False
        if plan.runner is not None and self._session is None:
            self._session_wanted = True
            return False
        if plan.runner == "get_or_create":
            plan.existing_name = self._fresh_name(f"_existing_{_snake(plan.model)}")
        elif plan.runner in ("values", "values_list"):
            plan.row_name = self._fresh_name("_row")
        target: _Positioned = last if isinstance(last, (ast.expr, ast.stmt)) else head
        awaited = self._parents.get(id(target))
        text = plan.render(self._session)
        if plan.runner is not None and isinstance(awaited, ast.Await):
            target = awaited  # our text carries its own `await`
        elif plan.runner is not None:
            text = f"({text})" if isinstance(awaited, ast.Attribute) else text
        self._replace_all_of(target, text)
        return True

    def _predicate(self, model: str, keyword: ast.keyword) -> str | None:
        """One `filter(**kw)` keyword as a wreath predicate expression."""
        if keyword.arg is None:
            if isinstance(keyword.value, ast.Name):
                self.needs.add("where_fields")
                return f"*where_fields({model}, {keyword.value.id})"
            return None
        column, suffix = split_lookup(keyword.arg)
        if "__" in column:
            if not _resolved_column_path(
                model, column, self.orm_relations, self.orm_columns
            ):
                return None
            column = ".".join(column.split("__"))
        value = self._seg(keyword.value)
        if suffix == "isnull":
            method = _NULL_METHOD.get(
                keyword.value.value if isinstance(keyword.value, ast.Constant) else None
            )
            return None if method is None else f"{model}.{column}.{method}()"
        if suffix in LOOKUP_METHOD:
            method, shape = LOOKUP_METHOD[suffix]
            return f"{model}.{column}.{method}({shape % value})"
        operator = LOOKUP_OPERATOR.get(suffix)
        return None if operator is None else f"{model}.{column} {operator} {value}"

    def _include_expression(self, model: str, path: str) -> str | None:
        """One resolved ``a__b`` relation trail as nested selectin options."""
        current = model
        parts: list[tuple[str, str]] = []
        for relation in path.split("__"):
            target = self.orm_relations.get(current, {}).get(relation)
            if target is None:
                return None
            parts.append((current, relation))
            current = target
        nested = ""
        for owner, relation in reversed(parts):
            suffix = f"({nested})" if nested else "()"
            nested = f"{owner}.{relation}.selectin{suffix}"
        return nested

    def _projection_pair(self, model: str, name: str) -> tuple[str, str] | None:
        """The legacy dictionary key and Wreath column selected for it."""
        if name in self.orm_relations.get(model, {}):
            return name, f"{name}_id"
        if name in self.orm_columns.get(model, set()):
            return name, name
        return None

    def _rewrite_http_exception(self, node: ast.Call) -> None:
        """`HTTPException(status_code=404, detail=x)` -> `NotFound(x)`.

        The verdict comes from `http_exception_rule`, the same function the
        report uses, so a status wreath ships no class for is annotated on both
        sides rather than reported translated here and skipped there. When the
        rewrite does not happen the *name* survives, and `visit_Name` sees that
        and keeps an import for it — pointed at `wreath.exceptions`, whose
        `HTTPException` is the base class of every class in this table.
        """
        rule_id = http_exception_rule(self.imports, node)
        if rule_id != "exc.http_literal":
            self._annotate(node.lineno, rule_id)
            return
        status = status_int(self.imports, http_exception_status(node))
        # `http_exception_rule` returned `exc.http_literal`, so the status is an
        # int this table has a class for. Read rather than asserted: `-O` strips
        # an assert, and a wrong lookup here would emit a call to a name that
        # does not exist.
        cls = STATUS_EXCEPTION[status] if status is not None else None
        if cls is None:  # pragma: no cover - unreachable while the rule agrees
            self._annotate(node.lineno, "exc.http_unmapped")
            return
        detail = next((kw.value for kw in node.keywords if kw.arg == "detail"), None)
        if detail is None and len(node.args) > 1:
            detail = node.args[1]
        self.needs.add(cls)
        self._rewritten.add(id(node.func))
        detail_src = self._seg(detail) if detail is not None else ""
        self._replace_all_of(node, f"{cls}({detail_src})")

    def _rewrite_cache_call(self, node: ast.Call, func: ast.expr) -> None:
        """`TTLCache(maxsize=500, ttl=60)` -> `BoundedCache(max_entries=500, ttl=60)`.

        The same bounded LRU with the same eviction, under the framework's own
        memory budget. `LRUCache`/`FIFOCache`/`LFUCache` land on it too: wreath
        has one bounded cache, and the eviction order is the part that changes.
        """
        self.needs.add("BoundedCache")
        self._rewritten.add(id(func))
        self._replace_all_of(func, "BoundedCache")
        for keyword in node.keywords:
            if keyword.arg == "maxsize":
                start = self.buf.start_of(keyword)
                self.buf._edits.append((start, start + len("maxsize"), b"max_entries"))

    def _rewrite_response_call(self, node: ast.Call, wreath_name: str) -> None:
        """Bring a response constructor's arguments over with its name.

        The class is a rename; its arguments are not quite. Wreath calls the
        status `status`, and takes the body as the first argument rather than
        `content=`. Renaming the import without these would have produced a
        handler that raises `TypeError` the first time it answers — which is
        exactly the sort of "translated" that is worse than a note.
        """
        rename = {"status_code": "status", "content": _RESPONSE_BODY_ARG[wreath_name]}
        for keyword in node.keywords:
            if keyword.arg not in rename:
                continue
            # An in-place rename of the keyword only. Rebuilding the call would
            # have been simpler to write and was wrong twice over: it reordered
            # `JSONResponse(status_code=…, content=…)` into a positional
            # argument after a keyword one, and it re-copied the argument source
            # over the top of edits already made inside it.
            start = self.buf.start_of(keyword)
            self.buf._edits.append(
                (start, start + len(keyword.arg or ""), rename[keyword.arg].encode())
            )
        unmapped = sorted(
            keyword.arg
            for keyword in node.keywords
            if keyword.arg not in rename and keyword.arg not in ("background", "status")
        )
        if unmapped:
            self._note(
                node.lineno,
                "resp.class",
                f"{wreath_name} has no "
                + ", ".join(f"{name}=" for name in unmapped)
                + ". Headers go in as a list of lowercase byte pairs, "
                '`[(b"x-total", b"12")]`, and the content type comes from the '
                "response class itself -- so move these across or drop them",
            )

    def _rewrite_add_middleware(self, node: ast.Call) -> None:
        function = node.func
        if not isinstance(function, ast.Attribute):
            raise RuntimeError("add_middleware rewrite requires an attribute call")
        if not node.args:
            return
        first = node.args[0]
        tail = self.imports.origin(first).split(".")[-1]
        policy = {
            "CORSMiddleware": ("cors", "CorsPolicy"),
            "TrustedHostMiddleware": ("trusted_host", "TrustedHostPolicy"),
        }.get(tail)
        if tail == "RateLimitingMiddleware" and self._rewrite_rate_limit_middleware(node):
            return
        if policy is None:
            self._annotate(node.lineno, "mw.custom")
            return
        field, class_name = policy
        if class_name == "TrustedHostPolicy":
            allowed = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "allowed_hosts"),
                None,
            )
            if (
                isinstance(allowed, (ast.List, ast.Tuple))
                and len(allowed.elts) == 1
                and isinstance(allowed.elts[0], ast.Constant)
                and allowed.elts[0].value == "*"
            ):
                self._replace_all_of(node, "")
                return
        self.needs.update({"HttpPolicy", class_name})
        arguments = [self._seg(argument) for argument in node.args[1:]]
        arguments.extend(f"{kw.arg}={self._seg(kw.value)}" for kw in node.keywords)
        receiver = self._seg(function.value)
        configured = f"{class_name}({', '.join(arguments)})"
        self.buf.replace(
            node,
            f"{receiver}.configure_http_policy(HttpPolicy({field}={configured}))",
        )

    def _rewrite_rate_limit_middleware(self, node: ast.Call) -> bool:
        provider = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "provider"),
            None,
        )
        if not (
            isinstance(provider, ast.Call)
            and (
                provider.func.attr
                if isinstance(provider.func, ast.Attribute)
                else getattr(provider.func, "id", "")
            )
            == "InMemoryLimitProvider"
        ):
            return False
        options = {keyword.arg: keyword.value for keyword in provider.keywords}
        if "limit" not in options or "timespan" not in options:
            return False
        arguments = [
            f"limit={self._seg(options['limit'])}",
            f"window={self._seg(options['timespan'])}",
        ]
        included = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "included_routes"),
            None,
        )
        if included is not None:
            routes = self._seg(included)
            arguments.append(
                "exempt=lambda request: not any("
                f"request.path.startswith(prefix) for prefix in {routes})"
            )
        receiver = self._seg(node.func.value) if isinstance(node.func, ast.Attribute) else "app"
        self.needs.update({"HttpPolicy", "RateLimitPolicy"})
        self._removed_middleware_imports.update(
            {"RateLimitingMiddleware", "InMemoryLimitProvider"}
        )
        self._replace_all_of(
            node,
            f"{receiver}.configure_http_policy(HttpPolicy("
            f"rate_limit=RateLimitPolicy({', '.join(arguments)})))",
        )
        blocked = options.get("block_duration")
        if not (
            blocked is None
            or isinstance(blocked, ast.Constant)
            and blocked.value is None
        ):
            self._note(
                node.lineno,
                "mw.custom",
                "the fixed-window limiter became Wreath's token bucket with the same "
                "limit and window. Its block_duration has no equivalent: refusals now "
                "carry Retry-After for the bucket's actual refill time",
            )
        else:
            self._resolve(node.lineno, "mw.custom")
        return True


# --------------------------------------------------------------------------- module predicates
# `_config_extra`, `_is_field_constraint`, `_is_lifespan`, `_is_false` and
# `_is_true` are imported from the analyzer rather than restated here: the
# emitter must recognize exactly what the analyzer billed, or the report and the
# output disagree about the same line.


def _mutable_factory(value: ast.AST | None) -> str | None:
    if isinstance(value, ast.List):
        return "list"
    if isinstance(value, ast.Dict):
        return "dict"
    if isinstance(value, ast.Set):
        return "set"
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in ("list", "dict", "set")
        and not value.args
    ):
        return value.func.id
    return None


def _ends_argument_list(source: bytes, close: int) -> bool:
    """Whether a new keyword can be written at `close` with no comma in front.

    True for an empty list `f()` and for one with a trailing comma `f(a,)`.
    Comments are skipped on the way back, because `f(a,  # why\n)` puts a
    newline and a comment between the comma and the parenthesis.
    """
    index = close - 1
    while index >= 0:
        byte = source[index : index + 1]
        if byte in b" \t\r\n":
            index -= 1
            continue
        if byte == b"\n":  # pragma: no cover - covered above
            index -= 1
            continue
        line_start = source.rfind(b"\n", 0, index) + 1
        hash_at = source.find(b"#", line_start, index + 1)
        if hash_at != -1 and source.find(b"\n", hash_at, index + 1) == -1:
            index = hash_at - 1  # step over a trailing comment
            continue
        return byte in b",("
    return True


def _marker_default(call: ast.Call) -> ast.expr | None:
    """The default value a `Query(...)`/`Path(...)` marker carries, if any.

    FastAPI accepts it either way round — `Query(20)` and `Query(default=20)`
    are the same parameter — and reading only the positional spelling made every
    `Query(default=False)` look like a *required* parameter, which is the
    opposite of what it says.
    """
    for keyword in call.keywords:
        if keyword.arg == "default":
            return keyword.value
    if call.args and not (
        isinstance(call.args[0], ast.Constant) and call.args[0].value is Ellipsis
    ):
        return call.args[0]
    return None


def _config_constraints(value: ast.AST | None) -> list[ast.Call]:
    """The `constraints=[...]` entries of an `ormar_config = base.copy(...)`."""
    if not isinstance(value, ast.Call):
        return []
    for kw in value.keywords:
        if kw.arg == "constraints" and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [entry for entry in kw.value.elts if isinstance(entry, ast.Call)]
    return []


def _copy_tablename(value: ast.AST | None) -> str | None:
    """Pull the tablename out of `base_ormar_config.copy(tablename="x")`."""
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg == "tablename" and isinstance(kw.value, ast.Constant):
                # Only a string literal is a usable table name; anything else
                # leaves the caller to emit its "add table=<name> by hand" note.
                table = kw.value.value
                return table if isinstance(table, str) else None
    return None


# --------------------------------------------------------------------------- public
def _provenance(source: str, body: str) -> str:
    src_h = hashlib.sha256(source.encode("utf-8")).hexdigest()
    out_h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return (
        f"{_HEADER_PREFIX} generated by `wreath port` — review before use.\n"
        f"{_HEADER_PREFIX} source-sha256={src_h}\n"
        f"{_HEADER_PREFIX} output-sha256={out_h}\n"
    )


def _read_source(source) -> str:
    """A `Path` is read from disk; a `str` is treated as source text verbatim."""
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8")
    return source


def emit_module(source, context: TreeContext | None = None, *, opinionated: bool = False) -> str:
    """Port one module. `source` may be a Path/path-string (read) or source text.

    Returns the ported source (with a provenance header), preserving every function
    body verbatim. Raises `EmitError` if the result is not valid Python.

    `context` carries what the rest of the tree knows — which models exist, what
    type each primary key is, which columns each one declares — so a foreign key
    onto a model in another file gets the right type and a body parameter is
    recognized as a body. `port_tree` reads it once for the whole tree; a lone
    module falls back to what it can see in itself.

    Two orderings here are load-bearing:

    * the walk runs **before** the imports are rewritten, because whether a name
      can be dropped from an import depends on whether every use of it was
      replaced, and only the walk knows that;
    * the emitter's own notes are written **before** the analyzer's, so where
      both have something to say about a line, the more specific one wins.
    """
    text = _read_source(source)
    tree = ast.parse(text)
    imports = _Imports().visit(tree)
    if context is None:
        context = (
            TreeContext.of([source], opinionated=opinionated)
            if isinstance(source, Path)
            else TreeContext()
        )
    resolved = {**context.pk_types, **module_pk_types(tree, imports)}
    emitter = _Emitter(text, imports, resolved, opinionated=opinionated)
    emitter.orm_columns = context.orm_columns
    emitter.orm_relations = context.orm_relations
    emitter.orm_tables = context.orm_tables
    emitter.orm_unique_constraints = context.orm_unique_constraints
    emitter.orm_mixins = frozenset(context.index.get("orm_mixin", ()))
    emitter.pydantic_models = frozenset(context.index.get("pydantic", ()))
    emitter.settings_models = frozenset(context.index.get("settings", ()))
    emitter.session_functions = context.session_functions
    emitter.session_definition_sites = context.session_definition_sites
    emitter.session_call_sites = context.session_call_sites
    emitter.session_sites_resolved = context.session_sites_resolved
    if isinstance(source, Path):
        emitter._source_path = str(source.resolve())
    emitter.collect_dep_targets(tree)
    emitter.visit(tree)
    path = source if isinstance(source, Path) else Path("<module>")
    emitter.annotate_findings(module_findings(path, path, tree, imports, context))
    emitter.rewrite_imports(tree)
    emitter.inject_imports()
    body = emitter.buf.render()
    try:
        ast.parse(body)
    except SyntaxError as exc:  # pragma: no cover - tool-bug guard
        raise EmitError(f"emitted module is not valid Python: {exc}") from exc
    return _provenance(text, body) + body


def _strip_provenance(text: str) -> str:
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].startswith(_HEADER_PREFIX):
        i += 1
    return "".join(lines[i:])


def _output_hash(text: str) -> str:
    return hashlib.sha256(_strip_provenance(text).encode("utf-8")).hexdigest()


def _recorded_hashes(text: str) -> tuple[str | None, str | None]:
    src = out = None
    for line in text.splitlines():
        if not line.startswith(_HEADER_PREFIX):
            break
        if "source-sha256=" in line:
            src = line.split("source-sha256=", 1)[1].strip()
        elif "output-sha256=" in line:
            out = line.split("output-sha256=", 1)[1].strip()
    return src, out


def port_tree(
    root: str | Path,
    output: str | Path | None = None,
    *,
    in_place: bool = False,
    force: bool = False,
    opinionated: bool = False,
) -> PortResult:
    """Port every `.py` under `root` into a sister tree (or in place).

    Idempotent: an unchanged source whose output still carries a matching provenance
    hash is skipped; an output that was hand-edited (its body hash no longer matches
    the recorded `output-sha256`) is left untouched unless `force`.

    **A source that cannot be read is recorded in `failed`, not fatal** — the same
    rule `analyze` follows, for the same reason: one broken symlink in a large tree
    must not end the run. **A destination that cannot be written *is* fatal**, because
    it condemns every remaining file and a partial output tree is indistinguishable
    from a complete one.
    """
    root = Path(root)
    if in_place and not force:
        raise ValueError("wreath port --in-place requires --force (or use --output <dir>)")
    if in_place:
        out_root = root
    elif output is None:
        raise ValueError("wreath port needs --output <dir> (or --in-place --force)")
    else:
        out_root = Path(output)
    result = PortResult()
    # One pass over the tree first, so a foreign key onto a model declared in
    # another file gets that model's real key type instead of a guess.
    sources = list(_iter_py(root))
    context = TreeContext.of(sources, opinionated=opinionated)
    for src_path in sources:
        rel = src_path.relative_to(root) if root != src_path else src_path.name
        dest = (out_root / rel) if not in_place else src_path

        # Reads are per-file recoverable; writes are not. Everything inside this
        # block touches only *input* — the source, and any existing output whose
        # provenance decides what to do next — so one bad file takes itself out
        # of the run and leaves the rest in, exactly as `analyze` does. The
        # writes below sit outside it deliberately (see the comment there).
        try:
            source = src_path.read_text(encoding="utf-8")
            if dest.exists() and not in_place:
                existing = dest.read_text(encoding="utf-8")
                rec_src, rec_out = _recorded_hashes(existing)
                cur_src = hashlib.sha256(source.encode("utf-8")).hexdigest()
                if rec_out is not None and rec_out != _output_hash(existing) and not force:
                    result.skipped.append(dest)  # hand-edited output — refuse to clobber
                    continue
                if rec_src == cur_src:
                    result.skipped.append(dest)  # unchanged source — idempotent no-op
                    continue
                emitted = emit_module(src_path, context, opinionated=opinionated)
                regenerating = True
            else:
                emitted = emit_module(src_path, context, opinionated=opinionated)
                regenerating = False
        except _SKIPPABLE as exc:
            # An unreadable source, a non-UTF-8 one, or one that is not valid
            # Python: recorded and stepped over. `EmitError` is deliberately not
            # caught — a structurally broken *emit* is a tool bug, and turning it
            # into a per-file skip is how a codemod quietly drops your code.
            key = _relative_to(src_path, root)
            result.failed.append(SkippedFile(key, _skip_reason(exc), _skip_detail(exc)))
            continue

        # A destination that cannot be written is fatal, and stays uncaught. An
        # unreadable *source* costs one file; an unwritable *destination* means
        # the output tree is wrong — a full disk, a read-only mount, a bad
        # --output path — and every remaining file would hit it too. Continuing
        # would hand back a half-written tree that looks like a complete port.
        if regenerating:
            dest.write_text(emitted, encoding="utf-8")
            result.regenerated.append(dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(emitted, encoding="utf-8")
            result.written_files.append(dest)
    return result
