"""The built-in theme: one self-contained HTML document per page.

No CDN, no web fonts, no JS framework — system font stack and a small blob of
CSS driven by custom properties, with a light/dark toggle that also honours the
OS preference (the same approach as ``_devtools/bench_report.py``). The palette's
primary/accent come from :class:`~wreath._docs.config.Palette`.
"""

from __future__ import annotations

from .config import Palette

_SANS = ('-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, '
         'Arial, sans-serif')
_SERIF = ('"Iowan Old Style", "Palatino Linotype", Palatino, Georgia, '
          '"Times New Roman", serif')

_STATIC_CSS = """
* { box-sizing: border-box; }
html { scroll-behavior:smooth; }
body { margin:0; background:var(--bg); color:var(--fg); line-height:1.65;
  font-family: var(--font); background-image:var(--surface-image); background-attachment:fixed;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: .15em;
  text-decoration-thickness: 1px; }
a code { color: inherit; }
::selection { background: color-mix(in srgb, var(--primary) 22%, transparent); }
:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; border-radius: 3px; }
:where(a, button, summary, input, [tabindex]):focus:not(:focus-visible) { outline: none; }

/* --- header ------------------------------------------------------------- */
header.site { position:sticky; top:0; z-index:40; display:flex; align-items:center;
  gap:1rem; padding:.7rem 1.4rem; border-bottom:1px solid var(--border);
  background:color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter:saturate(1.4) blur(8px); -webkit-backdrop-filter:saturate(1.4) blur(8px); }
header.site .brand { display:flex; align-items:center; gap:.55rem; font-weight:750;
  color:var(--fg); font-size:1.08rem; letter-spacing:-.01em; }
header.site .brand:hover { text-decoration:none; }
.brand .mark { width:1.35rem; height:1.35rem; border-radius:6px; flex:none;
  background:linear-gradient(135deg, var(--primary), var(--accent)); box-shadow:var(--shadow); }
header.site .spacer { flex:1; }
button.theme, .menu-btn { border:1px solid var(--border); background:transparent; color:var(--fg);
  border-radius:8px; padding:.32rem .6rem; cursor:pointer; font-size:.9rem; line-height:1;
  display:inline-flex; align-items:center; justify-content:center;
  transition:background .15s,border-color .15s; }
button.theme:hover, .menu-btn:hover { background:var(--code-bg); border-color:var(--link); }
.menu-btn { display:none; }

/* --- search ------------------------------------------------------------- */
.search { position:relative; }
#docs-search { border:1px solid var(--border); background:var(--bg); color:var(--fg);
  border-radius:8px; padding:.36rem .7rem; font-size:.85rem; width:12rem;
  transition:width .2s, box-shadow .15s, border-color .15s; }
#docs-search:focus { outline:none; width:14rem; border-color:var(--link);
  box-shadow:0 0 0 3px color-mix(in srgb, var(--primary) 22%, transparent); }
#docs-results { display:none; position:absolute; right:0; top:2.6rem; width:24rem; max-width:80vw;
  max-height:64vh; overflow:auto; background:var(--bg); border:1px solid var(--border);
  border-radius:12px; box-shadow:0 16px 44px rgba(0,0,0,.22); z-index:50; padding:.3rem; }
#docs-results a { display:block; padding:.45rem .6rem; border-radius:8px; color:var(--fg); }
#docs-results a:hover { background:var(--code-bg); text-decoration:none; }
#docs-results .r-title { font-weight:600; }
#docs-results .r-ctx { color:var(--muted); font-size:.85em; }

/* --- layout ------------------------------------------------------------- */
.layout { display:grid; grid-template-columns: 16rem minmax(0,1fr) 14rem;
  gap:2.4rem; max-width:86rem; margin:0 auto; padding:1.6rem 1.4rem; }
nav.side { font-size:.9rem; align-self:start; position:sticky; top:4.2rem;
  max-height:calc(100vh - 5.4rem); overflow-y:auto; overscroll-behavior:contain;
  padding:.2rem .6rem .2rem 0; scrollbar-width:thin; scrollbar-color:var(--border) transparent; }
nav.side::-webkit-scrollbar { width:9px; }
nav.side::-webkit-scrollbar-thumb { background:var(--border); border-radius:5px;
  border:2px solid var(--bg); }
nav.side::-webkit-scrollbar-thumb:hover { background:var(--muted); }
nav.side a { display:block; color:var(--muted); padding:.28rem .55rem; border-radius:7px;
  border-left:2px solid transparent; margin:1px 0; line-height:1.35;
  transition:background .12s, color .12s; }
nav.side a:hover { background:var(--code-bg); color:var(--fg); text-decoration:none; }
nav.side a.active { color:var(--link); font-weight:600; border-left-color:var(--link);
  background:color-mix(in srgb, var(--primary) 12%, var(--bg)); }
nav.side a.toc-active { color:var(--link); font-weight:600; }
details.section { margin:.05rem 0; }
details.section > summary { list-style:none; cursor:pointer; display:flex; align-items:center;
  gap:.4rem; font-weight:650; color:var(--fg); padding:.32rem .5rem; border-radius:7px;
  user-select:none; transition:background .12s; }
details.section > summary::-webkit-details-marker { display:none; }
details.section > summary::before { content:"\\203A"; display:inline-block; width:.7em;
  color:var(--muted); font-weight:800; transition:transform .18s; }
details.section[open] > summary::before { transform:rotate(90deg); }
details.section > summary:hover { background:var(--code-bg); }
/* nested groups get an indent guide */
nav.side details.section details.section,
nav.side details.section > a { margin-left:.5rem; padding-left:.6rem;
  border-left:1px solid var(--border); }
nav.side details.section > a.active { border-left-color:var(--link); }

aside.toc { font-size:.86rem; align-self:start; position:sticky; top:4.2rem;
  max-height:calc(100vh - 5.4rem); overflow-y:auto; scrollbar-width:thin; }
aside.toc strong { display:block; text-transform:uppercase; letter-spacing:.06em;
  font-size:.72rem; color:var(--muted); margin:.2rem 0 .5rem; }
aside.toc a { display:block; color:var(--muted); padding:.16rem 0 .16rem .7rem;
  border-left:2px solid var(--border); transition:color .12s, border-color .12s; }
aside.toc a:hover { color:var(--fg); text-decoration:none; }
aside.toc a.toc-active { color:var(--link); border-left-color:var(--link); font-weight:600; }

/* --- content ------------------------------------------------------------ */
main { min-width:0; }
main h1,main h2,main h3,main h4 { line-height:1.25; letter-spacing:-.01em;
  scroll-margin-top:4.6rem; }
main h1 { font-size:2.1rem; margin:.2rem 0 1rem; padding-bottom:.4rem;
  border-bottom:1px solid var(--border); }
main h2 { font-size:1.5rem; margin:2.4rem 0 .8rem; padding-top:.6rem; }
main h3 { font-size:1.2rem; margin:1.8rem 0 .6rem; }
main p, main li { max-width:44rem; }
main img { max-width:100%; height:auto; border-radius:var(--radius); }
.anchor { opacity:0; margin-left:.4rem; color:var(--muted); font-weight:400; }
h1:hover .anchor, h2:hover .anchor, h3:hover .anchor, h4:hover .anchor { opacity:1; }
code { background:var(--code-bg); padding:.15em .38em; border-radius:5px;
  font-size:.88em; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  border:1px solid color-mix(in srgb, var(--border) 60%, transparent); }
pre { background:var(--code-bg); border:var(--border-width) solid var(--border);
  border-radius:var(--radius); padding:1rem 1.1rem; overflow-x:auto; position:relative;
  box-shadow:var(--shadow); line-height:1.55; }
pre code { background:none; padding:0; border:none; font-size:.86rem; }
.copy-btn { position:absolute; top:.5rem; right:.5rem; border:1px solid var(--border);
  background:var(--bg); color:var(--muted); border-radius:6px; padding:.15rem .5rem;
  font-size:.72rem; cursor:pointer; opacity:0; transition:opacity .15s, color .15s; }
pre:hover .copy-btn { opacity:1; } .copy-btn:hover { color:var(--fg); }
.copy-btn.copied { color:var(--link); border-color:var(--link); }

/* --- prev/next ---------------------------------------------------------- */
.page-nav { display:flex; justify-content:space-between; gap:1rem; margin-top:3.5rem;
  padding-top:1.2rem; border-top:1px solid var(--border); }
.page-nav a { flex:1; max-width:49%; padding:.7rem .9rem; border:1px solid var(--border);
  border-radius:var(--radius); color:var(--fg); transition:border-color .15s, background .15s; }
.page-nav a:hover { border-color:var(--link); background:var(--code-bg); text-decoration:none; }
.page-nav .nav-next { text-align:right; margin-left:auto; }

/* --- syntax tokens ------------------------------------------------------ */
.tok-comment { color:#6a737d; font-style:italic; }
.tok-string { color:#032f62; } .tok-number { color:#005cc5; }
.tok-keyword { color:#d73a49; font-weight:600; } .tok-builtin { color:#6f42c1; }
.tok-variable { color:#e36209; } .tok-operator { color:#d73a49; }
:root[data-theme=dark] .tok-string, :root:not([data-theme=light]) .tok-string { color:#9ecbff; }
:root[data-theme=dark] .tok-number, :root:not([data-theme=light]) .tok-number { color:#79c0ff; }
:root[data-theme=dark] .tok-keyword, :root:not([data-theme=light]) .tok-keyword { color:#ff7b72; }
:root[data-theme=dark] .tok-builtin, :root:not([data-theme=light]) .tok-builtin { color:#d2a8ff; }
:root[data-theme=dark] .tok-comment, :root:not([data-theme=light]) .tok-comment { color:#8b949e; }
:root[data-theme=dark] .tok-variable, :root:not([data-theme=light]) .tok-variable { color:#ffa657; }
:root[data-theme=dark] .tok-operator, :root:not([data-theme=light]) .tok-operator { color:#ff7b72; }

/* --- blockquote --------------------------------------------------------- */
blockquote { margin:1.2rem 0; padding:.6rem 1.1rem; border-left:3px solid var(--accent);
  border-radius:0 var(--radius) var(--radius) 0; color:var(--muted);
  background:color-mix(in srgb, var(--accent) 6%, var(--bg)); }
blockquote > :first-child { margin-top:0; } blockquote > :last-child { margin-bottom:0; }

/* --- admonitions (typed callouts) --------------------------------------- */
.admonition { --adm:var(--accent); position:relative; margin:1.4rem 0;
  padding:.7rem 1rem .8rem 1.1rem;
  border:1px solid color-mix(in srgb, var(--adm) 32%, var(--border));
  border-left:4px solid var(--adm); border-radius:var(--radius); box-shadow:var(--shadow);
  background:var(--code-bg); background:color-mix(in srgb, var(--adm) 7%, var(--bg)); }
.admonition > :last-child { margin-bottom:0; } .admonition > :first-child { margin-top:0; }
.admonition-title { display:flex; align-items:center; gap:.5rem; font-weight:700;
  margin:0 0 .4rem; color:color-mix(in srgb, var(--adm) 45%, var(--fg)); }
.admonition-title::before { content:"\\24D8"; font-size:1.05em; line-height:1; color:var(--adm); }
.admonition.note,.admonition.info,.admonition.important { --adm:#3b82f6; }
.admonition.tip,.admonition.success,.admonition.check,.admonition.hint { --adm:#10b981; }
.admonition.tip .admonition-title::before,.admonition.success .admonition-title::before,
.admonition.check .admonition-title::before,
.admonition.hint .admonition-title::before { content:"\\2713"; }
.admonition.warning,.admonition.caution,.admonition.attention { --adm:#f59e0b; }
.admonition.warning .admonition-title::before,.admonition.caution .admonition-title::before,
.admonition.attention .admonition-title::before { content:"\\26A0"; }
.admonition.danger,.admonition.error,.admonition.bug,.admonition.failure { --adm:#ef4444; }
.admonition.danger .admonition-title::before,.admonition.error .admonition-title::before,
.admonition.bug .admonition-title::before,
.admonition.failure .admonition-title::before { content:"\\2715"; }
.admonition.question,.admonition.example,.admonition.faq { --adm:#8b5cf6; }
.admonition.question .admonition-title::before,.admonition.example .admonition-title::before,
.admonition.faq .admonition-title::before { content:"?"; font-weight:800; }
.admonition.quote,.admonition.abstract,.admonition.summary { --adm:var(--muted); }
.admonition.quote .admonition-title::before,.admonition.abstract .admonition-title::before,
.admonition.summary .admonition-title::before { content:"\\275D"; }

/* --- tabs --------------------------------------------------------------- */
.tabbed { margin:1.4rem 0; border:1px solid var(--border); border-radius:var(--radius);
  overflow:hidden; box-shadow:var(--shadow); }
.tab-labels { display:flex; gap:.2rem; background:var(--code-bg);
  border-bottom:1px solid var(--border); padding:.2rem .2rem 0; }
.tab-label { border:none; background:none; color:var(--muted); padding:.5rem .9rem;
  cursor:pointer; border-bottom:2px solid transparent; font-size:.9rem; font-weight:550;
  border-radius:6px 6px 0 0; transition:color .12s, background .12s; }
.tab-label:hover { color:var(--fg); background:var(--bg); }
.tab-label.active { color:var(--link); border-bottom-color:var(--link);
  background:var(--bg); }
.tab-panel { display:none; padding:.9rem 1rem; } .tab-panel.active { display:block; }
.tab-panel > :first-child { margin-top:0; } .tab-panel > :last-child { margin-bottom:0; }

/* --- misc --------------------------------------------------------------- */
hr { border:none; border-top:1px solid var(--border); margin:2.4rem 0; }
table { border-collapse:collapse; margin:1.4rem 0; font-size:.94rem; }
th,td { border:1px solid var(--border); padding:.5rem .8rem; text-align:left; }
thead th { background:var(--code-bg); font-weight:650; }
tbody tr:nth-child(even) { background:color-mix(in srgb, var(--fg) 3%, var(--bg)); }
figure.chart { margin:1.6rem 0; padding:1.1rem 1.2rem;
  border:var(--border-width) solid var(--border);
  border-radius:var(--radius); background:var(--code-bg); box-shadow:var(--shadow);
  color:var(--fg); overflow-x:auto; }
.chart-error { color:#b91c1c; font-size:.85rem; padding:.5rem; border:1px dashed var(--border);
  border-radius:var(--radius); }
li { margin:.2rem 0; }
li input[type=checkbox] { margin-right:.4rem; }

/* --- responsive / mobile drawer ----------------------------------------- */
.scrim { display:none; }
@media (max-width: 60rem) {
  .menu-btn { display:inline-flex; }
  .layout { grid-template-columns:1fr; gap:1.5rem; }
  aside.toc { display:none; }
  nav.side { position:fixed; top:0; left:0; bottom:0; width:18rem; max-width:82vw;
    max-height:100vh; z-index:60; background:var(--bg); border-right:1px solid var(--border);
    padding:1.2rem 1rem; transform:translateX(-100%); transition:transform .22s ease;
    box-shadow:0 0 44px rgba(0,0,0,.28); }
  body.nav-open nav.side { transform:none; }
  body.nav-open .scrim { display:block; position:fixed; inset:0; z-index:55;
    background:rgba(0,0,0,.45); }
}
@media (max-width: 40rem) {
  #docs-search { width:8.5rem; } #docs-search:focus { width:10rem; }
  main h1 { font-size:1.7rem; }
}
"""

