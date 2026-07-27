"""The built-in theme: one self-contained HTML document per page.

No CDN, no web fonts, no JS framework — a system font stack and a small blob of
CSS driven by custom properties, with a light/dark toggle that also honours the
OS preference (the same approach as ``_devtools/bench_report.py``).

**Everything is a token.** The theme used to carry about eleven unrelated font
sizes and thirty ad-hoc padding values, which is what made it read as
approximate rather than designed — a system font stack looks bad mostly when
nobody tunes the scale around it. There are now three scales (:data:`type
<_TYPE>`, space, elevation) declared once in :func:`critical_css`, and every
rule below spends them rather than inventing a number. If you find yourself
typing a raw ``rem`` into a component rule, the scale is missing a step; add
the step.

**Two stylesheets, on purpose.** :func:`critical_css` is the design tokens plus
enough paint to make an unstyled flash look intentional, and it is *inlined*
into every page. :func:`stylesheet` is the whole thing and is written once to
``assets/docs.css``, which the browser caches across a 129-page site. Inlining
only the tokens costs ~1.5 KiB per page and buys two things: a page whose
colours survive a missing stylesheet, and — less obviously — an auditable one.
``wreath audit``'s contrast, non-text-contrast, and focus rules only read inline
``<style>``, so while the whole theme lived in an external file those three
rules silently never ran on a single built page.
"""

from __future__ import annotations

from .config import Palette

_SANS = ('-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, '
         'Arial, sans-serif')
_SERIF = ('"Iowan Old Style", "Palatino Linotype", Palatino, Georgia, '
          '"Times New Roman", serif')
_MONO = ('ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, '
         '"Liberation Mono", monospace')

#: Type scale — 1rem base on a ~1.25 (major third) ratio, rounded to whole
#: pixels at 16px root so nothing lands on a half-pixel and blurs. Two steps
#: below the base carry the UI chrome; four above carry the headings.
_TYPE = (
    "--text-xs:.75rem;"        # 12px — copy button, captions
    "--text-sm:.875rem;"       # 14px — nav, TOC, tables, code
    "--text-base:1rem;"        # 16px — body
    "--text-lg:1.125rem;"      # 18px — lead paragraph, h4
    "--text-xl:1.375rem;"      # 22px — h3
    "--text-2xl:1.75rem;"      # 28px — h2
    "--text-3xl:2.25rem;"      # 36px — h1
)

#: Optical tracking. Type tightens as it grows: at 36px the default spacing
#: reads loose and the word shape falls apart, at 12px it reads cramped. These
#: are the corrections, not decoration.
_TRACK = (
    "--track-3xl:-.022em;--track-2xl:-.018em;--track-xl:-.012em;"
    "--track-base:0;--track-caps:.06em;"
)

#: Line height by role. Prose wants air (1.7 over a 74ch measure), headings
#: want none (1.2 — a two-line h1 with body leading looks like two headings),
#: and UI chrome sits between.
_LEADING = "--leading-tight:1.2;--leading-snug:1.4;--leading-normal:1.7;"

#: Space scale — 4px base, doubling from --space-4. Every margin, padding, and
#: gap in the theme is one of these eight values.
_SPACE = (
    "--space-1:.25rem;--space-2:.5rem;--space-3:.75rem;--space-4:1rem;"
    "--space-5:1.5rem;--space-6:2rem;--space-7:3rem;--space-8:4rem;"
)

#: Layout constants. `--measure` bounds *all* content, not just paragraphs:
#: capping `<p>` alone (as this did) left code blocks and tables running past
#: the text's right edge, which is the single most obvious "unfinished" tell.
_LAYOUT = "--measure:74ch;--sidebar-w:15.5rem;--toc-w:13rem;--header-h:3.5rem;"


