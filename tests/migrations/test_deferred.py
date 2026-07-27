"""The two deferred-migration shapes, and the pass each derives.

Declaration-time only: every refusal here is a startup error rather than a
half-converted table, which is the point of a declaration being a value.
"""

from __future__ import annotations

import pytest

from tests.orm.conftest import User
from wreath._migrations.deferred import DeferredDeclarationError, Recode, Retype
from wreath.passes import Sql

MAPPING = {"1": "planned", "2": "walking", "3": "done"}


# -- Shape A: Recode ----------------------------------------------------------


def test_a_recode_names_the_column_it_converts() -> None:
    decl = Recode(User.name, mapping=MAPPING)
    assert decl.converts == "public.users.name"
    assert decl.pass_name == "recode_public_users_name"


def test_a_recode_derives_a_bounded_walk_with_no_gate() -> None:
    """Shape A adds no column, so nothing narrows later and nothing waits."""
    walk = Recode(User.name, mapping=MAPPING).build()
    assert walk.name == "recode_public_users_name"
    assert walk.gate is None


def test_the_walk_only_touches_rows_still_holding_an_old_value() -> None:
    """Re-running a chunk is a no-op because the predicate excludes converted rows.

    One placeholder per mapped value rather than one bound array. ``= ANY(?)``
    is the tidier spelling and it does not survive contact with the driver: a
    parameter's type is inferred from its Python value and there is no case for
    ``list``, so the array form raised ``unsupported PostgreSQL value type`` the
    first time this walk reached a real server. Asserted on the shape here
    because a fake cannot notice the difference.
    """
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
    """Two old values to one new one cannot be widened back."""
    with pytest.raises(DeferredDeclarationError, match="invertible"):
        Recode(User.name, mapping={"1": "done", "2": "done"})


def test_a_value_on_both_sides_is_refused() -> None:
    """The walk could not tell a converted row from an unconverted one."""
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


# -- Shape B: Retype ----------------------------------------------------------


def test_a_retype_publishes_a_fact_about_the_old_column() -> None:
    """The fact names the column a later migration will narrow, not the new one."""
    decl = Retype(User.name, into="name_next", using="upper(name)")
    assert decl.publishes == "column:public.users.name"


def test_a_retype_derives_a_gate_that_publishes() -> None:
    walk = Retype(User.name, into="name_next", using="upper(name)").build()
    assert walk.gate is not None
    assert walk.gate.publishes == "column:public.users.name"
    assert walk.gate.scope == "pass"


def test_the_gate_verifies_with_the_constraint_the_swap_will_add() -> None:
    """Proven and later enforced are the same predicate, so a wrong walk cannot pass."""
    walk = Retype(User.name, into="name_next", using="upper(name)").build()
    assert walk.gate.verify.name == "name_next_present"
    assert walk.gate.verify.check_ == "name_next IS NOT NULL"


def test_the_walk_fills_only_unconverted_rows() -> None:
    walk = Retype(User.name, into="name_next", using="upper(name)").build()
    assert walk.work.where.text == "name_next IS NULL"
    assert walk.work.set_["name_next"].text == "upper(name)"


def test_verification_does_not_reuse_the_walks_predicate() -> None:
    """Doc 20 §10.3: the walk says "still needs converting", the gate says "none lack it"."""
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
    assert "no re-encode window" in report.describe()


# -- shared -------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [0, -1, True], ids=["zero", "negative", "bool"])
def test_a_bad_chunk_size_is_refused(chunk) -> None:
    with pytest.raises(DeferredDeclarationError, match="positive int"):
        Recode(User.name, mapping=MAPPING, chunk=chunk)


def test_a_keyless_model_never_reaches_the_deferred_layer() -> None:
    """The ORM refuses one at declaration, so the walk's own check is defensive.

    Recorded rather than deleted: `_primary_key` still refuses, because it reads
    `__wreath_columns__` rather than a validated spec and a future caller could
    hand it something the model metaclass never saw.
    """
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
    """`at_launch` refuses a key it cannot prove monotone; a deferred migration
    supplies the sentence that makes it sound, naming the primitive's own rule."""
    from wreath._migrations.deferred import PRECONDITION

    walk = Recode(User.name, mapping=MAPPING).build()
    assert walk.frontier.monotone == PRECONDITION
    assert "converts the past" in PRECONDITION


# -- the strict entry point ---------------------------------------------------


def test_a_scan_that_looked_at_nothing_refuses_rather_than_reporting_clean() -> None:
    """Doc 19's empty-denominator bug must not come back wearing a new hat."""
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
    """Shape B has no re-encode window, so an empty report is the right answer."""
    from wreath.migrations import scan_transitional_reads

    report = scan_transitional_reads(Retype(User.name, into="name_next", using="upper(name)"))
    assert report.shape == "retype"
