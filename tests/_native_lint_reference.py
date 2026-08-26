"""Independent Python definition of the native C-source lexical tape."""

from __future__ import annotations

import re

_LOOP_KEYWORD = re.compile(r"(for|while)\s*\(")


def strip_c(text: str) -> list[str]:
    """Blank comments and literal contents while preserving source lines."""
    out: list[list[str]] = [list(line) for line in text.split("\n")]
    row = col = 0
    state = "code"
    lines = text.split("\n")

    def blank(r: int, c: int) -> None:
        if c < len(out[r]):
            out[r][c] = " "

    while row < len(lines):
        line = lines[row]
        if col >= len(line):
            if state == "line":
                state = "code"
            row += 1
            col = 0
            continue
        char = line[col]
        following = line[col + 1] if col + 1 < len(line) else ""
        if state == "code":
            if char == "/" and following == "*":
                state = "block"
                blank(row, col)
                blank(row, col + 1)
                col += 2
                continue
            if char == "/" and following == "/":
                state = "line"
                continue
            if char == '"':
                state = "string"
                col += 1
                continue
            if char == "'":
                state = "char"
                col += 1
                continue
            col += 1
            continue
        if state == "block":
            if char == "*" and following == "/":
                blank(row, col)
                blank(row, col + 1)
                state = "code"
                col += 2
                continue
            blank(row, col)
            col += 1
            continue
        if state == "line":
            blank(row, col)
            col += 1
            continue
        if char == "\\":
            blank(row, col)
            blank(row, col + 1)
            col += 2
            continue
        if (state == "string" and char == '"') or (
            state == "char" and char == "'"
        ):
            state = "code"
            col += 1
            continue
        blank(row, col)
        col += 1
    return ["".join(line) for line in out]


def loop_depth_map(code_lines: list[str]) -> list[int]:
    """Readable definition of loop depth at the start of every source line."""
    text = "\n".join(code_lines)
    length = len(text)
    depth_at = [0] * (length + 1)
    stack: list[bool] = []
    current = 0
    index = 0
    while index < length:
        depth_at[index] = current
        if (
            _LOOP_KEYWORD.match(text, index)
            and (
                index == 0
                or not (text[index - 1].isalnum() or text[index - 1] == "_")
            )
        ):
            open_paren = text.find("(", index)
            balance = 0
            close = open_paren
            while close < length:
                if text[close] == "(":
                    balance += 1
                elif text[close] == ")":
                    balance -= 1
                    if balance == 0:
                        break
                close += 1
            for offset in range(index, min(close + 1, length)):
                depth_at[offset] = current
            probe = close + 1
            while probe < length and text[probe].isspace():
                depth_at[probe] = current
                probe += 1
            if probe < length and text[probe] == "{":
                stack.append(True)
                current += 1
                depth_at[probe] = current
                index = probe + 1
                continue
            index = close + 1
            continue
        char = text[index]
        if char == "{":
            stack.append(False)
        elif char == "}" and stack and stack.pop():
            current = max(0, current - 1)
        index += 1
    depth_at[length] = current

    out: list[int] = []
    offset = 0
    for line in code_lines:
        out.append(depth_at[min(offset, length)])
        offset += len(line) + 1
    return out
