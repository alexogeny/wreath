from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")


@pytest.fixture
def registry_root() -> Path:
    return Path(__file__).parent / "foreign" / "wracklines_registry"


def _findings(registry_root: Path) -> list:
    return port.analyze(registry_root).findings


def test_a_many_to_many_needs_the_table_django_never_declared(registry_root: Path) -> None:
    (finding,) = [f for f in _findings(registry_root) if f.rule_id == "orm.django.m2m"]
    assert finding.tag == port.UNSUPPORTED
    assert "association table" in finding.message


def test_the_three_fields_with_no_matching_column_are_refused_by_name(
    registry_root: Path,
) -> None:
    lines = [f.line for f in _findings(registry_root) if f.rule_id == "orm.django.column_unmapped"]
    assert lines == [36, 37, 38]


def test_a_model_carrying_a_manager_and_a_save_is_not_a_header_rename(
    registry_root: Path,
) -> None:
    by_line = {
        f.line: f for f in _findings(registry_root) if f.construct in ("orm_model", "django_model")
    }
    assert by_line[18].rule_id == "orm.django.model"
    assert by_line[18].tag == port.TRANSLATED
    assert by_line[30].rule_id == "foreign.django.model"
    assert by_line[30].tag == port.UNSUPPORTED


def test_no_query_in_a_django_tree_is_reported_as_translated(registry_root: Path) -> None:
    translated = [f for f in _findings(registry_root) if f.tag == port.TRANSLATED]
    assert {f.category for f in translated} == {"orm_models"}
    assert [f for f in _findings(registry_root) if f.category == "queries"] == []
