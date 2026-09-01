"""The built-in theme: one self-contained HTML document per page.

No CDN, no web fonts, no JS framework — three system font stacks and a blob of
CSS driven by custom properties, with a theme control that also honours the OS
preference.

**The frame is woven; the content is plain.** `AGENTS.md` states the brand law
as *"the brand may be poetic; the API must stay literal"*, and this theme is that
law rendered as design. Character lives in the chrome — the ring mark, the serif
page titles, the traced nav thread, the mono structural layer. The reading
column stays disciplined: one measure, one rhythm, full-contrast body text, and
nothing decorative between the reader and the words. A docs site is 95% prose
somebody has to read for an hour, so the boldness budget is spent on the frame.

**Three type voices, one job each.** `_SERIF` carries display (page titles,
section heads) — the poetic register. `_SANS` carries body copy — the
neutral one. `_MONO` carries *structure*: eyebrows, nav section labels, the
table-of-contents head, table headers, admonition titles, keyboard hints, code.
That last one is the deliberate move; setting the structural chrome in mono is
what makes the hierarchy legible without a single extra rule or box, and it is
the literal register the brand law asks for.

**Everything is a token.** There are four scales (`type`,
tracking, space, elevation) declared once in `critical_css`, and every rule
below spends them rather than inventing a number. If you find yourself typing a
raw `rem` into a component rule, the scale is missing a step; add the step.

**Two stylesheets, on purpose.** `critical_css` is the design tokens plus
enough paint to make an unstyled flash look intentional, and it is *inlined* into
every page. `stylesheet` is the whole thing and is written once to
`assets/docs.css`, which the browser caches across a 129-page site. Inlining
only the tokens costs ~2 KiB per page and buys two things: a page whose colours
survive a missing stylesheet, and — less obviously — an auditable one.
`wreath audit`'s contrast, non-text-contrast, and focus rules only read inline
`<style>`, so while the whole theme lived in an external file those three rules
silently never ran on a single built page.
"""

from __future__ import annotations

from .config import Palette
from .repo import RepoInfo, compact
from .scripts import BOOT

_SANS = (
    'ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", '
    '"Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
)
#: `ui-serif` resolves to New York on Apple platforms and Cambria on Windows —
#: both genuinely good text faces — with Charter and Georgia as the floor. No
#: web font is downloaded, so the display voice costs nothing and never flashes.
_SERIF = (
    'ui-serif, "New York", Charter, "Bitstream Charter", "Iowan Old Style", '
    'Cambria, "Palatino Linotype", Georgia, serif'
)
_MONO = (
    'ui-monospace, SFMono-Regular, "SF Mono", "Cascadia Mono", "JetBrains Mono", '
    'Menlo, Consolas, "Liberation Mono", monospace'
)

_FACES = {"system": _SANS, "sans": _SANS, "serif": _SERIF, "mono": _MONO}

#: Type scale — 1rem base, rounded to whole pixels at a 16px root so nothing
#: lands on a half-pixel and blurs. Three steps below the base carry the UI
#: chrome; four above carry the headings. `--text-3xl` is larger than a sans
#: scale would want because the display face is set at regular weight: at 40px
#: a serif carries a page title on size alone and never needs faux bold.
_TYPE = (
    "--text-2xs:.6875rem;"  # 11px — eyebrows, kbd, nav section labels
    "--text-xs:.75rem;"  # 12px — captions, copy button, code meta
    "--text-sm:.8125rem;"  # 13px — TOC, tabs, table cells
    "--text-ui:.875rem;"  # 14px — sidebar links, search results
    "--text-base:1rem;"  # 16px — body
    "--text-lg:1.125rem;"  # 18px — lead paragraph, h4
    "--text-xl:1.375rem;"  # 22px — h3
    "--text-2xl:1.75rem;"  # 28px — h2
    "--text-3xl:2.5rem;"  # 40px — h1
    "--text-4xl:3.5rem;"  # 56px — a hero headline, and only that
)

#: Optical tracking. Type tightens as it grows and opens as it shrinks: at 40px
#: default spacing reads loose and the word shape falls apart, at 11px it reads
#: cramped. These are the corrections, not decoration.
_TRACK = (
    "--track-4xl:-.022em;--track-3xl:-.018em;--track-2xl:-.014em;--track-xl:-.01em;"
    "--track-base:0;--track-caps:.085em;"
)

#: Line height by role. Prose wants air (1.7 over a 73ch measure), headings want
#: none (1.15 — a two-line h1 with body leading looks like two headings), and UI
#: chrome sits between.
_LEADING = "--leading-tight:1.15;--leading-snug:1.45;--leading-normal:1.7;"

#: Space scale — 4px base, doubling from --space-4. Every margin, padding, and
#: gap in the theme is one of these eight values.
_SPACE = (
    "--space-1:.25rem;--space-2:.5rem;--space-3:.75rem;--space-4:1rem;"
    "--space-5:1.5rem;--space-6:2rem;--space-7:3rem;--space-8:4rem;"
)

_LAYOUT = "--measure:73ch;--sidebar-w:16rem;--toc-w:14rem;--bar-h:3.25rem;--header-h:var(--bar-h);"


def _colour_tokens(palette: Palette) -> tuple[str, str]:
    """The light and dark colour roles for `palette`.

    Beyond the obvious ones: `--surface-2` gives hover and inset states somewhere
    to live that is not "the code background", and `--border-strong` separates a
    decorative hairline from the boundary of a control. WCAG 1.4.11 asks for 3:1
    on the latter and says nothing about the former — using one token for both
    forces a choice between a cage and a violation. `--border-strong` resolves to
    the muted text colour, which every theme already holds above 4.5:1.

    `--primary` and `--accent` are *fills* (the mark, chart bars, rules), and a
    fill tuned to read on paper disappears on a near-black ground. Both get a
    dark override, derived by lightening when the palette does not name one.
    """
    light_link = palette.link or palette.primary
    # A heavy brand primary reads as too-dark body links and is often below AA
    # on a dark surface, so dark links lighten the brand rather than inherit it.
    dark_link = palette.dark_link or f"color-mix(in oklab, {palette.primary} 45%, #ffffff)"
    dark_primary = palette.dark_primary or f"color-mix(in oklab, {palette.primary} 62%, #ffffff)"
    dark_accent = palette.dark_accent or f"color-mix(in oklab, {palette.accent} 62%, #ffffff)"
    light = (
        f"--primary:{palette.primary};--accent:{palette.accent};"
        f"--bg:{palette.bg};--surface:{palette.surface};"
        f"--surface-2:color-mix(in oklab, {palette.fg} 5%, {palette.bg});"
        f"--fg:{palette.fg};--fg-muted:{palette.muted};"
        f"--fg-subtle:color-mix(in oklab, {palette.muted} 72%, {palette.bg});"
        f"--border:{palette.border};--border-strong:{palette.muted};"
        f"--link:{light_link};"
        f"--tint:color-mix(in oklab, {light_link} 9%, transparent);"
        f"--tint-strong:color-mix(in oklab, {light_link} 16%, transparent);"
        # A translucent header over scrolling content: the blur does the work
        # where it is supported, the alpha keeps it readable where it is not.
        f"--bar-bg:color-mix(in oklab, {palette.bg} 82%, transparent);"
        # Shadows on a light surface are the shadow; on a dark one they mostly
        # vanish, so the dark set below leans on a lighter border instead.
        "--shadow-1:0 1px 2px rgba(0,0,0,.04),0 1px 3px rgba(0,0,0,.06);"
        "--shadow-2:0 4px 12px rgba(0,0,0,.08);"
        "--shadow-3:0 16px 48px rgba(0,0,0,.18);"
    )
    dark = (
        f"--primary:{dark_primary};--accent:{dark_accent};"
        f"--bg:{palette.dark_bg};--surface:{palette.dark_surface};"
        f"--surface-2:color-mix(in oklab, {palette.dark_fg} 8%, {palette.dark_bg});"
        f"--fg:{palette.dark_fg};--fg-muted:{palette.dark_muted};"
        f"--fg-subtle:color-mix(in oklab, {palette.dark_muted} 72%, {palette.dark_bg});"
        f"--border:{palette.dark_border};--border-strong:{palette.dark_muted};"
        f"--link:{dark_link};"
        f"--tint:color-mix(in oklab, {dark_link} 13%, transparent);"
        f"--tint-strong:color-mix(in oklab, {dark_link} 22%, transparent);"
        f"--bar-bg:color-mix(in oklab, {palette.dark_bg} 82%, transparent);"
        "--shadow-1:0 1px 2px rgba(0,0,0,.4);"
        "--shadow-2:0 4px 14px rgba(0,0,0,.5);"
        "--shadow-3:0 20px 56px rgba(0,0,0,.66);"
    )
    return light, dark


#: Syntax colours: a tuned hue per token, tinted into whatever theme is active.
#:
#: These were seven fixed GitHub hexes, so four of the five themes showed
#: GitHub's red-and-blue inside their own code blocks — a navy string literal on
#: sepia's warm paper is the loudest wrong note in the old theme.
#:
#: Deriving them from `--primary`/`--accent` alone was tried first and does not
#: work: there are only two brand hues, and a bright accent (nord's pale blue)
#: cannot be darkened enough for a *light* surface without collapsing into the
#: body colour — measured at 2.5:1 for nord, well under AA. Real light-mode
#: themes solve this the same way, with dark tuned hues; GitHub's own light
#: string colour is near-black navy.
#:
#: So: six hues chosen for legibility, each mixed 78% toward `--fg` so it sits in
#: the theme rather than on top of it. Measured floor across all five themes and
#: both modes is 5.2:1 against the code surface (AA wants 4.5), and the hues stay
#: distinguishable from each other, which pure derivation could not manage.
_TINT = 78  # % of the tuned hue; the rest is --fg


def _syntax(light: bool) -> str:
    hues = (
        {
            "keyword": "#b02a5b",
            "string": "#0a6b3d",
            "number": "#0b5fa5",
            "builtin": "#6b3fc0",
            "operator": "#b5390d",
            "variable": "#8a4b06",
        }
        if light
        else {
            "keyword": "#ff8098",
            "string": "#7ee787",
            "number": "#79c0ff",
            "builtin": "#d2a8ff",
            "operator": "#ffab70",
            "variable": "#ffc857",
        }
    )
    return (
        "".join(
            f"--tok-{name}:color-mix(in oklab, {hue} {_TINT}%, var(--fg));"
            for name, hue in hues.items()
        )
        + "--tok-comment:var(--fg-subtle);"
    )


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
a code{color:inherit;}
::selection{background:var(--tint-strong);}
:focus-visible{outline:2px solid var(--link);outline-offset:2px;
 border-radius:var(--radius-sm);}
