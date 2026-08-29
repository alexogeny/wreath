from __future__ import annotations

import pytest

from tests.orm.conftest import Post, User
from wreath._migrations.scan import (
    TransitionalHazard,
    clear_waivers,
    scan,
    scan_predicates,
    scan_select,
    transitional_read,
    waive_transitional,
    waiver_for,
)
from wreath.orm import Select
from wreath.queries import Param, Queries, query

#: A re-encode of `User.name`: numeric codes becoming names. Finite and total,
#: which is what makes the lattice decidable.
MAPPING = {"1": "planned", "2": "walking", "3": "done"}


def one(predicate) -> TransitionalHazard:
    found = scan_predicates((predicate,), User.name.column, MAPPING, site="test")
    assert len(found) == 1, f"expected exactly one finding, got {found}"
    return found[0]


def none(predicate) -> None:
    assert scan_predicates((predicate,), User.name.column, MAPPING, site="test") == []


def test_equality_widens_to_both_encodings() -> None:
    hazard = one(User.name == "planned")
    assert hazard.verdict == "rewritable"
    assert "'1'" in hazard.rewrite and "'planned'" in hazard.rewrite
    assert hazard.rewrite.startswith("name IN (")


def test_inequality_widens_to_exclude_both_encodings() -> None:
    hazard = one(User.name != "planned")
    assert hazard.verdict == "rewritable"
    assert hazard.rewrite.startswith("name NOT IN (")
    assert "'1'" in hazard.rewrite


def test_a_predicate_written_in_the_old_encoding_still_widens() -> None:
    hazard = one(User.name == "1")
    assert hazard.verdict == "rewritable"
    assert "'planned'" in hazard.rewrite


def test_membership_widens_every_value() -> None:
    hazard = one(User.name.in_(["planned", "walking"]))
    assert hazard.verdict == "rewritable"
    for token in ("'1'", "'planned'", "'2'", "'walking'"):
        assert token in hazard.rewrite


def test_a_null_test_is_unaffected_when_the_mapping_moves_no_nulls() -> None:
    none(User.name.is_null())


def test_a_null_test_is_refused_when_the_mapping_introduces_nulls() -> None:
    found = scan_predicates((User.name.is_null(),), User.name.column, {"1": None}, site="test")
    assert [item.verdict for item in found] == ["refused"]


def test_a_predicate_on_another_column_is_not_a_finding() -> None:
    none(User.email == "someone@example.test")


@pytest.mark.parametrize(
    "predicate",
    [
        User.name < "planned",
        User.name <= "planned",
        User.name > "planned",
        User.name >= "planned",
    ],
    ids=["lt", "le", "gt", "ge"],
)
def test_every_ordered_comparison_is_refused(predicate) -> None:
    hazard = one(predicate)
    assert hazard.verdict == "refused"
    assert "order" in hazard.detail


def test_the_refusal_names_monotonicity_so_a_total_mapping_can_be_waived() -> None:
    monotone = {"1": "aaa", "2": "bbb", "3": "ccc"}
    found = scan_predicates((User.name > "aaa",), User.name.column, monotone, site="test")
    assert found[0].verdict == "refused"
    assert "preserve order" in found[0].detail
    assert "waive" in found[0].detail.lower()


@pytest.mark.parametrize(
    "predicate",
    [User.name.like("plan%"), User.name.ilike("plan%")],
    ids=["like", "ilike"],
)
def test_pattern_matches_are_refused(predicate) -> None:
    assert one(predicate).verdict == "refused"


def test_a_value_the_mapping_does_not_mention_is_refused() -> None:
    hazard = one(User.name == "archived")
    assert hazard.verdict == "refused"
    assert "archived" in hazard.detail


def test_a_bound_parameter_is_undecidable_even_for_equality() -> None:
    hazard = one(User.name == Param("name"))
    assert hazard.verdict == "undecidable"
    assert "either encoding" in hazard.detail


def test_membership_against_a_param_is_not_expressible_today() -> None:
    with pytest.raises(TypeError, match="column-to-column"):
        User.name.in_([Param("a"), Param("b")])


