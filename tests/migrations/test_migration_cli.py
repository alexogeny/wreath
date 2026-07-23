"""Migration CLI exposes only operations backed by verified metal behavior."""

from __future__ import annotations

import argparse
import json
import struct
from types import SimpleNamespace

import pytest

from wreath import _migrations_cli
from wreath._cli import build_parser, main
from wreath.migrations import (
    MigrationDetection,
    MigrationGeneration,
    NativeMigrationDiff,
    NativeMigrationPlan,
    NativeMigrationSql,
    _build_native_artifact,
    _load_native_artifact,
)

MIGRATION_ID = bytes.fromhex("00112233445566778899aabbccddeeff")
EMPTY_TAPE = b"WMO1\x01\x00\x00\x00\x00\x00\x00\x00"
EMPTY_PLAN = b"WMP1\x01\x00\x00\x00\x00\x00\x00\x00"
EMPTY_SQL = b"WMS1\x01\x00\x00\x00\x00\x00\x00\x00"


def artifact_bytes() -> bytes:
    return _build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=bytes(32),
        source_fingerprint=b"s" * 32,
        target_fingerprint=b"t" * 32,
        operation_tape=EMPTY_TAPE,
        named_plan=EMPTY_PLAN,
        sql_tape=EMPTY_SQL,
    ).data


def test_migration_detect_parser_names_the_app_and_registry() -> None:
    namespace = build_parser().parse_args(
        ["migrations", "detect", "example:app", "--database", "billing", "--json"]
    )

    assert namespace.command == "migrations"
    assert namespace.migration_action == "detect"
    assert namespace.target == "example:app"
    assert namespace.database == "billing"
    assert namespace.json is True


def test_migration_check_parser_is_literal() -> None:
    namespace = build_parser().parse_args(
        ["migrations", "check", "example:app", "--database", "billing"]
    )

    assert namespace.migration_action == "check"
    assert namespace.target == "example:app"
    assert namespace.database == "billing"


def test_migration_generate_parser_is_literal() -> None:
    namespace = build_parser().parse_args(
        ["migrations", "generate", "example:app", "--database", "billing", "--json"]
    )

    assert namespace.migration_action == "generate"
    assert namespace.target == "example:app"
    assert namespace.database == "billing"
    assert namespace.json is True


def test_migration_status_parser_requires_ordered_artifacts() -> None:
    namespace = build_parser().parse_args(
        ["migrations", "status", "example:app", "0001.bin", "0002.bin", "--json"]
    )

    assert namespace.migration_action == "status"
    assert namespace.target == "example:app"
    assert namespace.artifacts == ["0001.bin", "0002.bin"]
    assert namespace.json is True


def test_migration_apply_parser_requires_explicit_artifact_and_supports_approval() -> None:
    namespace = build_parser().parse_args(
        [
            "migrations", "apply", "example:app", "migration.bin",
            "--allow-destructive", "--dsn-env", "DEPLOY_DATABASE_URL",
        ]
    )

    assert namespace.migration_action == "apply"
    assert namespace.artifact == "migration.bin"
    assert namespace.allow_destructive is True
    assert namespace.dsn_env == "DEPLOY_DATABASE_URL"