def _colour_tokens(palette: Palette) -> tuple[str, str]:
    """The light and dark colour roles for ``palette``.

    Six roles rather than the previous four: `--surface-2` gives hover and
    inset states somewhere to live that is not "the code background", and
    `--border-strong` separates a decorative hairline from the boundary of a
    control. WCAG 1.4.11 asks for 3:1 on the latter and says nothing about the
    former — using one token for both forces a choice between a cage and a
    violation. `--border-strong` resolves to the muted text colour, which every
    theme already holds above 4.5:1.
    """
    light_link = palette.link or palette.primary
    # A heavy brand primary reads as too-dark body links and is often below AA
    # on a dark surface, so dark links lighten the brand rather than inherit it.
    dark_link = palette.dark_link or f"color-mix(in oklab, {palette.primary} 45%, #ffffff)"
    light = (
        f"--primary:{palette.primary};--accent:{palette.accent};"
        f"--bg:{palette.bg};--surface:{palette.surface};"
        f"--surface-2:color-mix(in oklab, {palette.fg} 5%, {palette.bg});"
        f"--fg:{palette.fg};--fg-muted:{palette.muted};"
        f"--fg-subtle:color-mix(in oklab, {palette.muted} 72%, {palette.bg});"
        f"--border:{palette.border};--border-strong:{palette.muted};"
        f"--link:{light_link};"
        # Shadows on a light surface are the shadow; on a dark one they mostly
        # vanish, so the dark set below leans on a lighter border instead.
        "--shadow-1:0 1px 2px rgba(0,0,0,.04),0 1px 3px rgba(0,0,0,.06);"
        "--shadow-2:0 4px 12px rgba(0,0,0,.08);"
        "--shadow-3:0 16px 40px rgba(0,0,0,.16);"
    )
    dark = (
        f"--bg:{palette.dark_bg};--surface:{palette.dark_surface};"
        f"--surface-2:color-mix(in oklab, {palette.dark_fg} 8%, {palette.dark_bg});"
        f"--fg:{palette.dark_fg};--fg-muted:{palette.dark_muted};"
        f"--fg-subtle:color-mix(in oklab, {palette.dark_muted} 72%, {palette.dark_bg});"
        f"--border:{palette.dark_border};--border-strong:{palette.dark_muted};"
        f"--link:{dark_link};"
        "--shadow-1:0 1px 2px rgba(0,0,0,.4);"
        "--shadow-2:0 4px 14px rgba(0,0,0,.5);"
        "--shadow-3:0 20px 48px rgba(0,0,0,.6);"
    )
    return light, dark


#: Syntax colours: a tuned hue per token, tinted into whatever theme is active.
#:
#: These were seven fixed GitHub hexes, so four of the five themes showed
#: GitHub's red-and-blue inside their own code blocks — a navy string literal on
#: sepia's warm paper is the loudest wrong note in the old theme.
#:
#: Deriving them from `--primary`/`--accent` alone was tried first and does not
#: work: there are only two brand hues, and a bright accent (wreath's cyan,
#: nord's pale blue) cannot be darkened enough for a *light* surface without
#: collapsing into the body colour — measured at 2.5:1 for nord, well under AA.
#: Real light-mode themes solve this the same way, with dark tuned hues; GitHub's
#: own light string colour is near-black navy.
#:
#: So: six hues chosen for legibility, each mixed 22% toward `--fg` so it sits in
#: the theme rather than on top of it. Measured floor across all five themes and
#: both modes is 5.2:1 against the code surface (AA wants 4.5), and the hues stay
#: distinguishable from each other, which pure derivation could not manage.
_TINT = 78                                  # % of the tuned hue; the rest is --fg


def _syntax(light: bool) -> str:
    hues = (
        {"keyword": "#b02a5b", "string": "#0a6b3d", "number": "#0b5fa5",
         "builtin": "#6b3fc0", "operator": "#b5390d", "variable": "#8a4b06"}
        if light else
        {"keyword": "#ff8098", "string": "#7ee787", "number": "#79c0ff",
         "builtin": "#d2a8ff", "operator": "#ffab70", "variable": "#ffc857"}
    )
    return "".join(
        f"--tok-{name}:color-mix(in oklab, {hue} {_TINT}%, var(--fg));"
        for name, hue in hues.items()
    ) + "--tok-comment:var(--fg-subtle);"