def test_the_walk_reaches_inside_and_or_and_not() -> None:
    combined = (User.name == "planned") & (User.name > "walking")
    found = scan_predicates((combined,), User.name.column, MAPPING, site="test")
    assert sorted(item.verdict for item in found) == ["refused", "rewritable"]


def test_the_walk_reaches_inside_a_negation() -> None:
    found = scan_predicates((~(User.name > "walking"),), User.name.column, MAPPING, site="test")
    assert [item.verdict for item in found] == ["refused"]


def test_order_by_on_a_converting_column_is_refused() -> None:
    select = Select(User, (), (), (), (User.name.asc(),), None, None, False)
    found = scan_select(select, User.name.column, MAPPING, site="test")
    assert [item.operation for item in found] == ["ORDER BY"]
    assert "unstable between requests" in found[0].detail


def test_a_foreign_key_column_is_refused_from_the_model_declaration() -> None:

    class Registry:
        specs = ()

    report = scan(Post.author_id, {"1": "one"}, registry=Registry())
    assert any(item.operation == "join key" for item in report.hazards)


def test_a_column_referenced_by_another_model_is_a_join_key() -> None:
    class Spec:
        columns = (Post.author_id.column,)

    class Registry:
        specs = (Spec(),)

    report = scan(User.id, {"1": "one"}, registry=Registry())
    assert any(item.operation == "join key" for item in report.hazards)


class Reads(Queries[User]):
    by_name = query(User.name == "planned")
    ordered = query().order_by(User.name)
    ranked = query(User.name >= "walking")


def test_the_scan_finds_a_predicate_in_a_declared_query() -> None:
    report = scan(User.name, MAPPING, queries=(Reads,))
    assert report.examined >= 3
    assert any(item.site == "Reads.ranked" for item in report.hazards)
    assert any(item.site == "Reads.by_name" for item in report.rewrites)


def test_the_scan_finds_an_ordering_in_a_declared_query() -> None:
    report = scan(User.name, MAPPING, queries=(Reads,))
    assert any(
        item.site == "Reads.ordered" and item.operation == "ORDER BY" for item in report.hazards
    )


def test_a_report_that_scanned_nothing_says_so_rather_than_clean() -> None:
    report = scan(User.name, MAPPING)
    assert report.scanned_nothing
    assert report.blocking == ()
    assert "not the same as safe" in report.explain()


def test_discovery_finds_declarations_and_populations_in_a_module() -> None:
    import types

    from wreath._migrations.deferred import Recode
    from wreath._migrations.scan import (
        collect_declarations,
        collect_populations,
        scan_application,
    )

    module = types.ModuleType("app_models")
    module.restyle = Recode(User.name, mapping=MAPPING)
    module.Reads = Reads

    assert collect_declarations((module,)) == (module.restyle,)
    query_sets, _ = collect_populations((module,))
    assert Reads in query_sets

    reports = scan_application(modules=(module,))
    assert len(reports) == 1
    assert reports[0].column == "public.users.name"
    assert reports[0].blocking, "Reads.ranked is an ordered comparison"


def test_a_waiver_needs_a_written_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        transitional_read(User.name, reason="  ")


def test_a_waiver_records_the_column_and_the_reason() -> None:
    @transitional_read(User.name, reason="display only, nothing branches on it")
    def read() -> None: ...

    assert waiver_for(read, "public.users.name") == "display only, nothing branches on it"
    assert waiver_for(read, "public.users.email") is None


def test_a_declared_query_cannot_be_decorated_and_says_what_to_do_instead() -> None:

    class Slotted(Queries[User]):
        ranked = query(User.name >= "walking")

    with pytest.raises(TypeError, match="waive_transitional"):
        transitional_read(User.name, reason="display only")(Slotted.ranked)


def test_a_waived_hazard_is_counted_separately_and_stops_blocking() -> None:
    class Waived(Queries[User]):
        ranked = query(User.name >= "walking")

    waive_transitional(
        User.name,
        site="Waived.ranked",
        reason="admin console only, sorted for display",
    )
    try:
        report = scan(User.name, MAPPING, queries=(Waived,))
        assert report.blocking == ()
        assert len(report.waived) == 1
        assert "admin console" in report.waived[0].waiver
        assert "waived" in report.explain()
    finally:
        clear_waivers()