_TOGGLE_JS = (
    "<script>(function(){var r=document.documentElement,k='wreath-docs-theme';"
    "var s=localStorage.getItem(k);if(s)r.setAttribute('data-theme',s);"
    "document.getElementById('theme-toggle').addEventListener('click',function(){"
    "var d=r.getAttribute('data-theme')==='dark'?'light':'dark';"
    "r.setAttribute('data-theme',d);localStorage.setItem(k,d);});"
    # Mobile nav drawer: hamburger + scrim toggle body.nav-open.
    "var mb=document.getElementById('menu-toggle'),sc=document.getElementById('nav-scrim');"
    "function closeNav(){document.body.classList.remove('nav-open');}"
    "if(mb)mb.addEventListener('click',function(){document.body.classList.toggle('nav-open');});"
    "if(sc)sc.addEventListener('click',closeNav);"
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
    "b.className='copy-btn';b.type='button';b.textContent='copy';b.addEventListener('click',function(){"
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
    "flat": "",
    "elevated":
        ":root{--shadow:0 4px 14px rgba(0,0,0,.10);}",
    "papery":
        ":root{--radius:5px;--border-width:1px;"
        "--shadow:0 1px 3px rgba(80,60,20,.14);"
        "--surface-image:url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'"
        "%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' "
        "numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' "
        "filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E\");}"
        "main h1,main h2,main h3{font-family:" + _SERIF + ";letter-spacing:.01em;}",
    "hardcore":
        ":root{--radius:0px;--border-width:2px;--shadow:none;}"
        "header.site{border-bottom-width:2px;}"
        "a{text-decoration:underline;text-underline-offset:2px;}"
        "main h1,main h2{text-transform:uppercase;letter-spacing:.03em;}"
        "button.theme,#docs-search,.copy-btn{border-radius:0;}",
    "orby":
        ":root{--radius:16px;--shadow:0 10px 34px rgba(0,0,0,.14);}"
        "button.theme,#docs-search,.tab-label,.copy-btn{border-radius:999px;}"
        "#docs-results{border-radius:16px;}"
        "header.site{backdrop-filter:saturate(1.2) blur(2px);}",
}