#: The critical paint layer: reset, page colours, type, and the layout frame.
#: Inlined, so keep it lean — the components live in `_COMPONENT_CSS`.
_CRITICAL_CSS = """
*,*::before,*::after{box-sizing:border-box;}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%;}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font);
 font-size:var(--text-base);line-height:var(--leading-normal);
 background-image:var(--surface-image);background-attachment:fixed;
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
a{color:var(--link);text-decoration:none;}
a:hover{text-decoration:underline;text-underline-offset:.15em;text-decoration-thickness:1px;}
a code{color:inherit;}
::selection{background:color-mix(in oklab, var(--primary) 24%, transparent);}
:focus-visible{outline:2px solid var(--link);outline-offset:2px;border-radius:var(--radius-sm);}
/* There is deliberately no `:focus:not(:focus-visible)` outline reset here.
   Every current browser already withholds the ring for pointer interaction, so
   the reset only suppressed a ring nobody saw — and a blanket suppression is
   one edit away from being why a keyboard user cannot tell where they are.
   One focus treatment, applied everywhere, is also simply easier to trust. */
img{max-width:100%;height:auto;}

/* Skip link: the first thing a keyboard lands on, invisible until it matters. */
.skip{position:absolute;left:var(--space-2);top:-3rem;z-index:80;
 background:var(--bg);color:var(--link);border:1px solid var(--border-strong);
 border-radius:var(--radius-sm);padding:var(--space-2) var(--space-3);
 transition:top .15s;}
.skip:focus{top:var(--space-2);}

/* --- header --------------------------------------------------------------- */
header.site{position:sticky;top:0;z-index:40;display:flex;align-items:center;
 gap:var(--space-4);height:var(--header-h);padding:0 var(--space-5);
 border-bottom:1px solid var(--border);
 background:color-mix(in oklab, var(--bg) 86%, transparent);
 backdrop-filter:saturate(1.4) blur(10px);-webkit-backdrop-filter:saturate(1.4) blur(10px);}
header.site .brand{display:flex;align-items:center;gap:var(--space-2);font-weight:700;
 color:var(--fg);font-size:var(--text-lg);letter-spacing:var(--track-xl);}
header.site .brand:hover{text-decoration:none;}
.brand .mark{width:1.375rem;height:1.375rem;border-radius:var(--radius-sm);flex:none;
 background:linear-gradient(135deg,var(--primary),var(--accent));}
header.site .spacer{flex:1;}

/* --- layout --------------------------------------------------------------- */
/* The measure lives on the *column*, not on `main`. Capping `main` inside a
   `1fr` track leaves the track's leftover width as a hole between the content
   and the table of contents; sizing the track to the measure and centring the
   packed tracks puts that space in the margins where it belongs. */
.layout{display:grid;
 grid-template-columns:var(--sidebar-w) minmax(0,var(--measure)) var(--toc-w);
 justify-content:center;gap:var(--space-7);max-width:96rem;margin:0 auto;
 padding:var(--space-6) var(--space-5);}
main{min-width:0;}

/* --- prose ---------------------------------------------------------------- */
/* One rhythm rule instead of per-element margins that fight each other: every
   sibling gets the same top gap, and the headings widen it below. */
main>*+*{margin-top:var(--space-4);}
main h1,main h2,main h3,main h4{line-height:var(--leading-tight);
 scroll-margin-top:calc(var(--header-h) + var(--space-4));font-weight:700;}
main h1{font-size:var(--text-3xl);letter-spacing:var(--track-3xl);}
main h2{font-size:var(--text-2xl);letter-spacing:var(--track-2xl);}
main h3{font-size:var(--text-xl);letter-spacing:var(--track-xl);}
main h4{font-size:var(--text-lg);}
main>h2{margin-top:var(--space-7);}
main>h3{margin-top:var(--space-6);}
main>h4{margin-top:var(--space-5);}
main>h1+*,main>h2+*,main>h3+*,main>h4+*{margin-top:var(--space-3);}
main>ul,main>ol{padding-left:var(--space-5);}
li+li{margin-top:var(--space-1);}
li>ul,li>ol{margin-top:var(--space-1);padding-left:var(--space-5);}
:target{scroll-margin-top:calc(var(--header-h) + var(--space-4));}
"""

