"""Thin presentation-only bindings for Wreath-metal migration commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import struct
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .migrations import (
    _build_native_artifact,
    _load_native_artifact,
    _qualified_history_table,
    _verify_native_chain,
    apply_single_artifact,
    connect_migration,
    detect_single,
    generate_single_plan,
    revert_single_artifact,
    unpack_named_plan,
)


async def _pending_passes(connection: Any) -> list[dict[str, Any]]:
    """Passes still converting a column, so an operator sees them before applying.

    A migration that narrows one of these columns will be refused, and finding
    that out from a failed deploy is worse than reading it here. Absent ledger
    table means no passes, which is the ordinary case for most applications.
    """
    from ._passes import ledger as _pass_ledger

    table = _pass_ledger.table_name("wreath")
    exists = await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
    if not exists:
        return []
    entries = await _pass_ledger.all_pending_facts(connection, schema="wreath")
    return [
        {
            "pass": entry.name,
            "tenant": entry.tenant,
            "guards": entry.fact,
            "phase": entry.phase,
            "holes_open": entry.holes_open,
        }
        for entry in entries
    ]


async def _detect(
    namespace: argparse.Namespace, application: Any, *, with_passes: bool = False
) -> dict[str, Any]:
    registries = getattr(application, "_orm_registries", {})
    registry = registries.get(namespace.database)
    if registry is None:
        known = ", ".join(sorted(registries)) or "none"
        raise ValueError(
            f"application has no ORM registry {namespace.database!r}; configured: {known}"
        )
    database = registry.database
    await database.start()
    workload = "read"
    try:
        try:
            database.pool(workload)
        except KeyError:
            workload = "write"
        connection = await database.acquire(workload)
        try:
            detection = await detect_single(registry, connection)
            # `detect` answers "is there drift"; `check` answers "is it safe to
            # apply". Pending passes belong to the second question, so only that
            # one pays for the ledger read.
            passes = await _pending_passes(connection) if with_passes else []
        finally:
            await database.release(workload, connection)
    finally:
        await database.stop()
    payload = {
        "current": detection.current,
        "pending_passes": passes,
        "operation_count": detection.diff.operation_count,
        "desired_fingerprint": detection.desired_fingerprint.hex(),
        "actual_fingerprint": detection.actual_fingerprint.hex(),
    }
    if with_passes:
        payload["transitional"] = _transitional_findings(registry)
    return payload


def _transitional_findings(registry: Any) -> list[dict[str, Any]]:
    """The forward scan: reads that would mean something else mid-conversion.

    Doc 16 called this an extension of the existing hazard scan. It is not --
    ``_downgrade_hazards`` runs only from a revert, and ``check`` today is drift
    detection. This is new machinery, and it needs no connection, so it runs
    beside the drift check rather than inside it.
    """
    from ._migrations.scan import scan_application

    return [
        {
            "column": report.column,
            "shape": report.shape,
            "examined": report.examined,
            "scanned_nothing": report.scanned_nothing,
            "summary": report.describe(),
            "unsafe": [
                {
                    "site": item.site,
                    "operation": item.operation,
                    "verdict": item.verdict,
                    "detail": item.detail,
                }
                for item in report.blocking
            ],
            "waived": [
                {"site": item.site, "operation": item.operation, "reason": item.waiver}
                for item in report.waived
            ],
            "rewritable": [
                {"site": item.site, "rewrite": item.rewrite} for item in report.rewrites
            ],
        }
        for report in scan_application(registry)
    ]




def _unpack_sql_tape(tape: bytes) -> list[dict[str, Any]]:
    if len(tape) < 12 or tape[:4] != b"WMS1":
        raise ValueError("native SQL tape is invalid")
    count = int.from_bytes(tape[8:12], "little")
    offset = 12
    statements: list[dict[str, Any]] = []
    for _ in range(count):
        if len(tape) - offset < 8:
            raise ValueError("native SQL tape is truncated")
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        if flags & ~3 or length > len(tape) - offset:
            raise ValueError("native SQL statement is invalid")
        statements.append(
            {
                "destructive": bool(flags & 1),
                "manual": bool(flags & 2),
                "sql": tape[offset : offset + length].decode("utf-8"),
            }
        )
        offset += length
    if offset != len(tape):
        raise ValueError("native SQL tape has trailing bytes")
    return statements


def _review_sql(statements: list[dict[str, Any]]) -> str:
    lines = [
        statement["sql"] if statement["sql"] else f"-- MANUAL operation {index + 1}"
        for index, statement in enumerate(statements)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _write_generation_directory(
    output: Path, payload: dict[str, Any], artifact: Any
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"migration output already exists: {output}")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    try:
        (temporary / "migration.bin").write_bytes(artifact.data)
        (temporary / "migration.sql").write_text(
            payload["review_sql"], encoding="utf-8"
        )
        (temporary / "migration.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


async def _generate(namespace: argparse.Namespace, application: Any) -> dict[str, Any]:
    registries = getattr(application, "_orm_registries", {})
    registry = registries.get(namespace.database)
    if registry is None:
        known = ", ".join(sorted(registries)) or "none"
        raise ValueError(
            f"application has no ORM registry {namespace.database!r}; configured: {known}"
        )
    database = registry.database
    await database.start()
    workload = "read"
    try:
        try:
            database.pool(workload)
        except KeyError:
            workload = "write"
        connection = await database.acquire(workload)
        try:
            generation = await generate_single_plan(registry, connection)
        finally:
            await database.release(workload, connection)
    finally:
        await database.stop()
    operations = unpack_named_plan(generation.plan.tape)
    statements = _unpack_sql_tape(generation.sql.tape)
    payload: dict[str, Any] = {
        "format": "WMP1+WMS1",
        "operation_count": generation.plan.operation_count,
        "manual_count": generation.sql.manual_count,
        "destructive_count": generation.sql.destructive_count,
        "desired_fingerprint": generation.desired_fingerprint.hex(),
        "actual_fingerprint": generation.actual_fingerprint.hex(),
        "operations": operations,
        "statements": statements,
        "review_sql": _review_sql(statements),
    }
    if namespace.output is not None:
        if namespace.migration_id is None:
            raise ValueError("--output requires --migration-id")
        if not namespace.initial and namespace.parent is None:
            raise ValueError("--output requires exactly one of --initial or --parent")
        try:
            migration_id = bytes.fromhex(namespace.migration_id)
            parent = bytes(32) if namespace.initial else bytes.fromhex(namespace.parent)
        except ValueError as error:
            raise ValueError("migration and parent checksums must be hexadecimal") from error
        artifact = _build_native_artifact(
            migration_id=migration_id,
            parent_checksum=parent,
            source_fingerprint=generation.actual_fingerprint,
            target_fingerprint=generation.desired_fingerprint,
            operation_tape=generation.diff.tape,
            named_plan=generation.plan.tape,
            sql_tape=generation.sql.tape,
        )
        output = Path(namespace.output)
        payload["artifact_checksum"] = artifact.checksum.hex()
        payload["migration_id"] = artifact.migration_id.hex()
        payload["parent_checksum"] = artifact.parent_checksum.hex()
        payload["output"] = str(output)
        _write_generation_directory(output, payload, artifact)
    return payload


def _read_artifact(path: str) -> bytes:
    return Path(path).read_bytes()


def _read_artifacts(paths: list[str]) -> tuple[bytes, ...]:
    return tuple(_read_artifact(path) for path in paths)


async def _apply(namespace: argparse.Namespace, application: Any) -> dict[str, Any]:
    registries = getattr(application, "_orm_registries", {})
    registry = registries.get(namespace.database)
    if registry is None:
        known = ", ".join(sorted(registries)) or "none"
        raise ValueError(
            f"application has no ORM registry {namespace.database!r}; configured: {known}"
        )
    dsn = os.environ.get(namespace.dsn_env)
    if not dsn:
        raise ValueError(
            f"migration credential variable {namespace.dsn_env!r} is not set; "
            "apply never falls back to request-pool credentials"
        )
    artifact_data = _read_artifact(namespace.artifact)
    connection = await connect_migration(dsn)
    try:
        result = await apply_single_artifact(
            registry,
            connection,
            artifact_data,
            allow_destructive=namespace.allow_destructive,
        )
    finally:
        await connection.close()
    return {
        "applied": True,
        "migration_id": result.migration_id.hex(),
        "checksum": result.checksum.hex(),
        "source_fingerprint": result.source_fingerprint.hex(),
        "target_fingerprint": result.target_fingerprint.hex(),
        "destructive_approved": result.destructive_approved,
    }


async def _down(namespace: argparse.Namespace, application: Any) -> dict[str, Any]:
    registries = getattr(application, "_orm_registries", {})
    registry = registries.get(namespace.database)
    if registry is None:
        known = ", ".join(sorted(registries)) or "none"
        raise ValueError(
            f"application has no ORM registry {namespace.database!r}; configured: {known}"
        )
    dsn = os.environ.get(namespace.dsn_env)
    if not dsn:
        raise ValueError(
            f"migration credential variable {namespace.dsn_env!r} is not set; "
            "downgrade never falls back to request-pool credentials"
        )
    artifact_data = _read_artifact(namespace.artifact)
    connection = await connect_migration(dsn)
    try:
        result = await revert_single_artifact(
            registry,
            connection,
            artifact_data,
            allow_destructive=namespace.allow_destructive,
            force=namespace.force,
        )
    finally:
        await connection.close()
    return {
        "reverted": True,
        "migration_id": result.migration_id.hex(),
        "checksum": result.checksum.hex(),
        "source_fingerprint": result.source_fingerprint.hex(),
        "target_fingerprint": result.target_fingerprint.hex(),
        "destructive_approved": result.destructive_approved,
        "forced": result.forced,
    }


async def _status(namespace: argparse.Namespace, application: Any) -> dict[str, Any]:
    artifact_data = _read_artifacts(namespace.artifacts)
    first = _load_native_artifact(artifact_data[0])
    if first.parent_checksum != bytes(32):
        raise ValueError("status requires a complete artifact chain beginning at the root")
    chain = _verify_native_chain(
        artifact_data,
        expected_parent=bytes(32),
        expected_source=first.source_fingerprint,
    )
    registries = getattr(application, "_orm_registries", {})
    registry = registries.get(namespace.database)
    if registry is None:
        known = ", ".join(sorted(registries)) or "none"
        raise ValueError(
            f"application has no ORM registry {namespace.database!r}; configured: {known}"
        )
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError("status requires exactly one resolved physical schema")
    schema = next(iter(schemas))
    dsn = os.environ.get(namespace.dsn_env)
    if not dsn:
        raise ValueError(
            f"migration credential variable {namespace.dsn_env!r} is not set; "
            "status never falls back to request-pool credentials"
        )
    connection = await connect_migration(dsn)
    try:
        detection = await detect_single(registry, connection)
        history_exists = await connection.fetchval(
            "SELECT to_regclass('wreath_migrations.history') IS NOT NULL"
        )
        history_tip = None
        if history_exists:
            history_tip = await connection.fetchrow(
                f"""SELECT checksum, target_fingerprint
                    FROM {_qualified_history_table()}
                    WHERE target_schema = $1
                    ORDER BY sequence DESC
                    LIMIT 1""",
                schema,
            )
        pending_passes = await _pending_passes(connection)
    finally:
        await connection.close()
    history_checksum = bytes(history_tip[0]) if history_tip is not None else None
    history_target = bytes(history_tip[1]) if history_tip is not None else None
    history_matches = (
        history_checksum == chain.checksum
        and history_target == chain.target_fingerprint
    )
    desired = detection.desired_fingerprint
    actual = detection.actual_fingerprint
    return {
        "current": (
            actual == chain.target_fingerprint == desired and history_matches
        ),
        "catalog_matches_code": actual == desired,
        "artifacts_match_code": chain.target_fingerprint == desired,
        "history_matches_artifacts": history_matches,
        "history_present": history_tip is not None,
        "pending_passes": pending_passes,
        "migration_count": chain.migration_count,
        "chain_checksum": chain.checksum.hex(),
        "history_checksum": history_checksum.hex() if history_checksum else None,
        "artifact_target_fingerprint": chain.target_fingerprint.hex(),
        "desired_fingerprint": desired.hex(),
        "actual_fingerprint": actual.hex(),
    }


def _show(namespace: argparse.Namespace) -> dict[str, Any]:
    artifact = _load_native_artifact(Path(namespace.artifact).read_bytes())
    operation_count = int.from_bytes(artifact.operation_tape[8:12], "little")
    statements = _unpack_sql_tape(artifact.sql_tape)
    payload = {
        "format": "WMA1",
        "migration_id": artifact.migration_id.hex(),
        "checksum": artifact.checksum.hex(),
        "parent_checksum": artifact.parent_checksum.hex(),
        "source_fingerprint": artifact.source_fingerprint.hex(),
        "target_fingerprint": artifact.target_fingerprint.hex(),
        "operation_count": operation_count,
        "manual_count": sum(statement["manual"] for statement in statements),
        "destructive_count": sum(statement["destructive"] for statement in statements),
        "artifact_bytes": len(artifact.data),
        "operation_tape_bytes": len(artifact.operation_tape),
        "named_plan_bytes": len(artifact.named_plan),
        "sql_tape_bytes": len(artifact.sql_tape),
    }
    return payload


def execute(
    namespace: argparse.Namespace,
    load_application: Callable[..., Any],
) -> int:
    """Execute one migration command without starting application lifespan."""
    exit_code = 0
    if namespace.migration_action in {"detect", "check"}:
        application = load_application(namespace.target, factory=namespace.factory)
        checking = namespace.migration_action == "check"
        payload = asyncio.run(_detect(namespace, application, with_passes=checking))
        title = "schema is current" if payload["current"] else "schema drift detected"
        if checking and not payload["current"]:
            exit_code = 1
        if checking:
            # A read that cannot be proven safe fails `check` on its own, even
            # when the schema is current: the schema being right is not the same
            # as it being safe to start converting values underneath it.
            unproven = [
                item
                for item in payload.get("transitional", ())
                if item["unsafe"] or item["scanned_nothing"]
            ]
            if unproven:
                exit_code = 1
                title = (
                    f"{len(unproven)} deferred migration(s) have reads that are not "
                    f"proven safe for the conversion window"
                )
    elif namespace.migration_action == "generate":
        application = load_application(namespace.target, factory=namespace.factory)
        payload = asyncio.run(_generate(namespace, application))
        title = f"generated {payload['operation_count']} review operations"
    elif namespace.migration_action == "show":
        payload = _show(namespace)
        title = f"migration {payload['migration_id']}"
    elif namespace.migration_action == "status":
        application = load_application(namespace.target, factory=namespace.factory)
        payload = asyncio.run(_status(namespace, application))
        title = "migration state is current" if payload["current"] else "migration state differs"
        if not payload["current"]:
            exit_code = 1
    elif namespace.migration_action == "apply":
        application = load_application(namespace.target, factory=namespace.factory)
        payload = asyncio.run(_apply(namespace, application))
        title = f"applied migration {payload['migration_id']}"
    elif namespace.migration_action == "down":
        application = load_application(namespace.target, factory=namespace.factory)
        payload = asyncio.run(_down(namespace, application))
        title = f"reverted migration {payload['migration_id']}"
    else:
        raise ValueError(f"unsupported migration command {namespace.migration_action!r}")
    if namespace.json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(title)
        for key, value in payload.items():
            if key not in {
                "current", "migration_id", "operations", "statements", "review_sql",
                "pending_passes",
            }:
                print(f"  {key.replace('_', ' ')}: {value}")
        for entry in payload.get("pending_passes", []):
            who = entry["pass"] if not entry["tenant"] else f"{entry['pass']}[{entry['tenant']}]"
            note = f", {entry['holes_open']} hole(s)" if entry["holes_open"] else ""
            print(
                f"  pending pass: {who} guards {entry['guards']} "
                f"({entry['phase']}{note}) -- a migration narrowing it is refused"
            )
        for operation in payload.get("operations", []):
            target = ".".join(
                part for part in (
                    operation["schema"], operation["table"], operation["name"]
                ) if part
            )
            print(f"  {operation['action']} {operation['kind']} {target}")
        if payload.get("review_sql"):
            print("\nreview SQL (not execution input):")
            print(payload["review_sql"], end="")
    return exit_code


__all__ = ["execute"]
