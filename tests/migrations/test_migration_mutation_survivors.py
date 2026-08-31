from __future__ import annotations

import struct
from types import SimpleNamespace
from typing import Any

import pytest

import wreath.migrations as migrations


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tenant_id", "1", "TenantState.tenant_id"),
        ("migration", -1, "TenantState.migration"),
        ("checksum", "1", "TenantState.checksum"),
        ("generation", -1, "TenantState.generation"),
        ("tenant_id", 1 << 64, "tenant_id and migration must fit in 64 bits"),
        ("checksum", 1 << 64, "checksum must fit in 64 bits"),
        ("generation", 1 << 32, "generation must fit in 32 bits"),
    ],
)
def test_tenant_state_refuses_each_invalid_field(field: str, value: object, message: str) -> None:
    values: dict[str, object] = {
        "tenant_id": 1,
        "migration": 2,
        "checksum": 3,
        "generation": 4,
        "status": migrations.HISTORY_VERIFIED,
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        migrations.TenantState(**values)


def test_tenant_state_refuses_an_unknown_status() -> None:
    with pytest.raises(ValueError, match="TenantState.status 99 is not a HISTORY_\\* code"):
        migrations.TenantState(1, 2, 3, 4, 99)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("target_migration", "1"),
        ("target_checksum", -1),
        ("target_checksum", "1"),
        ("directory_generation", -1),
        ("directory_generation", "1"),
    ],
)
def test_resolve_fleet_refuses_each_invalid_target(keyword: str, value: object) -> None:
    arguments: dict[str, object] = {
        "target_migration": 1,
        "target_checksum": 2,
        "directory_generation": 3,
    }
    arguments[keyword] = value

    with pytest.raises(ValueError, match=keyword):
        migrations.resolve_fleet([], **arguments)


@pytest.mark.parametrize("value", [-1, "1"])
def test_managed_policy_refuses_invalid_sample_sizes(value: object) -> None:
    with pytest.raises(ValueError, match="sample_size must be a non-negative integer"):
        migrations.ResolutionPolicy.managed(sample_size=value)


@pytest.mark.parametrize(
    ("action", "tenant", "holes_open", "fragments"),
    [
        ("drop", "", 0, ("drops app.widgets.value", "pass 'recode'", "phase scan")),
        (
            "alter",
            "tenant-7",
            2,
            ("changes the type of app.widgets.value", "'recode[tenant-7]'", "2 chunk(s)"),
        ),
    ],
)
def test_pending_pass_hazard_explains_every_operational_detail(
    action: str,
    tenant: str,
    holes_open: int,
    fragments: tuple[str, ...],
) -> None:
    hazard = migrations.PendingPassHazard(
        "app", "widgets", "value", action, "recode", tenant, "scan", holes_open
    )

    explanation = hazard.explain()

    assert all(fragment in explanation for fragment in fragments)
    assert ("chunk(s)" in explanation) is bool(holes_open)


def test_blocked_pass_error_distinguishes_waiting_from_holes() -> None:
    waiting = migrations.PendingPassHazard("app", "widgets", "a", "alter", "clean", "", "scan", 0)
    barred = migrations.PendingPassHazard(
        "app", "widgets", "b", "drop", "clean", "tenant-2", "scan", 1
    )

    waiting_message = str(migrations.MigrationBlockedByPass("app", (waiting,)))
    barred_message = str(migrations.MigrationBlockedByPass("app", (waiting, barred)))

    assert "retry <name>" not in waiting_message
    assert "retry <name>" in barred_message
    assert "clean[tenant-2]" in barred_message


@pytest.mark.parametrize("tenant", ["", "tenant-4"])
def test_recoded_column_hazard_names_the_pass_and_tenant(tenant: str) -> None:
    hazard = migrations.RecodedColumnHazard(
        "app", "widgets", "value", "alter", "recode", tenant, "scan", False
    )

    expected = "'recode'" if not tenant else f"'recode[{tenant}]'"

    assert expected in hazard.explain()


