from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")

from wreath._port.analyzer import TreeContext  # noqa: E402  (after importorskip)
from wreath._port.emit import emit_module  # noqa: E402


@pytest.fixture
def birchmoor(corpus_root):
    return corpus_root / "birchmoor_tally"


@pytest.fixture
def birchmoor_findings(birchmoor):
    return port.analyze(birchmoor).findings


def _by_file(findings, name):
    return {(f.line, f.rule_id) for f in findings if f.file == name}


def test_two_modules_of_the_same_chains_get_the_same_verdict(birchmoor_findings) -> None:
    queries = {
        rule
        for line, rule in _by_file(birchmoor_findings, "queries.py")
        if rule.startswith("orm.query.")
    }
    writes = {
        rule
        for line, rule in _by_file(birchmoor_findings, "writes.py")
        if rule.startswith("orm.query.")
    }

    assert writes <= queries
    assert "orm.query.filter_exact" in writes
    assert "foreign.django.query" not in {
        rule for _line, rule in _by_file(birchmoor_findings, "writes.py")
    }


@pytest.mark.parametrize(
    ("line", "rule_id"),
    [
        (26, "orm.query.filter_exact"),  # filter(retired=False)
        (30, "orm.query.filter_exact"),  # filter(recorded_at__gte=x)
        (34, "orm.query.filter_exact"),  # filter(species__in=xs)
        (38, "orm.query.get_exact"),  # get(slug=x)
        (42, "orm.query.filter_exact"),  # filter(...).count()
        (46, "orm.query.order_exact"),  # order_by("-recorded_at")
    ],
)
def test_each_write_chain_lands_on_the_verdict_its_twin_earns(
    birchmoor_findings, line: int, rule_id: str
) -> None:
    assert (line, rule_id) in _by_file(birchmoor_findings, "writes.py")


def test_a_transaction_block_is_a_session_block(birchmoor_findings) -> None:
    atomic = {
        line
        for line, rule in _by_file(birchmoor_findings, "writes.py")
        if rule == "orm.transaction.atomic"
    }
    assert atomic == {50, 56, 58}


def test_the_transaction_block_is_rewritten_where_a_session_is_in_scope() -> None:
    source = (
        "from typing import Annotated\n"
        "\n"
        "from django.db import transaction\n"
        "from wreath.orm import FromORM, Session\n"
        "\n"
        "\n"
        "async def retire(session: Annotated[Session, FromORM()]) -> None:\n"
        "    with transaction.atomic():\n"
        "        pass\n"
    )
    emitted = emit_module(source, TreeContext())

    assert "async with session.begin():" in emitted
    assert "transaction.atomic()" not in emitted


def test_a_django_model_relation_resolves_across_files(birchmoor_findings) -> None:
    queries = _by_file(birchmoor_findings, "queries.py")

    # select_related("observer") -- a forward foreign key.
    assert (105, "orm.query.eager_exact") in queries
    # prefetch_related("tallies") -- the reverse of one, named by related_name.
    assert (109, "orm.query.eager_exact") in queries


def test_the_reverse_of_a_foreign_key_is_a_relation_and_the_reverse_of_m2m_is_not(
    corpus_root, foreign_root
) -> None:
    from wreath._port.analyzer.django import django_image
    from wreath._port.analyzer.sources import _iter_py

    corpus = django_image(list(_iter_py(corpus_root / "birchmoor_tally")))
    refused = django_image(list(_iter_py(foreign_root / "ironwood_tally")))

    assert corpus.relations["Range"]["tallies"] == "Tally"
    assert "sightings" not in refused.relations.get("Observer", {})


def test_the_emitted_module_no_longer_imports_django(corpus_root, tmp_path) -> None:
    result = port.port_tree(corpus_root / "birchmoor_tally", tmp_path)
    assert result.failed == []

    models = (tmp_path / "models.py").read_text(encoding="utf-8")
    views = (tmp_path / "views.py").read_text(encoding="utf-8")

    assert "from django" not in models
    assert 'class Range(Model, table="range")' in models
    assert "from django.http import" in views


def test_the_query_rewrites_reach_the_file(corpus_root, tmp_path) -> None:
    port.port_tree(corpus_root / "birchmoor_tally", tmp_path)
    written = (tmp_path / "writes.py").read_text(encoding="utf-8")

    assert "Range.select().where(Range.retired == False)" in written
    assert "Tally.select().where(Tally.recorded_at >= recorded_at)" in written
    assert "Tally.select().where(Tally.species.in_(species_list))" in written
    assert "Tally.select().order_by(Tally.recorded_at.desc())" in written
    # What is left is exactly the chains that *run*: `get`, `count`, `update`
    # and `delete` each need a session, and the note says so rather than the
    # rewrite guessing where one comes from.
    assert "return list(Range.objects" not in written
    assert "Range.objects.get(slug=slug)" in written
    assert "Range.objects.filter(id=range_id).update(retired=True)" in written