#: Everything below the fold or component-shaped. External and cached.
_COMPONENT_CSS = """
/* --- controls -------------------------------------------------------------- */
button.theme,.menu-btn{border:1px solid var(--border-strong);background:transparent;
 color:var(--fg);border-radius:var(--radius-sm);padding:var(--space-1) var(--space-2);
 cursor:pointer;font:inherit;font-size:var(--text-sm);line-height:1;min-width:2rem;
 min-height:2rem;display:inline-flex;align-items:center;justify-content:center;
 transition:background .15s,border-color .15s;}
button.theme:hover,.menu-btn:hover{background:var(--surface-2);border-color:var(--link);}
.menu-btn{display:none;}

/* --- search ---------------------------------------------------------------- */
.search{position:relative;}
#docs-search{border:1px solid var(--border-strong);background:var(--bg);color:var(--fg);
 border-radius:var(--radius-sm);padding:var(--space-1) var(--space-3);font:inherit;
 font-size:var(--text-sm);width:13rem;min-height:2rem;
 transition:width .2s,box-shadow .15s,border-color .15s;}
#docs-search::placeholder{color:var(--fg-subtle);}
/* No bespoke focus style: the global `:focus-visible` ring covers this too, and
   one focus treatment across the site beats a per-control invention. */
#docs-search:focus{width:16rem;border-color:var(--link);}
#docs-results{display:none;position:absolute;right:0;top:calc(100% + var(--space-2));
 width:26rem;max-width:80vw;max-height:64vh;overflow:auto;background:var(--bg);
 border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-3);
 z-index:50;padding:var(--space-1);}
#docs-results a{display:block;padding:var(--space-2) var(--space-3);
 border-radius:var(--radius-sm);color:var(--fg);}
#docs-results a:hover{background:var(--surface-2);text-decoration:none;}
#docs-results .r-title{font-weight:600;}
#docs-results .r-ctx{color:var(--fg-muted);font-size:var(--text-sm);}

/* --- sidebar --------------------------------------------------------------- */
nav.side{font-size:var(--text-sm);line-height:var(--leading-snug);align-self:start;
 position:sticky;top:calc(var(--header-h) + var(--space-4));
 max-height:calc(100vh - var(--header-h) - var(--space-6));overflow-y:auto;
 overscroll-behavior:contain;padding-right:var(--space-2);
 scrollbar-width:thin;scrollbar-color:var(--border) transparent;}
nav.side::-webkit-scrollbar{width:9px;}
nav.side::-webkit-scrollbar-thumb{background:var(--border);border-radius:5px;
 border:2px solid var(--bg);}
nav.side::-webkit-scrollbar-thumb:hover{background:var(--fg-muted);}
nav.side a{display:block;color:var(--fg-muted);padding:var(--space-1) var(--space-2);
 border-radius:var(--radius-sm);transition:background .12s,color .12s;}
nav.side a:hover{background:var(--surface-2);color:var(--fg);text-decoration:none;}
/* One signal for "you are here", not three: the old rule stacked a tinted
   fill, a left border, and a weight change, which read as a selection bug. */
nav.side a.active{color:var(--link);font-weight:600;
 background:color-mix(in oklab, var(--primary) 10%, transparent);}
details.section>summary{list-style:none;cursor:pointer;display:flex;align-items:center;
 gap:var(--space-2);font-weight:600;color:var(--fg);padding:var(--space-1) var(--space-2);
 border-radius:var(--radius-sm);user-select:none;transition:background .12s;}
details.section>summary::-webkit-details-marker{display:none;}
details.section>summary::before{content:"";width:.4em;height:.4em;flex:none;
 border-right:1.5px solid var(--fg-muted);border-bottom:1.5px solid var(--fg-muted);
 transform:rotate(-45deg);transition:transform .18s;margin-left:.15em;}
details.section[open]>summary::before{transform:rotate(45deg);}
details.section>summary:hover{background:var(--surface-2);}
nav.side details.section details.section,nav.side details.section>a{
 margin-left:var(--space-3);padding-left:var(--space-3);border-left:1px solid var(--border);}

/* --- on this page ---------------------------------------------------------- */
aside.toc{font-size:var(--text-sm);line-height:var(--leading-snug);align-self:start;
 position:sticky;top:calc(var(--header-h) + var(--space-4));
 max-height:calc(100vh - var(--header-h) - var(--space-6));overflow-y:auto;
 scrollbar-width:thin;}
aside.toc strong{display:block;text-transform:uppercase;letter-spacing:var(--track-caps);
 font-size:var(--text-xs);color:var(--fg-subtle);margin-bottom:var(--space-2);}
aside.toc a{display:block;color:var(--fg-muted);padding:var(--space-1) 0 var(--space-1)
 var(--space-3);border-left:2px solid var(--border);transition:color .12s,border-color .12s;}
aside.toc a:hover{color:var(--fg);text-decoration:none;}
aside.toc a.toc-active{color:var(--link);border-left-color:var(--link);font-weight:600;}

/* --- code ------------------------------------------------------------------ */
code{background:var(--surface);padding:.15em .4em;border-radius:var(--radius-sm);
 font-size:.875em;font-family:var(--font-mono);
 border:1px solid color-mix(in oklab, var(--border) 60%, transparent);}
pre{background:var(--surface);border:var(--border-width) solid var(--border);
 border-radius:var(--radius);padding:var(--space-4);overflow-x:auto;position:relative;
 box-shadow:var(--shadow-1);line-height:var(--leading-snug);}
pre code{background:none;padding:0;border:none;font-size:var(--text-sm);}
/* Reserve the button's lane so a long first line never runs underneath it. */
pre:has(.copy-btn) code{padding-right:var(--space-8);}
.copy-btn{position:absolute;top:var(--space-2);right:var(--space-2);
 border:1px solid var(--border-strong);background:var(--bg);color:var(--fg-muted);
 border-radius:var(--radius-sm);padding:var(--space-1) var(--space-2);
 font:inherit;font-size:var(--text-xs);cursor:pointer;opacity:0;transition:opacity .15s;}
/* Hover reveals it for a mouse; focus reveals it for a keyboard. It is a real
   tab stop, so hiding it on focus too would be a control nobody can reach. */
pre:hover .copy-btn,.copy-btn:focus-visible{opacity:1;}
.copy-btn:hover{color:var(--fg);}
.copy-btn.copied{color:var(--link);border-color:var(--link);}
.tok-comment{color:var(--tok-comment);font-style:italic;}
.tok-string{color:var(--tok-string);}
.tok-number{color:var(--tok-number);}
.tok-keyword{color:var(--tok-keyword);font-weight:600;}
.tok-builtin{color:var(--tok-builtin);}
.tok-variable{color:var(--tok-variable);}
.tok-operator{color:var(--tok-operator);}

/* --- headings: the anchor ---------------------------------------------------- */
.anchor{opacity:0;margin-left:var(--space-2);color:var(--fg-subtle);font-weight:400;
 text-decoration:none;}
h1:hover .anchor,h2:hover .anchor,h3:hover .anchor,h4:hover .anchor,
.anchor:focus-visible{opacity:1;}

/* --- blockquote ------------------------------------------------------------- */
blockquote{margin-inline:0;padding:var(--space-3) var(--space-4);
 border-left:3px solid var(--accent);border-radius:0 var(--radius) var(--radius) 0;
 color:var(--fg-muted);background:color-mix(in oklab, var(--accent) 7%, var(--bg));}
blockquote>*+*{margin-top:var(--space-3);}

/* --- admonitions ------------------------------------------------------------ */
.admonition{--adm:var(--accent);position:relative;padding:var(--space-3) var(--space-4);
 border:1px solid color-mix(in oklab, var(--adm) 30%, var(--border));
 border-left:3px solid var(--adm);border-radius:var(--radius);box-shadow:var(--shadow-1);
 background:color-mix(in oklab, var(--adm) 6%, var(--bg));}
.admonition>*+*{margin-top:var(--space-3);}
/* Title colour mixes the hue toward the *foreground*, not 45% of the hue with
   fg — the old formula produced a muddy mid-tone that failed contrast on the
   tinted background it sits on. */
.admonition-title{display:flex;align-items:baseline;gap:var(--space-2);font-weight:700;
 color:color-mix(in oklab, var(--adm) 30%, var(--fg));line-height:var(--leading-snug);}
/* A Unicode glyph rather than an icon font or an SVG sprite: it costs no bytes,
   needs no network, and still says *which kind* of callout this is — which a
   plain coloured dot does not. The glyph per kind is set below. */
.admonition-title::before{content:var(--adm-glyph,"\\24D8");color:var(--adm);
 font-size:1.05em;line-height:1;flex:none;font-weight:700;}
.admonition.note,.admonition.info,.admonition.important{--adm:#2563eb;}
.admonition.tip,.admonition.success,.admonition.check,.admonition.hint
{--adm:#059669;--adm-glyph:"\\2713";}
.admonition.warning,.admonition.caution,.admonition.attention{--adm:#b45309;--adm-glyph:"\\26A0";}
.admonition.danger,.admonition.error,.admonition.bug,.admonition.failure
{--adm:#dc2626;--adm-glyph:"\\2715";}
.admonition.question,.admonition.example,.admonition.faq{--adm:#7c3aed;--adm-glyph:"?";}
.admonition.quote,.admonition.abstract,.admonition.summary
{--adm:var(--fg-muted);--adm-glyph:"\\275D";}
/* Dark mode lifts each hue: the light-mode set is tuned for white and goes
   muddy on a near-black surface. */
:root[data-theme=dark] .admonition.note,:root[data-theme=dark] .admonition.info,
:root[data-theme=dark] .admonition.important{--adm:#60a5fa;}
:root[data-theme=dark] .admonition.tip,:root[data-theme=dark] .admonition.success,
:root[data-theme=dark] .admonition.check,:root[data-theme=dark] .admonition.hint{--adm:#34d399;}
:root[data-theme=dark] .admonition.warning,:root[data-theme=dark] .admonition.caution,
:root[data-theme=dark] .admonition.attention{--adm:#fbbf24;}
:root[data-theme=dark] .admonition.danger,:root[data-theme=dark] .admonition.error,
:root[data-theme=dark] .admonition.bug,:root[data-theme=dark] .admonition.failure{--adm:#f87171;}
:root[data-theme=dark] .admonition.question,:root[data-theme=dark] .admonition.example,
:root[data-theme=dark] .admonition.faq{--adm:#a78bfa;}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .admonition.note,
 :root:not([data-theme=light]) .admonition.info,
 :root:not([data-theme=light]) .admonition.important{--adm:#60a5fa;}
 :root:not([data-theme=light]) .admonition.tip,
 :root:not([data-theme=light]) .admonition.success,
 :root:not([data-theme=light]) .admonition.check,
 :root:not([data-theme=light]) .admonition.hint{--adm:#34d399;}
 :root:not([data-theme=light]) .admonition.warning,
 :root:not([data-theme=light]) .admonition.caution,
 :root:not([data-theme=light]) .admonition.attention{--adm:#fbbf24;}
 :root:not([data-theme=light]) .admonition.danger,
 :root:not([data-theme=light]) .admonition.error,
 :root:not([data-theme=light]) .admonition.bug,
 :root:not([data-theme=light]) .admonition.failure{--adm:#f87171;}
 :root:not([data-theme=light]) .admonition.question,
 :root:not([data-theme=light]) .admonition.example,
 :root:not([data-theme=light]) .admonition.faq{--adm:#a78bfa;}}

/* --- tabs ------------------------------------------------------------------- */
.tabbed{border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;
 box-shadow:var(--shadow-1);}
.tab-labels{display:flex;gap:var(--space-1);background:var(--surface);
 border-bottom:1px solid var(--border);padding:var(--space-1) var(--space-1) 0;}
.tab-label{border:none;background:none;color:var(--fg-muted);
 padding:var(--space-2) var(--space-3);cursor:pointer;border-bottom:2px solid transparent;
 font:inherit;font-size:var(--text-sm);font-weight:500;
 border-radius:var(--radius-sm) var(--radius-sm) 0 0;transition:color .12s,background .12s;}
.tab-label:hover{color:var(--fg);background:var(--bg);}
.tab-label.active{color:var(--link);border-bottom-color:var(--link);background:var(--bg);}
.tab-panel{display:none;padding:var(--space-4);}
.tab-panel.active{display:block;}
.tab-panel>*+*{margin-top:var(--space-3);}

/* --- tables ----------------------------------------------------------------- */
/* The wrapper is what stops a wide table scrolling the whole page sideways on
   a phone; the table itself only knows how to be as wide as its content. */
.table-wrap{overflow-x:auto;scrollbar-width:thin;
 border:1px solid var(--border);border-radius:var(--radius);}
table{border-collapse:collapse;width:100%;font-size:var(--text-sm);}
th,td{padding:var(--space-2) var(--space-3);text-align:left;
 border-bottom:1px solid var(--border);}
/* Horizontal rules only. A full 1px grid on every cell is the spreadsheet look
   and it fights the hairline weight used everywhere else. */
thead th{background:var(--surface);font-weight:600;white-space:nowrap;
 border-bottom:2px solid var(--border);}
tbody tr:last-child td{border-bottom:none;}
tbody tr:hover{background:var(--surface-2);}

/* --- prev/next --------------------------------------------------------------- */
.page-nav{display:flex;justify-content:space-between;gap:var(--space-4);
 margin-top:var(--space-8);padding-top:var(--space-5);border-top:1px solid var(--border);}
.page-nav a{flex:1;max-width:49%;padding:var(--space-3) var(--space-4);
 border:1px solid var(--border);border-radius:var(--radius);color:var(--fg);
 font-size:var(--text-sm);transition:border-color .15s,background .15s;}
.page-nav a:hover{border-color:var(--link);background:var(--surface-2);text-decoration:none;}
.page-nav .nav-next{text-align:right;margin-left:auto;}

/* --- misc -------------------------------------------------------------------- */
hr{border:none;border-top:1px solid var(--border);margin-block:var(--space-7);}
figure.chart{margin-inline:0;padding:var(--space-4);
 border:var(--border-width) solid var(--border);border-radius:var(--radius);
 background:var(--surface);box-shadow:var(--shadow-1);color:var(--fg);overflow-x:auto;}
.chart-error{color:var(--fg);background:color-mix(in oklab, #dc2626 10%, var(--bg));
 border:1px solid #dc2626;font-size:var(--text-sm);padding:var(--space-3);
 border-radius:var(--radius);}
li input[type=checkbox]{margin-right:var(--space-2);}

/* --- responsive -------------------------------------------------------------- */
.scrim{display:none;}
@media (max-width:80rem){
 .layout{grid-template-columns:var(--sidebar-w) minmax(0,var(--measure));}
 aside.toc{display:none;}
}
@media (max-width:60rem){
 .menu-btn{display:inline-flex;}
 .layout{grid-template-columns:1fr;gap:var(--space-5);padding:var(--space-5) var(--space-4);}
 nav.side{position:fixed;top:0;left:0;bottom:0;width:18rem;max-width:82vw;
  max-height:100vh;z-index:60;background:var(--bg);border-right:1px solid var(--border);
  padding:var(--space-5) var(--space-4);transform:translateX(-100%);
  transition:transform .22s ease;box-shadow:var(--shadow-3);}
 body.nav-open nav.side{transform:none;}
 body.nav-open .scrim{display:block;position:fixed;inset:0;z-index:55;
  background:rgba(0,0,0,.5);}
 main h1{font-size:var(--text-2xl);}
 main h2{font-size:var(--text-xl);}
}
@media (max-width:40rem){
 #docs-search{width:9rem;}#docs-search:focus{width:11rem;}
}

/* --- motion ------------------------------------------------------------------ */
/* WCAG 2.3.3. Not "less motion" — none, for anyone who asked. */
@media (prefers-reduced-motion:reduce){
 html{scroll-behavior:auto;}
 *,*::before,*::after{animation-duration:.01ms !important;animation-iteration-count:1 !important;
  transition-duration:.01ms !important;}
}

/* --- print -------------------------------------------------------------------- */
@media print{
 header.site,nav.side,aside.toc,.page-nav,.copy-btn,.skip,.scrim{display:none !important;}
 .layout{display:block;max-width:none;padding:0;}
 main{max-width:none;}
 pre,.admonition,.tabbed,figure.chart{box-shadow:none;break-inside:avoid;}
 a[href^="http"]::after{content:" (" attr(href) ")";font-size:var(--text-xs);
  color:var(--fg-muted);}
}
"""

