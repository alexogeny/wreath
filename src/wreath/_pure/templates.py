"""Pure-Python twin for Wreath's small, safe server-side template language.

The language is deliberately tiny and evaluates no arbitrary Python:

* ``{{ path }}`` — escaped interpolation of a dotted lookup path.
* ``{% if path %}`` / ``{% else %}`` / ``{% endif %}``.
* ``{% for name in path %}`` / ``{% endfor %}``.
* ``{% include "other.html" %}`` — spliced in at compile time.

Lookup resolves each dotted segment by subscript first (mapping keys) then by
attribute. Values are HTML-escaped unless wrapped in :class:`Markup`; plain
strings are always untrusted. Parsing and jump resolution happen once, at
compile time, producing a flat opcode tape that both this reference VM and the
native engine execute identically.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Opcodes on the shared tape. The native VM in _native/templates.c uses the
# same integers and instruction layout.
OP_TEXT = 0  # (OP_TEXT, fragment: bytes)
OP_VAR = 1  # (OP_VAR, path: tuple[str, ...], lineno: int)
OP_FOR = 2  # (OP_FOR, var: str, path: tuple[str, ...], end: int, lineno: int)
OP_ENDFOR = 3  # (OP_ENDFOR,)
OP_IF = 4  # (OP_IF, path: tuple[str, ...], else_target: int, lineno: int)
OP_JUMP = 5  # (OP_JUMP, target: int)
OP_ENDIF = 6  # (OP_ENDIF,)

# Bounds enforced identically by both engines.
MAX_LOOP_DEPTH = 64
MAX_OUTPUT_BYTES = 16 * 1024 * 1024

_MISSING: Any = object()


class TemplateSyntaxError(Exception):
    """Raised at compile time for malformed template source."""

    def __init__(self, message: str, *, line: int = 0) -> None:
        self.line = line
        super().__init__(f"{message} (line {line})" if line else message)


class TemplateRenderError(Exception):
    """Raised at render time (undefined name, bad lookup, overflow)."""

    def __init__(self, message: str, *, line: int = 0) -> None:
        self.line = line
        super().__init__(f"{message} (line {line})" if line else message)


class Markup(str):
    """A string already known to be safe HTML; rendered without escaping."""

    __slots__ = ()

    def __html__(self) -> str:
        return str(self)


def escape(value: str) -> str:
    """Escape the five HTML-significant characters."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&#34;")
        .replace("'", "&#39;")
    )


def _render_value(value: Any) -> bytes:
    if type(value) is Markup:
        return str(value).encode("utf-8")
    text = value if type(value) is str else str(value)
    return escape(text).encode("utf-8")


# --- parsing / compilation --------------------------------------------------

_IDENT = str.isidentifier


def _parse_path(expr: str, line: int) -> tuple[str, ...]:
    expr = expr.strip()
    if not expr:
        raise TemplateSyntaxError("empty expression", line=line)
    segments = expr.split(".")
    for segment in segments:
        if not _IDENT(segment):
            raise TemplateSyntaxError(f"invalid lookup path {expr!r}", line=line)
    return tuple(segments)


Resolver = Callable[[str], str | None]


def _tokenize(source: str) -> list[tuple[str, str, int]]:
    """Split source into (kind, text, line) tokens: text/var/tag."""
    tokens: list[tuple[str, str, int]] = []
    i = 0
    line = 1
    length = len(source)
    while i < length:
        start = source.find("{", i)
        if start == -1 or start + 1 >= length or source[start + 1] not in "{%":
            # No more tags (or a lone brace): the rest is literal text.
            if start == -1 or start + 1 >= length:
                tokens.append(("text", source[i:], line))
                break
            # A "{" not opening a tag; keep scanning past it.
            continue_at = start + 1
            tokens.append(("text", source[i:continue_at], line))
            line += source.count("\n", i, continue_at)
            i = continue_at
            continue
        if start > i:
            tokens.append(("text", source[i:start], line))
            line += source.count("\n", i, start)
        marker = source[start + 1]
        close = "}}" if marker == "{" else "%}"
        end = source.find(close, start + 2)
        if end == -1:
            raise TemplateSyntaxError("unterminated tag", line=line)
        inner = source[start + 2 : end]
        kind = "var" if marker == "{" else "tag"
        tokens.append((kind, inner, line))
        line += source.count("\n", start, end + 2)
        i = end + 2
    return tokens


