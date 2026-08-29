from __future__ import annotations

import contextlib
import io
import re
import shlex
from collections.abc import Iterator

from .codeblocks import Block, Finding

_SHELL = frozenset(("bash", "console", "sh", "shell"))
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^ \t`]*)")


def _blocks(text: str) -> Iterator[Block]:
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            index += 1
            continue
        marker = opening.group(1)
        info = lines[index][opening.end(1) :].strip()
        opening_line = index + 1
        index += 1
        body: list[str] = []
        while index < len(lines):
            candidate = lines[index].strip()
            if (
                len(candidate) >= len(marker)
                and candidate
                and set(candidate) == {marker[0]}
            ):
                break
            body.append(lines[index])
            index += 1
        yield Block(info, "\n".join(body), opening_line)
        index += 1


def _logical_lines(body: str) -> Iterator[tuple[int, str]]:
    pending: list[str] = []
    start = 0
    for offset, physical in enumerate(body.splitlines(), 1):
        if not pending:
            start = offset
        stripped = physical.rstrip()
        if stripped.endswith("\\"):
            pending.append(stripped[:-1] + " ")
            continue
        # complexity: allow SL-LINEAR-METHOD -- pending segments partition body
        yield start, "".join(pending) + physical
        pending = []
    if pending:
        yield start, "".join(pending)


def _wreath_arguments(line: str) -> list[str] | None:
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        return None
    first_command = 0
    while first_command < len(tokens) and _ASSIGNMENT.fullmatch(tokens[first_command]):
        first_command += 1
    tokens = tokens[first_command:]
    if tokens[:3] == ["uv", "run", "wreath"]:
        arguments = tokens[3:]
    elif tokens[:1] == ["wreath"]:
        arguments = tokens[1:]
    else:
        return None
    for index, token in enumerate(arguments):
        if token in ("|", ";", "&&", "||") or token.startswith(">"):
            return arguments[:index]
    return arguments


class Checker:
    def __init__(self) -> None:
        from wreath._cli import build_parser

        self._parser = build_parser()

    def check_page(self, text: str, page: str = "") -> list[Finding]:
        findings: list[Finding] = []
        for block in _blocks(text):
            if block.language not in _SHELL:
                continue
            for offset, line in _logical_lines(block.body):
                arguments = _wreath_arguments(line)
                if arguments is None:
                    continue
                output = io.StringIO()
                try:
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        if arguments[:1] == ["test"]:
                            self._parser.parse_known_args(arguments)
                        else:
                            self._parser.parse_args(arguments)
                except SystemExit as error:
                    if not error.code:
                        continue
                    detail = output.getvalue().strip().splitlines()
                    message = detail[-1] if detail else "invalid command"
                    findings.append(
                        Finding(page, block.line + offset, f"Wreath CLI example: {message}")
                    )
        return findings


def check_page(text: str, page: str = "") -> list[Finding]:
    return Checker().check_page(text, page)