_TOGGLE_JS = (
    "<script>(function(){var r=document.documentElement,k='wreath-docs-theme';"
    "var s=localStorage.getItem(k);if(s)r.setAttribute('data-theme',s);"
    "var tb=document.getElementById('theme-toggle');"
    # The control is a toggle, so it reports its state rather than only its name.
    "function sync(){var d=r.getAttribute('data-theme')==='dark'||"
    "(!r.getAttribute('data-theme')&&window.matchMedia('(prefers-color-scheme:dark)').matches);"
    "tb.setAttribute('aria-pressed',d?'true':'false');"
    "tb.setAttribute('aria-label',d?'Switch to light theme':'Switch to dark theme');}"
    "if(tb){sync();tb.addEventListener('click',function(){"
    "var d=r.getAttribute('data-theme')==='dark'?'light':'dark';"
    "r.setAttribute('data-theme',d);localStorage.setItem(k,d);sync();});}"
    # Mobile nav drawer: hamburger + scrim toggle body.nav-open.
    "var mb=document.getElementById('menu-toggle'),sc=document.getElementById('nav-scrim');"
    "function closeNav(){document.body.classList.remove('nav-open');"
    "if(mb)mb.setAttribute('aria-expanded','false');}"
    "if(mb)mb.addEventListener('click',function(){"
    "var o=document.body.classList.toggle('nav-open');"
    "mb.setAttribute('aria-expanded',o?'true':'false');});"
    "if(sc)sc.addEventListener('click',closeNav);"
    "document.addEventListener('keydown',function(e){if(e.key==='Escape')closeNav();});"
    "document.querySelectorAll('nav.side a').forEach(function(a){"
    "a.addEventListener('click',closeNav);});"
    # Content tabs: clicking a label activates its label + matching panel.
    "document.querySelectorAll('[data-tabs]').forEach(function(g){"
    "var labels=g.querySelectorAll('.tab-label'),panels=g.querySelectorAll('.tab-panel');"
    "labels.forEach(function(l,i){l.addEventListener('click',function(){"
    "labels.forEach(function(x){x.classList.remove('active');});"
    "panels.forEach(function(x){x.classList.remove('active');});"
    "l.classList.add('active');panels[i].classList.add('active');});});});"
    # Client-side search over the build-time JSON index (no library).
    "var si=document.getElementById('docs-search'),sb=document.getElementById('docs-results');"
    "if(si){var idx=null,rt=si.getAttribute('data-root')||'';"
    "function esc(s){var e=document.createElement('div');e.textContent=s;return e.innerHTML;}"
    "function load(cb){if(idx){cb();return;}fetch(rt+'assets/search-index.json')"
    ".then(function(r){return r.json();}).then(function(d){idx=d;cb();}).catch(function(){});}"
    "si.addEventListener('input',function(){var q=si.value.trim().toLowerCase();"
    "if(!q){sb.style.display='none';sb.innerHTML='';return;}load(function(){var h=[];"
    "for(var i=0;i<idx.length&&h.length<20;i++){var p=idx[i];var hh=null;"
    "for(var j=0;j<p.h.length;j++){if(p.h[j].t.toLowerCase().indexOf(q)>=0){hh=p.h[j];break;}}"
    "if(p.t.toLowerCase().indexOf(q)>=0||hh||p.b.toLowerCase().indexOf(q)>=0){"
    "h.push({u:rt+p.u+(hh?'#'+hh.s:''),t:p.t,c:hh?hh.t:''});}}"
    "sb.innerHTML=h.map(function(x){return '<a href=\"'+esc(x.u)+'\"><span class=\"r-title\">'"
    "+esc(x.t)+'</span>'+(x.c?' <span class=\"r-ctx\">'+esc(x.c)+'</span>':'')+'</a>';}).join('');"
    "sb.style.display=h.length?'block':'none';});});"
    "document.addEventListener('click',function(e){if(!si.parentNode.contains(e.target))"
    "sb.style.display='none';});}"
    # Copy-to-clipboard button on every code block.
    "document.querySelectorAll('pre').forEach(function(pre){var b=document.createElement('button');"
    "b.className='copy-btn';b.type='button';b.textContent='copy';"
    "b.setAttribute('aria-label','Copy code to clipboard');"
    "b.addEventListener('click',function(){"
    "var c=pre.querySelector('code');if(!c)return;navigator.clipboard.writeText(c.textContent)"
    ".then(function(){b.textContent='copied';b.classList.add('copied');"
    "setTimeout(function(){b.textContent='copy';b.classList.remove('copied');},1200);});});"
    "pre.appendChild(b);});"
    # Scroll-spy: highlight the current section in the on-this-page TOC.
    "var tl={};document.querySelectorAll('aside.toc a[href^=\"#\"]').forEach(function(a){"
    "tl[a.getAttribute('href').slice(1)]=a;});"
    "var hs=document.querySelectorAll('main h2[id],main h3[id]');"
    "if(hs.length&&'IntersectionObserver' in window){var ob=new IntersectionObserver(function(es){"
    "es.forEach(function(e){if(!e.isIntersecting)return;var l=tl[e.target.id];if(!l)return;"
    "for(var k in tl)tl[k].classList.remove('toc-active');l.classList.add('toc-active');});},"
    "{rootMargin:'0px 0px -70% 0px'});hs.forEach(function(h){ob.observe(h);});}"
    "})();</script>"
)


