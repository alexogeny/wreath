"""The template language up to the tape: opcodes, compiler, escaping, errors.

**The tape is the seam, and only one side of it is C.** Turning source into a
tape happens once, when a `Template` is built at startup, so there is nothing
there for C to win and no C tokenizer exists -- `_core.template_compile` takes an
*already compiled* tape and lowers it to a native program. Rendering is the
request-time hot path, and `_native/templates.c` executes only that program;
there is no execution surface that accepts the boxed tape directly.

That split is also why the compile-time refusals below need no native
counterpart. A lookup path may not contain a segment starting with `_`, and the
native engine never has to check that, because the only tapes it executes are
ones this compiler produced.

`Markup`, `escape` and the two error types are shared for a second reason:
`wreath.templates` hands `Markup` and `TemplateRenderError` to
`_core.template_configure`, so the C engine escapes against the same class a
caller wraps a value in and raises the same class a caller catches. Two
definitions would be two answers to `type(value) is Markup`.
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


_IDENT = str.isidentifier


def _parse_path(expr: str, line: int) -> tuple[str, ...]:
    expr = expr.strip()
    if not expr:
        raise TemplateSyntaxError("empty expression", line=line)
    segments = expr.split(".")
    for segment in segments:
        if not _IDENT(segment):
            raise TemplateSyntaxError(f"invalid lookup path {expr!r}", line=line)
        # A lookup resolves by subscript and then by `getattr`, so a dotted path
        # can walk an object's internals: `{{ u.__init__.__globals__.API_KEY }}`
        # reads a module global straight into the output. There is no call
        # opcode, so the ceiling is disclosure rather than execution -- but
        # disclosure of exactly the credentials worth stealing.
        # It only bites a template whose *source* came from outside, which is
        # already a mistake. It is refused here anyway, because the cost is one
        # comparison at compile time and the alternative is that the mistake is
        # unrecoverable: template injection through a config field is one of the
        # two paths that put an agent inside Hugging Face's data pipeline in
        # July 2026. No legitimate template reads a private attribute.
        if segment.startswith("_"):
            raise TemplateSyntaxError(
                f"{segment!r} in {expr!r} is a private name; templates may not "
                "read attributes beginning with an underscore",
                line=line,
            )
    return tuple(segments)


Resolver = Callable[[str], str | None]


def _tokenize(source: str) -> list[tuple[str, str, int]]:
    """Split source into (kind, text, line) tokens: text/var/tag."""
    tokens: list[tuple[str, str, int]] = []
    i = 0
    line = 1
    length = len(source)
    while (start := source.find("{", i)) != -1:
        if not source.startswith(("{{", "{%"), start):
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
    if i < length:
        tokens.append(("text", source[i:], line))
    return tokens


def compile_tape(
    source: str,
    name: str = "<string>",
    resolver: Resolver | None = None,
    _stack: frozenset[str] = frozenset(),
) -> tuple[tuple[Any, ...], ...]:
    """Compile `source` into a flat opcode tape.

    `resolver` maps an include name to its source; `None` rejects includes.
    `_stack` guards against include cycles.
    """
    # Instructions are built as mutable lists so jump targets can be backpatched,
    # then frozen into tuples at the end.
    tape: list[Any] = []
    # Open control blocks awaiting backpatching.
    blocks: list[dict[str, Any]] = []

    for kind, text, line in _tokenize(source):
        if kind == "text":
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
                raise TemplateSyntaxError("include requires a TemplateDirectory", line=line)
            if include_name in _stack:
                raise TemplateSyntaxError(f"include cycle through {include_name!r}", line=line)
            included = resolver(include_name)
            if included is None:
                raise TemplateSyntaxError(
                    f"included template {include_name!r} not found", line=line
                )
            tape.extend(compile_tape(included, include_name, resolver, _stack | {include_name}))
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