def test_descriptor_record_refuses_each_oversized_component() -> None:
    oversized = "x" * 65536

    for arguments in (
        (oversized, "table", "name", "signature"),
        ("schema", oversized, "name", "signature"),
        ("schema", "table", oversized, "signature"),
        ("schema", "table", "name", oversized),
    ):
        with pytest.raises(ValueError, match="exceeds 65535 bytes"):
            migrations._descriptor_record(*arguments[:3], 1, arguments[3])


def _column(**changes: Any) -> SimpleNamespace:
    values = {
        "pg_type": SimpleNamespace(oid=20, sql="bigint"),
        "oid": 20,
        "python_name": "value",
        "database_name": "value",
        "nullable": False,
        "generated_sql": None,
        "server_default": None,
        "unique": False,
        "primary_key": False,
        "indexed": False,
        "index_method": None,
        "index_with": (),
        "index_ops": None,
        "reference": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _spec(**changes: Any) -> SimpleNamespace:
    values = {
        "sql_namespace": "qualified",
        "schema": "app",
        "table": "widgets",
        "model_type": type("Widget", (), {}),
        "columns": (_column(primary_key=True),),
        "primary_key": (_column(database_name="id", primary_key=True),),
        "table_uniques": (),
        "table_indexes": (),
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _Registry:
    def __init__(self, specs: tuple[SimpleNamespace, ...]) -> None:
        self.specs = specs
        self.default_opclasses: dict[tuple[str, int], str] = {}

    def spec_for(self, model_type: type) -> SimpleNamespace:
        return next(spec for spec in self.specs if spec.model_type is model_type)


def test_registry_descriptor_refuses_an_unqualified_non_fleet_spec() -> None:
    for spec in (
        _spec(sql_namespace="tenant_search_path", schema="app"),
        _spec(sql_namespace="qualified", schema=""),
    ):
        with pytest.raises(ValueError, match="build it with fleet=True"):
            migrations._registry_descriptor(_Registry((spec,)))

    descriptor = migrations._registry_descriptor(
        _Registry((_spec(sql_namespace="tenant_search_path", schema=""),)), fleet=True
    )
    assert migrations.unpack_catalog_descriptor(descriptor)[0]["schema"] == ""


def test_registry_descriptor_preserves_default_generated_unique_and_index_facts() -> None:
    columns = (
        _column(database_name="id", primary_key=True),
        _column(database_name="defaulted", server_default="42"),
        _column(database_name="generated", generated_sql="defaulted + 1"),
        _column(database_name="email", unique=True),
        _column(database_name="unique_id", unique=True, primary_key=True),
        _column(database_name="indexed", indexed=True),
        _column(
            database_name="embedding",
            indexed=True,
            index_method="hnsw",
            index_ops="vector_l2_ops",
            index_with=(("m", 16),),
        ),
    )
    registry = _Registry((_spec(columns=columns, primary_key=(columns[0],)),))

    objects = migrations.unpack_catalog_descriptor(migrations._registry_descriptor(registry))
    facts = {(item["kind"], item["name"]): item["signature"] for item in objects}

    assert facts[("column", "defaulted")].endswith("\x1f42")
    assert facts[("column", "generated")].endswith("\x1fs\x1fdefaulted + 1")
    assert ("constraint", "u:email:::") in facts
    assert ("constraint", "u:unique_id:::") not in facts
    assert facts[("index", "i:indexed")] == "index:i:indexed:btree"
    assert facts[("index", "i:embedding:hnsw")].endswith("\x1fvector_l2_ops\x1fm=16")


def test_fleet_descriptor_neutralises_both_sides_of_a_foreign_key() -> None:
    parent_type = type("Parent", (), {})
    child_type = type("Child", (), {})
    parent_id = _column(database_name="id", primary_key=True)
    parent = _spec(
        model_type=parent_type,
        table="parents",
        columns=(parent_id,),
        primary_key=(parent_id,),
    )
    child_id = _column(database_name="id", primary_key=True)
    reference = SimpleNamespace(
        model_type=parent_type,
        column="id",
        on_delete="cascade",
        on_update="restrict",
        deferrable=True,
    )
    parent_ref = _column(database_name="parent_id", reference=reference)
    child = _spec(
        model_type=child_type,
        table="children",
        columns=(child_id, parent_ref),
        primary_key=(child_id,),
    )

    objects = migrations.unpack_catalog_descriptor(
        migrations._registry_descriptor(_Registry((parent, child)), fleet=True)
    )
    foreign = next(item for item in objects if item["name"].startswith("f:"))

    assert foreign["schema"] == ""
    assert foreign["name"] == "f:parent_id::parents:id"
    assert foreign["signature"].endswith(":cascade:restrict:1")


@pytest.mark.parametrize(
    ("descriptor", "message"),
    [
        (b"", "not WMD1"),
        (b"NOPE" + struct.pack("<II", 1, 0), "not WMD1"),
        (b"WMD1" + struct.pack("<II", 2, 0), "unsupported WMD1"),
        (b"WMD1" + struct.pack("<II", 1, 1), "truncated at object 0"),
        (
            b"WMD1" + struct.pack("<II", 1, 1) + struct.pack("<HHHHI", 0, 0, 0, 0, 1),
            "object 0 is invalid",
        ),
        (
            b"WMD1" + struct.pack("<II", 1, 1) + struct.pack("<HHHHI", 0, 2, 0, 0, 1) + b"x",
            "object 0 is invalid",
        ),
    ],
)
def test_catalog_descriptor_refuses_each_malformed_shape(descriptor: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        migrations.unpack_catalog_descriptor(descriptor)


def test_catalog_descriptor_refuses_a_short_header_with_the_right_magic() -> None:
    with pytest.raises(ValueError, match="not WMD1"):
        migrations.unpack_catalog_descriptor(b"WMD1")


@pytest.mark.asyncio
async def test_generate_baseline_checks_identity_and_resolves_operator_classes(monkeypatch) -> None:
    with pytest.raises(ValueError, match="exactly 16 bytes"):
        await migrations.generate_single_baseline(_Registry(()), None, migration_id=b"short")

    calls: list[tuple[object, object]] = []

    async def resolve(registry: object, connection: object) -> None:
        calls.append((registry, connection))

    registry = _Registry((_spec(schema="a"), _spec(schema="b")))
    connection = object()
    monkeypatch.setattr(migrations, "_resolve_default_opclasses", resolve)

    with pytest.raises(ValueError, match="exactly one resolved physical schema"):
        await migrations.generate_single_baseline(
            registry,
            connection,
            migration_id=bytes(16),
        )

    assert calls == [(registry, connection)]


def _empty_artifact(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "migration_id": bytes(16),
        "checksum": b"c" * 32,
        "parent_checksum": bytes(32),
        "source_fingerprint": b"f" * 32,
        "target_fingerprint": b"f" * 32,
        "operation_tape": b"WMO1" + struct.pack("<II", 1, 0),
        "named_plan": b"WMP1" + struct.pack("<II", 1, 0),
        "sql_tape": b"WMS1" + struct.pack("<II", 1, 0),
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_apply_single_artifact_refuses_an_impossible_skip(monkeypatch) -> None:
    artifact = _empty_artifact()

    async def bootstrap(connection: object) -> None:
        return None

    async def apply(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(migrations, "_load_native_artifact", lambda data: artifact)
    monkeypatch.setattr(migrations, "_bootstrap_migration_history", bootstrap)
    monkeypatch.setattr(migrations, "_apply_artifact_to_schema", apply)

    with pytest.raises(RuntimeError, match="already applied without being asked"):
        await migrations.apply_single_artifact(_Registry((_spec(),)), object(), b"artifact")


@pytest.mark.asyncio
async def test_adopt_baseline_refuses_each_artifact_and_registry_shape(monkeypatch) -> None:
    artifact = _empty_artifact(source_fingerprint=b"s" * 32)
    monkeypatch.setattr(migrations, "_load_native_artifact", lambda data: artifact)

    with pytest.raises(ValueError, match="source and target fingerprint"):
        await migrations.adopt_single_baseline(_Registry((_spec(),)), None, b"artifact")

    malformed_tapes = (
        ("operation_tape", b"WMO1" + struct.pack("<II", 1, 0) + b"x"),
        ("named_plan", b"NOPE" + struct.pack("<II", 1, 0)),
        ("sql_tape", b"WMS1" + struct.pack("<II", 1, 1)),
    )
    for field, tape in malformed_tapes:
        artifact = _empty_artifact(**{field: tape})
        monkeypatch.setattr(migrations, "_load_native_artifact", lambda data, value=artifact: value)
        with pytest.raises(ValueError, match="tape must be empty"):
            await migrations.adopt_single_baseline(_Registry((_spec(),)), None, b"artifact")

    artifact = _empty_artifact()
    monkeypatch.setattr(migrations, "_load_native_artifact", lambda data: artifact)
    with pytest.raises(ValueError, match="exactly one resolved physical schema"):
        await migrations.adopt_single_baseline(
            _Registry((_spec(schema="a"), _spec(schema="b"))), None, b"artifact"
        )


@pytest.mark.asyncio
async def test_adopt_baseline_resolves_operator_classes_before_compiling(monkeypatch) -> None:
    artifact = _empty_artifact()
    calls: list[str] = []

    async def resolve(registry: object, connection: object) -> None:
        calls.append("resolve")

    def compile_registry(registry: object) -> bytes:
        calls.append("compile")
        return b"image"

    monkeypatch.setattr(migrations, "_load_native_artifact", lambda data: artifact)
    monkeypatch.setattr(migrations, "_resolve_default_opclasses", resolve)
    monkeypatch.setattr(migrations, "_compile_registry_image", compile_registry)
    monkeypatch.setattr(migrations, "_fingerprint_image", lambda image: b"wrong" * 6 + b"xx")

    with pytest.raises(RuntimeError, match="current ORM fingerprint"):
        await migrations.adopt_single_baseline(_Registry((_spec(),)), object(), b"artifact")

    assert calls == ["resolve", "compile"]


class _Connection:
    def __init__(self, previous: object = None) -> None:
        self.previous = previous
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append(sql)
        return "OK"

    async def fetchval(self, *args: object) -> bool:
        return True

    async def fetchrow(self, *args: object) -> object:
        return self.previous


@pytest.mark.asyncio
async def test_adopt_baseline_refuses_existing_history(monkeypatch) -> None:
    artifact = _empty_artifact()

    async def resolve(*args: object) -> None:
        return None

    async def bootstrap(*args: object) -> None:
        return None

    monkeypatch.setattr(migrations, "_load_native_artifact", lambda data: artifact)
    monkeypatch.setattr(migrations, "_resolve_default_opclasses", resolve)
    monkeypatch.setattr(migrations, "_compile_registry_image", lambda registry: b"image")
    monkeypatch.setattr(migrations, "_fingerprint_image", lambda image: b"f" * 32)
    monkeypatch.setattr(migrations, "_bootstrap_migration_history", bootstrap)
    connection = _Connection(previous=(b"existing",))

    with pytest.raises(RuntimeError, match="already has Wreath history"):
        await migrations.adopt_single_baseline(_Registry((_spec(),)), connection, b"artifact")

    assert connection.executed == ["BEGIN", "ROLLBACK"]


@pytest.mark.asyncio
async def test_apply_entry_points_refuse_zero_or_multiple_schemas() -> None:
    artifact = migrations._build_native_artifact(
        migration_id=bytes(16),
        parent_checksum=bytes(32),
        source_fingerprint=bytes(32),
        target_fingerprint=bytes(32),
        operation_tape=b"WMO1" + struct.pack("<II", 1, 0),
        named_plan=b"WMP1" + struct.pack("<II", 1, 0),
        sql_tape=b"WMS1" + struct.pack("<II", 1, 0),
    )
    for function in (migrations.apply_single_artifact, migrations.revert_single_artifact):
        for specs in ((), (_spec(schema="a"), _spec(schema="b"))):
            registry = _Registry(specs)
            with pytest.raises(ValueError, match="exactly one resolved physical schema"):
                await function(registry, None, artifact.data)


@pytest.mark.asyncio
async def test_apply_fleet_refuses_empty_and_repeated_targets_before_loading_artifact() -> None:
    with pytest.raises(ValueError, match="at least one tenant schema"):
        await migrations.apply_fleet(None, b"invalid", [])
    with pytest.raises(ValueError, match="same schema twice: tenant"):
        await migrations.apply_fleet(None, b"invalid", ["tenant", "tenant"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_search_path", [False, True])
async def test_apply_artifact_binds_only_requested_search_paths(
    monkeypatch,
    bind_search_path: bool,
) -> None:
    artifact = _empty_artifact()
    connection = _Connection()

    async def decode(*args: object) -> migrations.NativeCatalogSnapshot:
        return migrations.NativeCatalogSnapshot(b"image", b"descriptor")

    async def no_hazards(*args: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    monkeypatch.setattr(migrations, "_fingerprint_image", lambda image: b"f" * 32)
    monkeypatch.setattr(migrations, "_pending_pass_hazards", no_hazards)

    await migrations._apply_artifact_to_schema(
        connection,
        artifact,
        schema="tenant",
        allow_destructive=False,
        skip_if_applied=False,
        bind_search_path=bind_search_path,
    )

    bound = 'SET LOCAL search_path TO "tenant"' in connection.executed
    assert bound is bind_search_path


@pytest.mark.asyncio
async def test_apply_artifact_refuses_a_non_root_without_history(monkeypatch) -> None:
    artifact = _empty_artifact(parent_checksum=b"p" * 32)
    connection = _Connection()

    with pytest.raises(RuntimeError, match="all-zero root checksum"):
        await migrations._apply_artifact_to_schema(
            connection,
            artifact,
            schema="tenant",
            allow_destructive=False,
            skip_if_applied=False,
        )

    assert connection.executed[-1] == "ROLLBACK"


@pytest.mark.asyncio
async def test_apply_artifact_refuses_live_catalog_drift(monkeypatch) -> None:
    artifact = _empty_artifact()
    connection = _Connection()

    async def decode(*args: object) -> migrations.NativeCatalogSnapshot:
        return migrations.NativeCatalogSnapshot(b"drift", b"descriptor")

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    monkeypatch.setattr(migrations, "_fingerprint_image", lambda image: b"d" * 32)

    with pytest.raises(RuntimeError, match="live catalog fingerprint"):
        await migrations._apply_artifact_to_schema(
            connection,
            artifact,
            schema="tenant",
            allow_destructive=False,
            skip_if_applied=False,
        )

    assert connection.executed == ["BEGIN", "ROLLBACK"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous", "fingerprint", "message"),
    [
        (None, b"t" * 32, "no Wreath history"),
        ((b"c" * 32, b"t" * 32), b"wrong" * 6 + b"xx", "live catalog fingerprint"),
    ],
)
async def test_revert_refuses_missing_history_and_catalog_drift(
    monkeypatch,
    previous: object,
    fingerprint: bytes,
    message: str,
) -> None:
    artifact = _empty_artifact(target_fingerprint=b"t" * 32)
    connection = _Connection(previous)

    async def bootstrap(*args: object) -> None:
        return None

    async def decode(*args: object) -> migrations.NativeCatalogSnapshot:
        return migrations.NativeCatalogSnapshot(b"image", b"descriptor")

    monkeypatch.setattr(migrations, "_load_native_artifact", lambda data: artifact)
    monkeypatch.setattr(migrations._postgres, "_migration_reverse_plan", lambda plan: plan)
    monkeypatch.setattr(
        migrations._postgres,
        "_migration_render_sql",
        lambda plan: artifact.sql_tape,
    )
    monkeypatch.setattr(migrations, "_downgrade_hazards", lambda registry, plan: ())
    monkeypatch.setattr(migrations, "_bootstrap_migration_history", bootstrap)
    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    monkeypatch.setattr(migrations, "_fingerprint_image", lambda image: fingerprint)

    with pytest.raises(RuntimeError, match=message):
        await migrations.revert_single_artifact(_Registry((_spec(),)), connection, b"artifact")

    assert connection.executed == ["BEGIN", "ROLLBACK"]


class _OversizedArtifact:
    def __len__(self) -> int:
        return 1 << 32


def test_native_chain_refuses_an_artifact_too_large_for_its_length_field() -> None:
    with pytest.raises(ValueError, match="exceeds WMC1 length limit"):
        migrations._verify_native_chain(
            (_OversizedArtifact(),),
            expected_parent=bytes(32),
            expected_source=bytes(32),
        )
