"""Hand-drawn explanatory figures — a ``` ``figure ```` block.

A small, closed registry of diagrams about wreath's own machinery, each emitted
as inline SVG animated with CSS. Not a diagram *language*: these are three
specific arguments, drawn on purpose, in the theme's own colours and type. A
general renderer would be a dependency and would draw worse.

    ```figure
    name: request-boundary
    title: Where a request enters Python
    note: Measured by `wreath-request-trace` against a realistic app.
    ```

Every figure is a **synchronised comparison**: the conventional structure and
wreath's run off one timeline, so the difference is something you watch happen
rather than something the caption asserts. The conventional side is drawn in the
muted "field" treatment the benchmark charts already use; wreath's is drawn in
the brand colour. Nothing here invents a colour or a font — the figures inherit
the same custom properties as the rest of the page, so they recolour with the
theme and work in light and dark.

Motion is pausable without JavaScript (a checkbox and `:has()`), and the theme's
`prefers-reduced-motion` rule stops it outright for anyone who asked. WCAG 2.2.2
wants a mechanism to pause anything that moves for more than five seconds; these
loop indefinitely, so it is not optional.
"""

from __future__ import annotations

from hashlib import sha256

from . import _fenced

__all__ = ["FIGURES", "extract", "restore"]

_OPEN = "```figure"


def extract(text: str) -> tuple[str, dict[str, str]]:
    """Replace each ```figure block with a token; return (text, {token: html})."""
    return _fenced.extract(text, _OPEN, lambda body: _render(_parse(body)), "FIGURE")


restore = _fenced.restore