#: Surface "feels" — how the theme's surfaces are treated (radius, borders,
#: shadows, texture), composable with any colour palette. Each is a CSS block
#: that overrides the surface variables and adds a flourish or two.
FEELS: dict[str, str] = {
    "flat": ":root{--shadow-1:none;--shadow-2:none;}",
    "elevated": "",
    "papery":
        ":root{--radius:5px;--border-width:1px;"
        "--shadow-1:0 1px 3px rgba(80,60,20,.14);"
        "--surface-image:url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'"
        "%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' "
        "numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' "
        "filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E\");}"
        "main h1,main h2,main h3{font-family:" + _SERIF + ";letter-spacing:.01em;}",
    "hardcore":
        ":root{--radius:0px;--border-width:2px;--shadow-1:none;--shadow-2:none;--shadow-3:none;}"
        "header.site{border-bottom-width:2px;}"
        "a{text-decoration:underline;text-underline-offset:2px;}"
        "main h1,main h2{text-transform:uppercase;letter-spacing:var(--track-caps);}"
        "button.theme,#docs-search,.copy-btn,.table-wrap{border-radius:0;}",
    "orby":
        ":root{--radius:16px;--shadow-1:0 2px 10px rgba(0,0,0,.08);"
        "--shadow-2:0 10px 34px rgba(0,0,0,.14);}"
        "button.theme,#docs-search,.tab-label,.copy-btn{border-radius:999px;}"
        "#docs-results{border-radius:16px;}"
        "header.site{backdrop-filter:saturate(1.2) blur(2px);}",
}


