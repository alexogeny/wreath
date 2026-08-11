"""The Django tree that must not contain a translated query.

`wracklines_registry` is the authority for the two Django ORM rules that refuse
rather than map: an association table Django creates without declaring it, and
the three field types with no wreath column that stores the same thing. It also
carries the model whose behaviour does not move, which is the case
`foreign.django.model` was written for and could not reach.
"""

from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def registry_root() -> Path:
    return Path(__file__).parent / "foreign" / "wracklines_registry"


def _findings(registry_root: Path) -> list:
    return port.analyze(registry_root).findings


def test_a_many_to_many_needs_the_table_django_never_declared(registry_root: Path) -> None:
    """`tags` is not a column, so there is nothing for a column rule to map."""
    (finding,) = [f for f in _findings(registry_root) if f.rule_id == "orm.django.m2m"]
    assert finding.tag == port.UNSUPPORTED
    assert "association table" in finding.message


def test_the_three_fields_with_no_matching_column_are_refused_by_name(
    registry_root: Path,
) -> None:
    """`TimeField`, `DurationField` and `GenericIPAddressField`, one finding each.

    Each is refused where it is written rather than mapped to the nearest thing
    that fits, which is the same discipline the ormar path uses: a column that
    stores the wrong thing is worse than one a human had to choose.
    """
    lines = [f.line for f in _findings(registry_root) if f.rule_id == "orm.django.column_unmapped"]
    assert lines == [36, 37, 38]


def test_a_model_carrying_a_manager_and_a_save_is_not_a_header_rename(
    registry_root: Path,
) -> None:
    """Two models in one file, on opposite sides of the split.

    `Tag` is fields, so its class header is a rename. `Strandline` declares two
    managers and normalises a field in `save()`, and neither has a declarative
    form -- so the class is refused while its columns are still read one at a
    time underneath it.
    """
    by_line = {f.line: f for f in _findings(registry_root) if f.construct in
               ("orm_model", "django_model")}
    assert by_line[18].rule_id == "orm.django.model"
    assert by_line[18].tag == port.TRANSLATED
    assert by_line[30].rule_id == "foreign.django.model"
    assert by_line[30].tag == port.UNSUPPORTED


def test_no_query_in_a_django_tree_is_reported_as_translated(registry_root: Path) -> None:
    """The reason this fixture reaches its queryset through `_default_manager`.

    Two managers here, and `.objects` is only one of them. Any rule that reads
    a Django read as an ormar one puts a `translated` verdict, with a rewrite
    attached, on a query whose predicate is somewhere else entirely.
    """
    translated = [f for f in _findings(registry_root) if f.tag == port.TRANSLATED]
    assert {f.category for f in translated} == {"orm_models"}
    assert [f for f in _findings(registry_root) if f.category == "queries"] == []
