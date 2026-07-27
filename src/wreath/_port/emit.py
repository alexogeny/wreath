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
    _SKIPPABLE,
    HTTP_METHODS,
    _base_kind,
    _config_extra,
    _Imports,
    _is_false,
    _is_field_constraint,
    _is_lifespan,
    _is_true,
    _iter_py,
    _relative_to,
    _returns_in,
    _skip_detail,
    _skip_reason,
    chain_tail,
    lifespan_names,
    lifespan_shape,
    module_pk_types,
    parent_map,
    query_rule,
    settings_class_rule,
    settings_required,
    status_code_rule,
    status_int,
)
from .ir import SkippedFile
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
    # NOTE: FastAPI's UploadFile has no wreath public equivalent — intentionally NOT
    # mapped, so it is left in place + flagged rather than emitted as a broken import.
}
_FASTAPI_RENAMED = {"FastAPI": "Wreath", "APIRouter": "Router"}

# A rewritten wreath name -> the module it is actually imported from. Markers live in
# `wreath.binding` and CORSMiddleware/TrustedHostMiddleware in `wreath.middleware`;
# everything else (Wreath, Router, Depends, Request) is top-level `wreath`.
_WREATH_MODULE = {
    "Query": "wreath.binding", "Path": "wreath.binding", "Header": "wreath.binding",
    "Cookie": "wreath.binding", "Form": "wreath.binding", "File": "wreath.binding",
    "CORSMiddleware": "wreath.middleware", "TrustedHostMiddleware": "wreath.middleware",
    "WebSocket": "wreath.websocket", "WebSocketDisconnect": "wreath.websocket",
    # `wreath` re-exports JSONResponse and Response; the rest live in wreath.response.
    "TextResponse": "wreath.response",
    "BadRequest": "wreath.exceptions", "Unauthorized": "wreath.exceptions",
    "Forbidden": "wreath.exceptions", "NotFound": "wreath.exceptions",
    "MethodNotAllowed": "wreath.exceptions", "Conflict": "wreath.exceptions",
    "UnprocessableEntity": "wreath.exceptions", "TooManyRequests": "wreath.exceptions",
    # ORM (Phase 2): declarative API from wreath.orm, PgTypes from wreath.orm.types.
    "Model": "wreath.orm", "Mapped": "wreath.orm", "column": "wreath.orm",
    "relationship": "wreath.orm", "Ge": "wreath.orm", "Le": "wreath.orm",
    "Gt": "wreath.orm", "Lt": "wreath.orm", "Length": "wreath.orm",
    "Uuid": "wreath.orm.types", "Varchar": "wreath.orm.types", "Text": "wreath.orm.types",
    "Int64": "wreath.orm.types", "Int32": "wreath.orm.types", "Int16": "wreath.orm.types",
    "Bool": "wreath.orm.types", "Float64": "wreath.orm.types", "Date": "wreath.orm.types",
    "Timestamp": "wreath.orm.types", "TimestampTz": "wreath.orm.types",
    "Json": "wreath.orm.types", "Jsonb": "wreath.orm.types", "Array": "wreath.orm.types",
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

# HTTP status literal -> wreath exception class (design 07 / docs/from-fastapi/index.md).
# Only statuses whose wreath exception class actually exists are rewritten; others
# (410/415/500/501/503) fall through to a needs-review annotation.
_STATUS_EXC = {
    400: "BadRequest", 401: "Unauthorized", 403: "Forbidden", 404: "NotFound",
    405: "MethodNotAllowed", 409: "Conflict", 422: "UnprocessableEntity",
    429: "TooManyRequests",
}

# Query/Path/... kwargs that map to a wreath marker (renamed where noted). Everything
# else (gt/lt/min_length/regex/description/...) is dropped from the marker and reported.
_KW_RENAME = {"ge": "minimum", "le": "maximum"}
_KW_KEEP = frozenset({"alias"})
_MARKERS = frozenset({"Query", "Path", "Header", "Cookie", "Form", "File"})

# ormar column type -> wreath PgType name (wreath.orm.types). DateTime is resolved by its
# timezone= kwarg; ARRAY (ormar_postgres_extensions) is handled by element type. Types with
# no wreath equivalent (Decimal/Numeric, LargeBinary, ...) are intentionally absent -> annotated.
_ORMAR_TYPE = {
    "UUID": "Uuid", "String": "Varchar", "Text": "Text", "Integer": "Int64",
    "BigInteger": "Int64", "SmallInteger": "Int16", "Boolean": "Bool",
    "Float": "Float64", "Date": "Date", "JSON": "Jsonb",
}
_SA_ELEM_TYPE = {"String": "Text", "Text": "Text", "Integer": "Int64", "Boolean": "Bool"}
# wreath PgType name -> the Python annotation for a FK column of that PK type.
_PG_PYANN = {
    "Uuid": "uuid.UUID", "Int64": "int", "Int32": "int", "Int16": "int",
    "Varchar": "str", "Text": "str",
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
_REWRITTEN = frozenset({
    "route.app", "route.router", "route.method", "route.include_static",
    "param.query", "param.path", "param.header", "param.cookie", "param.form", "param.file",
    "pydantic.model", "pydantic.field", "pydantic.config_forbid",
    "exc.http_literal", "exc.handler", "mw.cors", "mw.trustedhost", "depends.use",
})

_HEADER_PREFIX = "# wreath-port:"


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
        raw = self.b[start:(end if end != -1 else len(self.b))]
        return raw[: len(raw) - len(raw.lstrip(b" \t"))].decode("utf-8")

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
        self, source: str, imports: _Imports, pk_types: dict[str, str] | None = None
    ) -> None:
        self.buf = _Buffer(source)
        self.src = source
        self.imports = imports
        self.pk_types = pk_types or {}       # ORM model name -> PK PgType (FK inference)
        self.needs: set[str] = set()          # extra `from wreath import` names
        self._from_fastapi_wreath: set[str] = set()  # names already on the rewritten fastapi import
        self.needs_annotated = False          # `from typing import Annotated`
        self.needs_dataclass = False          # `from dataclasses import dataclass, field`
        self.annotated_lines: set[tuple[int, str]] = set()  # (line, rule_id) dedupe
        self._dep_targets: set[str] = set()   # function names referenced by Depends(<name>)
        self._claimed_objects: set[int] = set()  # `.objects` billed by its verb
        # Same parent map the analyzer builds, for the same reason: the verdict
        # a query gets depends on its arguments and on the verbs chained after
        # it, and neither is visible from the head node alone.
        self._parents: dict[int, ast.AST] = {}
        # Names handed to an application as `lifespan=`; filled by `visit_Module`,
        # since the `FastAPI(lifespan=...)` call sits below the `def` it names.
        self._lifespan_names: frozenset[str] = frozenset()

    def visit_Module(self, node: ast.Module) -> None:
        self._parents = parent_map(node)
        self._lifespan_names = lifespan_names(node)
        self.generic_visit(node)

    # -- helpers -----------------------------------------------------------------
    def _seg(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.src, node) or ""

    def _annotate(self, line: int, rule_id: str, extra: str = "") -> None:
        key = (line, rule_id)
        if key in self.annotated_lines:
            return
        self.annotated_lines.add(key)
        _c, _cat, tag, message = RULES[rule_id]
        if extra:
            message = f"{message} ({extra})"
        indent = self.buf.line_indent(line)
        self.buf.insert_before_line(
            line, f"{indent}# TODO(wreath-port: [{tag}] {message} [{rule_id}])"
        )

    # -- imports -----------------------------------------------------------------
    def rewrite_imports(self, tree: ast.Module) -> None:
        last_import_line = 0
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import_line = max(last_import_line, _span_end(node)[0])
            if isinstance(node, ast.ImportFrom) and node.module == "fastapi" and node.level == 0:
                self._rewrite_from_fastapi(node)
            elif isinstance(node, ast.ImportFrom) and node.module == "pydantic" and node.level == 0:
                self._rewrite_from_pydantic(node)
            elif (
                isinstance(node, ast.ImportFrom)
                and (module := node.module) is not None
                and module.startswith("fastapi.middleware")
                and any(a.name == "CORSMiddleware" for a in node.names)
            ):
                self.needs.add("CORSMiddleware")
                self.buf.replace(
                    node,
                    self._keep_leftover(node, drop={"CORSMiddleware"}, module=module),
                )
        self._last_import_line = last_import_line

    def _rewrite_from_fastapi(self, node: ast.ImportFrom) -> None:
        keep: list[ast.alias] = []
        wreath_names: list[str] = []
        for alias in node.names:
            if alias.name == "HTTPException":
                continue  # dropped; call sites become exception classes
            if alias.name in _FASTAPI_TO_WREATH:
                wreath_names.append(_FASTAPI_TO_WREATH[alias.name])
            else:
                keep.append(alias)
        self._from_fastapi_wreath.update(wreath_names)
        parts = []
        if wreath_names:
            parts.extend(_grouped_imports(wreath_names))
        if keep:
            parts.append("from fastapi import " + ", ".join(self._alias_str(a) for a in keep))
        self.buf.replace(node, "\n".join(parts) if parts else "")

    def _rewrite_from_pydantic(self, node: ast.ImportFrom) -> None:
        keep = [a for a in node.names if a.name not in ("BaseModel", "Field")]
        if any(a.name in ("BaseModel", "Field") for a in node.names):
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
        if self.needs_dataclass:
            lines.append("from dataclasses import dataclass, field")
        if lines and getattr(self, "_last_import_line", 0):
            self.buf.insert_before_line(self._last_import_line + 1, "\n".join(lines))
        elif lines:
            self.buf.insert_before_line(1, "\n".join(lines) + "\n")

    # -- dependencies (Phase 3): a function referenced by Depends(<name>) gains a
    # leading `request: Request` param, exactly like a route handler.
    def collect_dep_targets(self, tree: ast.Module) -> None:
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and self.imports.origin(n.func).split(".")[-1] == "Depends":
                if n.args and isinstance(n.args[0], ast.Name):
                    self._dep_targets.add(n.args[0].id)

    # -- classes (Pydantic DTOs / ORM / custom middleware) -----------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            if self.imports.origin(dec).split(".")[-1] == "as_form":
                # translated: whole-model Annotated[Model, Form()] replaces it
                self._delete_decorator(dec)
        kind = _base_kind(self.imports, node)
        if kind == "pydantic":
            self._rewrite_pydantic_class(node)
        elif kind == "settings":
            # Same verdict function the report uses. The emitter has no tree
            # index, so a sub-group field reads as `complex` rather than
            # `nested` — which lands on the same class verdict, since neither is
            # the `scalar` shape the translated verdict requires.
            rule_id = settings_class_rule(self.imports, node)
            self._annotate(
                node.lineno, rule_id,
                settings_required(node) if rule_id == "settings.class_env" else "",
            )
        elif kind == "ormar":
            self._rewrite_ormar_class(node)
        elif kind == "sqlmodel":
            self._annotate(node.lineno, "orm.model",
                           "SQLModel/SQLAlchemy translation is manual (Phase 2 targets ormar)")
        elif any(self.imports.origin(b).endswith("BaseHTTPMiddleware") for b in node.bases):
            self._annotate(node.lineno, "mw.custom",
                           "subclass — rework onto wreath's fused middleware base")
        self.generic_visit(node)

    def _rewrite_pydantic_class(self, node: ast.ClassDef) -> None:
        self.needs_dataclass = True
        indent = self.buf.line_indent(node.lineno)
        self.buf.insert_before_line(node.lineno, f"{indent}@dataclass")
        # Strip the BaseModel base. Only the clean sole-base case is auto-rewritten.
        base_origins = [self.imports.origin(b) for b in node.bases]
        if base_origins == ["pydantic.BaseModel"]:
            self._strip_all_bases(node)  # `class X(BaseModel):` -> `class X:`
        elif "pydantic.BaseModel" in base_origins:
            self._annotate(node.lineno, "pydantic.model",
                           "multiple bases — remove BaseModel by hand")
        for stmt in node.body:
            self._rewrite_pydantic_field(stmt)

    def _strip_all_bases(self, node: ast.ClassDef) -> None:
        """Remove the whole `(...)` base list — correct only when it is the sole base."""
        b = self.buf.b
        pstart = self.buf.start_of(node.bases[0])
        pend = self.buf.end_of(node.bases[-1])
        open_i = b.rfind(b"(", 0, pstart)
        close_i = b.find(b")", pend)
        if open_i != -1 and close_i != -1:
            self.buf._edits.append((open_i, close_i + 1, b""))

    def _rewrite_pydantic_field(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            # pydantic v1 nested config class — flag for manual removal, don't delete.
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
                elif extra == "ignore":
                    self._annotate(stmt.lineno, "pydantic.config_ignore")
                return
            ann = stmt.annotation
            if _is_field_constraint(stmt.value, self.imports) or (
                isinstance(ann, ast.Call)
                and self.imports.origin(ann.func).split(".")[-1]
                in ("confloat", "conint", "constr", "condecimal", "conbytes", "conlist",
                    "conset", "condate")
            ):
                self._annotate(stmt.lineno, "pydantic.field_constraint")
                return
            # mutable literal defaults -> field(default_factory=...)
            default = stmt.value
            factory = _mutable_factory(default) if default is not None else None
            if factory and default is not None:
                self.needs_dataclass = True
                self.buf.replace(default, f"field(default_factory={factory})")
        elif isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
        ):
            extra = _config_extra(stmt.value)
            if extra == "forbid":
                self.buf.replace(
                    stmt, "# wreath-port: extra='forbid' is wreath's default (dropped)"
                )
            elif extra == "ignore":
                self._annotate(stmt.lineno, "pydantic.config_ignore")

    # -- classes (ORM models, Phase 2) -------------------------------------------
    def _rewrite_ormar_class(self, node: ast.ClassDef) -> None:
        self.needs.add("Model")
        table, config_stmt = None, None
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ormar_config" for t in stmt.targets
            ):
                config_stmt, table = stmt, _copy_tablename(stmt.value)
        mixins = []
        for base in node.bases:
            if self.imports.origin(base) == "ormar.Model":
                self.buf.replace(base, "Model")
            else:
                mixins.append(base)
        if table is not None and node.bases:
            last = node.bases[-1]
            e = self.buf.end_of(last)
            self.buf._edits.append((e, e, f', table="{table}"'.encode()))
        else:
            self._annotate(node.lineno, "orm.model",
                           "add table=<name> by hand (tablename not found)")
        if config_stmt is not None:
            self.buf.replace(config_stmt, "")
        if mixins:
            self._annotate(node.lineno, "orm.model",
                           "mixin base(s) hold ormar columns — translate the mixin by hand")
        self._annotate(node.lineno, "orm.column",
                       "verify nullability: wreath columns are NOT NULL by default")
        for stmt in node.body:
            if (stmt is not config_stmt and isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.value, ast.Call)):
                # Pass the narrowed call along: `AnnAssign.value` is Optional, and
                # re-reading it in the callee would throw that narrowing away.
                self._rewrite_ormar_column(stmt, stmt.value)

    def _rewrite_ormar_column(self, stmt: ast.AnnAssign, call: ast.Call) -> None:
        tail = self.imports.origin(call.func).split(".")[-1]
        ann_src = self._seg(stmt.annotation)
        if tail == "ForeignKey":
            self._rewrite_ormar_fk(stmt, call, ann_src)
            return
        pgtype = self._ormar_pgtype(tail, call)
        if pgtype is None:
            self._annotate(stmt.lineno, "orm.column",
                           f"no wreath PgType for ormar.{tail} — map by hand")
            return
        self.needs.update({"Mapped", "column"})
        kwargs = self._ormar_kwargs(stmt, call)
        self.buf.replace(stmt.annotation, f"Mapped[{ann_src}]")
        args = "" if not kwargs else ", " + ", ".join(kwargs)
        self.buf.replace(call, f"column({pgtype}{args})")

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
        if pg is not None:  # translated: PK type resolved from the referenced model in-module
            pyann = _PG_PYANN.get(pg, "int")
            self.needs.update({"Mapped", "column", "relationship", pg})
            index = ", index=True" if idx else ""
            col = (f"{name}_id: Mapped[{pyann}] = "
                   f"column({pg}, references={target}.id{index})")
            self.buf.replace(stmt, f'{col}\n{indent}{name} = relationship({target}, load="raise")')
        else:  # needs-review: referenced PK not resolvable in this module -> Uuid default + flag
            self.needs.update({"Mapped", "column", "relationship", "Uuid"})
            index = ", index=True" if idx else ""
            col = (f"{name}_id: Mapped[uuid.UUID] = "
                   f"column(Uuid, references={target}.id{index})")
            self.buf.replace(stmt, f'{col}\n{indent}{name} = relationship({target}, load="raise")')
            self._annotate(stmt.lineno, "orm.fk",
                           "FK column type unresolved; defaulted to Uuid — set by hand")

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
            name = "TimestampTz" if any(
                kw.arg == "timezone" and _is_true(kw.value) for kw in call.keywords
            ) else "Timestamp"
            self.needs.add(name)
            return name
        name = _ORMAR_TYPE.get(tail)
        if name:
            self.needs.add(name)
        return name

    def _ormar_kwargs(self, stmt: ast.AnnAssign, call: ast.Call) -> list[str]:
        out: list[str] = []
        dropped: list[str] = []
        minimum = maximum = None
        for kw in call.keywords:
            if kw.arg in ("primary_key", "nullable", "unique", "index", "server_default",
                          "default"):
                out.append(f"{kw.arg}={self._seg(kw.value)}")
            elif kw.arg == "default_factory":
                out.append(f"default={self._seg(kw.value)}")
            elif kw.arg == "minimum":
                minimum = self._seg(kw.value)
            elif kw.arg == "maximum":
                maximum = self._seg(kw.value)
            elif kw.arg in ("timezone", "item_type"):
                continue
            else:
                dropped.append(kw.arg or "**kwargs")
        if minimum is not None:
            self.needs.add("Ge")
            out.append(f"check=Ge({minimum})")
            if maximum is not None:
                dropped.append(f"maximum={maximum} (combine into the check by hand)")
        elif maximum is not None:
            self.needs.add("Le")
            out.append(f"check=Le({maximum})")
        if dropped:
            self._annotate(stmt.lineno, "orm.column", "needs-review kwargs: " + ", ".join(dropped))
        return out

    # -- functions (routes) ------------------------------------------------------
    def visit_FunctionDef(self, node) -> None:
        route_dec = None
        for dec in node.decorator_list:
            attr = dec.func.attr if (isinstance(dec, ast.Call)
                                     and isinstance(dec.func, ast.Attribute)) else None
            if attr in HTTP_METHODS:
                route_dec = dec
                self._rewrite_route_options(dec, node)
            elif attr == "websocket":
                self._annotate(dec.lineno, "route.websocket")
            dec_origin = self.imports.origin(dec.func if isinstance(dec, ast.Call) else dec)
            tail = dec_origin.split(".")[-1]
            if tail in ("field_validator", "model_validator", "validator", "root_validator"):
                self._annotate(getattr(dec, "lineno", node.lineno), "pydantic.validator")
            elif tail == "asynccontextmanager" and _is_lifespan(
                node, self._lifespan_names, self.imports
            ):
                rule_id, reason = lifespan_shape(node)
                self._annotate(node.lineno, rule_id, reason)
            elif tail == "shared_task" or (tail == "task" and "celery" in dec_origin.lower()):
                self._annotate(getattr(dec, "lineno", node.lineno), "bg.celery")
        if route_dec is not None:
            self._ensure_request_param(node)
            self._split_markers(node)
            self._rewrite_as_form_params(node)
        elif node.name in self._dep_targets:
            self._ensure_request_param(node)  # Phase 3: dependency callable gains `request`
            self._rewrite_as_form_params(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _ensure_request_param(self, node) -> None:
        positional = list(node.args.posonlyargs) + list(node.args.args)
        # Any parameter already named `request` satisfies the injection. Checking
        # only position 0 produced `async def f(request: Request, x, request: Request)`
        # for a handler that declared `request` second — which `ast.parse` accepts
        # (duplicate arguments are a *compile* error, so the round-trip guard let
        # it through) and CPython then refuses to compile.
        if any(a.arg == "request" for a in positional + list(node.args.kwonlyargs)):
            return
        self.needs.add("Request")
        if positional:
            first = positional[0]
            s = self.buf.start_of(first)
            self.buf._edits.append((s, s, b"request: Request, "))
        # zero-arg handlers are rare; annotate rather than risk paren surgery
        elif not (node.args.kwonlyargs or node.args.vararg or node.args.kwarg):
            self._annotate(node.lineno, "route.method", "add `request: Request` param by hand")

    def _delete_decorator(self, dec) -> None:
        """Remove a whole `@decorator` line (assumes it sits on its own line)."""
        start = self.buf._starts[dec.lineno - 1]
        nxt = self.buf.b.find(b"\n", start)
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
        defaulted = list(zip(args.args[len(args.args) - len(args.defaults):], args.defaults,
                             strict=True))
        defaulted += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                      if d is not None]
        for arg, default in defaulted:
            if not (isinstance(default, ast.Call) and default.args
                    and self.imports.origin(default.func).split(".")[-1] == "Depends"):
                continue
            dep = default.args[0]
            if (isinstance(dep, ast.Attribute) and dep.attr == "as_form"
                    and arg.annotation is not None):
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
            self._annotate(dec.lineno, "route.include_in_schema")
        if drop:
            parts = [self._seg(a) for a in dec.args]
            parts += [
                (f"{kw.arg}={self._seg(kw.value)}" if kw.arg else f"**{self._seg(kw.value)}")
                for kw in dec.keywords if kw.arg not in drop
            ]
            self.buf.replace(dec, f"{self._seg(dec.func)}({', '.join(parts)})")

    def _split_markers(self, node) -> None:
        args = node.args
        defaulted = args.args[len(args.args) - len(args.defaults):]
        for arg, default in zip(defaulted, args.defaults, strict=True):
            if not isinstance(default, ast.Call):
                continue
            marker = self.imports.origin(default.func).split(".")[-1]
            if marker not in _MARKERS or arg.annotation is None:
                continue
            self._rewrite_marker_param(arg, default, marker)

    def _rewrite_marker_param(self, arg, call: ast.Call, marker: str) -> None:
        ann = self._seg(arg.annotation)
        # positional[0] is the default value (skip Ellipsis => required).
        default_src = None
        required = (call.args and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value is Ellipsis)
        if call.args and not required:
            default_src = self._seg(call.args[0])
        # A REQUIRED marker (no default) would become a non-default param, which cannot
        # legally follow a defaulted one after the split. Leave it and flag it, rather
        # than emit un-parseable code or reorder the signature.
        if default_src is None:
            self._annotate(
                arg.lineno, "param.query",
                "required marker: move to Annotated and order before defaulted params by hand",
            )
            return
        kept, dropped = [], []
        for kw in call.keywords:
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
            self._annotate(arg.lineno, "param.query_strconstraint",
                           "dropped kwargs: " + ", ".join(dropped))

    # -- calls (FastAPI/APIRouter name, HTTPException, CORS, queries, infra) ------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        origin = self.imports.origin(func)
        tail = origin.split(".")[-1]
        if isinstance(func, ast.Name) and origin in ("fastapi.FastAPI", "fastapi.APIRouter"):
            key = func.id if func.id in _FASTAPI_RENAMED else tail
            self.buf.replace(func, _FASTAPI_RENAMED[key])
            self.needs.add(_FASTAPI_RENAMED.get(tail, tail))
            if tail == "APIRouter":
                self._router_deps(node)
        elif tail == "HTTPException":
            self._rewrite_http_exception(node)
        elif isinstance(func, ast.Attribute) and func.attr == "add_middleware":
            self._rewrite_add_middleware(node)
        elif isinstance(func, ast.Attribute) and func.attr == "include_router" and self._in_loop:
            self._annotate(node.lineno, "route.include_dynamic")
        elif isinstance(func, ast.Attribute) and func.attr in ("delay", "apply_async"):
            self._annotate(node.lineno, "bg.celery")
        elif (isinstance(func, ast.Attribute) and func.attr == "create_task"
              and self.imports.origin(func.value) == "asyncio"):
            self._annotate(node.lineno, "bg.asyncio_loop")
        elif tail == "Process" and origin.startswith("multiprocessing"):
            self._annotate(node.lineno, "bg.multiprocessing")
        elif tail == "GraphQL":
            self._annotate(node.lineno, "graphql.mount")
        elif origin.startswith(("boto3", "aioboto3")):
            self._annotate(node.lineno, "ext.boto3")
        elif "dlock" in origin:
            self._annotate(node.lineno, "lock.dlock")
        elif tail in ("decode", "get_unverified_header") and "jwt" in origin.lower():
            self._annotate(node.lineno, "auth.jwt")
        elif tail == "OAuth2Session" or "authlib" in origin:
            self._annotate(node.lineno, "auth.oauth")
        if any(kw.arg == "postgresql_using" for kw in node.keywords):
            self._annotate(node.lineno, "mig.manual")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        value = node.value
        # Mirrors the analyzer: the verb after `.objects` names the rewrite, and
        # the `.objects` underneath it is claimed so one chain gets one TODO.
        if isinstance(value, ast.Attribute) and value.attr == "objects":
            self._claimed_objects.add(id(value))
            call = self._parents.get(id(node))
            self._annotate(
                value.lineno,
                query_rule(
                    node.attr,
                    call if isinstance(call, ast.Call) else None,
                    chain_tail(node, self._parents),
                ),
            )
        elif node.attr == "objects" and id(node) not in self._claimed_objects:
            self._annotate(node.lineno, "orm.query")
        elif node.attr == "get_pydantic":
            self._annotate(node.lineno, "pydantic.get_pydantic")
        self.generic_visit(node)

    _in_loop = False

    def visit_For(self, node) -> None:
        prev, self._in_loop = self._in_loop, True
        self.generic_visit(node)
        self._in_loop = prev

    visit_AsyncFor = visit_For

    def _router_deps(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "dependencies" and not isinstance(kw.value, (ast.List, ast.Tuple)):
                self._annotate(node.lineno, "depends.router_call")

    def _rewrite_http_exception(self, node: ast.Call) -> None:
        status = None
        detail = None
        for kw in node.keywords:
            if kw.arg == "status_code":
                status = kw.value
            elif kw.arg == "detail":
                detail = kw.value
        if status is None and node.args:
            status = node.args[0]
            if len(node.args) > 1 and detail is None:
                detail = node.args[1]
        if not (isinstance(status, ast.Constant) and isinstance(status.value, int)):
            self._annotate(node.lineno, "exc.http_variable")
            return
        cls = _STATUS_EXC.get(status.value)
        if cls is None:
            self._annotate(node.lineno, "exc.http_variable", f"no class for status {status.value}")
            return
        self.needs.add(cls)
        detail_src = self._seg(detail) if detail is not None else ""
        self.buf.replace(node, f"{cls}({detail_src})")

    def _rewrite_add_middleware(self, node: ast.Call) -> None:
        if not node.args:
            return
        first = node.args[0]
        tail = self.imports.origin(first).split(".")[-1]
        if tail not in ("CORSMiddleware", "TrustedHostMiddleware"):
            self._annotate(node.lineno, "mw.custom")
            return
        kwargs = ", ".join(f"{kw.arg}={self._seg(kw.value)}" for kw in node.keywords)
        # add_middleware(CORSMiddleware, a=1) -> add_middleware(CORSMiddleware(a=1))
        last = node.keywords[-1] if node.keywords else first
        self.buf.replace_span(first, last, f"{self._seg(first)}({kwargs})")


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
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in ("list", "dict", "set") and not value.args):
        return value.func.id
    return None


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


def emit_module(source) -> str:
    """Port one module. `source` may be a Path/path-string (read) or source text.

    Returns the ported source (with a provenance header), preserving every function
    body verbatim. Raises `EmitError` if the result is not valid Python.
    """
    text = _read_source(source)
    tree = ast.parse(text)
    imports = _Imports().visit(tree)
    emitter = _Emitter(text, imports, module_pk_types(tree, imports))
    emitter.rewrite_imports(tree)
    emitter.collect_dep_targets(tree)
    if imports.has_star:
        emitter._annotate(1, "resolve.star_import")
    emitter.visit(tree)
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
    for src_path in _iter_py(root):
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
                emitted = emit_module(source)
                regenerating = True
            else:
                emitted = emit_module(source)
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
