from __future__ import annotations

import pytest

from tests.orm.conftest import User
from wreath._migrations.deferred import DeferredDeclarationError, Recode, Retype
from wreath.passes import Sql

MAPPING = {"1": "planned", "2": "walking", "3": "done"}


def test_a_recode_names_the_column_it_converts() -> None:
    decl = Recode(User.name, mapping=MAPPING)
    assert decl.converts == "public.users.name"
    assert decl.pass_name == "recode_public_users_name"


def test_a_recode_derives_a_bounded_walk_with_no_gate() -> None:
    walk = Recode(User.name, mapping=MAPPING).build()
    assert walk.name == "recode_public_users_name"
    assert walk.gate is None


def test_the_walk_only_touches_rows_still_holding_an_old_value() -> None:
    walk = Recode(User.name, mapping=MAPPING).build()
    where = walk.work.where
    assert isinstance(where, Sql)
    assert "name IN (?, ?, ?)" in where.text
    assert where.values == ("1", "2", "3")
    assert not any(isinstance(value, list) for value in where.values)


def test_the_walk_sets_every_mapped_value() -> None:
    walk = Recode(User.name, mapping=MAPPING).build()
    statement = walk.work.set_["name"].text
    for old, new in MAPPING.items():
        assert f"WHEN '{old}' THEN '{new}'" in statement


def test_a_mapping_is_required() -> None:
    with pytest.raises(DeferredDeclarationError, match="enumerate its pairs"):
        Recode(User.name, mapping={})


def test_a_non_invertible_mapping_is_refused() -> None:
    with pytest.raises(DeferredDeclarationError, match="invertible"):
        Recode(User.name, mapping={"1": "done", "2": "done"})


def test_a_value_on_both_sides_is_refused() -> None:
    with pytest.raises(DeferredDeclarationError, match="both sides"):
        Recode(User.name, mapping={"1": "2", "2": "3"})


def test_a_literal_that_is_not_a_string_or_number_is_refused() -> None:
    with pytest.raises(DeferredDeclarationError, match="string or a number"):
        Recode(User.name, mapping={"1": None}).build()


def test_a_quote_in_a_mapping_value_is_escaped() -> None:
    walk = Recode(User.name, mapping={"1": "it's done"}).build()
    assert "'it''s done'" in walk.work.set_["name"].text


def test_a_recode_must_name_a_model_column() -> None:
    with pytest.raises(DeferredDeclarationError, match="model column"):
        Recode("name", mapping=MAPPING)


def test_a_retype_publishes_a_fact_about_the_old_column() -> None:
    decl = Retype(User.name, into="name_next", using="upper(name)")
    assert decl.publishes == "column:public.users.name"


def test_a_retype_derives_a_gate_that_publishes() -> None:
    walk = Retype(User.name, into="name_next", using="upper(name)").build()
    assert walk.gate is not None
    assert walk.gate.publishes == "column:public.users.name"
    assert walk.gate.scope == "pass"


def test_the_gate_verifies_with_the_constraint_the_swap_will_add() -> None:
    walk = Retype(User.name, into="name_next", using="upper(name)").build()
    assert walk.gate.verify.name == "name_next_present"
    assert walk.gate.verify.check_ == "name_next IS NOT NULL"


def test_the_walk_fills_only_unconverted_rows() -> None:
    walk = Retype(User.name, into="name_next", using="upper(name)").build()
    assert walk.work.where.text == "name_next IS NULL"
    assert walk.work.set_["name_next"].text == "upper(name)"


def test_verification_does_not_reuse_the_walks_predicate() -> None:
    walk = Retype(User.name, into="name_next", using="upper(name)").build()
    assert walk.work.where.text == "name_next IS NULL"
    assert walk.gate.verify.check_ == "name_next IS NOT NULL"


def test_draining_a_column_into_itself_is_refused() -> None:
    with pytest.raises(DeferredDeclarationError, match="names the column being drained"):
        Retype(User.name, into="name", using="upper(name)")


def test_a_retype_needs_an_expression() -> None:
    with pytest.raises(DeferredDeclarationError, match="produces the new value"):
        Retype(User.name, into="name_next", using=None)


def test_a_retype_has_nothing_to_scan_and_says_why() -> None:
    report = Retype(User.name, into="name_next", using="upper(name)").scan()
    assert report.shape == "retype"
    assert "no re-encode window" in report.explain()


@pytest.mark.parametrize("chunk", [0, -1, True], ids=["zero", "negative", "bool"])
def test_a_bad_chunk_size_is_refused(chunk) -> None:
    with pytest.raises(DeferredDeclarationError, match="positive int"):
        Recode(User.name, mapping=MAPPING, chunk=chunk)


def test_a_keyless_model_never_reaches_the_deferred_layer() -> None:
    from wreath.orm import Mapped, Model, column
    from wreath.orm.errors import DeclarationError
    from wreath.orm.types import Text

    with pytest.raises(DeclarationError, match="no primary-key column"):

        class Keyless(Model, table="keyless"):
            label: Mapped[str] = column(Text)


def test_a_composite_primary_key_pages_by_the_whole_key() -> None:
    from tests.orm.conftest import Membership

    walk = Recode(Membership.role, mapping=MAPPING).build()
    assert len(walk.units.keys) == 2


def test_the_ceiling_states_the_precondition_rather_than_switching_off_the_check() -> None:
    from wreath._migrations.deferred import PRECONDITION

    walk = Recode(User.name, mapping=MAPPING).build()
    assert walk.frontier.monotone == PRECONDITION
    assert "converts the past" in PRECONDITION


def test_a_scan_that_looked_at_nothing_refuses_rather_than_reporting_clean() -> None:
    from wreath.migrations import TransitionalContractUnproven, scan_transitional_reads

    with pytest.raises(TransitionalContractUnproven, match="nothing was scanned"):
        scan_transitional_reads(Recode(User.name, mapping=MAPPING))


def test_an_unsafe_read_refuses_and_names_it() -> None:
    from wreath.migrations import TransitionalContractUnproven, scan_transitional_reads
    from wreath.queries import Queries, query

    class Ranked(Queries[User]):
        worst = query(User.name >= "walking")

    with pytest.raises(TransitionalContractUnproven, match="ordered comparison"):
        scan_transitional_reads(Recode(User.name, mapping=MAPPING), queries=(Ranked,))


def test_a_clean_scan_returns_the_report() -> None:
    from wreath.migrations import scan_transitional_reads
    from wreath.queries import Queries, query

    class Fine(Queries[User]):
        exact = query(User.name == "planned")

    report = scan_transitional_reads(Recode(User.name, mapping=MAPPING), queries=(Fine,))
    assert report.blocking == ()
    assert len(report.rewrites) == 1


def test_a_retype_passes_the_strict_gate_with_nothing_to_scan() -> None:
    from wreath.migrations import scan_transitional_reads

    report = scan_transitional_reads(Retype(User.name, into="name_next", using="upper(name)"))
    assert report.shape == "retype"