def compile_tape(
    source: str,
    name: str = "<string>",
    resolver: Resolver | None = None,
    _stack: frozenset[str] = frozenset(),
) -> tuple[tuple[Any, ...], ...]:
    """Compile ``source`` into a flat opcode tape.

    ``resolver`` maps an include name to its source; ``None`` rejects includes.
    ``_stack`` guards against include cycles.
    """
    # Instructions are built as mutable lists so jump targets can be backpatched,
    # then frozen into tuples at the end.
    tape: list[Any] = []
    # Open control blocks awaiting backpatching.
    blocks: list[dict[str, Any]] = []

    for kind, text, line in _tokenize(source):
        if kind == "text":
            if text:
                tape.append((OP_TEXT, text.encode("utf-8")))
            continue
        if kind == "var":
            tape.append((OP_VAR, _parse_path(text, line), line))
            continue
        # A {% ... %} tag.
        stripped = text.strip()
        if not stripped:
            raise TemplateSyntaxError("empty tag", line=line)
        keyword, _, rest = stripped.partition(" ")
        rest = rest.strip()
        if keyword == "if":
            tape.append([OP_IF, _parse_path(rest, line), None, line])
            blocks.append({"type": "if", "index": len(tape) - 1, "jumps": [], "else": False})
        elif keyword == "else":
            if not blocks or blocks[-1]["type"] != "if":
                raise TemplateSyntaxError("'else' outside of 'if'", line=line)
            block = blocks[-1]
            if block["else"]:
                raise TemplateSyntaxError("duplicate 'else'", line=line)
            tape.append([OP_JUMP, None])
            block["jumps"].append(len(tape) - 1)
            tape[block["index"]][2] = len(tape)  # false path enters the else body
            block["else"] = True
        elif keyword == "endif":
            if not blocks or blocks[-1]["type"] != "if":
                raise TemplateSyntaxError("'endif' without 'if'", line=line)
            block = blocks.pop()
            tape.append((OP_ENDIF,))
            endif_index = len(tape) - 1
            if not block["else"]:
                tape[block["index"]][2] = endif_index
            for jump in block["jumps"]:
                tape[jump][1] = endif_index
        elif keyword == "for":
            loop_var, _, iterable = rest.partition(" in ")
            loop_var = loop_var.strip()
            if not _IDENT(loop_var):
                raise TemplateSyntaxError(f"invalid loop variable {loop_var!r}", line=line)
            tape.append([OP_FOR, loop_var, _parse_path(iterable, line), None, line])
            blocks.append({"type": "for", "index": len(tape) - 1})
        elif keyword == "endfor":
            if not blocks or blocks[-1]["type"] != "for":
                raise TemplateSyntaxError("'endfor' without 'for'", line=line)
            block = blocks.pop()
            tape.append((OP_ENDFOR,))
            tape[block["index"]][3] = len(tape)  # empty iterable skips past endfor
        elif keyword == "include":
            include_name = _parse_include_target(rest, line)
            if resolver is None:
                raise TemplateSyntaxError(
                    "include requires a TemplateDirectory", line=line
                )
            if include_name in _stack:
                raise TemplateSyntaxError(
                    f"include cycle through {include_name!r}", line=line
                )
            included = resolver(include_name)
            if included is None:
                raise TemplateSyntaxError(
                    f"included template {include_name!r} not found", line=line
                )
            tape.extend(
                compile_tape(
                    included, include_name, resolver, _stack | {include_name}
                )
            )
        else:
            raise TemplateSyntaxError(f"unknown tag {keyword!r}", line=line)

    if blocks:
        raise TemplateSyntaxError(f"unclosed {blocks[-1]['type']!r} block")
    # Freeze the mutable instructions into tuples for a stable, shareable tape.
    return tuple(tuple(instruction) for instruction in tape)


def _parse_include_target(rest: str, line: int) -> str:
    rest = rest.strip()
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
        return rest[1:-1]
    raise TemplateSyntaxError("include target must be a quoted string", line=line)


# --- reference VM -----------------------------------------------------------


def _lookup(context: dict[str, Any], path: tuple[str, ...], line: int) -> Any:
    current: Any = context
    first = True
    for segment in path:
        found: Any = _MISSING
        try:
            found = current[segment]
        except (KeyError, TypeError, IndexError):
            found = _MISSING
        if found is _MISSING:
            try:
                found = getattr(current, segment)
            except AttributeError:
                if first:
                    raise TemplateRenderError(
                        f"{segment!r} is undefined", line=line
                    ) from None
                raise TemplateRenderError(
                    f"cannot resolve {'.'.join(path)!r} at {segment!r}", line=line
                ) from None
        current = found
        first = False
    return current


def render_tape(
    tape: tuple[tuple[Any, ...], ...],
    context: dict[str, Any],
    max_output: int = MAX_OUTPUT_BYTES,
) -> bytes:
    out: list[bytes] = []
    size = 0
    # Each loop frame: [iterator, var, body_start, had_old, old_value].
    loops: list[list[Any]] = []
    ip = 0
    n = len(tape)
    local = dict(context)
    while ip < n:
        instruction = tape[ip]
        op = instruction[0]
        if op == OP_TEXT:
            fragment = instruction[1]
            size += len(fragment)
            if size > max_output:
                raise TemplateRenderError("template output too large")
            out.append(fragment)
            ip += 1
        elif op == OP_VAR:
            value = _lookup(local, instruction[1], instruction[2])
            fragment = _render_value(value)
            size += len(fragment)
            if size > max_output:
                raise TemplateRenderError("template output too large")
            out.append(fragment)
            ip += 1
        elif op == OP_IF:
            value = _lookup(local, instruction[1], instruction[3])
            ip = ip + 1 if value else instruction[2]
        elif op == OP_JUMP:
            ip = instruction[1]
        elif op == OP_ENDIF:
            ip += 1
        elif op == OP_FOR:
            _, var, path, end, line = instruction
            iterable = _lookup(local, path, line)
            try:
                iterator = iter(iterable)
            except TypeError:
                raise TemplateRenderError(
                    f"{'.'.join(path)!r} is not iterable", line=line
                ) from None
            try:
                item = next(iterator)
            except StopIteration:
                ip = end
                continue
            if len(loops) >= MAX_LOOP_DEPTH:
                raise TemplateRenderError("template loop nesting too deep")
            had = var in local
            loops.append([iterator, var, ip + 1, had, local.get(var)])
            local[var] = item
            ip += 1
        elif op == OP_ENDFOR:
            frame = loops[-1]
            iterator, var, body_start, had, old = frame
            try:
                item = next(iterator)
            except StopIteration:
                loops.pop()
                if had:
                    local[var] = old
                else:
                    local.pop(var, None)
                ip += 1
            else:
                local[var] = item
                ip = body_start
        else:  # pragma: no cover - compiler never emits other opcodes
            raise TemplateRenderError(f"invalid opcode {op}")
    return b"".join(out)
