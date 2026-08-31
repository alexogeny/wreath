"""A CommonMark *subset* renderer — the first-slice parser.

Covers the block and inline constructs wreath's own docs actually use: ATX
headings (with GitHub slugs + a table of contents), fenced code blocks,
unordered/ordered lists, blockquotes, thematic breaks, paragraphs, and inline
code / strong / emphasis / links / autolinks. The native `_docs` extension
compiles source into the versioned WDT1 block tape; this module is its semantic
render half.

Security is the load-bearing property: every text and attribute span is HTML-
escaped, and link targets are scheme-checked (no `javascript:` URLs). Full
CommonMark (nested lists, reference links, the emphasis delimiter stack, GFM
tables) remain follow-on work at the same tape boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from wreath._native import _docs as _native_docs

from .highlight import highlight

__all__ = ["Rendered", "TocEntry", "plain", "render", "slugify"]

_SAFE_SCHEME = re.compile(r"^(?:https?:|mailto:|#|/|\.{0,2}/|[^:]*$)", re.IGNORECASE)
#: `key="value"` attributes trailing a fence's language, e.g.
#: ``` ``python title="app.py" hl_lines="3 4" ````.
_FENCE_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
#: Optional trailing `{#custom-id}` on a heading (attr_list style) — lets a page
#: pin an explicit anchor, e.g. mkdocstrings' dotted `wreath.mod.Class` ids so
#: cross-references written against them keep resolving.
_FENCE = re.compile(r"^(```+|~~~+)\s*([^\s`]*)\s*(.*)$")
_THEMATIC = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_UL_ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_ITEM = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_SLUG_STRIP = re.compile(r"[^\w\- ]+")
_ADMONITION = re.compile(r'^(!!!|\?\?\?\+?|\?\?\?)\s+([\w-]+)(?:\s+"([^"]*)")?\s*$')
_CONTENT_TAB = re.compile(r'^=== +"([^"]*)"\s*$')
_TASK = re.compile(r"\[([ xX])\]\s+(.*)$")

_WDT_FENCE = 1
_WDT_HEADING = 2
_WDT_THEMATIC = 4
_WDT_ADMONITION = 8
_WDT_TAB = 16
_WDT_QUOTE = 32
_WDT_LIST = 64


#: Inline markup, for the places a heading is quoted as *text* rather than
#: rendered: the table of contents, the `<title>`, and the search index. A rail
#: reading "`wreath.binding`" with the backticks showing is the markdown source
#: leaking through the frame -- and in the search index it also cost every API
#: heading its exact-match score, because `"`query`"` is not `"query"`.
_MD_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"(\*\*|\*|__|_)(?=\S)(.+?)(?<=\S)\1")


def plain(text: str) -> str:
    """A heading's visible words, with the inline markers taken off."""
    text = _MD_LINK_TEXT.sub(r"\1", text)
    text = _MD_EMPHASIS.sub(r"\2", text)
    return text.replace("`", "")


@dataclass(frozen=True, slots=True)
class TocEntry:
    level: int
    slug: str
    text: str


@dataclass(frozen=True, slots=True)
class Rendered:
    html: str
    toc: tuple[TocEntry, ...]
    title: str | None  # first H1, for the page <title>


def slugify(text: str) -> str:
    """GitHub-style anchor slug: lowercase, spaces to hyphens, punctuation dropped."""
    text = _SLUG_STRIP.sub("", text.strip().lower())
    return re.sub(r"[\s]+", "-", text)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _safe_href(url: str) -> str:
    stripped = url.strip()
    if not _SAFE_SCHEME.match(stripped):
        return "#"  # reject javascript:, data:, etc.
    return _esc(stripped)


