import ast

import pytest

from wreath._port.analyzer.queries import query_rule

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


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("Llama.objects.filter(paddock_id=7)", "orm.query.filter_exact"),
        ("Llama.objects.get_or_none(id=7)", "orm.query.get_or_none_exact"),
        ("Llama.objects.get(id=7)", "orm.query.get_exact"),
        ("Llama.objects.create(name='Bea')", "orm.query.create_exact"),
        ("Llama.objects.all()", "orm.query.all"),
        # Argument-shaped, the same way `filter` is above: a named relation is
        # one `.include(...)`, while `select_all()` means *every* relation and
        # wreath has no such switch.
        ("Llama.objects.select_related('treks')", "orm.query.eager_exact"),
        ("Llama.objects.select_all()", "orm.query.select_all"),
        ("Llama.objects.prefetch_related('treks')", "orm.query.eager_exact"),
        ("Llama.objects.values(['name'])", "orm.query.values_exact"),
        ("Llama.objects.bulk_create([])", "orm.query.bulk"),
        ("Llama.objects.bulk_update([])", "orm.query.bulk"),
        ("Llama.objects.count()", "orm.query.count"),
        ("Llama.objects.exists()", "orm.query.exists"),
        ("Llama.objects.delete()", "orm.query.delete"),
        ("Llama.objects.first()", "orm.query.first"),
        (
            "Llama.objects.get_or_create(name='Bea')",
            "orm.query.get_or_create_exact",
        ),
        ("Llama.objects.update_or_create(name='Bea')", "orm.query.get_or_create"),
    ],
)
def test_a_query_verb_names_its_own_wreath_target(tmp_path, call, expected) -> None:
    assert _query_rules(tmp_path, f"x = {call}\n") == [expected]


def test_an_unrecognised_verb_still_reports(tmp_path) -> None:
    assert _query_rules(tmp_path, "x = Llama.objects.chunkify()\n") == ["orm.query"]


def test_a_bare_objects_attribute_reports(tmp_path) -> None:
    assert _query_rules(tmp_path, "manager = Llama.objects\n") == []
    assert _rule_ids(tmp_path, "manager = Llama.objects\n") == ["orm.manager_value"]


def test_a_chained_query_is_reported_once_at_its_head(tmp_path) -> None:
    source = "x = await Llama.objects.filter(paddock_id=7).order_by('name').all()\n"
    assert _query_rules(tmp_path, source) == ["orm.query.filter_exact"]


def test_two_separate_queries_are_two_findings(tmp_path) -> None:
    source = (
        "a = await Llama.objects.filter(paddock_id=7).all()\n"
        "b = await Trek.objects.get_or_none(id=1)\n"
    )
    assert _query_rules(tmp_path, source) == [
        "orm.query.filter_exact",
        "orm.query.get_or_none_exact",
    ]


def test_the_eager_load_message_names_include_and_the_guard(tmp_path) -> None:
    (finding,) = _analyze(tmp_path, "x = Llama.objects.select_related('treks')\n")
    assert "include(" in finding.message
    assert "include" in finding.message


def test_the_get_or_none_message_names_fetch_one(tmp_path) -> None:
    (finding,) = _analyze(tmp_path, "x = await Llama.objects.get_or_none(id=1)\n")
    assert "fetch_one" in finding.message


def test_the_create_message_names_the_session_contract(tmp_path) -> None:
    (finding,) = _analyze(tmp_path, "x = await Llama.objects.create(name='Bea')\n")
    assert "session.create" in finding.message


def test_the_get_message_names_the_required_row_contract(tmp_path) -> None:
    (finding,) = _analyze(tmp_path, "x = await Llama.objects.get(id=1)\n")
    assert "session.require" in finding.message


def test_the_emitter_never_rewrites_a_query(tmp_path) -> None:
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


