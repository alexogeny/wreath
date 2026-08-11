"""Queries held back by their arguments, migrations that are not derivable, and
the two module boundaries that stop attribution.

`fenland_ledger` is the corpus authority for the ORM verdicts that turn on what
was *passed*, not on which verb was called. Every query in `queries.py` has a
wreath spelling for its verb; each is reported anyway, because something in its
arguments has no answer this file can supply.
"""

from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def ledger_root() -> Path:
    return Path(__file__).parent / "corpus" / "fenland_ledger"


def _findings(ledger_root: Path, name: str) -> list:
    return [f for f in port.analyze(ledger_root).findings if f.file == name]


def _by_line(ledger_root: Path, name: str) -> dict[int, object]:
    return {f.line: f for f in _findings(ledger_root, name)}


def test_every_verb_in_the_query_module_is_held_back_by_its_arguments(
    ledger_root: Path,
) -> None:
    """Five reads, five different reasons, and not one of them is the verb.

    `filter`, `get` and `first` all translate elsewhere in the corpus. Here the
    relation target is outside the tree, the predicate is a positional `Q`, and
    the `first()` has no ordering -- so the same verbs land on the other side of
    the split, which is the whole point of classifying by argument.
    """
    lines = _by_line(ledger_root, "queries.py")
    assert lines[16].rule_id == "orm.query.filter"
    assert lines[20].rule_id == "orm.query.get"
    assert lines[24].rule_id == "orm.query.first"
    assert lines[28].rule_id == "orm.query.get_or_create"
    assert all(lines[line].tag == port.NEEDS_REVIEW for line in (16, 20, 24, 28))


def test_a_page_with_no_size_falls_through_to_the_generic_query(
    ledger_root: Path,
) -> None:
    """`paginate(page)` names a page and no size, so there is nothing to write."""
    finding = _by_line(ledger_root, "queries.py")[34]
    assert finding.rule_id == "orm.query"
    assert finding.tag == port.UNSUPPORTED


def test_a_foreign_key_to_a_model_outside_the_tree_is_not_typed(
    ledger_root: Path,
) -> None:
    """`Ledger` is in this tree and `Custodian` is not, so the two keys differ.

    The referenced primary key's type is what a wreath column needs, and it can
    only be read off a model the analyzer can open.
    """
    lines = _by_line(ledger_root, "models.py")
    assert lines[30].rule_id == "orm.fk_typed"
    assert lines[30].tag == port.TRANSLATED
    assert lines[31].rule_id == "orm.fk"
    assert lines[31].tag == port.NEEDS_REVIEW


def test_a_validator_that_is_a_rule_about_a_value_has_no_binding_form(
    ledger_root: Path,
) -> None:
    """Rejecting zero is not a restatement of `int`, so it is not `validator_literal`."""
    (finding,) = [f for f in _findings(ledger_root, "schemas.py") if f.construct == "validator"]
    assert finding.rule_id == "pydantic.validator"
    assert finding.tag == port.NEEDS_REVIEW


def test_a_star_import_is_reported_against_the_module_that_writes_it(
    ledger_root: Path,
) -> None:
    """Nothing `reports.py` calls can be attributed while the names are unresolved."""
    (finding,) = [
        f for f in _findings(ledger_root, "reports.py") if f.rule_id == "resolve.star_import"
    ]
    assert finding.tag == port.NEEDS_REVIEW
    assert finding.line == 1


def test_overriding_a_store_is_an_adapter_and_not_an_identity(ledger_root: Path) -> None:
    """`acting_as` answers "who is asking"; this override answers "where bytes go".

    They are separate rules because they have separate ports: one is a call on
    the test client, the other is an argument to `build_app`.
    """
    (finding,) = [
        f
        for f in _findings(ledger_root, "test_ledger.py")
        if f.construct == "test" and f.line == 21
    ]
    assert finding.rule_id == "test.dependency_override_adapter"
    assert finding.tag == port.NEEDS_REVIEW
    assert "build_app" in finding.message


def test_a_time_and_an_interval_column_make_a_table_no_model_can_produce(
    ledger_root: Path,
) -> None:
    lines = _by_line(ledger_root, "migrations/versions/0007_settlement_windows.py")
    assert lines[16].rule_id == "mig.unmodelled_type"
    assert lines[16].tag == port.NEEDS_REVIEW
    # A check constraint and an unqualified constraint drop are both schema ops
    # with no model form -- the drop does not even say what kind it is removing.
    assert lines[24].rule_id == "mig.schema_op"
    assert lines[29].rule_id == "mig.schema_op"


def test_a_row_by_row_backfill_and_an_expression_index_split_two_ways(
    ledger_root: Path,
) -> None:
    """`op.get_bind()` is a data decision; `op.execute` of DDL is raw SQL.

    Both are in the same revision on purpose: one is unsupported and one needs a
    reviewer, and collapsing them would give a backfill the same verdict as a
    string of DDL.
    """
    lines = _by_line(ledger_root, "migrations/versions/0008_currency_backfill.py")
    assert lines[18].rule_id == "mig.data"
    assert lines[18].tag == port.NEEDS_REVIEW
    assert lines[25].rule_id == "mig.raw_sql"
    assert lines[25].tag == port.UNSUPPORTED
    # The `alter_column`s the ORM image can derive stay translated around them.
    assert lines[26].rule_id == "mig.derived"
    assert lines[26].tag == port.TRANSLATED