def _root_blocks(palette: Palette, feel: str) -> str:
    """The `:root` custom-property blocks — light, OS-dark, and forced-dark."""
    font = _SERIF if palette.font == "serif" else _SANS
    light, dark = _colour_tokens(palette)
    base = (
        f"{light}{_syntax(True)}{_TYPE}{_TRACK}{_LEADING}{_SPACE}{_LAYOUT}"
        f"--radius:{palette.radius};--radius-sm:calc({palette.radius} / 2);"
        f"--font:{font};--font-mono:{_MONO};"
        "--border-width:1px;--surface-image:none;"
    )
    dark_block = dark + _syntax(False)
    return (
        f":root{{{base}}}"
        f"@media (prefers-color-scheme: dark){{:root:not([data-theme=light]){{{dark_block}}}}}"
        f":root[data-theme=dark]{{{dark_block}}}"
        + FEELS.get(feel, "")
    )


def critical_css(palette: Palette, feel: str = "flat") -> str:
    """Tokens plus the paint an unstyled first frame needs. Inlined per page.

    Kept small deliberately: ``wreath audit`` caps an un-nonced inline asset at
    16 KiB, and every byte here is paid once per page rather than once per site.
    Components belong in :func:`stylesheet`.
    """
    return _root_blocks(palette, feel) + _CRITICAL_CSS


