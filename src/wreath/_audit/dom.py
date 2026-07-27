"""A minimal, position-aware HTML tree over the standard-library `html.parser`.

Not a spec-complete DOM — just enough structure (parent/children, attributes, direct
text, and 1-based `(line, col)` from `getpos()`) for the curated audit rules to
locate findings. Zero third-party dependencies by design.
"""
from __future__ import annotations

from collections.abc import Iterator
from html.parser import HTMLParser

# HTML void elements never have children / end tags.
VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "line", "col", "_text")

    def __init__(self, tag: str, attrs, line: int, col: int, parent: Node | None) -> None:
        # attrs is a list of (name, value|None); a valueless attr keeps value "".
        self.attrs: dict[str, str] = {n: ("" if v is None else v) for n, v in attrs}
        self.tag = tag
        self.children: list[Node] = []
        self.parent = parent
        self.line = line
        self.col = col
        self._text = ""

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)

    def has_attr(self, name: str) -> bool:
        return name in self.attrs

    @property
    def text(self) -> str:
        """Concatenated descendant text (stripped)."""
        parts = [self._text]
        for child in self.children:
            parts.append(child.text)
        return " ".join(p for p in (s.strip() for s in parts) if p)

    def walk(self) -> Iterator[Node]:
        for child in self.children:
            yield child
            yield from child.walk()

    def find_all(self, *tags: str) -> list[Node]:
        want = set(tags)
        return [n for n in self.walk() if n.tag in want]

    def first(self, tag: str) -> Node | None:
        for n in self.walk():
            if n.tag == tag:
                return n
        return None

    @property
    def loc(self) -> str:
        return f"{self.line}:{self.col}"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", [], 0, 0, None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs) -> None:
        line, col = self.getpos()
        node = Node(tag, attrs, line, col, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs) -> None:
        line, col = self.getpos()
        node = Node(tag, attrs, line, col, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data) -> None:
        if data.strip():
            self._stack[-1]._text += data


def parse_html(html: str) -> Node:
    """Parse `html` into a `Node` tree rooted at `#document`."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root