def stylesheet(palette: Palette, feel: str = "flat") -> str:
    """The full stylesheet for ``palette`` + ``feel`` — CSS variables + rules."""
    font = _SERIF if palette.font == "serif" else _SANS
    # Links get their own colour, not the brand fill: a heavy brand primary reads
    # as too-dark body links, and is often below AA contrast on a dark surface.
    light_link = palette.link or palette.primary
    dark_link = palette.dark_link or f"color-mix(in srgb, {palette.primary} 45%, #ffffff)"
    light = (
        f"--primary:{palette.primary};--accent:{palette.accent};--bg:{palette.bg};"
        f"--fg:{palette.fg};--muted:{palette.muted};--border:{palette.border};"
        f"--code-bg:{palette.surface};--sidebar:{palette.surface};"
        f"--link:{light_link};--radius:{palette.radius};--font:{font};"
        "--border-width:1px;--shadow:none;--surface-image:none;"
    )
    dark = (
        f"--bg:{palette.dark_bg};--fg:{palette.dark_fg};--muted:{palette.dark_muted};"
        f"--border:{palette.dark_border};--code-bg:{palette.dark_surface};"
        f"--link:{dark_link};--sidebar:{palette.dark_bg};"
    )
    return (
        f":root{{{light}}}"
        f"@media (prefers-color-scheme: dark){{:root:not([data-theme=light]){{{dark}}}}}"
        f":root[data-theme=dark]{{{dark}}}"
        + _STATIC_CSS + FEELS.get(feel, "")
    )