@pytest.mark.parametrize(
    "call",
    [
        "Llama.objects.filter(paddock_id=7)",  # plain equality
        "Llama.objects.filter(grade__gte=3)",  # operator lookup
        "Llama.objects.filter(id__in=[1, 2])",  # membership
        "Llama.objects.filter(email__iexact='ADA@EXAMPLE.TEST')",
        "Llama.objects.filter(tags__jsonb_has_any=['a'])",
        "Llama.objects.filter(a=1).all(tags__jsonb_has_any=['b'])",
        "Llama.objects.filter(a=1, b=2)",  # several, all plain
        "Llama.objects.filter(retired=False).all()",  # mechanical terminal
        "Llama.objects.filter(retired=False).count()",
        "Llama.objects.filter(retired=False).exists()",
        "Llama.objects.filter(id=1).get_or_none()",
        "Llama.objects.filter(a=1).limit(10).offset(20).all()",
        "Llama.objects.filter(a=1).order_by('name').all()",
        "Llama.objects.filter(a=1).order_by('-created_at').all()",
        "Llama.objects.filter(a=1).delete()",
        "Llama.objects.filter(a=1).update(name='Bea')",
    ],
)
def test_a_mechanical_query_is_translated(tmp_path, call) -> None:
    (finding,) = [f for f in _analyze(tmp_path, f"x = {call}\n") if f.construct == "orm_query"]
    assert finding.tag == port.TRANSLATED, finding.message


@pytest.mark.parametrize(
    ("call", "why"),
    [
        ("Llama.objects.filter(retired__isnull=flag)", "which null test is not readable"),
        ("Llama.objects.filter(ranch__slug='x')", "relation target is cross-module"),
        ("Llama.objects.filter(Q(a=1))", "positional Q object"),
        ("Llama.objects.filter(**criteria)", "keys are a runtime value"),
        ("Llama.objects.filter(a=1).first()", "wreath needs an explicit order"),
        ("Llama.objects.filter(a=1).order_by(column)", "runtime column name"),
    ],
)
def test_a_query_needing_a_decision_is_not_translated(tmp_path, call, why) -> None:
    findings = [f for f in _analyze(tmp_path, f"x = {call}\n") if f.construct == "orm_query"]
    assert findings, call
    assert all(f.tag != port.TRANSLATED for f in findings), f"{call}: {why}"


def test_every_query_verb_stays_in_the_queries_category(tmp_path) -> None:
    source = "\n".join(
        f"x{n} = Llama.objects.{verb}()"
        for n, verb in enumerate(["filter", "get_or_none", "create", "eagerly"])
    )
    findings = [f for f in _analyze(tmp_path, source) if f.rule_id.startswith("orm.query")]
    assert {f.category for f in findings} == {"queries"}


def test_an_ordered_read_is_translated(tmp_path) -> None:
    source = "x = await Trek.objects.order_by('-started_at').first()\n"
    (finding,) = [f for f in _analyze(tmp_path, source) if f.construct == "orm_query"]
    assert finding.rule_id == "orm.query.order_exact"
    assert finding.tag == port.TRANSLATED
    assert "order_by" in finding.message


def test_an_unordered_first_is_still_not_translated(tmp_path) -> None:
    (finding,) = [
        f
        for f in _analyze(tmp_path, "x = await Trek.objects.filter(a=1).first()\n")
        if f.construct == "orm_query"
    ]
    assert finding.tag != port.TRANSLATED


def test_an_ordered_read_by_a_runtime_column_is_not_translated(tmp_path) -> None:
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
        ("Llama.objects.select_all()", "orm.query.select_all"),  # every relation
        ("Llama.objects.select_related('ranch__owner')", "orm.query.eager"),  # nested
        ("Llama.objects.select_related(name)", "orm.query.eager"),  # runtime name
    ],
)
def test_an_eager_load_is_split_by_what_it_names(tmp_path, call, expected) -> None:
    assert _query_rules(tmp_path, f"x = {call}\n") == [expected]