def _inline(text: str) -> str:
    code_marked = "`" in text
    link_marked = "[" in text
    autolink_marked = "<" in text
    strike_marked = "~" in text
    star_marked = "*" in text
    underscore_marked = "_" in text
    if not (
        code_marked
        or link_marked
        or autolink_marked
        or strike_marked
        or star_marked
        or underscore_marked
    ):
        return _esc(text)

    # 1. Pull out code spans first so their contents escape nothing further.
    spans: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        spans.append(f"<code>{_esc(match.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    if code_marked:
        text = re.sub(r"`([^`]+)`", _stash_code, text)

    # 2. Escape everything else (markers *_[]() survive escaping).
    text = _esc(text)

    # 3. Images (before links — an image is a `!`-prefixed link), then links.
    def _image(match: re.Match[str]) -> str:
        return f'<img src="{_safe_href(match.group(2))}" alt="{match.group(1)}" loading="lazy">'

    def _link(match: re.Match[str]) -> str:
        return f'<a href="{_safe_href(match.group(2))}">{match.group(1)}</a>'

    if link_marked:
        text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _image, text)
        text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, text)
    if autolink_marked:
        text = re.sub(
            r"&lt;(https?://[^\s>]+)&gt;",
            lambda m: f'<a href="{_safe_href(m.group(1))}">{m.group(1)}</a>',
            text,
        )

    # 4. Emphasis: strikethrough, then strong before emphasis (** __ and * _).
    if strike_marked:
        text = re.sub(r"~~(\S.*?\S|\S)~~", r"<del>\1</del>", text)
    if star_marked:
        text = re.sub(r"\*\*(\S.*?\S|\S)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*(\S.*?\S|\S)\*", r"<em>\1</em>", text)
    if underscore_marked:
        text = re.sub(r"__(\S.*?\S|\S)__", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w])_(\S.*?\S|\S)_(?![\w])", r"<em>\1</em>", text)

    # 5. Restore code spans.
    if code_marked and spans:
        return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text