/* There is deliberately no `:focus:not(:focus-visible)` outline reset here.
   Every current browser already withholds the ring for pointer interaction, so
   the reset only suppressed a ring nobody saw — and a blanket suppression is
   one edit away from being why a keyboard user cannot tell where they are.
   One focus treatment, applied everywhere, is also simply easier to trust. */
img{max-width:100%;height:auto;}
svg{display:block;}

/* Skip link: the first thing a keyboard lands on, invisible until it matters. */
.skip{position:absolute;left:var(--space-2);top:-4rem;z-index:80;
 background:var(--bg);color:var(--link);border:1px solid var(--border-strong);
 border-radius:var(--radius-sm);padding:var(--space-2) var(--space-3);
 font-size:var(--text-ui);transition:top .15s;}
.skip:focus{top:var(--space-2);}

/* --- header ---------------------------------------------------------------- */
header.site{position:sticky;top:0;z-index:40;border-bottom:1px solid var(--border);
 background:var(--bar-bg);backdrop-filter:saturate(1.6) blur(12px);
 -webkit-backdrop-filter:saturate(1.6) blur(12px);}
.bar{display:flex;align-items:center;gap:var(--space-3);height:var(--bar-h);
 padding:0 var(--space-5);max-width:100rem;margin:0 auto;}
.brand{display:flex;align-items:center;gap:var(--space-2);color:var(--fg);
 font-family:var(--font-display);font-size:var(--text-lg);font-weight:700;
 letter-spacing:var(--track-xl);}
.brand .mark{width:1.5rem;height:1.5rem;flex:none;color:var(--link);}
.browse{display:inline-flex;align-items:center;height:2rem;padding:0 var(--space-3);
 border:1px solid var(--border-strong);border-radius:var(--radius-sm);
 font-family:var(--font-mono);font-size:var(--text-sm);font-weight:700;
 color:var(--fg);text-decoration:none;letter-spacing:.01em;}
.browse:hover{border-color:var(--link);background:var(--surface-2);}
.browse:focus-visible{outline:2px solid var(--link);outline-offset:2px;}
.bar .spacer{flex:1;}
/* The links menu. Declared with the header rather than with the components so
   it does not arrive a frame late and shove the theme control sideways. */
.more{position:relative;flex:none;}
.more>summary{width:2rem;height:2rem;display:inline-flex;align-items:center;
 justify-content:center;cursor:pointer;list-style:none;border-radius:var(--radius-sm);
 border:1px solid transparent;color:var(--fg-muted);
 transition:background .14s,color .14s,border-color .14s;}
.more>summary::-webkit-details-marker{display:none;}
.more>summary:hover{background:var(--surface-2);color:var(--fg);}
.more>summary:focus-visible{outline:2px solid var(--link);outline-offset:2px;}
.more[open]>summary{background:var(--surface-2);color:var(--fg);
 border-color:var(--border-strong);}
.more>summary svg{width:1.125rem;height:1.125rem;}
.more-menu{position:absolute;top:calc(100% + var(--space-2));right:0;z-index:70;
 min-width:14rem;padding:var(--space-2);background:var(--bg);
 border:1px solid var(--border-strong);border-radius:var(--radius);
 box-shadow:var(--shadow-3);}
/* Every row carries its name. An icon that needs a tooltip was not carrying
   its meaning, which is what made three unlabelled glyphs read as strange. */
.more-item{display:flex;align-items:center;gap:var(--space-3);
 padding:var(--space-2) var(--space-3);border-radius:var(--radius-sm);
 color:var(--fg-muted);font-size:var(--text-ui);line-height:var(--leading-snug);
 text-decoration:none;}
.more-item:hover{background:var(--surface-2);color:var(--fg);}
.more-item:focus-visible{outline:2px solid var(--link);outline-offset:-2px;}
.more-item svg{width:1.125rem;height:1.125rem;flex:none;}

/* --- layout ---------------------------------------------------------------- */
/* The measure lives on the *column*, not on `main`. Capping `main` inside a
   `1fr` track leaves the track's leftover width as a hole between the content
   and the table of contents; sizing the track to the measure and centring the
   packed tracks puts that space in the margins where it belongs. */
.layout{display:grid;
 grid-template-columns:var(--sidebar-w) minmax(0,var(--measure)) var(--toc-w);
 justify-content:center;gap:var(--space-7);max-width:100rem;margin:0 auto;
 padding:var(--space-6) var(--space-5) var(--space-8);}
main{min-width:0;}
/* A page outside any nav section has no sidebar to render. Placing `main` in
   the second track anyway keeps the reading column at exactly the same x as on
   every other page -- which is what stops the column jumping under instant
   navigation -- where letting it flow into the empty first track would squeeze
   it to the sidebar's width and push the contents page into the middle. */
.layout.no-side>main{grid-column:2;}

/* --- prose ----------------------------------------------------------------- */
/* One rhythm rule instead of per-element margins that fight each other: every
   sibling gets the same top gap, and the headings widen it below. */
.prose>*+*{margin-top:var(--space-4);}
.prose h1,.prose h2,.prose h3{font-family:var(--font-display);
 line-height:var(--leading-tight);
 scroll-margin-top:calc(var(--header-h) + var(--space-4));}
.prose h1{font-size:var(--text-3xl);letter-spacing:var(--track-3xl);font-weight:400;}
.prose h2{font-size:var(--text-2xl);letter-spacing:var(--track-2xl);font-weight:700;}
.prose h3{font-size:var(--text-xl);letter-spacing:var(--track-xl);font-weight:700;}
/* h4 drops out of the display voice into the structural one — at the fourth
   level a label reads better than a smaller heading. */
.prose h4{font-family:var(--font-mono);font-size:var(--text-2xs);font-weight:700;
 text-transform:uppercase;letter-spacing:var(--track-caps);color:var(--fg-muted);
 line-height:var(--leading-snug);
 scroll-margin-top:calc(var(--header-h) + var(--space-4));}
.prose>h2{margin-top:var(--space-7);}
.prose>h3{margin-top:var(--space-6);}
.prose>h4{margin-top:var(--space-5);}
.prose>h1+*,.prose>h2+*,.prose>h3+*,.prose>h4+*{margin-top:var(--space-3);}
/* The lead: one step up, straight after the title. Costs one rule and does the
   work an author would otherwise do with a bold first sentence. */
.prose>h1+p{font-size:var(--text-lg);line-height:1.6;margin-top:var(--space-4);}
.prose>ul,.prose>ol{padding-left:var(--space-5);}
li+li{margin-top:var(--space-1);}
li>ul,li>ol{margin-top:var(--space-1);padding-left:var(--space-5);}
/* Prose links are underlined by default. Colour alone is not a distinguishing
   signal (WCAG 1.4.1) and hover-only underlines never help a reader who is
   scanning rather than pointing; the hairline weight keeps it quiet. */
.prose p a:not(.hero-action),.prose li a,.prose td a,
.prose blockquote a{text-decoration:underline;
 text-decoration-thickness:1px;text-underline-offset:.16em;
 text-decoration-color:color-mix(in oklab, var(--link) 38%, transparent);
 transition:text-decoration-color .12s;}
.prose p a:not(.hero-action):hover,.prose li a:hover,.prose td a:hover,
.prose blockquote a:hover{text-decoration-color:var(--link);}
:target{scroll-margin-top:calc(var(--header-h) + var(--space-4));}
"""

#: Everything below the fold or component-shaped. External and cached.
_COMPONENT_CSS = """
/* --- controls -------------------------------------------------------------- */
.icon-btn{border:1px solid transparent;background:transparent;color:var(--fg-muted);
 border-radius:var(--radius-sm);cursor:pointer;font:inherit;width:2rem;height:2rem;
 display:inline-flex;align-items:center;justify-content:center;flex:none;
 transition:background .14s,color .14s;}
.icon-btn:hover{background:var(--surface-2);color:var(--fg);}
.icon-btn svg{width:1.125rem;height:1.125rem;}
#menu-toggle{display:none;}
.theme svg{display:none;}
.theme[data-mode=system] .i-auto,.theme[data-mode=light] .i-sun,
.theme[data-mode=dark] .i-moon{display:block;}
kbd{font-family:var(--font-mono);font-size:var(--text-2xs);line-height:1;
 padding:.25em .4em;border:1px solid var(--border);border-bottom-width:2px;
 border-radius:4px;background:var(--surface);color:var(--fg-muted);
 white-space:nowrap;}

/* --- search trigger -------------------------------------------------------- */
/* A button that reads as a field. It is not an input: there is nothing to type
   into until the palette opens, and a decorative input is a keyboard trap that
   promises typing it cannot do. */
.search-open{display:inline-flex;align-items:center;gap:var(--space-2);
 border:1px solid var(--border-strong);background:var(--bg);color:var(--fg-muted);
 border-radius:var(--radius-sm);padding:0 var(--space-2) 0 var(--space-3);
 height:2rem;cursor:pointer;font:inherit;font-size:var(--text-ui);width:15rem;
 transition:border-color .14s,color .14s;}
.search-open:hover{border-color:var(--link);color:var(--fg);}
.search-open svg{width:1rem;height:1rem;flex:none;}
.search-open .label{flex:1;text-align:left;}
html.no-js .search-open{display:none;}

/* --- repository link ------------------------------------------------------- */
/* The name and the counts stack: the name is the link's subject, the counts are
   a footnote to it, and side-by-side they read as three separate controls. */
/* The sidebar names the section it is showing, and the name is the way back to
   that section's own landing page -- a cookbook recipe had no visible route to
   the cookbook index, only a nav entry called "Overview" with nothing saying
   what it was the overview of. */
.side-head{display:block;margin:0 0 var(--space-4);padding-bottom:var(--space-3);
 border-bottom:1px solid var(--border);font-family:var(--font-mono);
 font-size:var(--text-2xs);font-weight:700;text-transform:uppercase;
 letter-spacing:var(--track-caps);color:var(--fg-muted);text-decoration:none;
 transition:color .14s;}
.side-head:hover{color:var(--fg);}
.side-head:focus-visible{outline:2px solid var(--link);outline-offset:2px;}

.repo{line-height:var(--leading-snug);text-decoration:none;}
.repo-name{font-family:var(--font-mono);font-size:var(--text-sm);
 white-space:nowrap;max-width:12rem;overflow:hidden;text-overflow:ellipsis;}
/* Muted, not subtle: a star count is information at 11px, and `--fg-subtle` is
   the token that is deliberately below AA because nothing reads it for content. */
.repo-stats{display:flex;gap:var(--space-2);color:var(--fg-muted);
 font-size:var(--text-2xs);}
.repo-stats .stat{display:inline-flex;align-items:center;gap:.25em;}
.repo-stats svg{width:.75rem;height:.75rem;}
/* Name over counts in one column, so the mark centres against the pair. */
.repo-text{display:flex;flex-direction:column;min-width:0;}

/* --- section switcher ------------------------------------------------------ */
/* Replaces a row of twelve tabs. It occupies one slot of the bar at every
   width, so there is no second row to fit and nothing scrolls sideways. */
