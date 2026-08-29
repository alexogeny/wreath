"""The import block: which framework imports are dropped, renamed or kept, and
the wreath ones injected in their place.

Runs *after* the walk rather than before it, because a name is dropped only
once nothing is left referring to it."""

from __future__ import annotations

import ast

from .buffer import _span_end
from .state import _EmitterState
from .targets import (
    _CACHE_RENAME,
    _FASTAPI_TO_WREATH,
    _RESPONSE_MODULES,
    _RESPONSE_RENAME,
    _TESTCLIENT_MODULES,
    _grouped_imports,
)


class _ImportRewrite(_EmitterState):
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
        # The django names still mentioned once the walk is finished. A name is
        # live where a reference to it survived un-rewritten and outside every
        # replaced span -- the same question `live_httpx` above asks, and the
        # only honest one: `_retain` is an allow-list of specific origins, not a
        # record of what the module still uses.
        live_django = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and self.imports.origin(node).startswith("django.")
            and id(node) not in self._rewritten
            and not self._inside_replaced(node)
        }
        live_optional_models = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and self.imports.origin(node).split(".")[0] in {"pydantic_settings", "pydantic_partial"}
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
                and any(alias.name == "HTTPException" for alias in node.names)
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
                    and (alias.asname or alias.name) not in live_optional_models
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
                    and (alias.asname or alias.name) not in live_optional_models
                }
                self.buf.replace(node, self._keep_leftover(node, gone, node.module))
            elif (
                isinstance(node, ast.ImportFrom)
                and (module := node.module) is not None
                and module.startswith(("fastapi.middleware", "starlette.middleware"))
                and any(
                    alias.name in {"CORSMiddleware", "TrustedHostMiddleware"}
                    for alias in node.names
                )
            ):
                imported = {alias.name for alias in node.names}
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
                keep = [alias for alias in node.names if (alias.asname or alias.name) in live_httpx]
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
                    "import " + ", ".join(self._alias_str(alias) for alias in keep) if keep else "",
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
                    self.buf.replace(node, self._keep_leftover(node, gone, node.module or ""))
            elif (
                isinstance(node, ast.Import)
                and all((alias.asname or alias.name) not in self._retain for alias in node.names)
                and all(alias.name in ("arrow",) for alias in node.names)
            ):
                self.buf.replace(node, "")
            elif (
                isinstance(node, ast.Import)
                and all((alias.asname or alias.name) not in self._retain for alias in node.names)
                and all(alias.name == "strawberry" for alias in node.names)
            ):
                self.buf.replace(node, "")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "fastapi.encoders"
                and "jsonable_encoder" not in self._retain
            ):
                self.buf.replace(node, self._keep_leftover(node, {"jsonable_encoder"}, node.module))
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("django."):
                # A ported module that still imports django does not start
                # without django installed, which is the one thing the port was
                # for. Dropped per name and only where every mention of that name
                # was rewritten: an unmapped field, an `Http404` this pass cannot
                # move, or a `transaction.atomic()` left for a session all keep
                # their import, because the module still refers to them.
                gone = {
                    alias.name
                    for alias in node.names
                    if (alias.asname or alias.name) not in live_django
                }
                if gone:
                    self.buf.replace(node, self._keep_leftover(node, gone, node.module or ""))
        self._last_import_line = last_import_line

    def _drop_replaced(self, node: ast.ImportFrom, names: set[str]) -> None:
        """Drop the imported names whose every call site was rewritten."""
        gone = {
            alias.name
            for alias in node.names
            if alias.name in names and (alias.asname or alias.name) not in self._retain
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
        moved = [alias for alias in node.names if alias.name in rename and alias.asname is None]
        if not moved:
            return
        for alias in moved:
            self.needs.add(rename[alias.name])
        self.buf.replace(
            node,
            self._keep_leftover(node, {alias.name for alias in moved}, node.module or ""),
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
                f"from {node.module} import " + ", ".join(self._alias_str(alias) for alias in keep)
            )
        self.buf.replace(node, "\n".join(parts) if parts else "")

    def _rewrite_from_pydantic(self, node: ast.ImportFrom) -> None:
        # A name is only dropped once every use of it is gone. `BaseModel` on a
        # class with a second base, or `Field` on a field whose marker needed a
        # human, is still written in the file — deleting its import turns a
        # reviewable port into a module that will not import at all.
        dropped = ({"BaseModel", "Field"} - self._retain) | self._removed_pydantic_imports
        keep = [alias for alias in node.names if alias.name not in dropped]
        if any(alias.name in dropped for alias in node.names):
            self.needs_dataclass = True
        if keep:
            self.buf.replace(
                node,
                "from pydantic import " + ", ".join(self._alias_str(alias) for alias in keep),
            )
        else:
            self.buf.replace(node, "")

    def _keep_leftover(self, node: ast.ImportFrom, drop: set[str], module: str) -> str:
        keep = [alias for alias in node.names if alias.name not in drop]
        if not keep:
            return ""
        return f"from {module} import " + ", ".join(self._alias_str(alias) for alias in keep)

    @staticmethod
    def _alias_str(alias: ast.alias) -> str:
        return f"{alias.name} as {alias.asname}" if alias.asname else alias.name

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
        if self.needs_datetime and "datetime" not in self.imports.names:
            lines.append("import datetime")
        if self.needs_decimal and "decimal" not in self.imports.names:
            lines.append("import decimal")
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