class _Renderer:
    def __init__(self, counter: list[int] | None = None) -> None:
        self.out: list[str] = []
        self.toc: list[TocEntry] = []
        self.title: str | None = None
        self._slugs: dict[str, int] = {}
        #: Shared with nested renderers so a tab group inside an admonition
        #: still gets radio ids no other group on the page uses.
        self._counter = counter if counter is not None else [0]

    def _unique_slug(self, text: str) -> str:
        return self._claim_slug(slugify(text) or "section")

    def _claim_slug(self, base: str) -> str:
        seen = self._slugs.get(base, 0)
        self._slugs[base] = seen + 1
        return base if seen == 0 else f"{base}-{seen}"

    def render(self, source: str) -> Rendered:
        version, lines, kinds = _native_docs.block_tape(source)
        if version != 1:
            raise RuntimeError(f"unsupported WDT block tape version {version}; expected 1")
        stripped = _strip_frontmatter(lines)
        removed = len(lines) - len(stripped)
        lines = stripped
        kinds = kinds[removed:]
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            line_kind = kinds[i]
            if not line_kind:
                i += 1
                continue
            fence = _FENCE.match(line) if line_kind & _WDT_FENCE else None
            if fence:
                i = self._code_block(lines, i, fence)
            elif line_kind & _WDT_HEADING and (heading := _parse_heading(line)) is not None:
                self._heading(*heading)
                i += 1
            elif line_kind & _WDT_THEMATIC and _THEMATIC.match(line):
                self.out.append("<hr />")
                i += 1
            elif (
                line_kind & _WDT_ADMONITION and (admonition := _ADMONITION.match(line)) is not None
            ):
                i = self._admonition(lines, i, admonition)
            elif line_kind & _WDT_TAB and _CONTENT_TAB.match(line):
                i = self._tabs(lines, i)
            elif _is_table(lines, i):
                i = self._table(lines, i)
            elif line_kind & _WDT_QUOTE:
                i = self._blockquote(lines, i)
            elif line_kind & _WDT_LIST and (_UL_ITEM.match(line) or _OL_ITEM.match(line)):
                i = self._list(lines, i)
            else:
                i = self._paragraph(lines, kinds, i)
        return Rendered("\n".join(self.out), tuple(self.toc), self.title)

    def _heading(self, level: int, text: str) -> None:
        explicit = _parse_heading_id(text)
        if explicit is not None:
            text, identifier = explicit
            slug = self._claim_slug(identifier)
        else:
            slug = self._unique_slug(text)
        if level == 1 and self.title is None:
            self.title = plain(text)
        self.toc.append(TocEntry(level, slug, plain(text)))
        self.out.append(
            f'<h{level} id="{slug}">{_inline(text)}'
            f'<a class="anchor" href="#{slug}" aria-label="Permalink">#</a></h{level}>'
        )

    def _code_block(self, lines: list[str], i: int, fence: re.Match[str]) -> int:
        marker, info = fence.group(1), fence.group(2)
        attrs = dict(_FENCE_ATTR.findall(fence.group(3) or ""))
        body: list[str] = []
        i += 1
        while i < len(lines) and not lines[i].startswith(marker[0] * len(marker)):
            body.append(lines[i])
            i += 1
        i += 1  # consume the closing fence (or EOF)
        raw = "\n".join(body)
        lang_class = f' class="language-{_esc(info)}"' if info else ""
        code = highlight(raw, info) if info else _esc(raw)
        code = _mark_lines(code, _line_numbers(attrs.get("hl_lines", "")))
        # The chrome strip earns its place only when the fence names the file it
        # came from. A language chip over every block on a 129-page corpus is
        # noise; a filename is the thing a reader needs to act on the snippet.
        head = ""
        if title := attrs.get("title", ""):
            lang = f'<span class="code-lang">{_esc(info)}</span>' if info else ""
            head = (
                f'<div class="code-head"><span class="code-title">{_esc(title)}</span>{lang}</div>'
            )
        self.out.append(f'<div class="code">{head}<pre><code{lang_class}>{code}</code></pre></div>')
        return i

    def _blockquote(self, lines: list[str], i: int) -> int:
        inner: list[str] = []
        while i < len(lines) and lines[i].startswith(">"):
            inner.append(lines[i][1:].lstrip(" "))
            i += 1
        nested = _Renderer(self._counter).render("\n".join(inner))
        self.out.append(f"<blockquote>\n{nested.html}\n</blockquote>")
        return i

    def _list(self, lines: list[str], i: int) -> int:
        html, i = self._parse_list(lines, i)
        self.out.append(html)
        return i

    def _parse_list(self, lines: list[str], i: int) -> tuple[str, int]:
        base = _indent(lines[i])
        ordered = bool(_OL_ITEM.match(lines[i]))
        tag = "ol" if ordered else "ul"
        # Each item holds its *raw* markdown, a prefix of already-built HTML (the
        # task checkbox), and any nested lists. Inline rendering is deferred to
        # the end because an item's text can continue onto wrapped lines: running
        # `_inline` per line appended raw markdown to finished HTML, so a wrapped
        # bullet showed literal backticks and `[text](link.md)` to the reader.
        items: list[tuple[list[str], str, list[str]]] = []
        while i < len(lines):
            line = lines[i]
            m_ul, m_ol = _UL_ITEM.match(line), _OL_ITEM.match(line)
            if not (m_ul or m_ol):
                if line.strip() == "":
                    nxt = lines[i + 1] if i + 1 < len(lines) else ""
                    if _UL_ITEM.match(nxt) or _OL_ITEM.match(nxt):
                        i += 1
                        continue
                    break
                if line.startswith(" ") and items:  # continuation of the item text
                    items[-1][0].append(line.strip())
                    i += 1
                    continue
                break
            indent = _indent(line)
            if indent < base:
                break
            if indent > base:  # a sub-list under the last item
                nested, i = self._parse_list(lines, i)
                if items:
                    items[-1][2].append(nested)
                continue
            if m_ol is not None:
                content = m_ol.group(3)
            else:
                if m_ul is None:  # pragma: no cover - list dispatch matched one form
                    raise RuntimeError("list renderer received a non-list line")
                content = m_ul.group(2)
            task = _TASK.match(content)
            if task is not None:
                checked = " checked" if task.group(1).lower() == "x" else ""
                items.append(([task.group(2)], f'<input type="checkbox" disabled{checked}> ', []))
            else:
                items.append(([content], "", []))
            i += 1
        body = "".join(
            f"<li>{prefix}{_inline(' '.join(text))}{''.join(nested)}</li>"
            for text, prefix, nested in items
        )
        return f"<{tag}>{body}</{tag}>", i

    def _admonition(
        self,
        lines: list[str],
        i: int,
        match: re.Match[str],
    ) -> int:
        marker, kind = match.group(1), match.group(2).lower()
        title = match.group(3) if match.group(3) is not None else kind.capitalize()
        i += 1
        body: list[str] = []
        while i < len(lines) and (lines[i].startswith(("    ", "\t")) or not lines[i].strip()):
            body.append(lines[i][4:] if lines[i].startswith("    ") else lines[i].lstrip("\t"))
            i += 1
        inner = _Renderer(self._counter).render("\n".join(body).strip()).html
        if marker.startswith("?"):
            # `???` is collapsed, `???+` starts open — mkdocs' spelling, and a
            # <details> so it works with no script and prints expanded.
            open_attr = " open" if marker.endswith("+") else ""
            self.out.append(
                f'<details class="admonition {_esc(kind)}"{open_attr}>'
                f'<summary class="admonition-title">{_inline(title)}</summary>\n'
                f"{inner}\n</details>"
            )
            return i
        title_html = f'<p class="admonition-title">{_inline(title)}</p>' if title else ""
        self.out.append(f'<div class="admonition {_esc(kind)}">{title_html}\n{inner}\n</div>')
        return i

    def _tabs(self, lines: list[str], i: int) -> int:
        tabs: list[tuple[str, str]] = []
        while i < len(lines):
            match = _CONTENT_TAB.match(lines[i])
            if match is None:
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if lines[i].strip() == "" and _CONTENT_TAB.match(nxt):
                    i += 1
                    continue
                break
            title = match.group(1)
            i += 1
            body: list[str] = []
            while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                body.append(lines[i][4:] if lines[i].startswith("    ") else "")
                i += 1
            tabs.append((title, _Renderer(self._counter).render("\n".join(body).strip()).html))
        # A radio group, not buttons and script. The JS version hid every panel
        # but the first with `display:none`, so with JavaScript off the other
        # tabs' content was simply unreachable — and the labels were buttons
        # with no tab semantics for a screen reader. Radios are keyboard-native
        # (arrow keys move within the group, one tab stop for the whole set) and
        # need no runtime at all.
        self._counter[0] += 1
        group = f"tabs-{self._counter[0]}"
        inputs = "".join(
            f'<input type="radio" name="{group}" id="{group}-{k}"{" checked" if k == 0 else ""}>'
            for k in range(len(tabs))
        )
        labels = "".join(
            f'<label class="tab-label" for="{group}-{k}">{_inline(t)}</label>'
            for k, (t, _) in enumerate(tabs)
        )
        panels = "".join(f'<div class="tab-panel">{h}</div>' for _, h in tabs)
        self.out.append(
            f'<div class="tabbed">{inputs}<div class="tab-labels">{labels}</div>{panels}</div>'
        )
        return i

    def _table(self, lines: list[str], i: int) -> int:
        aligns = _table_aligns(lines[i + 1])
        header = _table_row(lines[i])
        i += 2
        rows: list[list[str]] = []
        while i < len(lines) and "|" in lines[i] and lines[i].strip():
            rows.append(_table_row(lines[i]))
            i += 1
        # scope="col" is what tells a screen reader which header belongs to a
        # cell; without it a table is read as an undifferentiated grid (WCAG
        # 1.3.1). Every table here is a column-headed data table.
        head = "".join(
            f'<th scope="col"{_align(aligns, c)}>{_inline(cell)}</th>'
            for c, cell in enumerate(header)
        )
        body = "".join(
            "<tr>"
            + "".join(f"<td{_align(aligns, c)}>{_inline(cell)}</td>" for c, cell in enumerate(row))
            + "</tr>"
            for row in rows
        )
        # Wrapped so a table wider than the column scrolls itself. Without it a
        # wide table scrolls the whole page sideways on a phone, which is a
        # layout bug the reader has to fight on every other page too.
        self.out.append(
            f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )
        return i

    def _paragraph(self, lines: list[str], kinds: bytes, i: int) -> int:
        buffer: list[str] = []
        while (
            i < len(lines)
            and lines[i].strip()
            and not _is_block_start(lines[i], kinds[i])
            and not _is_table(lines, i)
        ):
            buffer.append(lines[i].strip())
            i += 1
        self.out.append(f"<p>{_inline(' '.join(buffer))}</p>")
        return i


