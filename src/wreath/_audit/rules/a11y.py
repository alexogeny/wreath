"""Curated WCAG 2.1 A/AA rules applicable to server-generated HTML.

Each rule is a callable ``(root, surface) -> Iterator[Finding]`` registered in
``A11Y_RULES``. The set is deliberately curated to criteria that are decidable from
static markup — keyboard/focus-trap runtime behaviour, live regions, and motion are out
of scope (see docs/reference). Colour contrast (1.4.3) is Tier 2, not here.
"""
from __future__ import annotations

from collections.abc import Iterator

from ..dom import Node
from ..model import Finding, Severity

A11Y_RULES: list = []


def _rule(fn):
    A11Y_RULES.append(fn)
    return fn


def _f(rule_id, severity, surface, message, reference, node=None, suggestion=""):
    return Finding(rule_id, severity, surface, message, reference,
                   node.loc if node else "", suggestion)


# --- valueless/known-token tables -------------------------------------------------
_LABELABLE = {"input", "select", "textarea"}
_NO_LABEL_INPUT = {"hidden", "submit", "button", "reset", "image"}
_VAGUE_LINKS = {"click here", "here", "read more", "more", "link", "this", "learn more"}
_ARIA_ATTRS = {
    "aria-label", "aria-labelledby", "aria-describedby", "aria-hidden", "aria-live",
    "aria-expanded", "aria-controls", "aria-current", "aria-selected", "aria-checked",
    "aria-disabled", "aria-required", "aria-invalid", "aria-haspopup", "aria-pressed",
    "aria-modal", "aria-atomic", "aria-busy", "aria-owns", "aria-details", "aria-level",
    "aria-orientation", "aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext",
    "aria-roledescription", "aria-keyshortcuts", "aria-placeholder", "aria-readonly",
}
_ROLES = {
    "alert", "alertdialog", "application", "article", "banner", "button", "cell",
    "checkbox", "columnheader", "combobox", "complementary", "contentinfo", "definition",
    "dialog", "document", "feed", "figure", "form", "grid", "gridcell", "group", "heading",
    "img", "link", "list", "listbox", "listitem", "main", "menu", "menubar", "menuitem",
    "navigation", "none", "note", "option", "presentation", "progressbar", "radio",
    "radiogroup", "region", "row", "rowgroup", "rowheader", "search", "searchbox",
    "separator", "slider", "spinbutton", "status", "switch", "tab", "table", "tablist",
    "tabpanel", "term", "textbox", "toolbar", "tooltip", "tree", "treeitem",
}


def _accessible_name(node: Node) -> bool:
    return bool(node.text) or node.has_attr("aria-label") or node.has_attr("aria-labelledby") \
        or (node.tag == "img" and node.attr("alt")) or bool(node.attr("title"))


@_rule
def html_lang(root, surface) -> Iterator[Finding]:
    html = root.first("html")
    if html is None:
        return
    if not (html.attr("lang") or "").strip():
        yield _f("html-lang", Severity.ERROR, surface,
                 "<html> is missing a non-empty lang attribute", "WCAG 3.1.1",
                 html, suggestion='add lang="en" (or the document language)')


@_rule
def document_title(root, surface) -> Iterator[Finding]:
    if root.first("html") is None:
        return  # fragment, not a full document
    title = root.first("title")
    if title is None or not title.text:
        yield _f("document-title", Severity.ERROR, surface,
                 "document has no non-empty <title>", "WCAG 2.4.2",
                 suggestion="add a descriptive <title>")


@_rule
def img_alt(root, surface) -> Iterator[Finding]:
    for img in root.find_all("img"):
        if not img.has_attr("alt"):
            yield _f("img-alt", Severity.ERROR, surface,
                     "<img> has no alt attribute", "WCAG 1.1.1", img,
                     suggestion='add alt="…" (or alt="" if purely decorative)')


@_rule
def control_label(root, surface) -> Iterator[Finding]:
    labelled_for = {lbl.attr("for") for lbl in root.find_all("label") if lbl.attr("for")}
    for node in root.walk():
        if node.tag not in _LABELABLE:
            continue
        if node.tag == "input" and (node.attr("type") or "text") in _NO_LABEL_INPUT:
            continue
        wrapped = node.parent is not None and node.parent.tag == "label"
        has_name = (
            node.has_attr("aria-label") or node.has_attr("aria-labelledby")
            or node.has_attr("title") or wrapped
            or (node.attr("id") in labelled_for)
        )
        if not has_name:
            yield _f("control-label", Severity.ERROR, surface,
                     f"<{node.tag}> has no associated label or accessible name",
                     "WCAG 1.3.1/4.1.2", node,
                     suggestion="add a <label for>, wrap in <label>, or aria-label")