def page(
    *, site_name: str, page_title: str, content: str, nav_html: str, toc_html: str,
    css_href: str, palette: Palette, search_root: str = "", description: str = "",
    footer: str = "", home_href: str = "index.html",
) -> str:
    """Assemble one full HTML document (no external requests)."""
    title = f"{page_title} · {site_name}" if page_title else site_name
    meta_desc = f'<meta name="description" content="{_e(description)}">' if description else ""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title>{meta_desc}"
        f'<link rel="stylesheet" href="{_e(css_href)}"></head><body>'
        '<header class="site">'
        '<button class="menu-btn" id="menu-toggle" aria-label="Toggle navigation">☰</button>'
        f'<a class="brand" href="{_e(home_href)}"><span class="mark"></span>{_e(site_name)}</a>'
        '<span class="spacer"></span>'
        '<div class="search"><input id="docs-search" type="search" placeholder="Search…" '
        f'aria-label="Search" data-root="{_e(search_root)}" autocomplete="off">'
        '<div id="docs-results" role="listbox"></div></div>'
        '<button class="theme" id="theme-toggle" aria-label="Toggle theme">☾</button></header>'
        '<div class="scrim" id="nav-scrim"></div>'
        '<div class="layout">'
        f'<nav class="side">{nav_html}</nav>'
        f"<main>{content}{footer}</main>"
        f'<aside class="toc">{toc_html}</aside>'
        "</div>" + _TOGGLE_JS + "</body></html>"
    )


def _e(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


__all__ = ["page", "stylesheet"]
