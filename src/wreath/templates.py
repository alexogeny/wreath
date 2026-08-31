"""Wreath's small, safe server-side template system.

Templates compile once (at startup) into a flat opcode tape and render escaped
HTML by default:

```python
templates = TemplateDirectory("templates")
table = templates.compile("table.html")
return HTMLResponse(table.render(rows=rows))
```
The language escapes `{{ value }}` unless the value is `Markup`, and
supports `{% if %}`/`{% else %}`/`{% endif %}`, `{% for x in xs %}`/
`{% endfor %}`, and compile-time `{% include "other.html" %}`. It evaluates
no arbitrary Python.

Rendering is the request-time hot path and runs in C: `_native/templates.c`
executes the tape. Parsing stays in Python -- the compiler, the escaping and the
two error types live in `src/wreath/_template_tape.py`, and the engine is handed
the error types so a caller catches one class either way.
"""

from __future__ import annotations

import os
from typing import Any

from ._fsguard import _HAVE_DIR_FD, ContainmentError, open_beneath, open_root
from ._native import _core

# The compiler and the vocabulary the C engine executes -- see
# `wreath._template_tape`.
from ._template_tape import (
    MAX_OUTPUT_BYTES,
    Markup,
    TemplateRenderError,
    TemplateSyntaxError,
    compile_tape,
    escape,
)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


# The native engine needs the Markup and error types to match escaping and
# error behaviour exactly; it never imports them itself. There is one of each --
# `_template_tape`'s -- so what it is handed here is what a caller wraps a value
# in and what a caller catches.
_core.template_configure(Markup, TemplateRenderError)
_native_compile = _core.template_compile
_native_render_compiled = _core.template_render_compiled
_native_render_compiled_tail = _core.template_render_compiled_tail


class Template:
    """A compiled template. Immutable and safe to render concurrently."""

    __slots__ = ("_program", "name")

    def __init__(self, tape: tuple[tuple[Any, ...], ...], name: str = "<string>") -> None:
        self._program = _native_compile(tape)
        self.name = name

    @classmethod
    def from_string(cls, source: str, name: str = "<string>") -> Template:
        """Compile `source` directly. Includes are not resolvable here."""
        return cls(compile_tape(source, name), name)

    def render(self, /, max_output: int = MAX_OUTPUT_BYTES, **context: Any) -> str:
        """Render to a `str`. Keyword arguments form the template context."""
        return self.render_bytes(context, max_output=max_output).decode("utf-8")

    def render_bytes(self, context: dict[str, Any], max_output: int = MAX_OUTPUT_BYTES) -> bytes:
        """Render to UTF-8 bytes from an explicit context mapping."""
        return _native_render_compiled(self._program, context, max_output)

    def _render_bytes_tail(
        self,
        context: dict[str, Any],
        tail: bytes,
        max_output: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        return _native_render_compiled_tail(self._program, context, tail, max_output)


class TemplateDirectory:
    """Compiles templates from a filesystem directory, resolving includes.

    Sources are read at compile time; `compile` returns a ready
    `Template`. Nothing touches disk on the render path.
    """

    __slots__ = ("_encoding", "_root_fd", "root")

    def __init__(self, root: str | os.PathLike[str], *, encoding: str = "utf-8") -> None:
        self.root = os.fspath(root)
        self._encoding = encoding
        # A trusted root descriptor so both direct compiles and includes are read
        # beneath the root without following symlinks out of it.
        self._root_fd = open_root(self.root) if _HAVE_DIR_FD else -1

    def _read(self, name: str) -> str | None:
        # Contain lookups within the root; a traversing or symlinked name (direct
        # or via an include) resolves to None, so it is never read or compiled.
        if _HAVE_DIR_FD:
            try:
                fd, _info = open_beneath(self._root_fd, name)
            except ContainmentError, OSError:
                return None
            try:
                return _read_all(fd).decode(self._encoding)
            finally:
                os.close(fd)
        # No openat: resolve symlinks and contain against the real root path.
        target = os.path.realpath(os.path.join(self.root, name))
        root = os.path.realpath(self.root)
        if target != root and not target.startswith(root + os.sep):
            return None
        try:
            with open(target, encoding=self._encoding) as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def compile(self, name: str) -> Template:
        source = self._read(name)
        if source is None:
            raise TemplateSyntaxError(f"template {name!r} not found")
        tape = compile_tape(source, name, self._read)
        return Template(tape, name)


__all__ = [
    "Markup",
    "Template",
    "TemplateDirectory",
    "TemplateRenderError",
    "TemplateSyntaxError",
    "escape",
]
