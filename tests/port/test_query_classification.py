"""Every ``.objects.`` call is a worklist item, so say *which* worklist.

The ormar query chain is the single largest construct in a real FastAPI/ormar
codebase — in the corpus this catalog was built from, ``.objects.`` appears
1105 times, roughly a third of every framework construct in the tree. Reporting
all of them as one undifferentiated "rewrite by hand" is technically true and
practically useless: it tells a porter the size of the job and nothing about its
shape.

Classified by method it becomes a plan. ``get_or_none`` (152 sites) is a direct
contract match for ``session.fetch_one``; ``create`` (150) is a mechanical
two-line expansion; ``select_related`` (30) is the eager-load that wreath makes
mandatory. Those are three different afternoons, and a porter can schedule them
separately.

Within a verb, the *arguments* decide the verdict. ``filter(id=x)`` carries
across untouched. ``filter(name__icontains=x)`` does not — the value has to be
wrapped in wildcards, and choosing that is not translating it.
``filter(ranch__slug=x)`` does not either — though **not** because the join is a
decision. It is not one: ``Model.ranch.slug`` is a ``RelatedColumnExpr`` and
``plan_filter_joins`` emits the INNER JOIN automatically, INNER rather than LEFT
because a parent with no matching child cannot satisfy a predicate on the
child's column. What blocks it is *resolution* — knowing ``ranch`` is a relation
and ``slug`` a column on its target, when the model is declared in another
module. ``analyze`` has a tree-wide index; ``emit_module`` is per-module, and
``query_rule`` is shared so the report and the emitted TODO cannot disagree.
Same verb, three verdicts, and the argument list is what tells them apart.

The invariant underneath does not move: **the emitter never rewrites a query
body.** A translated verdict says the target is fully determined, not that
Phase 1 performs it — bodies are copied byte-for-byte either way.
"""
import pytest

port = pytest.importorskip("wreath.port")


def _analyze(tmp_path, source: str):
    """Findings for one module of source, written to a scratch file."""
    path = tmp_path / "queries.py"
    path.write_text(source, encoding="utf-8")
    return port.analyze(path).findings


def _rule_ids(tmp_path, source: str) -> list[str]:
    return [f.rule_id for f in _analyze(tmp_path, source)]


def _query_rules(tmp_path, source: str) -> list[str]:
    return [r for r in _rule_ids(tmp_path, source) if r.startswith("orm.query")]


# --- one rule per query verb ----------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("Llama.objects.filter(paddock_id=7)", "orm.query.filter_exact"),
        ("Llama.objects.get_or_none(id=7)", "orm.query.get_or_none_exact"),
        ("Llama.objects.get(id=7)", "orm.query.get"),
        ("Llama.objects.create(name='Bea')", "orm.query.create"),
        ("Llama.objects.all()", "orm.query.all"),
        # Argument-shaped, the same way `filter` is above: a named relation is
        # one `.include(...)`, while `select_all()` means *every* relation and
        # wreath has no such switch.
        ("Llama.objects.select_related('treks')", "orm.query.eager_exact"),
        ("Llama.objects.select_all()", "orm.query.eager"),
        ("Llama.objects.prefetch_related('treks')", "orm.query.eager_exact"),
        ("Llama.objects.values(['name'])", "orm.query.values"),
        ("Llama.objects.bulk_create([])", "orm.query.bulk"),
        ("Llama.objects.bulk_update([])", "orm.query.bulk"),
        ("Llama.objects.count()", "orm.query.count"),
        ("Llama.objects.exists()", "orm.query.exists"),
        ("Llama.objects.delete()", "orm.query.delete"),
        ("Llama.objects.first()", "orm.query.first"),
        ("Llama.objects.get_or_create(name='Bea')", "orm.query.get_or_create"),
        ("Llama.objects.update_or_create(name='Bea')", "orm.query.get_or_create"),
    ],
)
def test_a_query_verb_names_its_own_wreath_target(tmp_path, call, expected) -> None:
    assert _query_rules(tmp_path, f"x = {call}\n") == [expected]


def test_an_unrecognised_verb_still_reports(tmp_path) -> None:
    """A new ormar method must degrade to the generic finding, never to silence."""
    assert _query_rules(tmp_path, "x = Llama.objects.chunkify()\n") == ["orm.query"]


def test_a_bare_objects_attribute_reports(tmp_path) -> None:
    """`.objects` handed around as a value is still a query surface to port."""
    assert _query_rules(tmp_path, "manager = Llama.objects\n") == ["orm.query"]