@pytest.mark.asyncio
async def test_apply_never_falls_back_to_request_pool_credentials(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_MIGRATION_DSN", raising=False)
    application = SimpleNamespace(
        _orm_registries={"main": SimpleNamespace(database=object())}
    )
    namespace = argparse.Namespace(
        database="main",
        dsn_env="MISSING_MIGRATION_DSN",
        artifact="migration.bin",
        allow_destructive=False,
    )

    with pytest.raises(ValueError, match="never falls back to request-pool credentials"):
        await _migrations_cli._apply(namespace, application)


def test_migration_show_parser_is_literal() -> None:
    namespace = build_parser().parse_args(
        ["migrations", "show", "0001/migration.bin", "--json"]
    )

    assert namespace.command == "migrations"
    assert namespace.migration_action == "show"
    assert namespace.artifact == "0001/migration.bin"
    assert namespace.json is True


def test_migration_show_verifies_and_prints_machine_readable_metadata(
    tmp_path, capsys
) -> None:
    path = tmp_path / "migration.bin"
    path.write_bytes(artifact_bytes())

    assert main(["migrations", "show", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "WMA1"
    assert payload["migration_id"] == MIGRATION_ID.hex()
    assert payload["operation_count"] == 0
    assert payload["checksum"] == artifact_bytes()[136:168].hex()


@pytest.mark.asyncio
async def test_detect_starts_only_selected_database_and_always_stops(monkeypatch) -> None:
    class Database:
        started = 0
        stopped = 0
        released = 0
        connection = object()

        async def start(self) -> None:
            self.started += 1

        async def stop(self) -> None:
            self.stopped += 1

        def pool(self, workload: str) -> object:
            assert workload == "read"
            return object()

        async def acquire(self, workload: str) -> object:
            assert workload == "read"
            return self.connection

        async def release(self, workload: str, connection: object) -> None:
            assert workload == "read" and connection is self.connection
            self.released += 1

    database = Database()
    registry = SimpleNamespace(database=database)
    application = SimpleNamespace(_orm_registries={"billing": registry})

    async def detect(selected_registry, connection) -> MigrationDetection:
        assert selected_registry is registry and connection is database.connection
        return MigrationDetection(b"d" * 32, b"a" * 32, NativeMigrationDiff(2, b"tape"))

    monkeypatch.setattr(_migrations_cli, "detect_single", detect)
    namespace = argparse.Namespace(database="billing")

    payload = await _migrations_cli._detect(namespace, application)

    assert payload["current"] is False
    assert payload["operation_count"] == 2
    assert database.started == database.stopped == database.released == 1


def test_named_plan_unpacking_is_bounded_and_literal() -> None:
    values = (b"app", b"widgets", b"name", b"old", b"new")
    tape = (
        b"WMP1"
        + (1).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + struct.pack(
            "<IIHHHHHH", 3, 2, *(len(value) for value in values), 0
        )
        + b"".join(values)
    )

    assert _migrations_cli._unpack_named_plan(tape) == [
        {
            "action": "alter",
            "kind": "column",
            "schema": "app",
            "table": "widgets",
            "name": "name",
            "before": "old",
            "after": "new",
        }
    ]


def test_review_sql_comes_from_the_bounded_native_statement_tape() -> None:
    sql = b'create table "app"."widgets" ();'
    tape = (
        b"WMS1"
        + (1).to_bytes(4, "little")
        + (2).to_bytes(4, "little")
        + struct.pack("<II", 0, len(sql))
        + sql
        + struct.pack("<II", 2, 0)
    )

    statements = _migrations_cli._unpack_sql_tape(tape)

    assert statements == [
        {
            "destructive": False,
            "manual": False,
            "sql": 'create table "app"."widgets" ();',
        },
        {"destructive": False, "manual": True, "sql": ""},
    ]
    assert _migrations_cli._review_sql(statements) == (
        'create table "app"."widgets" ();\n-- MANUAL operation 2\n'
    )


def test_check_returns_one_for_drift_and_keeps_json_output(monkeypatch, capsys) -> None:
    async def drift(namespace, application):
        assert application == "application"
        return {
            "current": False,
            "operation_count": 2,
            "desired_fingerprint": "dd",
            "actual_fingerprint": "aa",
        }

    monkeypatch.setattr(_migrations_cli, "_detect", drift)
    namespace = argparse.Namespace(
        migration_action="check",
        target="example:app",
        factory=False,
        database="main",
        json=True,
    )

    result = _migrations_cli.execute(namespace, lambda target, factory: "application")

    assert result == 1
    assert json.loads(capsys.readouterr().out)["operation_count"] == 2


@pytest.mark.asyncio
async def test_generate_can_write_one_verified_immutable_artifact_directory(
    monkeypatch, tmp_path
) -> None:
    class Database:
        async def start(self) -> None: pass
        async def stop(self) -> None: pass
        def pool(self, workload: str) -> object: return object()
        async def acquire(self, workload: str) -> object: return object()
        async def release(self, workload: str, connection: object) -> None: pass

    registry = SimpleNamespace(database=Database())
    application = SimpleNamespace(_orm_registries={"main": registry})

    async def generate(selected, connection) -> MigrationGeneration:
        assert selected is registry
        return MigrationGeneration(
            b"t" * 32,
            b"s" * 32,
            NativeMigrationDiff(0, EMPTY_TAPE),
            NativeMigrationPlan(0, b"WMP1" + (1).to_bytes(4, "little") + bytes(4)),
            NativeMigrationSql(
                0, 0, 0, b"WMS1" + (1).to_bytes(4, "little") + bytes(4)
            ),
        )

    monkeypatch.setattr(_migrations_cli, "generate_single_plan", generate)
    output = tmp_path / "0001"
    namespace = argparse.Namespace(
        database="main",
        output=str(output),
        migration_id=MIGRATION_ID.hex(),
        initial=True,
        parent=None,
    )

    payload = await _migrations_cli._generate(namespace, application)

    artifact = _load_native_artifact((output / "migration.bin").read_bytes())
    assert artifact.migration_id == MIGRATION_ID
    assert artifact.source_fingerprint == b"s" * 32
    assert artifact.target_fingerprint == b"t" * 32
    assert json.loads((output / "migration.json").read_text())["artifact_checksum"] == (
        artifact.checksum.hex()
    )
    assert (output / "migration.sql").read_text() == ""
    assert payload["output"] == str(output)


@pytest.mark.asyncio
async def test_status_requires_chain_code_and_catalog_to_agree(monkeypatch, tmp_path) -> None:
    artifact = _build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=bytes(32),
        source_fingerprint=b"s" * 32,
        target_fingerprint=b"t" * 32,
        operation_tape=EMPTY_TAPE,
        named_plan=EMPTY_PLAN,
        sql_tape=EMPTY_SQL,
    )
    path = tmp_path / "migration.bin"
    path.write_bytes(artifact.data)

    registry = SimpleNamespace(
        specs=(SimpleNamespace(schema="app"),), database=object()
    )
    application = SimpleNamespace(_orm_registries={"main": registry})

    class Connection:
        async def fetchval(self, sql: str) -> bool:
            return True

        async def fetchrow(self, sql: str, schema: str):
            return artifact.checksum, artifact.target_fingerprint

        async def close(self) -> None:
            return None

    async def detect(selected, connection) -> MigrationDetection:
        assert selected is registry
        return MigrationDetection(
            b"t" * 32, b"t" * 32, NativeMigrationDiff(0, EMPTY_TAPE)
        )

    async def connect(dsn: str) -> Connection:
        assert dsn == "postgresql://migration"
        return Connection()

    monkeypatch.setenv("TEST_MIGRATION_DSN", "postgresql://migration")
    monkeypatch.setattr(_migrations_cli, "detect_single", detect)
    monkeypatch.setattr(_migrations_cli, "connect_migration", connect)
    namespace = argparse.Namespace(
        artifacts=[str(path)], database="main", dsn_env="TEST_MIGRATION_DSN"
    )

    payload = await _migrations_cli._status(namespace, application)

    assert payload["current"] is True
    assert payload["catalog_matches_code"] is True
    assert payload["artifacts_match_code"] is True
    assert payload["chain_checksum"] == artifact.checksum.hex()


def test_migration_show_rejects_tampered_artifact(tmp_path, capsys) -> None:
    path = tmp_path / "migration.bin"
    data = bytearray(artifact_bytes())
    data[-1] ^= 1
    path.write_bytes(data)

    assert main(["migrations", "show", str(path)]) == 2
    assert "checksum mismatch" in capsys.readouterr().err
