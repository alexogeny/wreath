"""Curated WCAG 2.2 A/AA rules applicable to server-generated HTML.

Each rule is a callable `(root, surface) -> Iterator[Finding]` registered in
`A11Y_RULES`. The set is deliberately curated to criteria that are decidable from
static markup — keyboard/focus-trap runtime behaviour, live regions, and motion are out
of scope (see docs/reference). Colour contrast (1.4.3) is Tier 2, not here.

WCAG 2.2 note: 4.1.1 (Parsing) is obsolete, so structural checks that once cited
it (e.g. duplicate ids) map to 4.1.2 / 1.3.1 instead.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from ..dom import Node
from ..model import Finding, Severity

A11Y_RULES: list = []

#: `outline:none` / `outline:0[unit]` terminated by ; } or end of string.
_OUTLINE_OFF = re.compile(r"outline:(none|0(px|em|rem|%)?)(;|\}|$)")


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
# The complete WAI-ARIA 1.2 states and properties. An allow-list narrower than
# the spec makes valid, conformant markup (e.g. aria-sort on a sortable column)
# report as an "unknown ARIA attribute" -- a false positive against WCAG 4.1.2.
_ARIA_ATTRS = {
    "aria-activedescendant", "aria-atomic", "aria-autocomplete", "aria-braillelabel",
    "aria-brailleroledescription", "aria-busy", "aria-checked", "aria-colcount",
    "aria-colindex", "aria-colindextext", "aria-colspan", "aria-controls", "aria-current",
    "aria-describedby", "aria-description", "aria-details", "aria-disabled",
    "aria-dropeffect", "aria-errormessage", "aria-expanded", "aria-flowto", "aria-grabbed",
    "aria-haspopup", "aria-hidden", "aria-invalid", "aria-keyshortcuts", "aria-label",
    "aria-labelledby", "aria-level", "aria-live", "aria-modal", "aria-multiline",
    "aria-multiselectable", "aria-orientation", "aria-owns", "aria-placeholder",
    "aria-posinset", "aria-pressed", "aria-readonly", "aria-relevant", "aria-required",
    "aria-roledescription", "aria-rowcount", "aria-rowindex", "aria-rowindextext",
    "aria-rowspan", "aria-selected", "aria-setsize", "aria-sort", "aria-valuemax",
    "aria-valuemin", "aria-valuenow", "aria-valuetext",
}
# The complete set of non-abstract WAI-ARIA 1.2 roles.
_ROLES = {
    "alert", "alertdialog", "application", "article", "associationlist",
    "associationlistitemkey", "associationlistitemvalue", "banner", "blockquote",
    "button", "caption", "cell", "checkbox", "code", "columnheader", "combobox",
    "complementary", "contentinfo", "definition", "deletion", "dialog", "directory",
    "document", "emphasis", "feed", "figure", "form", "generic", "grid", "gridcell",
    "group", "heading", "img", "insertion", "link", "list", "listbox", "listitem",
    "log", "main", "mark", "marquee", "math", "menu", "menubar", "menuitem",
    "menuitemcheckbox", "menuitemradio", "meter", "navigation", "none", "note", "option",
    "paragraph", "presentation", "progressbar", "radio", "radiogroup", "region", "row",
    "rowgroup", "rowheader", "scrollbar", "search", "searchbox", "separator", "slider",
    "spinbutton", "status", "strong", "subscript", "superscript", "switch", "tab",
    "table", "tablist", "tabpanel", "term", "textbox", "time", "timer", "toolbar",
    "tooltip", "tree", "treegrid", "treeitem",
}


def _accessible_name(node: Node) -> bool:
    return bool(node.text) or node.has_attr("aria-label") or node.has_attr("aria-labelledby") \
        or bool(node.tag == "img" and node.attr("alt")) or bool(node.attr("title"))


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
    # <input type="image"> is a functional image submit; WCAG 1.1.1 requires a
    # text alternative, and -- unlike a decorative <img> -- an empty alt won't
    # do because the control still needs a name.
    for control in root.find_all("input"):
        if (control.attr("type") or "").lower() != "image":
            continue
        if control.has_attr("aria-label") or control.has_attr("aria-labelledby"):
            continue
        if not (control.attr("alt") or "").strip():
            yield _f("img-alt", Severity.ERROR, surface,
                     '<input type="image"> has no text alternative', "WCAG 1.1.1",
                     control, suggestion='add a non-empty alt="…" (or aria-label)')
    # An <area href> is a functional image-map hotspot; 1.1.1 needs a non-empty
    # alt (a hotspot with no name is unusable to a screen reader).
    for area in root.find_all("area"):
        if not area.has_attr("href"):
            continue
        if area.has_attr("aria-label") or area.has_attr("aria-labelledby"):
            continue
        if not (area.attr("alt") or "").strip():
            yield _f("img-alt", Severity.ERROR, surface,
                     "<area href> image-map hotspot has no alt text", "WCAG 1.1.1",
                     area, suggestion='add a non-empty alt="…" describing the target')


@_rule
def frame_title(root, surface) -> Iterator[Finding]:
    """WCAG 4.1.2 — an embedded frame needs an accessible name (a `title`)."""
    for frame in root.find_all("iframe"):
        if (frame.attr("aria-hidden") or "").lower() == "true":
            continue  # hidden from assistive tech: no name needed
        has_name = (
            (frame.attr("title") or "").strip()
            or frame.has_attr("aria-label")
            or frame.has_attr("aria-labelledby")
        )
        if not has_name:
            yield _f("frame-title", Severity.ERROR, surface,
                     "<iframe> has no title (accessible name)", "WCAG 4.1.2", frame,
                     suggestion='add title="…" describing the frame content')


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
            # WCAG 2.2 obsoleted 4.1.1 (Parsing); a duplicate id is a defect
            # because it breaks id-based references (for=, aria-labelledby, ...),
            # which is 4.1.2 (Name, Role, Value) / 1.3.1 territory.
            yield _f("duplicate-id", Severity.ERROR, surface,
                     f"duplicate id {ident!r}", "WCAG 4.1.2", node,
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


@_rule
def non_text_contrast(root, surface) -> Iterator[Finding]:
    """WCAG 1.4.11 — form-control / focus borders meet 3:1 against the surface."""
    from ..contrast import nontext_contrast_findings

    for style in root.find_all("style"):
        css = style.text
        if css:
            yield from nontext_contrast_findings(css, surface)


@_rule
def autoplay(root, surface) -> Iterator[Finding]:
    """WCAG 1.4.2 — audio that plays automatically needs a control (or be muted)."""
    for node in root.find_all("audio", "video"):
        if node.has_attr("autoplay") and not node.has_attr("muted"):
            yield _f("autoplay", Severity.WARN, surface,
                     f"<{node.tag} autoplay> plays sound with no control", "WCAG 1.4.2",
                     node, suggestion="add muted, or provide a pause/stop control")


@_rule
def meta_refresh(root, surface) -> Iterator[Finding]:
    """WCAG 2.2.1 — a timed meta refresh moves the page out from under the user."""
    for meta in root.find_all("meta"):
        if (meta.attr("http-equiv") or "").lower() != "refresh":
            continue
        content = (meta.attr("content") or "").strip()
        delay = content.split(";", 1)[0].strip()
        # A non-zero timed refresh (and any client-side timed redirect) is the
        # failure; an instant 0-second redirect belongs server-side as a 3xx.
        if delay and delay != "0":
            yield _f("meta-refresh", Severity.WARN, surface,
                     f"<meta http-equiv=refresh content={content!r}> auto-refreshes the page",
                     "WCAG 2.2.1", meta,
                     suggestion="remove it, or let the user control the timing")


@_rule
def focus_visible(root, surface) -> Iterator[Finding]:
    """WCAG 2.4.7 — removing the focus outline hides the keyboard focus indicator."""
    def _kills_outline(css: str) -> bool:
        # outline:none / outline:0[unit], terminated by ; } or end (so a rule
        # with no trailing semicolon, e.g. style="outline:0", still matches).
        packed = css.lower().replace(" ", "")
        return bool(_OUTLINE_OFF.search(packed))

    for style in root.find_all("style"):
        if style.text and _kills_outline(style.text):
            yield _f("focus-visible", Severity.WARN, surface,
                     "a <style> sets 'outline: none/0', which can remove the focus ring",
                     "WCAG 2.4.7", style,
                     suggestion="pair :focus { outline: none } with a visible :focus-visible style")
    for node in root.walk():
        inline = node.attr("style")
        if inline and _kills_outline(inline):
            yield _f("focus-visible", Severity.WARN, surface,
                     f"<{node.tag} style> removes the outline (focus indicator)",
                     "WCAG 2.4.7", node,
                     suggestion="keep a visible focus indicator (outline or box-shadow)")