# --- the chain is one finding, not one per link ---------------------------------


def test_a_chained_query_is_reported_once_at_its_head(tmp_path) -> None:
    """`filter(...).all()` is one rewrite, so it must not bill as two.

    The head verb is the one that names the shape; `.all()` on the end is how
    ormar spells "run it", and wreath spells that `session.fetch(...)`.
    """
    source = "x = await Llama.objects.filter(paddock_id=7).order_by('name').all()\n"
    assert _query_rules(tmp_path, source) == ["orm.query.filter_exact"]


def test_two_separate_queries_are_two_findings(tmp_path) -> None:
    source = (
        "a = await Llama.objects.filter(paddock_id=7).all()\n"
        "b = await Trek.objects.get_or_none(id=1)\n"
    )
    assert _query_rules(tmp_path, source) == [
        "orm.query.filter_exact", "orm.query.get_or_none_exact"
    ]


# --- the messages have to be actionable ------------------------------------------


def test_the_eager_load_message_names_include_and_the_guard(tmp_path) -> None:
    """select_related is the N+1 fix, and wreath now ships a detector for it."""
    (finding,) = _analyze(tmp_path, "x = Llama.objects.select_related('treks')\n")
    assert "include(" in finding.message
    assert "NPlusOneGuard" in finding.message


def test_the_get_or_none_message_names_fetch_one(tmp_path) -> None:
    (finding,) = _analyze(tmp_path, "x = await Llama.objects.get_or_none(id=1)\n")
    assert "fetch_one" in finding.message


def test_the_create_message_names_add_and_flush(tmp_path) -> None:
    (finding,) = _analyze(tmp_path, "x = await Llama.objects.create(name='Bea')\n")
    assert "session.add" in finding.message and "flush" in finding.message


def test_the_get_message_warns_that_the_miss_contract_differs(tmp_path) -> None:
    """ormar's `get` raises NoMatch; wreath's `fetch_one` returns None.

    Porting the call without porting the miss branch is a silent behaviour
    change, which is exactly the class of bug this tool exists to surface.
    """
    (finding,) = _analyze(tmp_path, "x = await Llama.objects.get(id=1)\n")
    assert "NoMatch" in finding.message


# --- the standing invariant -------------------------------------------------------


def test_the_emitter_never_rewrites_a_query(tmp_path) -> None:
    """Design 07 §6, stated as the thing it actually means.

    This used to assert that no query is ever tagged ``translated``, which was a
    proxy for the real rule and stopped being one: a tag says whether the target
    is *determined*, and the emitter's contract is that function bodies are
    copied byte-for-byte whatever the tag says. Assert the contract directly, so
    the honest verdicts above cannot loosen it by accident.
    """
    source = (
        "async def handler():\n"
        "    a = await Llama.objects.filter(paddock_id=7).all()\n"
        "    b = await Llama.objects.get_or_none(id=1)\n"
        "    return a, b\n"
    )
    path = tmp_path / "queries.py"
    path.write_text(source, encoding="utf-8")
    emitted = port.emit_module(path)

    for line in source.splitlines():
        if ".objects." in line:
            assert line in emitted, "a query line was rewritten, not copied"


# --- the arguments are what decide, not the verb ----------------------------------


@pytest.mark.parametrize(
    "call",
    [
        "Llama.objects.filter(paddock_id=7)",              # plain equality
        "Llama.objects.filter(grade__gte=3)",              # operator lookup
        "Llama.objects.filter(id__in=[1, 2])",             # membership
        "Llama.objects.filter(a=1, b=2)",                  # several, all plain
        "Llama.objects.filter(retired=False).all()",       # mechanical terminal
        "Llama.objects.filter(retired=False).count()",
        "Llama.objects.filter(retired=False).exists()",
        "Llama.objects.filter(id=1).get_or_none()",
        "Llama.objects.filter(a=1).limit(10).offset(20).all()",
        "Llama.objects.filter(a=1).order_by('name').all()",
        "Llama.objects.filter(a=1).order_by('-created_at').all()",
    ],
)
def test_a_mechanical_query_is_translated(tmp_path, call) -> None:
    (finding,) = [
        f for f in _analyze(tmp_path, f"x = {call}\n") if f.construct == "orm_query"
    ]
    assert finding.tag == port.TRANSLATED, finding.message