def _parse(config: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in config:
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _render(config: dict[str, str]) -> str:
    name = config.get("name", "")
    draw = FIGURES.get(name)
    if draw is None:
        known = ", ".join(sorted(FIGURES)) or "none"
        return (
            f'<div class="chart-error">figure: no figure named {_esc(name)}; '
            f"this build has {_esc(known)}</div>"
        )
    # Stable across builds, and unique per figure on a page: two figures sharing
    # a checkbox id would make one figure's pause control operate the other.
    uid = sha256(repr(sorted(config.items())).encode()).hexdigest()[:8]
    title = config.get("title", "")
    note = config.get("note", "")
    head = f'<span class="fig-title">{_esc(title)}</span>' if title else "<span></span>"
    tail = f'<p class="fig-note">{_esc(note)}</p>' if note else ""
    return (
        f'<figure class="fig fig-{_esc(name)}">'
        f'<figcaption class="fig-head">{head}'
        f'<input type="checkbox" class="fig-pause" id="pause-{uid}">'
        f'<label class="fig-pause-label" for="pause-{uid}">Pause animation</label>'
        f'</figcaption><div class="fig-body">{draw(uid)}</div>{tail}</figure>'
    )


#: Phase names in request order, straight from the columns
#: `wreath-request-trace` reports and `request-boundary-baseline.json` records.
_PHASES = ("ingress", "middleware", "routing", "auth", "handler", "egress")


def _phase_labels(y: int) -> str:
    return "".join(
        f'<text class="f-tick" x="{120 + column * 100}" y="{y}" text-anchor="middle">{phase}</text>'
        for column, phase in enumerate(_PHASES)
    )


def _request_boundary(uid: str) -> str:
    """One request against the Python/native line, twice.

    The top track crosses at every stage; the bottom one stays native until the
    route is *activated*. That is wreath's stated shape, and the number under it
    is the checked-in measurement rather than a claim.
    """
    # A stage-by-stage zigzag: up into Python for the stage, back down after it.
    upper = (
        "M 64 250 "
        + " ".join(
            f"L {86 + column * 100} 250 L {104 + column * 100} 198 "
            f"L {136 + column * 100} 198 L {154 + column * 100} 250"
            for column in range(6)
        )
        + " L 700 250"
    )
    # Native the whole way, one excursion at `handler`, then native again.
    lower = "M 64 106 L 528 106 L 546 54 L 578 54 L 596 106 L 700 106"
    return (
        '<svg viewBox="0 0 720 300" role="img" width="100%" style="max-width:720px" '
        'aria-label="A request crossing the Python/native boundary: a conventional '
        "stack crosses at every stage; wreath crosses once, when the route is "
        'activated.">'
        '<text class="f-label" x="0" y="176">A stack written in Python</text>'
        '<line class="f-boundary" x1="64" y1="224" x2="700" y2="224"/>'
        '<text class="f-side" x="0" y="202">python</text>'
        '<text class="f-side" x="0" y="256">native</text>'
        f'<path class="f-path f-path-other" d="{upper}"/>'
        f'<circle class="f-dot f-dot-other" r="5" style="offset-path:path(\'{upper}\')"/>'
        + _phase_labels(282)
        + '<text class="f-label" x="0" y="32">Wreath</text>'
        '<line class="f-boundary" x1="64" y1="80" x2="700" y2="80"/>'
        '<text class="f-side" x="0" y="58">python</text>'
        '<text class="f-side" x="0" y="112">native</text>'
        f'<path class="f-path f-path-wreath" d="{lower}"/>'
        f'<circle class="f-dot f-dot-wreath" r="5" style="offset-path:path(\'{lower}\')"/>'
        '<text class="f-note-in" x="562" y="42" text-anchor="middle">1 frame</text>'
        + _phase_labels(138)
        + "</svg>"
    )


#: The six routes in one `(method, segment-count)` group, and where each one
#: stops matching `GET /orders/42/items`. `alive` is how many of the three
#: segment tests it survives, so the grid is the algorithm's own state rather
#: than a drawing of it: `survivors &= literal[p][seg] | param[p]`, one column
#: per position.
_ROUTES = (
    ("/orders/{id}/items", 3),
    ("/orders/{id}/notes", 2),
    ("/orders/new/items", 1),
    ("/users/{id}/items", 0),
    ("/users/me/items", 0),
    ("/users/{id}/notes", 0),
)
#: The column headers: the starting set, then one per segment of the request.
_COLUMNS = ("all", "orders", "42", "items")


def _route_bitset(uid: str) -> str:
    """Six routes, one request, and the two ways to narrow it down.

    Left: a decision tree has to repeat a parameter route under every literal
    branch, so the same route exists many times over. Right: each route is one
    bit, and each segment of the request turns off the bits that cannot match.
    Reading a row tells you when a route dropped out; reading a column tells you
    how many were still in the running.
    """

    def node(x, y, label, ghost=False):
        css = "f-box f-box-ghost" if ghost else "f-box"
        return (
            f'<g><rect class="{css}" x="{x - 30}" y="{y - 11}" width="60" '
            f'height="22" rx="4"/><text class="f-box-t" x="{x}" y="{y + 4}" '
            f'text-anchor="middle">{_esc(label)}</text></g>'
        )

    tree = [
        '<text class="f-label" x="0" y="14">Decision tree</text>',
        '<path class="f-edge" d="M 165 44 L 95 63 M 165 44 L 235 63"/>',
        '<path class="f-edge" d="M 95 85 L 62 111 M 95 85 L 128 111 '
        'M 235 85 L 202 111 M 235 85 L 268 111"/>',
        '<circle class="f-node" cx="165" cy="36" r="8"/>',
        '<text class="f-box-t" x="165" y="40" text-anchor="middle">/</text>',
        node(95, 74, "orders"),
        node(235, 74, "users"),
        node(128, 122, "new"),
        node(268, 122, "me"),
    ]
    # The two copies of the same parameter route, drawn as copies: they arrive
    # one after the other with a brace joining them, which is the argument.
    for order, x in enumerate((62, 202)):
        tree.append(f'<g class="f-fold" style="--i:{order}">{node(x, 122, "{id}", True)}</g>')
    tree.append(
        '<g class="f-fold" style="--i:2">'
        '<path class="f-brace" d="M 62 140 L 62 150 L 202 150 L 202 140"/>'
        '<text class="f-tick f-tick-warn" x="132" y="166" text-anchor="middle">'
        "one route, two copies</text></g>"
    )
    tree.append(
        '<g class="f-fold" style="--i:3">'
        '<text class="f-tick" x="132" y="190" text-anchor="middle">'
        "\u2026 and again under every branch below</text></g>"
    )

    grid = ['<text class="f-label" x="356" y="14">Bitset</text>']
    for column, header in enumerate(_COLUMNS):
        grid.append(
            f'<text class="f-tick" x="{574 + column * 40}" y="36" '
            f'text-anchor="middle">{_esc(header)}</text>'
        )
    for row, (route, alive) in enumerate(_ROUTES):
        y = 56 + row * 25
        matched = alive == len(_COLUMNS) - 1
        label = "f-route f-route-hit" if matched else "f-route"
        grid.append(f'<text class="{label}" x="356" y="{y + 11}">{_esc(route)}</text>')
        for column in range(len(_COLUMNS)):
            on = column <= alive
            css = "f-cell f-cell-on" if on else "f-cell"
            if on and matched:
                css += " f-cell-hit"
            grid.append(
                f'<rect class="{css}" style="--t:{column * 0.55:.2f}" '
                f'x="{562 + column * 40}" y="{y}" width="24" height="16" rx="3"/>'
            )
    grid.append(
        '<text class="f-tick" x="356" y="216">'
        "a parameter is one bit, not one copy per branch</text>"
    )

    return (
        '<svg viewBox="0 0 720 230" role="img" width="100%" style="max-width:720px" '
        'aria-label="Matching GET /orders/42/items against six routes. The decision '
        "tree repeats the parameter route under every literal branch. The bitset "
        "gives each route one bit and switches off the bits that cannot match, "
        'segment by segment, until one route is left.">'
        f'<g transform="translate(8,8)">{"".join(tree)}</g>'
        f"{''.join(grid)}"
        "</svg>"
    )


def _pips(x: int, y: int, count: int, first: float, css: str) -> str:
    """A row of step markers that light one per operation, in time."""
    return "".join(
        f'<rect class="f-pip {css}" style="--t:{first + index * 0.3:.2f}" '
        f'x="{x + index * 14}" y="{y}" width="9" height="9" rx="2"/>'
        for index in range(count)
    )


def _heap(target: tuple[int, int]) -> str:
    """Fifteen nodes in four levels, with one leaf marked for cancellation."""
    parts: list[str] = []
    positions: dict[tuple[int, int], tuple[float, float]] = {}
    for depth in range(4):
        count = 2**depth
        y = 42 + depth * 38
        for slot in range(count):
            x = (250 / (count + 1)) * (slot + 1) + 28
            positions[(depth, slot)] = (x, y)
            if depth:
                px, py = positions[(depth - 1, slot // 2)]
                parts.append(
                    f'<line class="f-edge" x1="{px:.1f}" y1="{py + 7:.1f}" '
                    f'x2="{x:.1f}" y2="{y - 7:.1f}"/>'
                )
    # The walk a cancelled leaf's replacement makes: its own slot, then each
    # ancestor in turn. One pulse per level, which is the log-n the caption
    # claims, drawn rather than asserted.
    path = [(3, target[1])]
    while path[-1][0]:
        depth, slot = path[-1]
        path.append((depth - 1, slot // 2))
    path_keys = set(path)
    for step, key in enumerate(path):
        x, y = positions[key]
        parts.append(
            f'<circle class="f-node f-sift" style="--t:{1.0 + step * 0.3:.2f}" '
            f'cx="{x:.1f}" cy="{y:.1f}" r="7"/>'
        )
    for key, (x, y) in positions.items():
        if key not in path_keys:
            parts.append(f'<circle class="f-node" cx="{x:.1f}" cy="{y:.1f}" r="7"/>')
    tx, ty = positions[target]
    parts.append(f'<circle class="f-mark" cx="{tx:.1f}" cy="{ty:.1f}" r="11"/>')
    parts.append(
        f'<text class="f-tick f-tick-warn" x="{tx:.1f}" y="{ty + 26:.1f}" '
        'text-anchor="middle">cancel</text>'
    )
    return "".join(parts)


def _timing_wheel(uid: str) -> str:
    """The same cancellation, counted on both structures.

    The ring is drawn with the mark's own broken stroke: the wheel is a circular
    buffer, and the mark at the top of every page is a circle of separate things.
    One slot is opened up to show what a bucket actually holds -- an intrusive
    doubly linked list, which is why a cancel is a splice and not a search.
    """
    ring: list[str] = []
    for index in range(16):
        angle = index * 22.5 - 90
        dot = (
            '<circle class="f-timer" cx="466" cy="72" r="4"/>'
            if index in {1, 3, 6, 7, 11, 14}
            else ""
        )
        ring.append(
            f'<g transform="rotate({angle} 466 132)">'
            f'<line class="f-slot-tick" x1="466" y1="60" x2="466" y2="70"/>'
            f"{dot}</g>"
        )
    # One bucket, opened: prev <-> node <-> next. The middle node leaves and the
    # link closes over it; that is the entire cancel path.
    chain = (
        '<path class="f-callout" d="M 520 108 L 566 118"/>'
        '<text class="f-tick" x="638" y="104" text-anchor="middle">one bucket</text>'
        '<line class="f-link" x1="596" y1="132" x2="680" y2="132"/>'
        '<line class="f-link f-link-closed" x1="596" y1="132" x2="680" y2="132"/>'
        '<circle class="f-chain" cx="596" cy="132" r="7"/>'
        '<circle class="f-chain f-chain-gone" cx="638" cy="132" r="7"/>'
        '<circle class="f-chain" cx="680" cy="132" r="7"/>'
        '<text class="f-tick" x="638" y="158" text-anchor="middle">'
        "unlink \u2192 link closes</text>"
    )
    return (
        '<svg viewBox="0 0 720 250" role="img" width="100%" style="max-width:720px" '
        'aria-label="Cancelling one timer. The binary heap walks four levels and '
        "later pays a compaction pass; the timing wheel unlinks the node from its "
        'bucket in a single step.">'
        '<text class="f-label" x="8" y="20">Binary heap</text>'
        f"{_heap((3, 5))}"
        '<rect class="f-compact" x="8" y="30" width="290" height="180" rx="6"/>'
        f"{_pips(8, 216, 4, 1.0, 'f-pip-other')}"
        '<text class="f-tick" x="76" y="225">4 swaps up the tree</text>'
        '<text class="f-tick f-compact-t" x="8" y="243">'
        "\u2026 and a compaction pass, later</text>"
        '<text class="f-label" x="360" y="20">Hashed timing wheel</text>'
        '<circle class="f-ring" cx="466" cy="132" r="58"/>'
        f"{''.join(ring)}"
        '<line class="f-hand" x1="466" y1="132" x2="466" y2="80"/>'
        '<circle class="f-hub" cx="466" cy="132" r="4"/>'
        f"{chain}"
        f"{_pips(360, 216, 1, 1.0, 'f-pip-wreath')}"
        '<text class="f-tick" x="380" y="225">1 splice</text>'
        '<text class="f-tick f-tick-hit" x="360" y="243">'
        "no reallocation, no heapify, nothing to compact</text>"
        "</svg>"
    )


#: Every figure this build can draw. A name outside this map renders a visible
#: note rather than an empty box, the same way a bad chart source does.
FIGURES = {
    "request-boundary": _request_boundary,
    "route-bitset": _route_bitset,
    "timing-wheel": _timing_wheel,
}
