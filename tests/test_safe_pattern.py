from __future__ import annotations

import re

import pytest

from wreath._safe_pattern import UnsafePatternError, compile_safe_pattern


@pytest.mark.parametrize(
    ("pattern", "accepted", "rejected"),
    [
        (r"^[a-z0-9-]+$", "stock-42", "Stock_42"),
        (r"^[A-Z]{3}-\d+$", "ABC-123", "AB-123"),
        (r"^[a-z][a-z-]*$", "wreath-route", "Wreath"),
        (r"\S", "a b", "   "),
        (r"^(cat|dog)$", "cat", "fox"),
    ],
)
def test_safe_patterns_keep_search_semantics(pattern: str, accepted: str, rejected: str) -> None:
    compiled = compile_safe_pattern(pattern)

    assert compiled.search(accepted) is not None
    assert compiled.search(rejected) is None
    assert compiled.pattern == pattern


@pytest.mark.parametrize(
    "pattern",
    [
        r"^(a+)+$",
        r"^(a|aa)+$",
        r"^a+a+$",
        r"^a{0,1025}$",
        r"a+$",
        r"(\d+)",
        r"^(a+|b+c+)$",
        r"(?m)^a+$",
        r"^(?=a+$)",
        r"^(a+)\1$",
    ],
)
def test_unsafe_backtracking_shapes_are_refused_at_compilation(pattern: str) -> None:
    with pytest.raises(UnsafePatternError, match="linear-safe"):
        compile_safe_pattern(pattern)


def test_invalid_regex_keeps_the_stdlib_syntax_error() -> None:
    with pytest.raises(re.error):
        compile_safe_pattern("(unclosed")


def test_bytes_patterns_are_refused_by_the_string_validator() -> None:
    with pytest.raises(TypeError, match="bytes"):
        compile_safe_pattern(re.compile(b"a"))


def test_excessive_pattern_source_is_refused_before_matching() -> None:
    with pytest.raises(UnsafePatternError, match="4096"):
        compile_safe_pattern("a" * 4097)


def test_compiled_patterns_keep_supported_flags() -> None:
    compiled = compile_safe_pattern(re.compile(r"^[a-z]+$", re.IGNORECASE))

    assert compiled.search("Wreath") is not None


def test_compiled_multiline_anchor_does_not_count_as_input_start() -> None:
    with pytest.raises(UnsafePatternError, match="anchor"):
        compile_safe_pattern(re.compile(r"^a+$", re.MULTILINE))
