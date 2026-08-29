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
    # Raised from 0.50 once `BaseSettings` was split by field shape: a class of
    # plain scalars with literal defaults is `load_env` plus a dataclass with
    # nothing left to choose, and most settings classes are that. The
    # floor sits below the achieved 0.89 on purpose — it guards the gain without
    # pinning it, so adding a fixture with a validator or a sub-group does not
    # fail a test that was measuring something else.
    "settings": 0.75,
    # The `.objects.` tar-pit was annotate-only when the verdict was per *verb*.
    # Reading the arguments splits it: `filter(id=x)` carries across untouched,
    # `filter(name__icontains=x)` rewrites the value and `filter(ranch__slug=x)`
    # is a join, so the same verb lands on both sides. Under half, because the
    # half that needs a decision genuinely does — see the floor, not a target.
    "queries": 0.40,
}


def test_corpus_analysis_is_complete_and_meets_coverage_floors(corpus_app_roots):
    reports = [port.analyze(root) for root in corpus_app_roots]
    for root, report in zip(corpus_app_roots, reports, strict=True):
        assert report.recognized_constructs > 0
        # Nothing in the corpus is unreadable, so a skip here means the walk or
        # the reader broke — the coverage above would be over a shrunken tree.
        assert report.skipped == [], root
        assert report.files_analyzed > 0

    report = port.Report.merge(reports)
    for category, floor in CATEGORY_FLOORS.items():
        measured = report.coverage(category)
        # `None` means the category recognized nothing at all — which used to
        # report as 1.0 and sail over every floor here. Name it, so a category
        # the analyzer stopped seeing fails as a gap rather than as a triumph.
        assert measured is not None, f"{category}: nothing recognized"
        assert measured >= floor, category

    # Design 07 §5 expected ~45-60%; reading constructs by *shape* rather than by
    # name first took it to ~0.78. Exact primary-key reads, statically proven
    # field mappings, and local TestClient lifetimes now take it to ~0.83; each
    # has an emitted first-class Wreath contract rather than a verdict-only
    # relabel, which is why crossing the former 0.80 ceiling is honest. The
    # ceiling is deliberate: it is not a target to reach but a
    # tripwire: passing it should mean someone re-reads what `TRANSLATED` was
    # allowed to cover and writes down why the new number is honest, because the
    # cheapest way to raise this figure has always been to loosen the word. The
    # remaining ~17% is queries needing a join decision, bespoke auth and
    # validator bodies, and libraries that are correctly kept — none of which a
    # static analyzer should claim.
    overall = report.coverage_overall()
    assert overall is not None, "an empty denominator is n/a, never a perfect score"
    assert 0.40 <= overall <= 0.85