def stylesheet(palette: Palette, feel: str = "flat") -> str:
    """The full stylesheet for ``palette`` + ``feel`` — written to assets/docs.css.

    Standalone on purpose: it re-declares the tokens so the file works on its own
    for anyone linking it directly, and so a page keeps its theme if the inline
    block is ever stripped by a sanitiser.
    """
    return critical_css(palette, feel) + _COMPONENT_CSS


def page(
    *, site_name: str, page_title: str, content: str, nav_html: str, toc_html: str,
    css_href: str, palette: Palette, search_root: str = "", description: str = "",
    footer: str = "", home_href: str = "index.html", feel: str = "flat",
) -> str:
    """Assemble one full HTML document (no external requests)."""
    title = f"{page_title} · {site_name}" if page_title else site_name
    meta_desc = f'<meta name="description" content="{_e(description)}">' if description else ""
    toc = (f'<aside class="toc" aria-label="On this page">{toc_html}</aside>'
           if toc_html else "")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title>{meta_desc}"
        f"<style>{critical_css(palette, feel)}</style>"
        f'<link rel="stylesheet" href="{_e(css_href)}"></head><body>'
        '<a class="skip" href="#content">Skip to content</a>'
        '<header class="site">'
        '<button class="menu-btn" id="menu-toggle" type="button" aria-expanded="false"'
        ' aria-controls="site-nav" aria-label="Show navigation">☰</button>'
        f'<a class="brand" href="{_e(home_href)}">'
        f'<span class="mark" aria-hidden="true"></span>{_e(site_name)}</a>'
        '<span class="spacer"></span>'
        '<div class="search"><input id="docs-search" type="search" placeholder="Search…" '
        f'aria-label="Search documentation" data-root="{_e(search_root)}" autocomplete="off">'
        # Not a listbox: its children are links, and `role=listbox` promises
        # `role=option` children that a link cannot be. A polite live region
        # announces the result count without lying about the widget.
        '<div id="docs-results" aria-live="polite"></div></div>'
        '<button class="theme" id="theme-toggle" type="button" aria-pressed="false"'
        ' aria-label="Switch to dark theme">☾</button></header>'
        '<div class="scrim" id="nav-scrim"></div>'
        '<div class="layout">'
        f'<nav class="side" id="site-nav" aria-label="Documentation">{nav_html}</nav>'
        f'<main id="content">{content}{footer}</main>'
        f"{toc}"
        "</div>" + _TOGGLE_JS + "</body></html>"
    )


def _e(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


__all__ = ["FEELS", "critical_css", "page", "stylesheet"]
