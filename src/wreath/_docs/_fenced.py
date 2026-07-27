"""One scanner for the build-time fenced blocks (```` ```chart ````, ``figure``, ``hero``).

These blocks are read out of the markdown *before* it is parsed, which is what
lets them emit SVG the parser would otherwise escape. The catch is that a
line-by-line search for ```` ```figure ```` also finds one written inside a
longer fence — which is exactly how a guide documents the syntax. The docs page
for the generator rendered its own examples for real, and one of them failed the
build's dead-link check.

So the scan tracks enclosing fences: a run of three or more backticks or tildes
opens one, a run at least as long closes it, and nothing inside is a block. That
is CommonMark's own rule for nested fences, and it is the reason a four-backtick
wrapper is how you show a three-backtick example.
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = ["extract", "restore"]

_FENCE = re.compile(r"^(`{3,}|~{3,})")


def extract(
    text: str, opener: str, render: Callable[[list[str]], str], label: str,
) -> tuple[str, dict[str, str]]:
    """Replace each ``opener`` block with a token; return (text, {token: html}).

    ``render`` receives the block's body lines and returns the markup to splice
    back in after the markdown pass. ``label`` only distinguishes one block
    type's tokens from another's in the same document.
    """
    lines = text.splitlines()
    out: list[str] = []
    tokens: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == opener:
            index += 1
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != "```":
                body.append(lines[index])
                index += 1
            index += 1                              # consume the closing fence
            token = f"\x00{label}{len(tokens)}\x00"
            tokens[token] = render(body)
            out.append(token)
            continue
        fence = _FENCE.match(stripped)
        if fence is None:
            out.append(line)
            index += 1
            continue
        # An enclosing fence: copy it through untouched, closing marker included.
        marker = fence.group(1)
        out.append(line)
        index += 1
        while index < len(lines):
            out.append(lines[index])
            closing = _FENCE.match(lines[index].strip())
            index += 1
            if (closing is not None
                    and closing.group(1)[0] == marker[0]
                    and len(closing.group(1)) >= len(marker)
                    and not lines[index - 1].strip()[len(closing.group(1)):].strip()):
                break
    return "\n".join(out), tokens


def restore(html: str, tokens: dict[str, str]) -> str:
    """Swap tokens (as rendered by the markdown pass) for their markup."""
    for token, markup in tokens.items():
        html = html.replace(f"<p>{token}</p>", markup).replace(token, markup)
    return html
