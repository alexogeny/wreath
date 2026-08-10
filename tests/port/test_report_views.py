"""The two report views that were being rewritten by hand against ``--json``.

The default report lists findings one per line in file order, which answers
"what does this file need" and hides "what does this codebase need" — a rule
firing seven times across five files reads as seven unrelated problems. Both
views here existed as throwaway scripts before they existed as flags.
"""
from argparse import Namespace
from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")

from wreath._port.cli import (  # noqa: E402  (after importorskip, by design)
    execute,
    render_by_rule,
    render_sites,
)

_APP = """\
from fastapi import APIRouter
from llamas.models import Llama

router = APIRouter()


@router.get("/llamas")
async def by_ranch(slug: str):
    return await Llama.objects.filter(ranch__slug=slug).all()


@router.get("/llamas/tagged")
async def by_tag(tag: str):
    return await Llama.objects.filter(tags__jsonb_has_any=[tag]).all()


@router.get("/llamas/{pk}")
async def one(pk: str):
    return await Llama.objects.get(**{"pk": pk})
"""


def _namespace(root: Path, **overrides) -> Namespace:
    base = dict(source=[str(root)], as_json=False, by_rule=False, rule=None, context=0,
                in_place=False, output=None, force=False)
    return Namespace(**(base | overrides))


@pytest.fixture
def app(tmp_path: Path) -> Path:
    (tmp_path / "routes.py").write_text(_APP)
    return tmp_path


def test_by_rule_clusters_and_ranks_by_count(app: Path) -> None:
    report = port.analyze_all([app])

    rows = report.rule_counts()

    counts = dict((rule, n) for rule, _cat, _tag, n in rows)
    assert counts["orm.query.filter"] == 1
    assert counts["orm.query.get"] == 1
    # Heaviest first: that ranking is the entire reason for the view.
    assert [n for *_, n in rows] == sorted((n for *_, n in rows), reverse=True)


def test_by_rule_excludes_the_findings_that_need_no_decision(app: Path) -> None:
    """Ranking translated findings ranks work nobody has to do."""
    report = port.analyze_all([app])

    assert {tag for _rule, _cat, tag, _n in report.rule_counts()} == {"needs-review"}
    assert any(f.tag == "translated" for f in report.findings), "fixture has translated ones"


def test_by_rule_says_so_when_nothing_needs_a_decision(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text("VALUE = 1\n")
    report = port.analyze_all([tmp_path])

    assert "nothing needs review" in render_by_rule(report)


def test_rule_filter_selects_only_that_rule(app: Path) -> None:
    report = port.analyze_all([app])

    rendered = render_sites(report, {"orm.query.get"}, 0)

    assert rendered.count("orm.query.get") == 1
    assert "orm.query.filter" not in rendered


def test_an_unknown_rule_says_so_rather_than_rendering_empty(app: Path) -> None:
    """Silence here reads as 'this codebase is clean of that', which is a lie."""
    report = port.analyze_all([app])

    rendered = render_sites(report, {"orm.query.nosuchrule"}, 0)

    assert "no findings for rule `orm.query.nosuchrule`" in rendered


def test_context_shows_the_source_and_marks_the_hit(app: Path) -> None:
    report = port.analyze_all([app])

    rendered = render_sites(report, {"orm.query.get"}, 2)

    assert 'Llama.objects.get(**{"pk": pk})' in rendered
    marked = [line for line in rendered.splitlines() if line.lstrip().startswith(">")]
    assert len(marked) == 1 and "Llama.objects.get" in marked[0]


def test_context_survives_a_source_it_cannot_resolve(app: Path) -> None:
    """A report can outlive its tree; the view must degrade, not raise."""
    report = port.analyze_all([app])
    (app / "routes.py").unlink()

    rendered = render_sites(report, set(), 2)

    assert "source not found" in rendered


def test_context_resolves_against_the_root_the_finding_came_from(tmp_path: Path) -> None:
    """`Finding.file` is spelled relative to a root, so two roots can collide."""
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "routes.py").write_text(_APP)
    report = port.analyze_all([tmp_path / "alpha", tmp_path / "beta"])

    rendered = render_sites(report, {"orm.query.get"}, 1)

    assert rendered.count('Llama.objects.get(**{"pk": pk})') == 2


def test_the_views_do_not_change_the_exit_code(app: Path, capsys) -> None:
    """A view is a rendering. What CI reads must not depend on which one ran."""
    plain = execute(_namespace(app))
    capsys.readouterr()
    assert execute(_namespace(app, by_rule=True)) == plain
    capsys.readouterr()
    assert execute(_namespace(app, rule=["orm.query.get"])) == plain
    capsys.readouterr()


def test_json_still_wins_over_the_views(app: Path, capsys) -> None:
    """`--json` is a machine contract; a view flag must not silently reshape it."""
    execute(_namespace(app, as_json=True, by_rule=True))

    import json

    parsed = json.loads(capsys.readouterr().out)
    assert "findings" in parsed and "coverage_overall" in parsed