#: `highlight()` emits a flat, never-nested run of token spans, which is what
#: makes splitting its output by line safe: at most one span is open at a time,
#: so a line break only has to close it and reopen the same tag.
_SPAN = re.compile(r"(<span[^>]*>|</span>)")


def _line_numbers(spec: str) -> frozenset[int]:
    """Parse an `hl_lines` spec — `"2 5-7"` — into a set of line numbers."""
    out: set[int] = set()
    for part in spec.split():
        start, sep, end = part.partition("-")
        try:
            if sep:
                out.update(range(int(start), int(end) + 1))
            else:
                out.add(int(start))
        except ValueError:
            continue  # a malformed range highlights nothing
    return frozenset(out)


def _mark_lines(html: str, wanted: frozenset[int]) -> str:
    """Wrap the 1-indexed lines in `wanted` so they can be shaded."""
    if not wanted:
        return html
    lines: list[str] = [""]
    open_tag = ""
    for piece in _SPAN.split(html):
        if not piece:
            continue
        if piece.startswith("</span"):
            open_tag = ""
            lines[-1] += piece
        elif piece.startswith("<span"):
            open_tag = piece
            lines[-1] += piece
        else:
            head, *rest = piece.split("\n")
            lines[-1] += head
            for tail in rest:
                if open_tag:
                    lines[-1] += "</span>"
                lines.append(open_tag + tail)
    return "\n".join(
        f'<span class="hl">{line}</span>' if number in wanted else line
        for number, line in enumerate(lines, 1)
    )


