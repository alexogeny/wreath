"""Django `.objects` chains, and the fact that decides whether they translate.

`corpus/birchmoor_tally/queries.py` and `corpus/birchmoor_tally/writes.py` carry
the same verbs against the same models. `queries.py` imports only `.models`;
`writes.py` also imports `django.db`. The porter used to give them opposite
verdicts -- `orm.query.filter_exact` here, `foreign.django.query` there -- off
nothing but that import, because the gate read the *querying module's* import
list and called it "is this Django".

The reason for the gate was real and is unchanged: `Model.objects` is whatever
`get_queryset()` left, and rewriting the verb alone widens the query for exactly
the rows somebody meant to hide. But whose `objects` it is belongs to the
**model**, and the model is declared somewhere else entirely. So the manager is
resolved over the whole tree (`_port/analyzer/django.py`) and every call site is
classified against that. `foreign/ironwood_tally/` is the other half of this
test: same framework, same spellings, a manager and a `save()` override, and
every chain there stays refused.
"""
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
    """The finding this fixture exists for, stated as an equality.

    `live_ranges`, `tallies_since`, `tallies_for_species`, `range_by_slug`,
    `count_tallies` and `newest_tallies` are written once in each module. The
    verdict for each pair has to match, and it did not: `writes.py` said
    unsupported for all six because it also imports `django.db.transaction`.
    """
    queries = {
        rule for line, rule in _by_file(birchmoor_findings, "queries.py")
        if rule.startswith("orm.query.")
    }
    writes = {
        rule for line, rule in _by_file(birchmoor_findings, "writes.py")
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
        (26, "orm.query.filter_exact"),   # filter(retired=False)
        (30, "orm.query.filter_exact"),   # filter(recorded_at__gte=x)
        (34, "orm.query.filter_exact"),   # filter(species__in=xs)
        (38, "orm.query.get_exact"),      # get(slug=x)
        (42, "orm.query.filter_exact"),   # filter(...).count()
        (46, "orm.query.order_exact"),    # order_by("-recorded_at")
    ],
)
def test_each_write_chain_lands_on_the_verdict_its_twin_earns(
    birchmoor_findings, line: int, rule_id: str
) -> None:
    assert (line, rule_id) in _by_file(birchmoor_findings, "writes.py")


def test_a_transaction_block_is_a_session_block(birchmoor_findings) -> None:
    """`with transaction.atomic():` has one wreath spelling and it is exact.

    Nested is a savepoint on both sides, so the nested block at line 58 is the
    same rewrite as the outer one at 56 rather than a different construct.
    """
    atomic = {
        line for line, rule in _by_file(birchmoor_findings, "writes.py")
        if rule == "orm.transaction.atomic"
    }
    assert atomic == {50, 56, 58}


def test_the_transaction_block_is_rewritten_where_a_session_is_in_scope() -> None:
    """The verdict is `translated`, so something has to write it out.

    A session is what the rewrite needs and the corpus's sync module has none --
    the same reason a running `.objects` chain there keeps its note. Given one,
    the whole block moves: `with` becomes `async with`, and Django's
    thread-local connection becomes this handler's session.
    """
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
    """`select_related("observer")` is only a rename once `observer` is resolved.

    The relation is declared in `models.py` and read in `queries.py`, which is
    the ordinary case rather than the awkward one -- and until Django models
    were indexed the way ormar's are, the emitter had nothing to write the
    `.include(...)` from.
    """
    queries = _by_file(birchmoor_findings, "queries.py")

    # select_related("observer") -- a forward foreign key.
    assert (105, "orm.query.eager_exact") in queries
    # prefetch_related("tallies") -- the reverse of one, named by related_name.
    assert (109, "orm.query.eager_exact") in queries


def test_the_reverse_of_a_foreign_key_is_a_relation_and_the_reverse_of_m2m_is_not(
    corpus_root, foreign_root
) -> None:
    """`related_name` means two different things, and only one has a wreath model.

    On a `ForeignKey` it names the one-to-many wreath declares, so
    `prefetch_related("tallies")` resolves. On a `ManyToManyField` it names an
    association table Django created implicitly and wreath has none, so
    `Observer.objects.filter(sightings__species=...)` must stay a decision --
    registering that accessor would turn a query over a table that does not
    exist into a `translated` verdict.
    """
    from wreath._port.analyzer.django import django_image
    from wreath._port.analyzer.sources import _iter_py

    corpus = django_image(list(_iter_py(corpus_root / "birchmoor_tally")))
    refused = django_image(list(_iter_py(foreign_root / "ironwood_tally")))

    assert corpus.relations["Range"]["tallies"] == "Tally"
    assert "sightings" not in refused.relations.get("Observer", {})


def test_the_emitted_module_no_longer_imports_django(corpus_root, tmp_path) -> None:
    """A ported module that still imports django does not start without django.

    Only where every mention moved: `views.py` keeps `django.http` because
    `Http404` and `JsonResponse` have not been translated yet, and `writes.py`
    keeps `transaction` because its blocks are waiting on a session.
    """
    result = port.port_tree(corpus_root / "birchmoor_tally", tmp_path)
    assert result.failed == []

    models = (tmp_path / "models.py").read_text(encoding="utf-8")
    views = (tmp_path / "views.py").read_text(encoding="utf-8")

    assert "from django" not in models
    assert "class Range(Model, table=\"range\")" in models
    assert "from django.http import" in views


def test_the_query_rewrites_reach_the_file(corpus_root, tmp_path) -> None:
    """`translated` has to mean the emitter wrote something.

    The chains that only *build* a query need nothing from the caller and come
    out in full. The ones that run keep their note until a session is in scope,
    which is the same contract every ormar chain has had.
    """
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
