"""Byte-accurate edits over one source file.

The emitter never unparses: it replaces spans in the original text, so
comments, formatting and every untouched body survive byte for byte."""

from __future__ import annotations

import ast

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
