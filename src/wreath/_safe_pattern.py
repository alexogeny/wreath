from __future__ import annotations

import re
from importlib import import_module
from typing import Any

__all__ = ["UnsafePatternError", "compile_safe_pattern"]

_MAX_PATTERN_CHARS = 4096
_PARSER = import_module("re._parser")
_REPEATS = frozenset(
    {
        _PARSER.MAX_REPEAT,
        _PARSER.MIN_REPEAT,
        _PARSER.POSSESSIVE_REPEAT,
    }
)
_ATOMS = frozenset(
    {
        _PARSER.ANY,
        _PARSER.CATEGORY,
        _PARSER.IN,
        _PARSER.LITERAL,
        _PARSER.NOT_LITERAL,
    }
)
_ALLOWED = _ATOMS | _REPEATS | frozenset(
    {
        _PARSER.AT,
        _PARSER.BRANCH,
        _PARSER.SUBPATTERN,
    }
)


class UnsafePatternError(ValueError):
    pass


def _refuse(pattern: str, reason: str, correct: str) -> None:
    raise UnsafePatternError(f"pattern {pattern!r} is not linear-safe: {reason}; {correct}")


def _deterministic_atom(nodes: Any) -> bool:
    while len(nodes) == 1 and nodes[0][0] is _PARSER.SUBPATTERN:
        nodes = nodes[0][1][3]
    return len(nodes) == 1 and nodes[0][0] in _ATOMS


def _unbounded_repeats(pattern: str, nodes: Any) -> int:
    count = 0
    for opcode, argument in nodes:
        if opcode not in _ALLOWED:
            _refuse(
                pattern,
                f"{opcode} can require backtracking",
                "use literals, character classes, grouping, alternation, and simple repetition",
            )
        if opcode in _REPEATS:
            minimum, maximum, repeated = argument
            if not _deterministic_atom(repeated):
                _refuse(
                    pattern,
                    "a repetition contains a group, branch, or another repetition",
                    "repeat one literal, dot, escape, or character class at a time",
                )
            if maximum == _PARSER.MAXREPEAT:
                count += 1
            elif maximum - minimum > 1024:
                _refuse(
                    pattern,
                    f"bounded repetition spans {maximum - minimum} alternatives",
                    "use a bound no more than 1024 values wide",
                )
        elif opcode is _PARSER.SUBPATTERN:
            count += _unbounded_repeats(pattern, argument[3])
        elif opcode is _PARSER.BRANCH:
            count += max(
                (_unbounded_repeats(pattern, branch) for branch in argument[1]),
                default=0,
            )
        if count > 1:
            _refuse(
                pattern,
                "one match path contains more than one unbounded repetition",
                "keep one '*', '+', or open-ended repeat per alternative",
            )
    return count


def _starts_at_input_start(nodes: Any, flags: int) -> bool:
    if not nodes or nodes[0][0] is not _PARSER.AT:
        return False
    anchor = nodes[0][1]
    if anchor is _PARSER.AT_BEGINNING_STRING:
        return True
    return anchor is _PARSER.AT_BEGINNING and not flags & re.MULTILINE


def compile_safe_pattern(pattern: str | re.Pattern[str]) -> re.Pattern[str]:
    if isinstance(pattern, re.Pattern):
        source = pattern.pattern
        flags = pattern.flags
    else:
        source = pattern
        flags = 0
    if not isinstance(source, str):
        raise TypeError(f"pattern must be str, got {type(source).__name__}")
    if len(source) > _MAX_PATTERN_CHARS:
        _refuse(
            source,
            f"its source exceeds {_MAX_PATTERN_CHARS} characters",
            f"use at most {_MAX_PATTERN_CHARS} characters",
        )
    compiled = re.compile(source, flags)
    parsed = _PARSER.parse(source, flags)
    unbounded = _unbounded_repeats(source, parsed)
    if unbounded and not _starts_at_input_start(parsed, parsed.state.flags):
        _refuse(
            source,
            "an unbounded repetition can restart at every input position",
            "anchor the pattern with '^' or '\\A', or use a finite bound",
        )
    return compiled
