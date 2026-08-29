"""What each framework name becomes.

One table per question, in one module, because the import rewrite and the
call-site rewrite have to give the same answer: a name whose import is
swapped and whose use is not produces a module that imports nothing and
runs nowhere."""

from __future__ import annotations

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
_DJANGO_PYANN = {
    "Uuid": "uuid.UUID",
    "Int16": "int",
    "Int32": "int",
    "Int64": "int",
    "Varchar": "str",
    "Text": "str",
    "Bool": "bool",
    "Float64": "float",
    "Numeric": "decimal.Decimal",
    "Date": "datetime.date",
    "TimestampTz": "datetime.datetime",
    "Jsonb": "dict",
    "Bytea": "bytes",
}

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