@_rule
def heading_order(root, surface) -> Iterator[Finding]:
    headings = [n for n in root.walk() if n.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}]
    if not headings:
        return
    h1s = [h for h in headings if h.tag == "h1"]
    if len(h1s) != 1:
        yield _f("heading-order", Severity.WARN, surface,
                 f"document has {len(h1s)} <h1> elements (expected exactly 1)",
                 "WCAG 1.3.1", headings[0])
    prev = 0
    for h in headings:
        level = int(h.tag[1])
        if prev and level > prev + 1:
            yield _f("heading-order", Severity.WARN, surface,
                     f"heading level jumps from h{prev} to h{level} (skipped level)",
                     "WCAG 1.3.1", h, suggestion="do not skip heading levels")
        prev = level


@_rule
def landmarks(root, surface) -> Iterator[Finding]:
    if root.first("html") is None:
        return
    has_main = root.first("main") is not None or any(
        n.attr("role") == "main" for n in root.walk()
    )
    if not has_main:
        yield _f("landmarks", Severity.WARN, surface,
                 "document has no <main> landmark", "WCAG 1.3.1",
                 suggestion="wrap primary content in <main>")
    for node in root.find_all("button"):
        if not _accessible_name(node):
            yield _f("landmarks", Severity.WARN, surface,
                     "<button> has no accessible name", "WCAG 4.1.2", node,
                     suggestion="add text content or aria-label")


@_rule
def link_text(root, surface) -> Iterator[Finding]:
    for a in root.find_all("a"):
        if not a.has_attr("href"):
            continue
        text = a.text.strip().lower()
        if not text and not a.has_attr("aria-label"):
            yield _f("link-text", Severity.WARN, surface,
                     "link has no discernible text", "WCAG 2.4.4", a,
                     suggestion="add link text or aria-label")
        elif text in _VAGUE_LINKS:
            yield _f("link-text", Severity.WARN, surface,
                     f"non-descriptive link text: {text!r}", "WCAG 2.4.4", a,
                     suggestion="use link text that describes the destination")


@_rule
def table_headers(root, surface) -> Iterator[Finding]:
    for table in root.find_all("table"):
        if table.attr("role") in {"presentation", "none"}:
            continue
        ths = table.find_all("th")
        if not ths:
            yield _f("table-headers", Severity.WARN, surface,
                     "data <table> has no <th> header cells", "WCAG 1.3.1", table,
                     suggestion="use <th> for header cells (or role=presentation if layout)")
        for th in ths:
            if not th.has_attr("scope"):
                yield _f("table-headers", Severity.WARN, surface,
                         "<th> has no scope attribute", "WCAG 1.3.1", th,
                         suggestion='add scope="col" or scope="row"')


@_rule
def aria_valid(root, surface) -> Iterator[Finding]:
    for node in root.walk():
        for name in node.attrs:
            if name.startswith("aria-") and name not in _ARIA_ATTRS:
                yield _f("aria-valid", Severity.WARN, surface,
                         f"unknown ARIA attribute {name!r}", "WCAG 4.1.2", node,
                         suggestion="remove or correct the aria-* attribute")
        role = node.attr("role")
        if role is not None and role not in _ROLES:
            yield _f("aria-valid", Severity.WARN, surface,
                     f"unknown role {role!r}", "WCAG 4.1.2", node,
                     suggestion="use a valid ARIA role token")


@_rule
def duplicate_id(root, surface) -> Iterator[Finding]:
    seen: dict[str, Node] = {}
    for node in root.walk():
        ident = node.attr("id")
        if not ident:
            continue
        if ident in seen:
            yield _f("duplicate-id", Severity.ERROR, surface,
                     f"duplicate id {ident!r}", "WCAG 4.1.1", node,
                     suggestion="ids must be unique within a document")
        else:
            seen[ident] = node


@_rule
def tabindex(root, surface) -> Iterator[Finding]:
    for node in root.walk():
        raw = node.attr("tabindex")
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            yield _f("tabindex", Severity.WARN, surface,
                     f"positive tabindex={value} disrupts natural focus order",
                     "WCAG 2.4.3", node, suggestion="use tabindex=0 or restructure the DOM")


@_rule
def viewport_scale(root, surface) -> Iterator[Finding]:
    for meta in root.find_all("meta"):
        if (meta.attr("name") or "").lower() != "viewport":
            continue
        content = (meta.attr("content") or "").lower().replace(" ", "")
        if "user-scalable=no" in content or "maximum-scale=1" in content:
            yield _f("viewport-scale", Severity.WARN, surface,
                     "viewport meta disables zoom", "WCAG 1.4.4", meta,
                     suggestion="remove user-scalable=no / maximum-scale to allow zoom")


@_rule
def contrast(root, surface) -> Iterator[Finding]:
    """WCAG 1.4.3 — colour contrast of the design-token pairs in any inline <style>."""
    from ..contrast import contrast_findings

    for style in root.find_all("style"):
        css = style.text
        if css:
            yield from contrast_findings(css, surface)