.sections{position:relative;flex:none;min-width:0;}
.sections>summary{display:inline-flex;align-items:center;gap:var(--space-2);
 height:2rem;padding:0 var(--space-2) 0 var(--space-3);cursor:pointer;
 border:1px solid transparent;border-radius:var(--radius-sm);
 font-family:var(--font-mono);font-size:var(--text-sm);color:var(--fg);
 white-space:nowrap;min-width:0;list-style:none;
 transition:border-color .14s,background .14s;}
.sections>summary::-webkit-details-marker{display:none;}
.sections>summary:hover{border-color:var(--border-strong);background:var(--surface-2);}
.sections>summary:focus-visible{outline:2px solid var(--link);outline-offset:2px;}
.sections-here{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.sections .chev{width:1rem;height:1rem;flex:none;color:var(--fg-subtle);
 transition:transform .16s;}
.sections[open] .chev{transform:rotate(180deg);}
.sections[open]>summary{border-color:var(--border-strong);background:var(--surface-2);}
.sections-menu{position:absolute;top:calc(100% + var(--space-2));left:0;z-index:70;
 min-width:15rem;max-width:min(22rem, calc(100vw - var(--space-5) * 2));
 max-height:min(28rem, calc(100vh - var(--bar-h) - var(--space-6)));overflow-y:auto;
 padding:var(--space-2);background:var(--bg);border:1px solid var(--border-strong);
 border-radius:var(--radius);box-shadow:var(--shadow-3);}
.sections-menu a{display:block;padding:var(--space-2) var(--space-3);
 border-radius:var(--radius-sm);border-left:2px solid transparent;
 color:var(--fg-muted);font-size:var(--text-ui);line-height:var(--leading-snug);}
.sections-menu a:hover{background:var(--surface-2);color:var(--fg);}
.sections-menu a.active{color:var(--fg);border-left-color:var(--link);
 background:var(--surface-2);}
.sections-menu a:focus-visible{outline:2px solid var(--link);outline-offset:-2px;}

/* --- sidebar: the thread --------------------------------------------------- */
/* The signature. One continuous stroke runs from the section root down to the
   page you are on, so "you are here" carries its whole ancestry instead of
   being a highlight with no context. Off-path branches keep the same rail in
   the hairline colour, so the thread is a change of state, not of structure. */
nav.side{font-size:var(--text-ui);line-height:var(--leading-snug);align-self:start;
 position:sticky;top:calc(var(--header-h) + var(--space-5));
 max-height:calc(100vh - var(--header-h) - var(--space-7));overflow-y:auto;
 overscroll-behavior:contain;padding-right:var(--space-2);
 scrollbar-width:thin;scrollbar-color:var(--border) transparent;}
nav.side::-webkit-scrollbar{width:9px;}
nav.side::-webkit-scrollbar-thumb{background:var(--border);border-radius:5px;
 border:2px solid var(--bg);}
nav.side::-webkit-scrollbar-thumb:hover{background:var(--fg-muted);}
.lvl{display:flex;flex-direction:column;}
.lvl:not(.lvl-0){border-left:1px solid var(--border);margin-left:var(--space-2);
 padding-left:var(--space-3);transition:border-color .18s;}
.lvl.on-path{border-left-color:var(--link);box-shadow:inset 1px 0 0 var(--link);}
.nav-page{position:relative;color:var(--fg-muted);padding:var(--space-1) 0;
 transition:color .12s;}
.nav-page:hover{color:var(--fg);}
.nav-page.active{color:var(--link);font-weight:600;}
/* The terminal node: a dot sitting on the thread, centred over the 1px rail. */
.lvl:not(.lvl-0)>.nav-page.active::before{content:"";position:absolute;
 left:calc(var(--space-3) * -1 - 3px);top:calc(50% - 2.5px);width:5px;height:5px;
 border-radius:50%;background:var(--link);}
.sec>summary{list-style:none;cursor:pointer;display:flex;align-items:center;
 gap:var(--space-2);color:var(--fg);padding:var(--space-1) 0;user-select:none;}
.sec>summary::-webkit-details-marker{display:none;}
.sec>summary::before{content:"";width:.34em;height:.34em;flex:none;margin-left:.15em;
 border-right:1.5px solid var(--fg-subtle);border-bottom:1.5px solid var(--fg-subtle);
 transform:rotate(-45deg);transition:transform .18s;}
.sec[open]>summary::before{transform:rotate(45deg);}
.sec>summary:hover{color:var(--link);}
/* Depth is carried by the voice: the top level of the sidebar is a structural
   label in mono, everything under it is a page-shaped title in the body face. */
.sec-0>summary{font-family:var(--font-mono);font-size:var(--text-2xs);
 text-transform:uppercase;letter-spacing:var(--track-caps);color:var(--fg-muted);
 font-weight:700;margin-top:var(--space-4);}
.lvl-0>.sec-0:first-child>summary{margin-top:0;}
.sec-1>summary,.sec-2>summary{font-weight:600;}

/* --- on this page ---------------------------------------------------------- */
aside.toc{font-size:var(--text-sm);line-height:var(--leading-snug);align-self:start;
 position:sticky;top:calc(var(--header-h) + var(--space-5));
 max-height:calc(100vh - var(--header-h) - var(--space-7));overflow-y:auto;
 scrollbar-width:thin;}
.toc-head{font-family:var(--font-mono);font-size:var(--text-2xs);font-weight:700;
 text-transform:uppercase;letter-spacing:var(--track-caps);color:var(--fg-subtle);
 margin-bottom:var(--space-3);}
/* One continuous rail with a lit segment, not a border per row: per-row borders
   leave hairline gaps at every margin and read as a dashed line by accident. */
.toc-rail{border-left:1px solid var(--border);}
.toc-rail a{display:block;color:var(--fg-muted);padding:var(--space-1) 0
 var(--space-1) var(--space-3);margin-left:-1px;border-left:2px solid transparent;
 transition:color .12s,border-color .12s;}
.toc-rail a:hover{color:var(--fg);}
.toc-rail a.sub{padding-left:var(--space-5);}
.toc-rail a.toc-active{color:var(--link);border-left-color:var(--link);}

/* --- code ------------------------------------------------------------------ */
code{background:color-mix(in oklab, var(--fg) 6%, var(--bg));padding:.1em .3em;
 border-radius:var(--radius-sm);font-size:.875em;font-family:var(--font-mono);}
.code{position:relative;border:var(--border-width) solid var(--border);
 border-radius:var(--radius);background:var(--surface);box-shadow:var(--shadow-1);
 overflow:hidden;}
.code pre{margin:0;background:none;border:none;border-radius:0;box-shadow:none;
 padding:var(--space-4);overflow-x:auto;line-height:var(--leading-snug);
 scrollbar-width:thin;}
pre code{background:none;padding:0;border:none;font-size:var(--text-sm);}
/* The chrome strip: what this block is on the left, what you can do with it on
   the right. Present only when the fence names a language or a title. */
.code-head{display:flex;align-items:center;gap:var(--space-3);
 padding:var(--space-2) var(--space-2) var(--space-2) var(--space-4);
 border-bottom:1px solid var(--border);
 background:color-mix(in oklab, var(--fg) 3%, var(--surface));}
.code-title{font-family:var(--font-mono);font-size:var(--text-xs);color:var(--fg);
 flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.code-lang{font-family:var(--font-mono);font-size:var(--text-2xs);
 text-transform:uppercase;letter-spacing:var(--track-caps);color:var(--fg-subtle);}
.copy-btn{border:1px solid transparent;background:transparent;color:var(--fg-muted);
 border-radius:var(--radius-sm);padding:var(--space-1);cursor:pointer;font:inherit;
 display:inline-flex;align-items:center;transition:color .14s,background .14s;}
.copy-btn svg{width:.95rem;height:.95rem;}
.copy-btn:hover{color:var(--fg);background:var(--surface-2);}
.copy-btn.copied{color:var(--link);}
/* Without a head strip the button floats. Hover reveals it for a mouse; focus
   reveals it for a keyboard — it is a real tab stop, so hiding it on focus too
   would be a control nobody can reach. */
.code>.copy-btn{position:absolute;top:var(--space-2);right:var(--space-2);
 background:var(--bg);border-color:var(--border);opacity:0;transition:opacity .15s;}
.code:hover>.copy-btn,.code>.copy-btn:focus-visible{opacity:1;}
.code:has(>.copy-btn) pre{padding-right:var(--space-8);}
.hl{display:block;margin:0 calc(var(--space-4) * -1);
 padding:0 var(--space-4) 0 calc(var(--space-4) - 2px);
 border-left:2px solid var(--accent);background:var(--tint);}
.tok-comment{color:var(--tok-comment);font-style:italic;}
.tok-string{color:var(--tok-string);}
.tok-number{color:var(--tok-number);}
.tok-keyword{color:var(--tok-keyword);font-weight:600;}
.tok-builtin{color:var(--tok-builtin);}
.tok-variable{color:var(--tok-variable);}
.tok-operator{color:var(--tok-operator);}

/* --- headings: the anchor -------------------------------------------------- */
.anchor{opacity:0;margin-left:var(--space-2);color:var(--fg-subtle);font-weight:400;
 font-family:var(--font-mono);font-size:.7em;text-decoration:none;}
h1:hover .anchor,h2:hover .anchor,h3:hover .anchor,h4:hover .anchor,
.anchor:focus-visible{opacity:1;}
.anchor:hover{color:var(--link);}

/* --- blockquote ------------------------------------------------------------ */
/* No fill and no card. A pull quote that is boxed competes with admonitions,
   which are boxed because they mean something; this one only means "aside". */
blockquote{margin-inline:0;padding-left:var(--space-4);
 border-left:2px solid var(--border-strong);color:var(--fg-muted);}
blockquote>*+*{margin-top:var(--space-3);}

/* --- admonitions ----------------------------------------------------------- */
/* A rule, a mono label, and the faintest wash rather than a saturated card. */
.admonition{--adm:var(--accent);position:relative;padding:var(--space-3)
 var(--space-4) var(--space-4);border-left:2px solid var(--adm);
 border-radius:0 var(--radius) var(--radius) 0;}
.admonition.warning,.admonition.caution,.admonition.attention,.admonition.danger,
.admonition.error,.admonition.bug,.admonition.failure{
 background:color-mix(in oklab, var(--adm) 6%, var(--bg));}
.admonition>*+*{margin-top:var(--space-3);}
.admonition-title{display:flex;align-items:center;gap:var(--space-2);
 font-family:var(--font-mono);font-size:var(--text-2xs);font-weight:700;
 text-transform:uppercase;letter-spacing:var(--track-caps);
 color:color-mix(in oklab, var(--adm) 30%, var(--fg));line-height:var(--leading-snug);}
/* A Unicode glyph rather than an icon font or an SVG sprite: it costs no bytes,
   needs no network, and still says *which kind* of callout this is — which a
   plain coloured rule does not. The glyph per kind is set below. */
.admonition-title::before{content:var(--adm-glyph,"\\24D8");color:var(--adm);
 font-size:1.25em;line-height:1;flex:none;}
details.admonition>summary{list-style:none;cursor:pointer;}
details.admonition>summary::-webkit-details-marker{display:none;}
details.admonition>summary::after{content:"+";font-family:var(--font-mono);
 color:var(--adm);margin-left:auto;font-weight:700;font-size:1.2em;}
details.admonition[open]>summary::after{content:"\\2212";}
details.admonition[open]>summary{margin-bottom:var(--space-3);}
.admonition.note,.admonition.info,.admonition.important{--adm:#2563eb;}
.admonition.tip,.admonition.success,.admonition.check,.admonition.hint
{--adm:#059669;--adm-glyph:"\\2713";}
.admonition.warning,.admonition.caution,.admonition.attention
{--adm:#b45309;--adm-glyph:"\\26A0";}
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

/* --- tabs ------------------------------------------------------------------ */
/* CSS-only, driven by a radio group. The previous version was JS-only with
   `display:none` panels, so with JavaScript off *every tab but the first was
   unreachable content* — and the labels were buttons with no tab semantics.
   A radio group is keyboard-navigable natively (arrow keys, one tab stop) and
   needs no script at all. */
.tabbed{border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;}
.tabbed>input{position:absolute;opacity:0;width:0;height:0;}
.tab-labels{display:flex;gap:var(--space-1);background:var(--surface);
 border-bottom:1px solid var(--border);padding:var(--space-1) var(--space-2) 0;
 overflow-x:auto;scrollbar-width:none;}
.tab-label{font-family:var(--font-mono);font-size:var(--text-sm);
 color:var(--fg-muted);padding:var(--space-2) var(--space-3);cursor:pointer;
 border-bottom:2px solid transparent;white-space:nowrap;
 transition:color .12s,border-color .12s;}
.tab-label:hover{color:var(--fg);}
.tab-panel{display:none;padding:var(--space-4);}
.tab-panel>*+*{margin-top:var(--space-3);}

/* --- tables ---------------------------------------------------------------- */
/* The wrapper is what stops a wide table scrolling the whole page sideways on
   a phone; the table itself only knows how to be as wide as its content. */
.table-wrap{overflow-x:auto;scrollbar-width:thin;
 border:1px solid var(--border);border-radius:var(--radius);}
table{border-collapse:collapse;width:100%;font-size:var(--text-sm);
 font-variant-numeric:tabular-nums;}
th,td{padding:var(--space-2) var(--space-4);text-align:left;
 border-bottom:1px solid var(--border);}
/* Horizontal rules only. A full 1px grid on every cell is the spreadsheet look
   and it fights the hairline weight used everywhere else. */
thead th{background:var(--surface);font-family:var(--font-mono);
 font-size:var(--text-2xs);font-weight:700;text-transform:uppercase;
 letter-spacing:var(--track-caps);color:var(--fg-muted);white-space:nowrap;}
tbody tr:last-child td{border-bottom:none;}
tbody tr:hover{background:var(--surface-2);}

/* --- page footer ----------------------------------------------------------- */
.page-nav{display:flex;justify-content:space-between;gap:var(--space-4);
 margin-top:var(--space-8);padding-top:var(--space-5);
 border-top:1px solid var(--border);}
.page-nav a{flex:1;max-width:49%;padding:var(--space-3) var(--space-4);
 border:1px solid var(--border);border-radius:var(--radius);color:var(--fg);
 transition:border-color .15s,background .15s;}
.page-nav a:hover{border-color:var(--link);background:var(--surface-2);}
.page-nav .dir{display:block;font-family:var(--font-mono);font-size:var(--text-2xs);
 text-transform:uppercase;letter-spacing:var(--track-caps);color:var(--fg-subtle);
 margin-bottom:var(--space-1);}
.page-nav .title{font-family:var(--font-display);font-size:var(--text-lg);
 line-height:var(--leading-snug);}
.page-nav .nav-next{text-align:right;margin-left:auto;}
.page-meta{margin-top:var(--space-5);font-family:var(--font-mono);
 font-size:var(--text-xs);color:var(--fg-subtle);}
.page-meta a{color:var(--fg-muted);}
.page-meta a:hover{color:var(--link);}

/* --- search palette -------------------------------------------------------- */
/* A native <dialog>: focus containment, Escape, and inertness of the page
   behind it are all browser behaviour rather than a script to get wrong. */
/* Positioned rather than margin-centred so it lands in the same place whether
   the browser opened it modally or fell back to a plain `open` attribute. */
dialog.palette{position:fixed;inset:12vh 0 auto 0;margin-inline:auto;
 border:1px solid var(--border);border-radius:var(--radius);
 background:var(--bg);color:var(--fg);box-shadow:var(--shadow-3);padding:0;
 width:min(40rem,92vw);max-height:min(30rem,72vh);
 overflow:hidden;flex-direction:column;}
dialog.palette[open]{display:flex;}
dialog.palette::backdrop{background:color-mix(in oklab, #000 44%, transparent);
 backdrop-filter:blur(2px);}
dialog.palette[open]{animation:pop .14s ease-out;}
@keyframes pop{from{opacity:0;transform:translateY(-6px) scale(.99);}}
.palette-bar{display:flex;align-items:center;gap:var(--space-3);
 padding:var(--space-3) var(--space-4);border-bottom:1px solid var(--border);}
.palette-bar svg{width:1.1rem;height:1.1rem;color:var(--fg-subtle);flex:none;}
#docs-search{flex:1;min-width:0;border:none;background:none;color:var(--fg);
 font:inherit;font-size:var(--text-lg);outline:none;}
#docs-search::placeholder{color:var(--fg-subtle);}
#docs-search::-webkit-search-cancel-button{display:none;}
.palette-results{overflow-y:auto;padding:var(--space-2);scrollbar-width:thin;}
.palette-results .group{font-family:var(--font-mono);font-size:var(--text-2xs);
 font-weight:700;text-transform:uppercase;letter-spacing:var(--track-caps);
 color:var(--fg-subtle);padding:var(--space-3) var(--space-2) var(--space-1);
 display:flex;gap:var(--space-2);align-items:baseline;}
/* Where the page sits in the nav. Un-bolded: it qualifies the page name, and
   two equally loud labels on one line read as two separate groups. */
.palette-results .group .in{font-weight:400;text-transform:none;
 letter-spacing:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.palette-results a{display:block;padding:var(--space-2) var(--space-3);
 border-radius:var(--radius-sm);color:var(--fg);border-left:2px solid transparent;}
.palette-results a:hover,.palette-results a:focus-visible{background:var(--surface-2);
 border-left-color:var(--link);outline:none;}
.palette-results .r-title{display:block;font-weight:600;}
.palette-results .r-ctx{display:block;color:var(--fg-muted);font-size:var(--text-sm);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.palette-results mark{background:var(--tint-strong);color:inherit;
 border-radius:2px;padding:0 1px;}
.palette-empty{padding:var(--space-6) var(--space-4);text-align:center;
 color:var(--fg-muted);font-size:var(--text-ui);}
.palette-hint{display:flex;gap:var(--space-3);align-items:center;
 padding:var(--space-2) var(--space-4);border-top:1px solid var(--border);
 background:var(--surface);font-size:var(--text-2xs);color:var(--fg-subtle);}

/* --- back to top ----------------------------------------------------------- */
.to-top{position:fixed;right:var(--space-5);bottom:var(--space-5);z-index:30;
 width:2.5rem;height:2.5rem;border-radius:50%;border:1px solid var(--border-strong);
 background:var(--bg);color:var(--fg-muted);cursor:pointer;box-shadow:var(--shadow-2);
 display:flex;align-items:center;justify-content:center;
 opacity:0;visibility:hidden;transform:translateY(6px);
 transition:opacity .18s,transform .18s,visibility .18s,color .14s;}
.to-top svg{width:1.05rem;height:1.05rem;}
.to-top.on{opacity:1;visibility:visible;transform:none;}
.to-top:hover{color:var(--link);border-color:var(--link);}

/* --- hero -------------------------------------------------------------------- */
/* The one place a docs page is allowed to be loud. It earns that by being rare:
   two pages in the whole site open with one, and the band is built from the same
   three voices as everything else -- mono eyebrow, display headline, body lede.
   No panel, no fill, no gradient; the space around it does the work. */
.hero{position:relative;isolation:isolate;overflow:hidden;
 padding:var(--hero-pad,var(--space-6) 0 var(--space-7));
 border:1px solid var(--hero-border,var(--border));
 border-width:var(--hero-border-width,0 0 1px);border-radius:var(--hero-radius,0);
 background:var(--hero-bg,transparent);box-shadow:var(--hero-shadow,none);
 margin-bottom:var(--space-7);}
.hero::before,.hero::after{content:"";position:absolute;z-index:-1;
 border-radius:50%;pointer-events:none;opacity:var(--hero-glow,0);filter:blur(2px);}
.hero::before{width:24rem;height:24rem;right:-10rem;top:-13rem;
 background:radial-gradient(circle,var(--tint-strong),transparent 68%);}
.hero::after{width:19rem;height:19rem;right:5rem;bottom:-15rem;
 background:radial-gradient(circle,
 color-mix(in oklab,var(--accent) 18%,transparent),transparent 70%);}
.hero-signals{display:flex;flex-wrap:wrap;gap:var(--space-2);margin-top:var(--space-5);}
.hero-signals span{padding:.3rem .65rem;border:1px solid var(--border);
 border-radius:999px;background:color-mix(in oklab,var(--bg) 72%,transparent);
 color:var(--fg-muted);font-family:var(--font-mono);font-size:var(--text-2xs);
 letter-spacing:.02em;}
.layout.no-side .hero.wide,.layout.no-side .story-grid.wide{width:min(80rem,calc(100vw - 3rem));
 margin-left:50%;transform:translateX(-50%);}

/* --- the dependency plate -------------------------------------------------- */
/* The home page's opener. A herbarium plate: a small-caps caption, the
   specimen, and a determination line under it. The specimen here is the list
   of packages the reader no longer installs, set dense enough that it reads as
   engraving hatch at arm's length and as their own requirements.txt up close.
   That double reading is the whole idea, and it is why the names are set small
   rather than large -- a list this long set big is a wall, set small it is a
   texture with an argument inside it. */
.plate{padding:var(--space-6) 0 var(--space-7);margin-bottom:var(--space-7);
 border-bottom:1px solid var(--border);}
.plate-caption{margin:0 0 var(--space-4);font-family:var(--font-mono);
 font-size:var(--text-2xs);font-weight:700;text-transform:uppercase;
 letter-spacing:var(--track-caps);color:var(--fg-muted);}
.plate-title{margin:0;font-family:var(--font-display);font-weight:400;
 font-size:clamp(2rem, 5vw, var(--text-4xl));line-height:1.05;
 letter-spacing:var(--track-4xl);text-wrap:balance;}
.plate-lede{margin:var(--space-5) 0 0;font-size:var(--text-lg);
 line-height:1.55;color:var(--fg-muted);max-width:54ch;}

/* The specimen. `column-width` rather than a fixed count, so the block reflows
   from five columns to one without a breakpoint for each step. */
.plate-names{margin:var(--space-6) 0 0;padding:0;list-style:none;
 column-width:8.5rem;column-gap:var(--space-5);
 font-family:var(--font-mono);font-size:var(--text-2xs);
 line-height:1.9;color:var(--fg-subtle);
 /* A band, not a wall. Five columns hold the whole list on a laptop and the
    cap never engages; two columns on a phone would run to some 1600px of
    names before the reader reached a sentence, which is the same
    small-viewport failure this redesign exists to fix. Faded rather than cut,
    because a hard edge reads as a bug and a fade reads as hatch continuing
    past the plate -- and the count below states the total either way, so
    nothing is hidden that is not also said. */
 max-height:24rem;overflow:hidden;
 -webkit-mask-image:linear-gradient(to bottom, #000 82%, transparent 100%);
 mask-image:linear-gradient(to bottom, #000 82%, transparent 100%);
 /* The rules top and bottom are the plate's edges. Hairlines, because an
    engraving is made of them and a heavier border would read as a card. */
 border-top:1px solid var(--border);border-bottom:1px solid var(--border);
 padding-block:var(--space-4);}
.plate-names li{break-inside:avoid;}
/* The strike is a rule through the name, not a decoration on it: one pixel, in
   the ink colour rather than the text colour, so the name greys out and the
   line stays crisp. `line-through` and not `<del>` styling, because these were
   never in the document -- they are things the reader will not add. */
.plate-names li{text-decoration:line-through;text-decoration-thickness:1px;
 text-decoration-color:color-mix(in oklab, var(--fg) 42%, transparent);}
.plate-count{margin:var(--space-4) 0 0;font-family:var(--font-mono);
 font-size:var(--text-xs);color:var(--fg-muted);max-width:54ch;
 line-height:var(--leading-snug);}
.plate-count strong{color:var(--fg);font-weight:700;}
.plate-actions{display:flex;flex-wrap:wrap;gap:var(--space-3);
 margin:var(--space-6) 0 0;}
.plate-action{display:inline-flex;align-items:center;gap:var(--space-2);
 font-family:var(--font-mono);font-size:var(--text-sm);color:var(--fg);
 border:1px solid var(--border-strong);border-radius:var(--radius-sm);
 padding:var(--space-2) var(--space-4);
 transition:border-color .14s,background .14s,color .14s;}
.plate-action::after{content:"\\2192";color:var(--fg-subtle);transition:color .14s;}
.plate-action:hover{border-color:var(--link);color:var(--link);
 background:var(--tint);text-decoration:none;}
.plate-action:hover::after{color:var(--link);}
.plate-action.primary{border-color:var(--link);color:var(--link);}
@media (max-width:46rem){
 .plate-names{column-width:7rem;column-gap:var(--space-4);}
}
.hero-eyebrow{margin:0 0 var(--space-4);font-family:var(--font-mono);
 font-size:var(--text-2xs);font-weight:700;text-transform:uppercase;
 letter-spacing:var(--track-caps);color:var(--fg-muted);}
.hero-title{margin:0;font-family:var(--font-display);font-weight:400;
 font-size:clamp(2.25rem, 5.5vw, var(--text-4xl));
 line-height:1.05;letter-spacing:var(--track-4xl);text-wrap:balance;}
.hero-lede{margin:var(--space-5) 0 0;font-size:var(--text-lg);line-height:1.55;
 color:var(--fg-muted);max-width:52ch;}
.hero-actions{display:flex;flex-wrap:wrap;gap:var(--space-3);
 margin:var(--space-6) 0 0;}
.hero-action{display:inline-flex;align-items:center;gap:var(--space-2);
 font-family:var(--font-mono);font-size:var(--text-sm);color:var(--fg);
 border:1px solid var(--border-strong);border-radius:var(--radius-sm);
 padding:var(--space-2) var(--space-4);
 transition:border-color .14s,background .14s,color .14s;}
.hero-action::after{content:"\\2192";color:var(--fg-subtle);transition:color .14s;}
.hero-action:hover{border-color:var(--link);color:var(--link);
 background:var(--tint);text-decoration:none;}
.hero-action:hover::after{color:var(--link);}
.hero-action.primary{border-color:var(--link);color:var(--link);}

/* --- story cards ---------------------------------------------------------- */
.story-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
 gap:var(--space-4);margin:var(--space-7) 0;}
.story-card{position:relative;display:flex;min-height:17rem;flex-direction:column;
 padding:var(--space-5);overflow:hidden;color:var(--fg);text-decoration:none;
 border:1px solid var(--border);border-radius:var(--radius);
 background:var(--card-bg,var(--bg));box-shadow:var(--card-shadow,var(--shadow-1));
 transition:transform .2s ease,border-color .2s ease,box-shadow .2s ease;}
.story-card::before{content:"";position:absolute;width:12rem;height:12rem;
 right:-7rem;top:-7rem;border-radius:50%;opacity:var(--card-glow,0);
 background:radial-gradient(circle,var(--tint-strong),transparent 68%);
 transition:transform .25s ease,opacity .25s ease;}
.story-card:hover{transform:translateY(-3px);border-color:var(--link);
 box-shadow:var(--card-hover-shadow,var(--shadow-2));}
.story-card:hover::before{transform:scale(1.2);opacity:1;}
.story-card h2{margin:var(--space-6) 0 0;font-family:var(--font-display);
 font-size:var(--text-xl);line-height:1.15;letter-spacing:var(--track-xl);}
.story-card p{margin:var(--space-3) 0 var(--space-6);color:var(--fg-muted);
 line-height:1.55;font-size:var(--text-ui);}
.story-index,.story-meta{font-family:var(--font-mono);font-size:var(--text-2xs);
 text-transform:uppercase;letter-spacing:var(--track-caps);color:var(--fg-subtle);}
.story-meta{margin-top:auto;color:var(--fg-muted);}
.story-arrow{position:absolute;right:var(--space-5);top:var(--space-5);
 color:var(--link);font-size:var(--text-lg);transition:transform .2s ease;}
.story-card:hover .story-arrow{transform:translate(2px,-2px);}
@media (max-width:44rem){
 .story-grid{grid-template-columns:1fr;}
 .story-card{min-height:14rem;}
 .layout.no-side .hero.wide,.layout.no-side .story-grid.wide{width:100%;
  margin-left:0;transform:none;}
}

/* --- explanatory figures ---------------------------------------------------- */
/* Every figure runs one 8s timeline shared by both halves of its comparison, so
   the difference is something you watch rather than something a caption claims.
   The conventional structure borrows the charts' muted "field" treatment; only
   wreath's half gets the brand colour. */
.fig{margin-inline:0;border:var(--border-width) solid var(--border);
 border-radius:var(--radius);background:var(--bg);overflow:hidden;}
.fig-head{display:flex;align-items:center;gap:var(--space-3);
 padding:var(--space-2) var(--space-3) var(--space-2) var(--space-4);
 border-bottom:1px solid var(--border);
 background:color-mix(in oklab, var(--fg) 3%, var(--bg));}
.fig-title{flex:1;font-family:var(--font-mono);font-size:var(--text-2xs);
 font-weight:700;text-transform:uppercase;letter-spacing:var(--track-caps);
 color:var(--fg-muted);}
/* A checkbox, not a script: WCAG 2.3.2 wants a way to stop anything that moves
   for more than five seconds, and a control that only exists once a runtime has
   loaded is not one. */
.fig-pause{position:absolute;opacity:0;width:0;height:0;}
.fig-pause-label{font-family:var(--font-mono);font-size:var(--text-2xs);
 color:var(--fg-muted);border:1px solid var(--border-strong);
 border-radius:var(--radius-sm);padding:var(--space-1) var(--space-2);
 cursor:pointer;white-space:nowrap;transition:color .14s,border-color .14s;}
.fig-pause-label::before{content:"\\23F8  ";}
.fig-pause:checked+.fig-pause-label{color:var(--link);border-color:var(--link);}
.fig-pause:checked+.fig-pause-label::before{content:"\\25B6  ";}
.fig-pause:focus-visible+.fig-pause-label{outline:2px solid var(--link);
 outline-offset:2px;}
.fig:has(.fig-pause:checked) .fig-body *{animation-play-state:paused !important;}
.fig-body{padding:var(--space-4);overflow-x:auto;color:var(--fg);}
.fig-note{margin:0;padding:0 var(--space-4) var(--space-4);
 font-size:var(--text-xs);color:var(--fg-subtle);}

/* Figure type: the same three voices as the page. Labels take the body face,
   everything measured or literal takes mono. */
.f-label{font-family:var(--font-mono);font-size:11px;font-weight:700;
 text-transform:uppercase;letter-spacing:var(--track-caps);fill:var(--fg-muted);}
.f-tick{font-family:var(--font-mono);font-size:11px;fill:var(--fg-subtle);}
.f-side{font-family:var(--font-mono);font-size:9px;letter-spacing:.08em;
 fill:var(--fg-subtle);}
.f-node-t{font-family:var(--font-mono);font-size:9px;fill:var(--fg-muted);}
.f-boundary{stroke:var(--border-strong);stroke-width:1;stroke-dasharray:2 4;}
.f-edge{stroke:var(--border-strong);stroke-width:1;fill:none;}
.f-node{fill:var(--surface);stroke:var(--border-strong);stroke-width:1.2;}
.f-rule{stroke:var(--border-strong);stroke-width:1;}

/* --- figure: the boundary --- */
.f-path{fill:none;stroke-width:1.5;}
.f-path-other{stroke:var(--fg-subtle);stroke-dasharray:3 3;}
.f-path-wreath{stroke:var(--primary);}
.f-dot{offset-rotate:0deg;}
.f-dot-other{fill:var(--fg-muted);animation:f-travel-other 8s linear infinite;}
.f-dot-wreath{fill:var(--primary);animation:f-travel-wreath 8s linear infinite;}
/* Same pixel speed, different distance: wreath's dot arrives at 72% of the
   cycle and waits, which is the whole comparison in one number. */
@keyframes f-travel-other{from{offset-distance:0%;}to{offset-distance:100%;}}
@keyframes f-travel-wreath{0%{offset-distance:0%;}72%{offset-distance:100%;}
 100%{offset-distance:100%;}}
.f-note-in{font-family:var(--font-mono);font-size:10px;font-weight:700;
 fill:var(--primary);opacity:0;animation:f-appear 8s linear infinite;}
@keyframes f-appear{0%,55%{opacity:0;}62%,100%{opacity:1;}}

/* --- figure: routing --- */
.f-box{fill:var(--surface);stroke:var(--border-strong);stroke-width:1.2;}
.f-box-ghost{fill:none;stroke:var(--accent);stroke-dasharray:3 3;}
.f-box-t{font-family:var(--font-mono);font-size:10px;fill:var(--fg);}
.f-brace{fill:none;stroke:var(--accent);stroke-width:1.2;}
.f-tick-warn{fill:var(--accent);}
.f-fold{opacity:0;animation:f-reveal 8s ease-out infinite;
 animation-delay:calc(1s + var(--i) * .45s);}
@keyframes f-reveal{0%{opacity:0;transform:translateY(-3px);}
 4%,92%{opacity:1;transform:none;}99%,100%{opacity:0;}}
.f-route{font-family:var(--font-mono);font-size:10.5px;fill:var(--fg-muted);}
.f-route-hit{fill:var(--primary);font-weight:700;}
/* A cell is one route's bit after one segment test. Off cells stay drawn, so a
   row shows you *when* a route dropped out rather than just that it did. */
.f-cell{fill:none;stroke:var(--border-strong);stroke-width:1;opacity:.45;}
.f-cell-on{fill:var(--fg-muted);stroke:none;opacity:0;
 animation:f-cell-in 8s ease-out infinite;animation-delay:calc(var(--t) * 1s + .6s);}
.f-cell-hit{fill:var(--primary);}
@keyframes f-cell-in{0%{opacity:0;}3%,92%{opacity:.9;}99%,100%{opacity:0;}}

/* --- figure: timers --- */
.f-mark{fill:none;stroke:var(--accent);stroke-width:1.5;stroke-dasharray:3 3;
 opacity:0;animation:f-reveal 8s ease-out infinite;animation-delay:.6s;}
/* One pulse per level of the sift, so the log-n in the caption is something you
   count rather than something you are told. */
.f-sift{animation:f-sift 8s ease-out infinite;animation-delay:calc(var(--t) * 1s);}
@keyframes f-sift{0%,100%{fill:var(--surface);stroke:var(--border-strong);}
 2%{fill:var(--fg-muted);stroke:var(--fg-muted);}
 9%{fill:var(--surface);stroke:var(--border-strong);}}
.f-pip{opacity:.2;animation:f-pip 8s steps(1,end) infinite;
 animation-delay:calc(var(--t) * 1s);}
.f-pip-other{fill:var(--fg-muted);}
.f-pip-wreath{fill:var(--primary);}
@keyframes f-pip{0%{opacity:.2;}2%,94%{opacity:1;}99%,100%{opacity:.2;}}
.f-compact{fill:none;stroke:var(--fg-subtle);stroke-width:1;stroke-dasharray:3 3;
 opacity:0;animation:f-compact 8s ease-in-out infinite;}
@keyframes f-compact{0%,58%{opacity:0;}66%,80%{opacity:.85;}88%,100%{opacity:0;}}
.f-compact-t{opacity:0;animation:f-compact 8s ease-in-out infinite;}
/* The mark's own broken stroke, scaled to r=58: the wheel is a circular buffer
   and the mark is a circle of separate things held in one shape. */
.f-ring{fill:none;stroke:var(--border);stroke-width:2;stroke-linecap:round;
 stroke-dasharray:101 27 61 27 88 61;}
.f-slot-tick{stroke:var(--border-strong);stroke-width:1;}
.f-timer{fill:var(--primary);opacity:.85;}
.f-hand{stroke:var(--accent);stroke-width:2;stroke-linecap:round;
 transform-origin:466px 132px;animation:f-sweep 8s linear infinite;}
@keyframes f-sweep{from{transform:rotate(0deg);}to{transform:rotate(360deg);}}
.f-hub{fill:var(--accent);}
.f-callout{fill:none;stroke:var(--border-strong);stroke-width:1;stroke-dasharray:2 3;}
.f-link{stroke:var(--border-strong);stroke-width:1.5;}
/* The link that closes over the departed node: drawn from nothing, at the same
   instant the node leaves. That splice is the entire cost of a cancel. */
.f-link-closed{stroke:var(--primary);stroke-dasharray:84;stroke-dashoffset:84;
 animation:f-splice 8s ease-out infinite;animation-delay:1s;}
@keyframes f-splice{0%{stroke-dashoffset:84;}3%,94%{stroke-dashoffset:0;}
 99%,100%{stroke-dashoffset:84;}}
.f-chain{fill:var(--surface);stroke:var(--border-strong);stroke-width:1.5;}
.f-chain-gone{fill:var(--primary);stroke:var(--primary);
 animation:f-unlink 8s ease-out infinite;animation-delay:1s;}
@keyframes f-unlink{0%{opacity:1;transform:none;}
 4%{opacity:0;transform:translateY(-14px);}
 94%{opacity:0;transform:translateY(-14px);}99%,100%{opacity:1;transform:none;}}

/* --- misc ------------------------------------------------------------------ */
hr{border:none;border-top:1px solid var(--border);margin-block:var(--space-7);}
figure.chart{margin-inline:0;padding:var(--space-4);
 border:var(--border-width) solid var(--border);border-radius:var(--radius);
 background:var(--bg);color:var(--fg);overflow-x:auto;}
.chart-title{font-family:var(--font-mono);font-size:var(--text-2xs);font-weight:700;
 text-transform:uppercase;letter-spacing:var(--track-caps);color:var(--fg-muted);
 margin-bottom:var(--space-4);}
.chart-label{font-family:var(--font);font-size:13px;}
.chart-value{font-family:var(--font-mono);font-size:12px;
 font-variant-numeric:tabular-nums;opacity:.85;}
.chart-error{color:var(--fg);background:color-mix(in oklab, #dc2626 10%, var(--bg));
 border:1px solid #dc2626;font-size:var(--text-sm);padding:var(--space-3);
 border-radius:var(--radius);}
li input[type=checkbox]{margin-right:var(--space-2);}
del{color:var(--fg-subtle);}

/* --- responsive ------------------------------------------------------------ */
.scrim{display:none;}
@media (max-width:82rem){
 .layout{grid-template-columns:var(--sidebar-w) minmax(0,var(--measure));
  gap:var(--space-6);}
 aside.toc{display:none;}
}
@media (max-width:62rem){
 #menu-toggle{display:inline-flex;}
 .layout,.layout.no-side{grid-template-columns:1fr;gap:var(--space-5);
  padding:var(--space-5) var(--space-4) var(--space-7);}
 .layout.no-side>main{grid-column:auto;}   /* one track again; track 2 is implicit */
 nav.side{position:fixed;top:0;left:0;bottom:0;width:19rem;max-width:84vw;
  max-height:100vh;z-index:60;background:var(--bg);border-right:1px solid var(--border);
  padding:var(--space-5) var(--space-4);transform:translateX(-100%);
  transition:transform .22s ease;box-shadow:var(--shadow-3);}
 body.nav-open nav.side{transform:none;}
 body.nav-open .scrim{display:block;position:fixed;inset:0;z-index:55;
  background:rgba(0,0,0,.5);}
 .prose h1{font-size:var(--text-2xl);}
 .prose h2{font-size:var(--text-xl);}
 .page-nav{flex-direction:column;}
 .page-nav a{max-width:none;}
}
@media (max-width:46rem){
 .search-open{width:2rem;padding:0;justify-content:center;}
 .search-open .label,.search-open kbd{display:none;}
 .bar{padding:0 var(--space-3);gap:var(--space-2);}
 /* The word beside the mark goes before the section name does. The mark
    identifies the site; the section name is the only way to reach the other
    eleven sections from a phone, because the sidebar shows the section you are
    already in, so losing it would strand a reader in one branch of the tree.
    Hidden with `display:none` on its own span rather than `font-size:0` on the
    link: the link keeps its accessible name from `aria-label`, and a zeroed
    font size is a trick some screen readers read differently from others. */
 .brand-name{display:none;}
 .brand{gap:0;}
 .sections-here{max-width:9rem;}
}
@media (max-width:23rem){
 /* A 320px phone in portrait. The section name truncates rather than the
    control disappearing, so the switcher is still operable. */
 .sections-here{max-width:5.5rem;}
}

/* --- motion ---------------------------------------------------------------- */
/* WCAG 2.3.3. Not "less motion" — none, for anyone who asked. */
@media (prefers-reduced-motion:reduce){
 html{scroll-behavior:auto;}
 *,*::before,*::after{animation-duration:.01ms !important;
  animation-iteration-count:1 !important;transition-duration:.01ms !important;}
 /* Figures carry their argument in elements that animate *in*. With motion off
    they have to be visible, not stuck on whichever keyframe came last. */
 .f-fold,.f-note-in,.f-timer,.f-cell-on,.f-pip,.f-mark{opacity:1 !important;}
 .f-link-closed{stroke-dashoffset:0 !important;}
 .f-chain-gone{opacity:0 !important;}
 .fig-pause-label{display:none;}
 ::view-transition-group(*),::view-transition-old(*),::view-transition-new(*){
  animation:none !important;}
}

/* --- view transitions ------------------------------------------------------ */
/* Instant navigation swaps <main> in place. Cross-fading only the content —
   not the chrome — is what makes it read as "the page updated" rather than
   "something reloaded", and it keeps the sidebar's scroll position visibly
   untouched. */
@view-transition{navigation:none;}
::view-transition-old(content){animation:fade-out .1s ease both;}
::view-transition-new(content){animation:fade-in .16s ease both;}
@keyframes fade-out{to{opacity:0;}}
@keyframes fade-in{from{opacity:0;transform:translateY(4px);}}
main{view-transition-name:content;}

/* --- print ----------------------------------------------------------------- */
@media print{
 header.site,nav.side,aside.toc,.page-nav,.copy-btn,.skip,.scrim,.to-top,
 dialog.palette,.page-meta{display:none !important;}
 .layout,.layout.no-side{display:block;max-width:none;padding:0;}
 main{max-width:none;}
 .code,.admonition,.tabbed,figure.chart,.fig{box-shadow:none;break-inside:avoid;}
 .fig-pause-label{display:none;}
 .tab-panel{display:block !important;}
 a[href^="http"]::after{content:" (" attr(href) ")";font-size:var(--text-xs);
  color:var(--fg-muted);}
}
"""


#: Surface "feels" — how the theme's surfaces are treated (radius, borders,
#: shadows, texture), composable with any colour palette. Each is a CSS block
#: that overrides the surface variables and adds a flourish or two.
FEELS: dict[str, str] = {
    "flat": ":root{--shadow-1:none;--shadow-2:none;}",
    "elevated": "",
    "papery": ":root{--radius:5px;--border-width:1px;"
    "--shadow-1:0 1px 3px rgba(80,60,20,.14);"
    "--surface-image:url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'"
    "%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' "
    "numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' "
    "filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E\");}",
    "hardcore": ":root{--radius:0px;--border-width:2px;--shadow-1:none;--shadow-2:none;"
    "--shadow-3:0 0 0 2px var(--fg);}"
    "header.site{border-bottom-width:2px;}"
    ".prose h1,.prose h2{text-transform:uppercase;letter-spacing:var(--track-caps);}"
    ".icon-btn,.search-open,.copy-btn,.table-wrap,.code,kbd,.to-top{border-radius:0;}"
    ".to-top{border-width:2px;}",
    "orby": ":root{--radius:16px;--shadow-1:0 2px 10px rgba(0,0,0,.08);"
    "--shadow-2:0 10px 34px rgba(0,0,0,.14);}"
    ".search-open,.copy-btn,kbd,.icon-btn{border-radius:999px;}"
    "dialog.palette{border-radius:18px;}"
    "header.site{backdrop-filter:saturate(1.2) blur(6px);}",
    "luminous": ":root{--radius:18px;"
    "--surface-image:radial-gradient(circle at 8% 0%,var(--tint-strong),transparent 34rem),"
    "radial-gradient(circle at 94% 12%,"
    "color-mix(in oklab,var(--accent) 10%,transparent),transparent 30rem);"
    "--hero-pad:var(--space-8) var(--space-7);--hero-border-width:1px;"
    "--hero-radius:calc(var(--radius) * 1.35);"
    "--hero-bg:linear-gradient(135deg,color-mix(in oklab,var(--bg) 90%,transparent),"
    "color-mix(in oklab,var(--surface) 78%,transparent));"
    "--hero-border:color-mix(in oklab,var(--link) 28%,var(--border));"
    "--hero-shadow:var(--shadow-2);--hero-glow:1;"
    "--card-bg:color-mix(in oklab,var(--bg) 88%,transparent);--card-glow:.68;"
    "--card-shadow:0 10px 35px color-mix(in oklab,var(--primary) 8%,transparent);"
    "--card-hover-shadow:0 20px 54px color-mix(in oklab,var(--primary) 15%,transparent);"
    "}header.site{backdrop-filter:saturate(1.4) blur(18px);"
    "-webkit-backdrop-filter:saturate(1.4) blur(18px);}",
}


#: How many tabs one content-tab group can hold. The CSS-only tab group needs
#: one `:nth-of-type` pair per position, so the count is bounded rather than
#: arbitrary — eight is well past any real docs page, and a ninth tab simply
#: stays unselectable rather than breaking the group.
_MAX_TABS = 8


def _tab_rules() -> str:
    """`:nth-of-type` pairs that make the radio group behave like tabs.

    The markup is `.tabbed > input* , .tab-labels , .tab-panel*`: the labels
    box is the first child `div`, so panel *k* is the `(k + 2)`-th one.
    """
    rules = []
    for index in range(_MAX_TABS):
        checked = f".tabbed>input:nth-of-type({index + 1}):checked"
        rules.append(f"{checked}~.tab-panel:nth-of-type({index + 2}){{display:block;}}")
        rules.append(
            f"{checked}~.tab-labels>.tab-label:nth-of-type({index + 1})"
            "{color:var(--link);border-bottom-color:var(--link);}"
        )
        rules.append(
            f".tabbed>input:nth-of-type({index + 1}):focus-visible"
            f"~.tab-labels>.tab-label:nth-of-type({index + 1})"
            "{outline:2px solid var(--link);outline-offset:-2px;}"
        )
    return "".join(rules)


def _root_blocks(palette: Palette, feel: str) -> str:
    """The `:root` custom-property blocks — light, OS-dark, and forced-dark."""
    light, dark = _colour_tokens(palette)
    base = (
        f"{light}{_syntax(True)}{_TYPE}{_TRACK}{_LEADING}{_SPACE}{_LAYOUT}"
        f"--radius:{palette.radius};--radius-sm:calc({palette.radius} / 2);"
        f"--font:{_FACES[palette.font]};--font-display:{_FACES[palette.display]};"
        f"--font-mono:{_MONO};"
        "--border-width:1px;--surface-image:none;"
    )
    dark_block = dark + _syntax(False)
    return (
        f":root{{{base}}}"
        f"@media (prefers-color-scheme: dark){{:root:not([data-theme=light]){{{dark_block}}}}}"
        f":root[data-theme=dark]{{{dark_block}}}" + FEELS.get(feel, "")
    )


def critical_css(palette: Palette, feel: str = "flat") -> str:
    """Tokens plus the paint an unstyled first frame needs. Inlined per page.

    Kept small deliberately: `wreath audit` caps an un-nonced inline asset at
    16 KiB, and every byte here is paid once per page rather than once per site.
    Components belong in `stylesheet`.
    """
    return _root_blocks(palette, feel) + _CRITICAL_CSS


def stylesheet(palette: Palette, feel: str = "flat") -> str:
    """The full stylesheet for `palette` + `feel` — written to assets/docs.css.

    Standalone on purpose: it re-declares the tokens so the file works on its own
    for anyone linking it directly, and so a page keeps its theme if the inline
    block is ever stripped by a sanitiser.
    """
    return critical_css(palette, feel) + _COMPONENT_CSS + _tab_rules()


#: The mark: a ring drawn as one stroke broken into unequal segments — separate
#: things gathered until they hold a single shape, which is what the name means.
#: The segment lengths are irregular on purpose; four equal arcs read as a
#: loading spinner. 54 units is the circumference at r=8.6, so the dash pattern
#: closes exactly rather than overlapping at the seam.
#: The brand glyph: two vines braided into a ring, with leaf ticks and the two
#: flowers the engraving carries. Not a dashed circle -- that is what this used
#: to be, and a dashed circle is a loading spinner in every other product a
#: reader has open. The logo is a botanical engraving, so the mark is the same
#: object drawn small: two counter-phase arcs that cross where a real wreath is
#: bound, six leaves on the outside, two dots for the flowers.
#:
#: Drawn on a 24 unit box at stroke 1.5 so it holds together at the 20px the bar
#: renders it at; anything finer disappeared, and anything heavier read as a
#: doughnut rather than as woven stems.
_MARK = (
    '<svg class="mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<circle cx="12" cy="12" r="8.25" stroke="currentColor" stroke-width="1.4" opacity=".35"/>'
    '<path d="M4.8 8.1c3.1-4.8 11.3-4.8 14.4 0s-1 11.6-7.2 11.6S1.7 12.9 4.8 8.1Z" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    '<path d="M7.2 4.8c4.8-3.1 11.6 1 11.6 7.2s-6.8 10.3-11.6 7.2-4.8-11.3 0-14.4Z" '
    'stroke="currentColor" stroke-width="1.15" stroke-linecap="round" opacity=".68"/>'
    '<circle cx="18.6" cy="8" r="1.55" fill="currentColor"/>'
    '<circle cx="6.1" cy="16.4" r="1.15" fill="currentColor" opacity=".65"/>'
    "</svg>"
)

_ICON_SEARCH = (
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" aria-hidden="true">'
    '<circle cx="8.6" cy="8.6" r="5.4"/><path d="M12.6 12.6 17 17"/></svg>'
)
_ICON_MENU = (
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" aria-hidden="true">'
    '<path d="M3 6h14M3 10h14M3 14h14"/></svg>'
)
_ICON_CHEVRON = (
    '<svg class="chev" viewBox="0 0 20 20" fill="none" '
    'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>'
)
#: The theme control reports its state by *showing* it: three icons in the
#: markup, one revealed per mode by CSS. Swapping them in script would leave the
#: button lying about itself for as long as the runtime takes to arrive.
_ICONS_THEME = (
    '<svg class="i-auto" viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="10" cy="10" r="6.4"/><path d="M10 3.6a6.4 6.4 0 0 1 0 12.8z" '
    'fill="currentColor" stroke="none"/></svg>'
    '<svg class="i-sun" viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" aria-hidden="true">'
    '<circle cx="10" cy="10" r="3.6"/>'
    '<path d="M10 1.8v2.2M10 16v2.2M3.7 3.7l1.6 1.6M14.7 14.7l1.6 1.6'
    'M1.8 10h2.2M16 10h2.2M3.7 16.3l1.6-1.6M14.7 5.3l1.6-1.6"/></svg>'
    '<svg class="i-moon" viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M16.2 12.3A6.8 6.8 0 0 1 7.7 3.8a6.9 6.9 0 1 0 8.5 8.5z"/></svg>'
)
_ICON_MORE = (
    '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">'
    '<circle cx="4.6" cy="10" r="1.5"/><circle cx="10" cy="10" r="1.5"/>'
    '<circle cx="15.4" cy="10" r="1.5"/></svg>'
)
_ICON_TOP = (
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true"><path d="M10 16V4M4.8 9.2 10 4l5.2 5.2"/></svg>'
)

#: One stroked mark per `config.ICONS` name, drawn on the same 20-unit grid and
#: the same 1.7 weight as the rest of the chrome so a header link never reads as
#: a pasted-in logo. `github` and `gitlab` are filled marks because a stroked
#: outline of either is unrecognisable at 18px.
_STROKE = (
    'viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'
)
_FILL = 'viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"'
ICON_MARKS: dict[str, str] = {
    "link": f'<svg {_STROKE}><path d="M8.4 11.6a3.4 3.4 0 0 0 5 .3l2.2-2.2a3.4 3.4 0 0 '
    f'0-4.8-4.8L9.5 6.2"/><path d="M11.6 8.4a3.4 3.4 0 0 0-5-.3l-2.2 2.2a3.4 3.4 '
    f'0 0 0 4.8 4.8l1.3-1.3"/></svg>',
    "home": f'<svg {_STROKE}><path d="M3.2 9.4 10 3.6l6.8 5.8"/>'
    f'<path d="M5 8.6v7.8h10V8.6"/><path d="M8.2 16.4v-4.2h3.6v4.2"/></svg>',
    "github": f'<svg {_FILL}><path d="M10 1.4a8.6 8.6 0 0 0-2.7 16.8c.43.08.59-.19.59-.41'
    f"0-.2-.01-.87-.01-1.58-2.18.4-2.74-.53-2.92-1.02-.1-.25-.53-1.02-.9-1.23"
    f"-.31-.16-.75-.57-.02-.58.69-.01 1.18.63 1.34.9.78 1.32 2.03.95 2.53.72"
    f".08-.57.31-.95.56-1.17-1.94-.22-3.96-.97-3.96-4.31 0-.95.34-1.74.9-2.35"
    f"-.09-.22-.39-1.12.09-2.33 0 0 .73-.23 2.4.9a8.1 8.1 0 0 1 4.36 0c1.66"
    f"-1.13 2.39-.9 2.39-.9.48 1.21.18 2.11.09 2.33.56.61.9 1.39.9 2.35 0 3.35"
    f"-2.03 4.09-3.96 4.31.31.27.59.79.59 1.6 0 1.16-.01 2.09-.01 2.38 0 .23"
    f'.16.5.6.41A8.6 8.6 0 0 0 10 1.4z"/></svg>',
    "gitlab": f'<svg {_FILL}><path d="M10 18.4 6.7 8.2h6.6L10 18.4zM10 18.4 2.2 8.2h4.5'
    f"L10 18.4zM2.2 8.2 1.2 11.4c-.1.3 0 .6.27.8L10 18.4 2.2 8.2zM2.2 8.2h4.5"
    f"L4.8 2.3c-.1-.3-.5-.3-.6 0L2.2 8.2zM10 18.4l3.3-10.2h4.5L10 18.4z"
    f"M17.8 8.2l1 3.2c.1.3 0 .6-.27.8L10 18.4l7.8-10.2zM17.8 8.2h-4.5l1.9-5.9"
    f'c.1-.3.5-.3.6 0l2 5.9z"/></svg>',
    "package": f'<svg {_STROKE}><path d="M10 2.8 16.6 6v8L10 17.2 3.4 14V6z"/>'
    f'<path d="M3.4 6 10 9.2 16.6 6"/><path d="M10 9.2v8"/></svg>',
    "chat": f'<svg {_STROKE}><path d="M16.6 12.2a1.8 1.8 0 0 1-1.8 1.8H7.2L3.4 17V5.4'
    f'a1.8 1.8 0 0 1 1.8-1.8h9.6a1.8 1.8 0 0 1 1.8 1.8z"/></svg>',
    "mail": f'<svg {_STROKE}><path d="M3.2 5.4h13.6v9.2H3.2z"/>'
    f'<path d="m3.2 6 6.8 4.6L16.8 6"/></svg>',
    "rss": f'<svg {_STROKE}><path d="M4.4 4.2a11.4 11.4 0 0 1 11.4 11.4"/>'
    f'<path d="M4.4 9.2a6.4 6.4 0 0 1 6.4 6.4"/>'
    f'<circle cx="4.9" cy="15.1" r="1.1" fill="currentColor" stroke="none"/></svg>',
    "book": f'<svg {_STROKE}><path d="M3.6 4.2h4.2A2.2 2.2 0 0 1 10 6.4v9.4a1.8 1.8 0 0 '
    f'0-1.8-1.8H3.6z"/><path d="M16.4 4.2h-4.2A2.2 2.2 0 0 0 10 6.4v9.4a1.8 1.8 '
    f'0 0 1 1.8-1.8h4.6z"/></svg>',
}

#: Star and fork, drawn only next to a repository link that carries counts.
_ICON_STAR = (
    '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">'
    '<path d="m10 2.8 2.24 4.54 5.01.73-3.62 3.53.85 4.99L10 14.24l-4.48 2.35'
    '.85-4.99L2.75 8.07l5.01-.73z"/></svg>'
)
_ICON_FORK = (
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" aria-hidden="true">'
    '<circle cx="5.6" cy="4.8" r="1.9"/><circle cx="14.4" cy="4.8" r="1.9"/>'
    '<circle cx="10" cy="15.2" r="1.9"/>'
    '<path d="M5.6 6.7v1.1a2.2 2.2 0 0 0 2.2 2.2h4.4a2.2 2.2 0 0 0 2.2-2.2V6.7'
    'M10 10v3.3"/></svg>'
)


def repo_link(info: RepoInfo | None) -> str:
    """The header's repository link, with counts when the build resolved them.

    The counts are two spans of text, not badge images: an `<img>` from
    shields.io would be the one remote request in an otherwise self-contained
    page, and it would be the request that renders *last*, on a service the docs
    do not control."""
    if info is None:
        return ""
    mark = ICON_MARKS.get(info.host or "link", ICON_MARKS["link"])
    stats = ""
    if info.stars > 0 or info.forks > 0:
        stats = (
            f'<span class="stat">{_ICON_STAR}'
            f"<span>{_e(compact(info.stars))}</span></span>"
            f'<span class="stat">{_ICON_FORK}'
            f"<span>{_e(compact(info.forks))}</span></span>"
        )
        label = f"{info.title} on {info.host or 'the web'}: {info.stars} stars, {info.forks} forks"
    else:
        label = f"{info.title} on {info.host or 'the web'}"
    body = (
        f'<span class="repo-name">{_e(info.title)}</span>'
        f"{f'<span class="repo-stats">{stats}</span>' if stats else ''}"
    )
    return (
        f'<a class="more-item repo" href="{_e(info.url)}" rel="noopener noreferrer" '
        f'aria-label="{_e(label)}">{mark}<span class="repo-text">{body}</span></a>'
    )


def link_row(links) -> str:
    """The extra header links (homepage, package page, chat), as menu rows."""
    if not links:
        return ""
    return "".join(
        f'<a class="more-item" href="{_e(link.url)}" rel="noopener noreferrer">'
        f"{ICON_MARKS[link.icon]}<span>{_e(link.label)}</span></a>"
        for link in links
    )


def _more(repo_html: str, links_html: str) -> str:
    """The right-hand controls, behind one disclosure."""
    body = f"{repo_html}{links_html}"
    if not body:
        return ""
    return (
        '<details class="more"><summary aria-label="Links and options">'
        f'{_ICON_MORE}</summary><div class="more-menu">{body}</div></details>'
    )


def page(
    *,
    site_name: str,
    page_title: str,
    content: str,
    nav_html: str,
    toc_html: str,
    css_href: str,
    palette: Palette,
    search_root: str = "",
    description: str = "",
    footer: str = "",
    home_href: str = "index.html",
    feel: str = "flat",
    js_href: str = "assets/docs.js",
    tabs_html: str = "",
    canonical: str = "",
    repo_html: str = "",
    links_html: str = "",
    section_title: str = "",
    section_href: str = "",
    map_href: str = "",
    head_html: str = "",
    extra_stylesheets: tuple[str, ...] = (),
    extra_scripts: tuple[str, ...] = (),
    layout: str = "docs",
) -> str:
    """Assemble one full HTML document (no external requests)."""
    title = f"{page_title} · {site_name}" if page_title else site_name
    meta_desc = f'<meta name="description" content="{_e(description)}">' if description else ""
    link_canonical = f'<link rel="canonical" href="{_e(canonical)}">' if canonical else ""
    toc = (
        f'<aside class="toc" aria-label="On this page">'
        f'<div class="toc-head">On this page</div>{toc_html}</aside>'
        if toc_html
        else ""
    )
    # The sidebar names the section it is showing, and the name is the link back
    # to that section's own landing page. Without it the only route from a
    # cookbook recipe to the cookbook index was a nav entry labelled "Overview",
    # with nothing anywhere on the page saying which section "Overview" belonged
    # to -- the way back existed and did not read as one. It also gives the
    # mobile drawer a heading, which it never had.
    side_head = (
        f'<a class="side-head" href="{_e(section_href)}">{_e(section_title)}</a>'
        if nav_html and section_title and section_href
        else ""
    )
    side = (
        f'<nav class="side" id="site-nav" aria-label="Documentation">{side_head}{nav_html}</nav>'
        if nav_html
        else ""
    )
    sections = (
        f'<details class="sections"><summary aria-label="Switch section">'
        f'<span class="sections-here">{_e(section_title) or "Sections"}</span>'
        f"{_ICON_CHEVRON}</summary>"
        f'<div class="sections-menu">{tabs_html}</div></details>'
        if tabs_html
        else ""
    )
    browse = f'<a class="browse" href="{_e(map_href)}">Browse</a>' if map_href else ""
    layout_class = "layout" if nav_html else "layout no-side"
    documentation = layout == "docs"
    menu = (
        f'<button class="icon-btn" id="menu-toggle" type="button" aria-expanded="false"'
        f' aria-controls="site-nav" aria-label="Show navigation">{_ICON_MENU}</button>'
        if documentation
        else ""
    )
    search_button = (
        f'<button class="search-open" id="search-open" type="button" '
        f'aria-label="Search documentation" aria-haspopup="dialog">{_ICON_SEARCH}'
        '<span class="label">Search</span><kbd>Ctrl K</kbd></button>'
        if documentation
        else ""
    )
    search_dialog = (
        f'<dialog class="palette" id="search-dialog" aria-label="Search documentation">'
        f'<div class="palette-bar">{_ICON_SEARCH}'
        '<input id="docs-search" type="search" placeholder="Search the docs" '
        f'aria-label="Search documentation" data-root="{_e(search_root)}" '
        'autocomplete="off" spellcheck="false"><kbd>esc</kbd></div>'
        '<div class="palette-results" id="docs-results" aria-live="polite"></div>'
        '<div class="palette-hint"><kbd>&uarr;</kbd><kbd>&darr;</kbd> navigate'
        "<kbd>&crarr;</kbd> open<kbd>esc</kbd> close</div></dialog>"
        if documentation
        else ""
    )
    scrim = '<div class="scrim" id="nav-scrim"></div>' if documentation else ""
    stylesheet_links = "".join(
        f'<link rel="stylesheet" href="{_e(href)}">' for href in extra_stylesheets
    )
    script_links = "".join(f'<script src="{_e(src)}" defer></script>' for src in extra_scripts)
    return (
        "<!doctype html>\n"
        '<html lang="en" class="no-js"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title>{meta_desc}{link_canonical}{head_html}"
        f'<meta name="color-scheme" content="light dark">'
        f"<style>{critical_css(palette, feel)}</style>"
        f'<link rel="stylesheet" href="{_e(css_href)}">{stylesheet_links}'
        f"<script>{BOOT}</script></head>"
        "<body>"
        '<a class="skip" href="#content">Skip to content</a>'
        '<header class="site"><div class="bar">'
        f"{menu}"
        f'<a class="brand" href="{_e(home_href)}" aria-label="{_e(site_name)}, home">'
        f'{_MARK}<span class="brand-name">{_e(site_name)}</span></a>'
        f"{browse}"
        f"{sections}"
        '<span class="spacer"></span>'
        f"{search_button}"
        f"{_more(repo_html, links_html)}"
        f'<button class="icon-btn theme" id="theme-toggle" type="button"'
        f' data-mode="system" aria-label="Theme: match system. Switch to light."'
        f">{_ICONS_THEME}</button>"
        f"</div></header>"
        f"{scrim}"
        f'<div class="{layout_class}">{side}'
        f'<main id="content"><article class="prose">{content}</article>{footer}</main>'
        f"{toc}</div>"
        f"{search_dialog}"
        f'<button class="to-top" id="to-top" type="button" aria-label="Back to top">'
        f"{_ICON_TOP}</button>"
        f'<script src="{_e(js_href)}" defer></script>{script_links}'
        "</body></html>"
    )


def _e(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


__all__ = ["FEELS", "critical_css", "page", "stylesheet"]
