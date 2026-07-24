"""Auto-translation coverage over the anonymized corpus.

Skipped today (``wreath.port`` does not exist yet); auto-activates when the tool
ships. Encodes design 07 §5's honest per-category expectations, not aspirations.
"""
import pytest

port = pytest.importorskip("wreath.port")

# Coverage = translated / recognized constructs, per category.
CATEGORY_FLOORS = {
    "routing": 0.90,
    "params": 0.85,
    "pydantic_models": 0.85,
    "dependencies": 0.80,
    "orm_models": 0.60,
    "exceptions": 0.80,
    "settings": 0.50,
    "queries": 0.0,  # ormar .objects. tar-pit is annotate-only by design
}


def test_each_app_root_analyzes(corpus_app_roots):
    for root in corpus_app_roots:
        report = port.analyze(root)
        assert report.recognized_constructs > 0


def test_category_coverage_floors(corpus_app_roots):
    report = port.analyze_all(corpus_app_roots)
    for category, floor in CATEGORY_FLOORS.items():
        assert report.coverage(category) >= floor, category


def test_overall_coverage_is_honest(corpus_app_roots):
    report = port.analyze_all(corpus_app_roots)
    # End-to-end construct coverage lands ~45-60% (design 07 §5); the value is a
    # precise report of the rest, not a high percentage.
    assert 0.40 <= report.coverage_overall() <= 0.80