def test_a_tree_resolved_relationship_filter_is_translated(tmp_path) -> None:
    (tmp_path / "models.py").write_text(
        "import ormar\n"
        "class Ranch(ormar.Model):\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    slug: str = ormar.String(max_length=40)\n"
        "class Llama(ormar.Model):\n"
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    ranch: Ranch = ormar.ForeignKey(Ranch)\n",
        encoding="utf-8",
    )
    (tmp_path / "queries.py").write_text(
        "x = Llama.objects.filter(ranch__slug='north').all()\n",
        encoding="utf-8",
    )

    findings = port.analyze(tmp_path).findings
    query = next(item for item in findings if item.construct == "orm_query")

    assert query.rule_id == "orm.query.filter_exact"
    assert query.tag == port.TRANSLATED


def test_a_locally_built_plain_filter_mapping_is_translated(tmp_path) -> None:
    source = (
        "async def search(name):\n"
        "    terms = {}\n"
        "    if name:\n"
        "        terms['name'] = name\n"
        "    return await Llama.objects.filter(**terms).all()\n"
    )

    query = next(item for item in _analyze(tmp_path, source) if item.construct == "orm_query")

    assert query.rule_id == "orm.query.filter_exact"
    assert query.tag == port.TRANSLATED


def test_a_dynamic_filter_mapping_key_stays_reviewable(tmp_path) -> None:
    source = (
        "async def search(key, value):\n"
        "    terms = {key: value}\n"
        "    return await Llama.objects.filter(**terms).all()\n"
    )

    query = next(item for item in _analyze(tmp_path, source) if item.construct == "orm_query")

    assert query.rule_id == "orm.query.filter"


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ("terms.update(name=name)", "orm.query.filter_exact"),
        ("terms.update({'name': name})", "orm.query.filter_exact"),
        ("terms.update({key: name})", "orm.query.filter"),
        ("terms.update(name__icontains=name)", "orm.query.filter"),
    ],
)
def test_mapping_update_keeps_static_and_dynamic_filter_keys_apart(
    tmp_path, update, expected
) -> None:
    source = (
        "async def search(name, key='name'):\n"
        "    terms = {}\n"
        f"    {update}\n"
        "    return await Llama.objects.filter(**terms).all()\n"
    )
    query = next(item for item in _analyze(tmp_path, source) if item.construct == "orm_query")
    assert query.rule_id == expected


@pytest.mark.parametrize(
    "arguments",
    [
        "name=name, _defaults=defaults",
        "name=name, defaults=defaults",
        "name=name, **defaults",
    ],
)
def test_get_or_create_reserved_value_sources_never_become_creation_fields(
    tmp_path, arguments
) -> None:
    source = (
        "async def create(name, defaults):\n"
        f"    return await Llama.objects.get_or_create({arguments})\n"
    )
    assert _query_rules(tmp_path, source) == ["orm.query.get_or_create"]


def test_order_by_head_checks_its_own_argument_shape_directly() -> None:
    literal = ast.parse("Llama.objects.order_by('-created_at')").body[0].value
    dynamic = ast.parse("Llama.objects.order_by(column)").body[0].value

    assert isinstance(literal, ast.Call)
    assert isinstance(dynamic, ast.Call)
    assert query_rule("order_by", literal, model="Llama") == "orm.query.order_exact"
    assert query_rule("order_by", dynamic, model="Llama") == "orm.query.order"


def test_limit_head_checks_its_own_argument_shape_directly() -> None:
    literal = ast.parse("Llama.objects.limit(10)").body[0].value
    missing = ast.parse("Llama.objects.limit()").body[0].value

    assert isinstance(literal, ast.Call)
    assert isinstance(missing, ast.Call)
    assert query_rule("limit", literal, model="Llama") == "orm.query.page_exact"
    assert query_rule("limit", missing, model="Llama") == "orm.query"


def test_fields_must_be_consumed_by_values_before_another_chain_step(tmp_path) -> None:
    source = (
        "async def rows():\n"
        "    return await Llama.objects.filter(id=7).fields(['id']).limit(1).all()\n"
    )
    assert _query_rules(tmp_path, source) == ["orm.query.filter"]