@pytest.mark.parametrize(
    ("call", "why"),
    [
        ("Llama.objects.filter(name__icontains='b')", "rewrites the value"),
        ("Llama.objects.filter(name__startswith='b')", "rewrites the value"),
        ("Llama.objects.filter(retired__isnull=True)", "no negated form"),
        ("Llama.objects.filter(ranch__slug='x')", "relation target is cross-module"),
        ("Llama.objects.filter(tags__jsonb_has_any=['a'])", "container operator"),
        ("Llama.objects.filter(Q(a=1))", "positional Q object"),
        ("Llama.objects.filter(**criteria)", "keys are a runtime value"),
        ("Llama.objects.filter(a=1).first()", "wreath needs an explicit order"),
        ("Llama.objects.filter(a=1).delete()", "bulk delete has no query form"),
        ("Llama.objects.filter(a=1).values(['b'])", "rows come back as models"),
        ("Llama.objects.filter(a=1).update(b=2)", "a write, not a read"),
        ("Llama.objects.filter(a=1).order_by(column)", "runtime column name"),
        ("Llama.objects.filter(a=1).all(name__icontains='b')", "lookup in the tail"),
    ],
)
def test_a_query_needing_a_decision_is_not_translated(tmp_path, call, why) -> None:
    """The honesty half. Each of these *looks* like the mechanical case.

    ``all(name__icontains=...)`` is the one worth keeping: the terminal verb is
    on the mechanical list, so checking the verb alone would have let the very
    lookup the head test rejects through the back door.
    """
    findings = [
        f for f in _analyze(tmp_path, f"x = {call}\n") if f.construct == "orm_query"
    ]
    assert findings, call
    assert all(f.tag != port.TRANSLATED for f in findings), f"{call}: {why}"


def test_every_query_verb_stays_in_the_queries_category(tmp_path) -> None:
    """So the category floor keeps measuring the same thing it always did."""
    source = "\n".join(
        f"x{n} = Llama.objects.{verb}()"
        for n, verb in enumerate(["filter", "get_or_none", "create", "eagerly"])
    )
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id.startswith("orm.query")]
    assert {f.category for f in findings} == {"queries"}


# --- an explicitly ordered read -------------------------------------------------


def test_an_ordered_read_is_translated(tmp_path) -> None:
    """`order_by('-x').first()` is the one shape the generic verdict got wrong.

    Without a rule for ``order_by`` as a head verb the whole chain fell through
    to ``orm.query``, which is *unsupported* — so the read wreath expresses most
    directly was reported as the one it cannot do. The usual objection to
    ``first()`` is that an unordered "first" is not deterministic; this chain
    states the order, so the objection does not apply to it.
    """
    source = "x = await Trek.objects.order_by('-started_at').first()\n"
    (finding,) = [f for f in _analyze(tmp_path, source) if f.construct == "orm_query"]
    assert finding.rule_id == "orm.query.order_exact"
    assert finding.tag == port.TRANSLATED
    assert "fetch_one" in finding.message


def test_an_unordered_first_is_still_not_translated(tmp_path) -> None:
    """The promotion above must not leak to a chain with no order to carry."""
    (finding,) = [
        f
        for f in _analyze(tmp_path, "x = await Trek.objects.filter(a=1).first()\n")
        if f.construct == "orm_query"
    ]
    assert finding.tag != port.TRANSLATED


def test_an_ordered_read_by_a_runtime_column_is_not_translated(tmp_path) -> None:
    """A column named at runtime is a lookup this analyzer cannot do."""
    (finding,) = [
        f
        for f in _analyze(tmp_path, "x = Trek.objects.order_by(column).all()\n")
        if f.construct == "orm_query"
    ]
    assert finding.rule_id == "orm.query.order"
    assert finding.tag != port.TRANSLATED


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("Llama.objects.select_related('treks')", "orm.query.eager_exact"),
        ("Llama.objects.select_related('treks', 'ranch')", "orm.query.eager_exact"),
        ("Llama.objects.select_all()", "orm.query.eager"),          # every relation
        ("Llama.objects.select_related('ranch__owner')", "orm.query.eager"),  # nested
        ("Llama.objects.select_related(name)", "orm.query.eager"),  # runtime name
    ],
)
def test_an_eager_load_is_split_by_what_it_names(tmp_path, call, expected) -> None:
    """A named relation is one `.include(...)`; "all of them" is not a rename.

    Wreath has no `select_all()`, so that call needs someone to write out the
    relations the caller actually reads — which is a decision, not a rewrite.
    """
    assert _query_rules(tmp_path, f"x = {call}\n") == [expected]
