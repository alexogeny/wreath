"""A compact, table-driven syntax highlighter — deliberately *not* Pygments.

One scanner core plus a small per-language descriptor (a single alternation regex
with named groups whose names are the token classes) emits a coarse vocabulary:
comment / string / number / keyword / builtin / variable / operator. Tier-1 langs
(python, bash, c, json) get a grammar; anything else — or plain `text` — is
escaped and left alone. This mirrors the design's "ship our own bounded engine"
call: good enough to read, no dependency, no scope creep into a full lexer.

Each token's text is HTML-escaped, so the output is safe to drop into `<pre>`.
"""

from __future__ import annotations

import re

__all__ = ["highlight", "languages"]


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _spec(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.MULTILINE | re.DOTALL)


_PY_KW = (r"\b(?P<keyword>def|class|return|if|elif|else|for|while|import|from|as|with|try|"
          r"except|finally|raise|yield|await|async|lambda|pass|break|continue|in|is|not|and|"
          r"or|None|True|False|global|nonlocal|assert|del|match|case)\b")
_PY_BUILTIN = (r"\b(?P<builtin>print|len|range|enumerate|zip|dict|list|set|tuple|int|str|float|"
               r"bool|bytes|isinstance|issubclass|super|self|cls|type|open|map|filter|sorted|"
               r"any|all|min|max|sum|abs|getattr|setattr|hasattr)\b")

_SPECS: dict[str, re.Pattern[str]] = {
    "python": _spec(
        r"(?P<comment>#[^\n]*)",
        r"(?P<string>[rbfRBF]{0,2}(?:'''.*?'''|\"\"\".*?\"\"\"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"))",
        r"(?P<number>\b\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?\b)",
        _PY_KW, _PY_BUILTIN,
        r"(?P<operator>[+\-*/%=<>!&|^~@]=?|:=|->)",
    ),
    "bash": _spec(
        r"(?P<comment>#[^\n]*)",
        r"(?P<string>'[^']*'|\"(?:\\.|[^\"\\])*\")",
        r"(?P<variable>\$\w+|\$\{[^}]*\})",
        r"\b(?P<keyword>if|then|else|elif|fi|for|while|do|done|case|esac|function|in|return|"
        r"export|local|source|set|echo|cd|exit)\b",
        r"(?P<number>\b\d+\b)",
    ),
    "c": _spec(
        r"(?P<comment>//[^\n]*|/\*.*?\*/)",
        r"(?P<string>\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*')",
        r"(?P<keyword>\b(?:int|char|void|const|static|struct|union|enum|typedef|return|if|else|"
        r"for|while|switch|case|default|break|continue|sizeof|unsigned|signed|long|short|float|"
        r"double|goto|do|extern|register|volatile|inline)\b|#\s*\w+)",
        r"(?P<number>\b(?:0[xX][0-9a-fA-F]+|\d+\.?\d*)\b)",
        r"(?P<operator>[+\-*/%=<>!&|^~]=?|->|\+\+|--)",
    ),
    "json": _spec(
        r"(?P<string>\"(?:\\.|[^\"\\])*\")",
        r"(?P<keyword>\b(?:true|false|null)\b)",
        r"(?P<number>-?\b\d+\.?\d*(?:[eE][+-]?\d+)?\b)",
    ),
}
_ALIASES = {"py": "python", "sh": "bash", "shell": "bash", "console": "bash",
            "js": "json", "typescript": "json"}


def languages() -> tuple[str, ...]:
    return tuple(_SPECS)


def highlight(code: str, lang: str) -> str:
    """Return HTML for `code` in `lang`, with token spans; escaped throughout."""
    spec = _SPECS.get(_ALIASES.get(lang, lang))
    if spec is None:
        return _esc(code)
    out: list[str] = []
    pos = 0
    for match in spec.finditer(code):
        if match.start() > pos:
            out.append(_esc(code[pos:match.start()]))
        kind = match.lastgroup or "text"
        out.append(f'<span class="tok-{kind}">{_esc(match.group())}</span>')
        pos = match.end()
    out.append(_esc(code[pos:]))
    return "".join(out)
