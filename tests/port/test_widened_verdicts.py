from __future__ import annotations

import pytest

port = pytest.importorskip("wreath.port")


def _analyze(tmp_path, source: str, name: str = "m.py"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return port.analyze(path).findings


def _rules(findings) -> set[str]:
    return {f.rule_id for f in findings}


@pytest.mark.parametrize("column_type", ["sa.Numeric(10, 2)", "sa.NUMERIC()", "sa.DECIMAL()"])
def test_a_numeric_column_is_derivable(tmp_path, column_type) -> None:
    findings = _analyze(
        tmp_path,
        "import sqlalchemy as sa\nfrom alembic import op\n\n"
        f'op.add_column("llamas", sa.Column("fleece_kg", {column_type}, nullable=False))\n',
    )
    assert "mig.derived" in _rules(findings)
    assert "mig.unmodelled_type" not in _rules(findings)


def test_an_ormar_uuid_column_is_a_uuid_not_a_char(tmp_path) -> None:
    findings = _analyze(
        tmp_path,
        "import ormar\nimport sqlalchemy as sa\nfrom alembic import op\n\n"
        'op.create_table("llamas",\n'
        '    sa.Column("id", ormar.fields.sqlalchemy_uuid.CHAR(36), nullable=False),\n'
        ")\n",
    )
    assert "mig.derived" in _rules(findings)
    assert "mig.unmodelled_type" not in _rules(findings)


def test_a_plain_char_column_is_still_unmodelled(tmp_path) -> None:
    findings = _analyze(
        tmp_path,
        "import sqlalchemy as sa\nfrom alembic import op\n\n"
        'op.add_column("llamas", sa.Column("code", sa.CHAR(2), nullable=False))\n',
    )
    assert "mig.unmodelled_type" in _rules(findings)


def test_a_foreign_key_finds_its_model_in_another_file(tmp_path) -> None:
    (tmp_path / "ranches.py").write_text(
        "import ormar\n\nbase = None\n\n\n"
        "class Ranch(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="ranches")\n'
        "    id: int = ormar.Integer(primary_key=True)\n",
        encoding="utf-8",
    )
    (tmp_path / "llamas.py").write_text(
        "import ormar\nfrom ranches import Ranch\n\nbase = None\n\n\n"
        "class Llama(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="llamas")\n'
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    ranch: Ranch = ormar.ForeignKey(Ranch)\n",
        encoding="utf-8",
    )
    rules = _rules(port.analyze(tmp_path).findings)
    assert "orm.fk_typed" in rules
    assert "orm.fk" not in rules


def test_two_models_of_the_same_name_resolve_to_neither(tmp_path) -> None:
    for index, pk in enumerate(("ormar.Integer", "ormar.UUID")):
        (tmp_path / f"app{index}.py").write_text(
            "import ormar\n\nbase = None\n\n\n"
            "class Ranch(ormar.Model):\n"
            f'    ormar_config = base.copy(tablename="ranches{index}")\n'
            f"    id: object = {pk}(primary_key=True)\n",
            encoding="utf-8",
        )
    (tmp_path / "llamas.py").write_text(
        "import ormar\nfrom app0 import Ranch\n\nbase = None\n\n\n"
        "class Llama(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="llamas")\n'
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    ranch: Ranch = ormar.ForeignKey(Ranch)\n",
        encoding="utf-8",
    )
    rules = _rules(port.analyze(tmp_path).findings)
    assert "orm.fk" in rules
    assert "orm.fk_typed" not in rules


def test_a_filter_followed_by_an_eager_load_is_still_determined(tmp_path) -> None:
    findings = _analyze(
        tmp_path,
        'rows = Llama.objects.filter(herd="north").select_related("ranch").all()\n',
    )
    assert "orm.query.filter_exact" in _rules(findings)


def test_an_eager_load_followed_by_a_filter_is_too(tmp_path) -> None:
    findings = _analyze(
        tmp_path,
        'rows = Llama.objects.select_related("ranch").filter(herd="north").all()\n',
    )
    assert "orm.query.eager_exact" in _rules(findings)


@pytest.mark.parametrize("lookup", ["name__icontains", "name__startswith", "name__iendswith"])
def test_a_pattern_lookup_is_a_translation_not_a_decision(tmp_path, lookup) -> None:
    findings = _analyze(tmp_path, f"rows = Llama.objects.filter({lookup}=term).all()\n")
    assert "orm.query.filter_exact" in _rules(findings)


def test_a_null_check_translates_only_when_its_value_is_written_out(tmp_path) -> None:
    assert "orm.query.filter_exact" in _rules(
        _analyze(tmp_path, "rows = Llama.objects.filter(retired_at__isnull=True).all()\n")
    )
    assert "orm.query.filter" in _rules(
        _analyze(tmp_path, "rows = Llama.objects.filter(retired_at__isnull=flag).all()\n")
    )


def test_a_relation_traversal_still_needs_a_person(tmp_path) -> None:
    findings = _analyze(tmp_path, 'rows = Llama.objects.filter(ranch__slug="north").all()\n')
    assert "orm.query.filter" in _rules(findings)


def test_a_status_wreath_has_a_class_for_is_translated(tmp_path) -> None:
    findings = _analyze(
        tmp_path, "from fastapi import HTTPException\nraise HTTPException(status_code=413)\n"
    )
    assert "exc.http_literal" in _rules(findings)


@pytest.mark.parametrize("status", [502, 503, 501])
def test_a_status_it_has_no_class_for_is_not(tmp_path, status) -> None:
    findings = _analyze(
        tmp_path,
        f"from fastapi import HTTPException\nraise HTTPException(status_code={status})\n",
    )
    assert "exc.http_unmapped" in _rules(findings)
    assert "exc.http_literal" not in _rules(findings)


def test_every_finding_needing_a_person_reaches_the_ported_file(tmp_path) -> None:
    source = (
        "import pandas as pd\n"
        "import httpx\n"
        "from alembic import op\n"
        "from fastapi.testclient import TestClient\n\n"
        "op.execute(\"UPDATE llamas SET herd = 'north'\")\n"
        "frame = pd.DataFrame()\n"
        "client = httpx.AsyncClient()\n"
    )
    path = tmp_path / "m.py"
    path.write_text(source, encoding="utf-8")
    flagged = {f.rule_id for f in port.analyze(path).findings if f.tag != port.TRANSLATED}
    emitted = port.emit_module(source)
    for rule_id in flagged:
        assert f"[{rule_id}])" in emitted, rule_id


def test_a_foreign_key_with_no_annotation_is_rewritten_like_any_other(tmp_path) -> None:
    source = (
        "import ormar\n\nbase = None\n\n\n"
        "class Ranch(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="ranches")\n'
        "    id: int = ormar.Integer(primary_key=True)\n"
        "\n\n"
        "class Llama(ormar.Model):\n"
        '    ormar_config = base.copy(tablename="llamas")\n'
        "    id: int = ormar.Integer(primary_key=True)\n"
        "    ranch = ormar.ForeignKey(Ranch, index=True)\n"
    )
    path = tmp_path / "m.py"
    path.write_text(source, encoding="utf-8")
    assert "orm.fk_typed" in _rules(port.analyze(path).findings)

    emitted = port.emit_module(source)

    assert "ranch_id: Mapped[int] = column(Int64, references=Ranch.id, index=True)" in emitted
    assert 'ranch = relationship(Ranch, load="raise")' in emitted
    assert "ormar.ForeignKey" not in emitted
    assert "no column type matching" not in emitted


def test_a_boto3_service_is_billed_once_per_module_and_split_by_service(tmp_path) -> None:
    source = (
        'import boto3\n\na = boto3.client("s3")\nb = boto3.client("s3")\nc = boto3.client("sqs")\n'
    )
    emitted = port.emit_module(source)
    assert emitted.count("[ext.boto3_s3])") == 1
    assert emitted.count("[ext.boto3])") == 1
