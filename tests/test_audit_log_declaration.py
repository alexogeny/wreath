from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from wreath.audit_log import (
    ERASURE_SETTING,
    REDACTED,
    AuditTrail,
    Change,
    Unattributed,
    actor,
    append_only_statements,
    audited,
    changed_fields,
    current_actor,
    declaration,
)
from wreath.log import KEEP_FOREVER, PostgresLog


class _Database:
    """Enough of `wreath.postgres.Database` to register statements."""

    def __init__(self) -> None:
        self.registered: dict[str, str] = {}

    def statement(self, name: str, sql: str, *, workload: str = "read") -> object:
        if name in self.registered:
            raise ValueError(f"duplicate PostgreSQL statement: {name}")
        self.registered[name] = sql
        return object()


class _Recording:
    """A connection that records what it was asked to run and answers plausibly."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append((sql, args))
        rows = sql.count("), (") + 1 if "VALUES" in sql else 0
        return f"INSERT 0 {rows}"


def _trail() -> tuple[AuditTrail, _Recording]:
    return AuditTrail(PostgresLog(_Database(), declaration())), _Recording()


def test_the_trail_is_declared_to_keep_its_rows_forever():
    # A compliance decision rather than a disk-space one, and the reason
    # `retention_pass` refuses it: nothing ages an audit record out by accident.
    assert declaration().retain is KEEP_FOREVER


def test_the_trail_declares_the_four_columns_a_record_needs():
    assert [column.name for column in declaration().columns] == [
        "actor",
        "op",
        "row_key",
        "fields",
    ]
    assert all(not column.null for column in declaration().columns)


def test_the_trails_schema_claim_installs_the_guard_as_version_two():
    component = declaration().schema_claim("audit_log")
    assert component.target_version == 2
    assert component.steps[0].statements == declaration().statements()
    assert component.steps[1].statements == append_only_statements("audit_records")


def test_a_records_stream_is_one_per_audited_row():
    # A stream per row rather than per table is what makes "everything that ever
    # happened to this row" a range scan, and it is also the unit an erasure
    # names.
    change = Change(table="public.photos", key="41", operation="update", actor="user:1", fields={})
    assert change.subject == "public.photos:41"


def test_the_append_only_ddl_qualifies_the_table_with_its_schema():
    statements = append_only_statements("audit_records", schema="wreath")
    assert all('"wreath".audit_records' in statement for statement in statements)


def test_an_unqualified_trail_emits_unqualified_ddl():
    # `schema=""` is the log's own spelling for "wherever the search path
    # points", and the trigger has to land on the same table the log writes to.
    # Emitting `"".audit_records` here would create the guard on a table that
    # does not exist and leave the real one writable.
    statements = append_only_statements("audit_records", schema="")
    assert all('"".' not in statement for statement in statements)
    assert statements[0] == "REVOKE UPDATE, DELETE, TRUNCATE ON audit_records FROM PUBLIC"
    assert "audit_records_guard()" in statements[1]
    assert "ON audit_records" in statements[3]
    assert "BEFORE TRUNCATE" in statements[5]


def test_the_ddl_is_a_revoke_and_a_trigger_because_either_alone_escapes():
    statements = append_only_statements("audit_records")
    assert statements[0].startswith("REVOKE UPDATE, DELETE, TRUNCATE")
    assert "CREATE OR REPLACE FUNCTION" in statements[1]
    assert statements[2].startswith("DROP TRIGGER IF EXISTS")
    assert "BEFORE UPDATE OR DELETE" in statements[3]
    assert statements[4].startswith("DROP TRIGGER IF EXISTS")
    assert "BEFORE TRUNCATE" in statements[5]


def test_the_trigger_refuses_update_unconditionally_and_delete_conditionally():
    body = append_only_statements("audit_records")[1]
    # The only escape is a `DELETE` in a transaction that declared itself an
    # erasure. There is no reading of this text under which an `UPDATE` passes.
    assert f"TG_OP = 'DELETE' AND current_setting('{ERASURE_SETTING}', true) = 'on'" in body
    assert "RETURN OLD" in body
    assert "is append-only" in body


def test_the_trigger_body_is_one_line_so_a_naive_splitter_cannot_cut_it():
    # A dollar-quoted body containing `;\n` survives a tuple and does not
    # survive the older call sites that split a `schema_sql()` blob on `";\n"`.
    body = append_only_statements("audit_records")[1]
    assert ";\n" not in body


def test_a_redaction_naming_nothing_is_still_a_valid_declaration():
    assert audited().redact == frozenset()


def test_a_redaction_set_is_sorted_and_frozen():
    facet = audited(redact={"b", "a"})
    assert facet.columns == ("a", "b")
    assert facet.redact == frozenset({"a", "b"})


def test_changed_fields_honours_the_dirty_mask_and_insert_sentinel() -> None:
    spec = SimpleNamespace(
        columns=(
            SimpleNamespace(python_name="name"),
            SimpleNamespace(python_name="secret"),
        ),
    )
    instance = SimpleNamespace(name="alpaca", secret="token")
    facet = audited(redact={"secret"})

    assert changed_fields(instance, spec, facet, mask=1) == {"name": "alpaca"}
    assert changed_fields(instance, spec, facet, mask=None) == {
        "name": "alpaca",
        "secret": REDACTED,
    }


@pytest.mark.parametrize("name", ["", None, 7])
def test_a_redaction_that_is_not_a_column_name_is_refused(name):
    with pytest.raises(ValueError, match="takes column names"):
        audited(redact=[name])


def test_an_unattributed_write_names_what_an_actor_is_for():
    trail, _ = _trail()
    # Not merely that a refusal happened: every refusal here mentions the actor.
    with pytest.raises(Unattributed, match="an audit record with nobody's name on it"):
        trail.attribute()
    assert trail.refused == 1


@pytest.mark.parametrize("name", ["", "   ", "\t\n", 7, None, b"user:41"])
def test_an_actor_that_is_not_a_usable_name_is_refused_where_it_is_bound(name):
    with pytest.raises(ValueError, match="non-empty name"):
        with actor(name):
            pass


def test_an_actor_is_read_from_the_task_that_bound_it():
    trail, _ = _trail()
    assert current_actor() is None
    with actor("job:nightly"):
        assert trail.attribute() == "job:nightly"
    assert trail.refused == 0


def test_a_flushs_records_are_one_statement_per_rung_not_one_each():
    import asyncio

    trail, connection = _trail()
    changes = [
        Change(
            table="public.photos",
            key=str(index),
            operation="insert",
            actor="user:41",
            fields={"caption": f"row {index}"},
        )
        for index in range(11)
    ]
    written = asyncio.run(trail.record_many(changes, connection=connection))

    assert written == 11
    # 11 = 8 + 2 + 1, so three statements rather than eleven.
    assert len(connection.executed) == 3
    assert trail.recorded == 11


def test_an_empty_batch_runs_no_statement_and_records_nothing():
    import asyncio

    trail, connection = _trail()
    assert asyncio.run(trail.record_many([], connection=connection)) == 0
    assert connection.executed == []
    assert trail.recorded == 0


def test_a_batched_record_carries_the_same_payload_a_single_one_does():
    import asyncio

    trail, connection = _trail()
    change = Change(
        table="public.photos",
        key="41",
        operation="update",
        actor="user:41",
        fields={"caption": "after", "exif_gps": REDACTED},
    )
    asyncio.run(trail.record_many([change], connection=connection))

    _, args = connection.executed[0]
    assert args[0] == "public.photos:41"
    assert args[1] == "user:41"
    assert args[2] == "update"
    assert args[3] == "41"
    # Sorted keys, so two records of the same change are byte-identical and a
    # reader diffing them sees a change rather than a re-ordering.
    assert args[4] == json.dumps({"caption": "after", "exif_gps": REDACTED}, sort_keys=True)


def test_a_value_json_cannot_render_becomes_a_string_rather_than_a_failure():
    import asyncio
    import uuid

    trail, connection = _trail()
    identifier = uuid.uuid4()
    asyncio.run(
        trail.record_many(
            [
                Change(
                    table="t",
                    key="1",
                    operation="insert",
                    actor="user:1",
                    fields={"id": identifier},
                )
            ],
            connection=connection,
        )
    )
    _, args = connection.executed[0]
    assert json.loads(args[4]) == {"id": str(identifier)}