def _is_block_start(line: str, kind: int) -> bool:
    if not kind:
        return False
    return bool(
        (kind & _WDT_HEADING and _parse_heading(line))
        or (kind & _WDT_FENCE and _FENCE.match(line))
        or (kind & _WDT_THEMATIC and _THEMATIC.match(line))
        or (kind & _WDT_ADMONITION and _ADMONITION.match(line))
        or (kind & _WDT_TAB and _CONTENT_TAB.match(line))
        or kind & _WDT_QUOTE
        or (kind & _WDT_LIST and (_UL_ITEM.match(line) or _OL_ITEM.match(line)))
    )


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_frontmatter(lines: list[str]) -> list[str]:
    """Drop a leading `---` … `---` YAML front-matter block, if present."""
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                return lines[j + 1 :]
    return lines


def _is_table(lines: list[str], i: int) -> bool:
    return (
        i + 1 < len(lines)
        and "|" in lines[i]
        and _parse_table_aligns(lines[i + 1]) is not None
    )


def _table_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def _table_aligns(delim: str) -> list[str]:
    return _parse_table_aligns(delim) or []


def _parse_heading(line: str) -> tuple[int, str] | None:
    level = 0
    while level < min(6, len(line)) and line[level] == "#":
        level += 1
    if level == 0 or level == len(line) or not line[level].isspace():
        return None
    text = line[level:].lstrip().rstrip()
    return level, text.rstrip("#").rstrip()


def _parse_heading_id(text: str) -> tuple[str, str] | None:
    if not text.endswith("}"):
        return None
    start = text.rfind("{#")
    if start < 0:
        return None
    identifier = text[start + 2 : -1]
    if not identifier or any(
        not (character == "_" or character.isalnum() or character in ".:-")
        for character in identifier
    ):
        return None
    return text[:start].rstrip(), identifier


def _parse_table_aligns(line: str) -> list[str] | None:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = stripped.split("|")
    if len(cells) < 2:
        return None
    aligns: list[str] = []
    for raw_spec in cells:
        spec = raw_spec.strip()
        left, right = spec.startswith(":"), spec.endswith(":")
        hyphens = spec.removeprefix(":").removesuffix(":")
        if not hyphens or hyphens.strip("-"):
            return None
        aligns.append(
            "center" if left and right else "right" if right else "left" if left else ""
        )
    return aligns


def _align(aligns: list[str], column: int) -> str:
    value = aligns[column] if column < len(aligns) else ""
    return f' style="text-align:{value}"' if value else ""


def render(source: str) -> Rendered:
    """Render markdown `source` to HTML plus its table of contents and title."""
    return _Renderer().render(source)
