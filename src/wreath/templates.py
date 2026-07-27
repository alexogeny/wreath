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

Rendering is the request-time hot path, so it is the native target: the C
engine in `_native/templates.c` executes the same tape and produces
byte-identical UTF-8. Parsing stays in Python.
"""

from __future__ import annotations

import os
from typing import Any

from ._fsguard import _HAVE_DIR_FD, ContainmentError, open_beneath, open_root
from ._native import _core
from ._pure.templates import (
    MAX_OUTPUT_BYTES,
    Markup,
    TemplateRenderError,
    TemplateSyntaxError,
    compile_tape,
    escape,
    render_tape,
)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


if _core is not None and hasattr(_core, "template_render"):
    # The native engine needs the Markup and error types to match escaping and
    # error behaviour exactly; it never imports this module itself.
    _core.template_configure(Markup, TemplateRenderError)
    _native_render = _core.template_render
else:
    _native_render = None


class Template:
    """A compiled template. Immutable and safe to render concurrently."""

    __slots__ = ("_tape", "name")

    def __init__(self, tape: tuple[tuple[Any, ...], ...], name: str = "<string>") -> None:
        self._tape = tape
        self.name = name

    @classmethod
    def from_string(cls, source: str, name: str = "<string>") -> Template:
        """Compile `source` directly. Includes are not resolvable here."""
        return cls(compile_tape(source, name), name)

    def render(self, /, max_output: int = MAX_OUTPUT_BYTES, **context: Any) -> str:
        """Render to a `str`. Keyword arguments form the template context."""
        return self.render_bytes(context, max_output=max_output).decode("utf-8")

    def render_bytes(
        self, context: dict[str, Any], max_output: int = MAX_OUTPUT_BYTES
    ) -> bytes:
        """Render to UTF-8 bytes from an explicit context mapping."""
        if _native_render is not None:
            return _native_render(self._tape, context, max_output)
        return render_tape(self._tape, context, max_output)


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
            except (ContainmentError, OSError):
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
