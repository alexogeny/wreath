"""WCAG 1.4.3 (text) and 1.4.11 (non-text) contrast over a design-token stylesheet.

Tokens are declared as CSS custom properties in up to three theme contexts — light
`:root`, `@media (prefers-color-scheme: dark)`, and `:root[data-theme=...]` — and
each theme is resolved independently (`var()` chains included). Only *unambiguous*
foreground/background pairs are checked: a `color` and `background` in the same rule,
or a semantic `var()` text colour on the base surface. That keeps the check conservative
— it reports genuine failures without the cascade false-positives a full browser would
need to avoid. Findings are WARN (advisory): a static check cannot know a run's text size
or opacity, both of which change the applicable threshold, so contrast is surfaced for
review rather than treated as a hard error. The reported band (fails normal <4.5:1 vs
fails even large text <3:1) is in the message. Pairs unresolvable to hex are skipped.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from .model import Finding, Severity

_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
_DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;}]+)")
_HEX = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b")
_MEDIA = re.compile(r"@media[^{]*\{")
_NAMED = {"white": "#ffffff", "black": "#000000"}

_NORMAL_AA = 4.5
_LARGE_AA = 3.0


# --- colour maths -----------------------------------------------------------------
def _hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except ValueError:
        return None


def _linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_linear(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex1: str, hex2: str) -> float | None:
    a, b = _hex_to_rgb(hex1), _hex_to_rgb(hex2)
    if a is None or b is None:
        return None
    l1, l2 = relative_luminance(a), relative_luminance(b)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# --- token + rule parsing ---------------------------------------------------------
def _extract(css: str, brace_idx: int) -> tuple[str, int]:
    """Body of the brace group opening at `brace_idx` and the index past its close."""
    depth = 0
    for j in range(brace_idx, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[brace_idx + 1:j], j + 1
    return css[brace_idx + 1:], len(css)


def _decls(body: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _DECL.finditer(body)}


def _resolve_token(name: str, raw: dict[str, str], seen: frozenset[str]) -> str:
    value = raw.get(name, "").strip()
    if name in seen:
        return value
    m = _VAR.match(value)
    if m:
        return _resolve_token(m.group(1), raw, seen | {name})
    return value


def parse_tokens(css: str) -> dict[str, dict[str, str]]:
    """`{"light": {"--ink": "#..."}, "dark": {...}}` with `var()` chains resolved."""
    light_raw: dict[str, str] = {}
    dark_raw: dict[str, str] = {}
    rest = css
    m = re.search(r"@media[^{]*prefers-color-scheme\s*:\s*dark[^{]*\{", rest)
    if m:
        body, end = _extract(rest, m.end() - 1)
        mr = re.search(r":root\s*\{", body)
        if mr:
            inner, _ = _extract(body, mr.end() - 1)
            dark_raw.update(_decls(inner))
        rest = rest[:m.start()] + rest[end:]
    for pattern, target in (
        (r":root\s*\{", light_raw),
        (r":root\[data-theme=light\]\s*\{", light_raw),
        (r":root\[data-theme=dark\]\s*\{", dark_raw),
    ):
        for mm in re.finditer(pattern, rest):
            body, _ = _extract(rest, mm.end() - 1)
            target.update(_decls(body))
    return {
        theme: {name: _resolve_token(name, raw, frozenset()) for name in raw}
        for theme, raw in (("light", light_raw), ("dark", dark_raw))
    }


def _strip_media(css: str) -> str:
    out, i = [], 0
    while True:
        m = _MEDIA.search(css, i)
        if not m:
            # complexity: allow SL-SLICE-LOOP -- tail copied once before break
            out.append(css[i:])
            break
        out.append(css[i:m.start()])
        _body, end = _extract(css, m.end() - 1)
        i = end
    return "".join(out)


def _rules(css: str) -> Iterator[tuple[str, str]]:
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_media(css)):
        yield m.group(1).strip(), m.group(2)


def _prop(body: str, name: str) -> str | None:
    m = re.search(rf"(?:^|;)\s*{name}\s*:\s*([^;]+)", body)
    return m.group(1).strip() if m else None


def _bg_color(shorthand: str | None) -> str | None:
    if not shorthand:
        return None
    v = _VAR.search(shorthand) or _HEX.search(shorthand)
    return v.group(0) if v else None


def _color_bg_pairs(css: str) -> list[tuple[str, str]]:
    base_bg: str | None = None
    for selector, body in _rules(css):
        bg = _prop(body, "background-color") or _bg_color(_prop(body, "background"))
        if bg and selector in ("body", ":root"):
            base_bg = bg
    pairs: list[tuple[str, str]] = []
    for _selector, body in _rules(css):
        color = _prop(body, "color")
        if not color:
            continue
        bg = _prop(body, "background-color") or _bg_color(_prop(body, "background"))
        if bg:
            pairs.append((color, bg))
        elif color.startswith("var(") and base_bg:
            # Semantic text token with no own background inherits the base surface.
            # Literal-hex colours (e.g. white badge text) are skipped — they sit on a
            # coloured background declared in a sibling rule and would false-positive.
            pairs.append((color, base_bg))
    return pairs


def _resolve_expr(expr: str, tokens: dict[str, str]) -> str | None:
    expr = expr.strip()
    m = _VAR.match(expr)
    if m:
        value = tokens.get(m.group(1))
        return value if value and _hex_to_rgb(value) else None
    if expr.lower() in _NAMED:
        return _NAMED[expr.lower()]
    return expr if _hex_to_rgb(expr) else None


#: Selectors whose border/outline is (usually) the only visual boundary of a UI
#: component, so 1.4.11 applies. Scoped tight to avoid flagging decorative borders.
_UI_SELECTOR = re.compile(r"(?:^|[\s,>~+])(?:input|select|textarea|button)\b|:focus")


def _border_pairs(css: str) -> list[tuple[str, str]]:
    """(border/outline colour, base surface) for UI-component selectors only."""
    base_bg: str | None = None
    for selector, body in _rules(css):
        bg = _prop(body, "background-color") or _bg_color(_prop(body, "background"))
        if bg and selector in ("body", ":root"):
            base_bg = bg
    if base_bg is None:
        return []
    pairs: list[tuple[str, str]] = []
    for selector, body in _rules(css):
        if not _UI_SELECTOR.search(selector):
            continue
        for prop in ("border-color", "outline-color"):
            value = _prop(body, prop)
            if value:
                pairs.append((value, base_bg))
        for shorthand in ("border", "outline"):
            value = _bg_color(_prop(body, shorthand))
            if value:
                pairs.append((value, base_bg))
    return pairs


def nontext_contrast_findings(css: str, surface: str) -> Iterator[Finding]:
    """WCAG 1.4.11 — a UI component's boundary needs 3:1 against its surface.

    Conservative like `contrast_findings`: only form-control and focus-state
    borders/outlines (the boundaries a component's identity depends on) are
    checked, against the base surface, at the 3:1 non-text threshold.
    """
    themes = parse_tokens(css)
    # Form borders are often literal hex, not tokens; fall back to a token-less
    # theme so a plain `border-color:#ddd` is still resolvable and checked.
    active = {name: tokens for name, tokens in themes.items() if tokens} or {"default": {}}
    seen: set[tuple] = set()
    for fg_expr, bg_expr in _border_pairs(css):
        for theme_name, tokens in active.items():
            fg = _resolve_expr(fg_expr, tokens)
            bg = _resolve_expr(bg_expr, tokens)
            if fg is None or bg is None:
                continue
            ratio = contrast_ratio(fg, bg)
            if ratio is None or ratio >= _LARGE_AA:
                continue
            key = (fg_expr, bg_expr, theme_name)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                "non-text-contrast", Severity.WARN, surface,
                f"UI border {fg_expr} on {bg_expr} is {ratio:.2f}:1 in the {theme_name} "
                f"theme (WCAG 1.4.11 needs {_LARGE_AA}:1 for component boundaries)",
                "WCAG 1.4.11", "",
                "raise the border/outline colour contrast to at least 3:1",
            )


def contrast_findings(css: str, surface: str) -> Iterator[Finding]:
    themes = parse_tokens(css)
    seen: set[tuple] = set()
    for fg_expr, bg_expr in _color_bg_pairs(css):
        for theme_name, tokens in themes.items():
            if not tokens:
                continue
            fg = _resolve_expr(fg_expr, tokens)
            bg = _resolve_expr(bg_expr, tokens)
            if fg is None or bg is None:
                continue
            ratio = contrast_ratio(fg, bg)
            if ratio is None or ratio >= _NORMAL_AA:
                continue
            band = "fails even large text" if ratio < _LARGE_AA else "fails normal text"
            key = (fg_expr, bg_expr, theme_name)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(
                "contrast", Severity.WARN, surface,
                f"{fg_expr} on {bg_expr} is {ratio:.2f}:1 in the {theme_name} theme "
                f"({band}; WCAG AA needs {_NORMAL_AA}:1 normal / {_LARGE_AA}:1 large)",
                "WCAG 1.4.3", "",
                "adjust the token colours so the pair meets the contrast ratio",
            )
