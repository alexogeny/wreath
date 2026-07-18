"""Deterministic regressions for the Python-complexity audit (QPY-*)."""

from __future__ import annotations

import pytest

from wreath.orm.introspection import _normalize_default


def _old_normalize(value: str) -> str:
    # The previous O(N^2) implementation, kept as the parity oracle.
    text = " ".join(value.split()).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text.lower()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "nextval('s'::regclass)",
        "(0)",
        "((0))",
        "( ( 0 ) )",
        "(())",
        "(x",
        "x)",
        "((0)",  # unbalanced
        "(0))",  # unbalanced
        "  (  'a b'  )  ",
        "(NULL)",
        "CURRENT_TIMESTAMP",
    ],
)
def test_normalize_default_matches_previous_semantics(value: str) -> None:
    assert _normalize_default(value) == _old_normalize(value)


def test_normalize_default_deep_nesting_matches_and_is_bounded() -> None:
    for n in (1, 2, 64, 4096):
        text = "(" * n + "0" + ")" * n
        assert _normalize_default(text) == "0" == _old_normalize(text)


@pytest.mark.performance
def test_normalize_default_scaling_is_linear() -> None:
    import statistics
    import time

    def median_ns(n: int) -> float:
        text = "(" * n + "0" + ")" * n
        samples = []
        for _ in range(15):
            start = time.perf_counter_ns()
            _normalize_default(text)
            samples.append(time.perf_counter_ns() - start)
        return statistics.median(samples)

    median_ns(1000)  # warm up
    ratio = median_ns(16384) / median_ns(8192)
    assert ratio < 2.6, ratio
