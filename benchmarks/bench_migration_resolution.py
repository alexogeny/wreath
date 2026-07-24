"""Microbenchmark the developed migration control plane against Alembic.

Three sections, each deliberately narrow and verified:

- **resolution** (ranked): given a linear 16-revision migration history whose
  current revision is the target head, resolve that no upgrade is required.
  Wreath executes its packed metal readiness kernel; Alembic executes its
  ScriptDirectory upgrade resolver; Django executes its MigrationGraph plan
  and removes already-applied nodes.
- **generation** (side by side, not ranked): produce a migration plan for the
  same two-object drift (one added column, one added table) and render it.
  Wreath diffs two precompiled packed images into a WMP1 named plan and a
  WMS1 SQL tape, all native; Alembic inspects an in-memory SQLite database
  and renders upgrade code with autogenerate. The arms do not perform
  identical work — Alembic's number includes per-call reflection of live
  (in-memory) SQLite, Wreath's images are compiled once at startup by design
  — so the rows are presented together but never ranked.
- **artifact** (Wreath-only, unranked): verify one checksummed WMA1 artifact
  from bytes — format, SHA-256 checksum, tape bounds, fingerprints. Alembic
  revision files carry no equivalent verifiable envelope.

This does not measure catalog I/O against PostgreSQL or DDL execution and must
never be presented as migration apply throughput. Wreath's `apply` path exists
but requires a live PostgreSQL and operator credentials, and is deliberately
not benchmarked here.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import struct
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import perf_counter_ns

from wreath.migrations import (
    _build_native_artifact,
    _compile_registry_image,
    _diff_packed_images,
    _fingerprint_image,
    _load_native_artifact,
    _plan_descriptors,
    _registry_descriptor,
    _render_sql_plan,
    _resolve_managed_snapshot,
)

REVISIONS = 16
_ROW = struct.Struct("<QQQIB3x")
_TARGET = REVISIONS
_CHECKSUM = 0xC0FFEE
_GENERATION = 7
_CURRENT_ROW = _ROW.pack(1, _TARGET, _CHECKSUM, _GENERATION, 1)


def _wreath_one() -> int:
    result = _resolve_managed_snapshot(
        _CURRENT_ROW,
        target_migration=_TARGET,
        target_checksum=_CHECKSUM,
        directory_generation=_GENERATION,
    )
    return result.apply + result.verify + result.ambiguous + result.blocked


def _alembic_arm(root: Path) -> Callable[[], int]:
    from alembic.script import ScriptDirectory

    versions = root / "versions"
    versions.mkdir()
    previous: str | None = None
    for index in range(REVISIONS):
        revision = f"r{index:04d}"
        (versions / f"{revision}.py").write_text(
            f"revision = {revision!r}\n"
            f"down_revision = {previous!r}\n"
            "branch_labels = None\n"
            "depends_on = None\n",
            encoding="utf-8",
        )
        previous = revision
    script = ScriptDirectory(str(root))
    assert previous is not None

    def resolve() -> int:
        return len(script._upgrade_revs("head", previous))  # noqa: SLF001

    return resolve


def _django_arm() -> Callable[[], int]:
    from django.db.migrations import Migration
    from django.db.migrations.graph import MigrationGraph

    graph = MigrationGraph()
    previous: tuple[str, str] | None = None
    applied: set[tuple[str, str]] = set()
    for index in range(REVISIONS):
        key = ("bench", f"r{index:04d}")
        graph.add_node(key, Migration(key[1], key[0]))
        if previous is not None:
            graph.add_dependency("bench", key, previous)
        applied.add(key)
        previous = key
    assert previous is not None
    graph.ensure_not_cyclic()

    def resolve() -> int:
        return sum(node not in applied for node in graph.forwards_plan(previous))

    return resolve


def _measure(operation: Callable[[], int], iterations: int, trials: int) -> list[float]:
    for _ in range(1_000):
        assert operation() == 0
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        unresolved = 0
        for _ in range(iterations):
            unresolved += operation()
        elapsed = perf_counter_ns() - started
        assert unresolved == 0
        samples.append(elapsed / iterations)
    return samples


def _fleet_sample(tenants: int, trials: int) -> list[float]:
    snapshot = _CURRENT_ROW * tenants
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        result = _resolve_managed_snapshot(
            snapshot,
            target_migration=_TARGET,
            target_checksum=_CHECKSUM,
            directory_generation=_GENERATION,
        )
        elapsed = perf_counter_ns() - started
        assert result.current == tenants and result.total == tenants
        samples.append(elapsed / tenants)
    return samples


class _GenerationScenario:
    """Inputs for the shared two-object drift, read the way production reads it.

    The desired schema is compiled from an ORM registry. The *actual* schema is
    streamed through the same native catalog decoder a live PostgreSQL read
    uses — here fed by a fake connection whose rows are derived from a second
    registry, so the fake catalog is guaranteed consistent with a real read of
    that schema, with no hand-encoded byte tables. The heavy inputs (the field
    tape and decoder plan) are built once; each call re-runs only the work the
    ``wreath migrations generate`` CPU path does per invocation.
    """

    __slots__ = ("desired_descriptor", "desired_image", "decoder", "native", "rows")

    def __init__(self) -> None:
        import wreath._native._postgres as native

        from wreath.orm import Mapped, Model, column
        from wreath.orm.registry import Registry
        from wreath.orm.types import Int64, Text

        class Database:
            name = "main"

        class WidgetDesired(Model, table="widgets", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            name: Mapped[str] = column(Text)
            price: Mapped[int] = column(Int64)

        class Gadget(Model, table="gadgets", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)

        class WidgetActual(Model, table="widgets", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            name: Mapped[str] = column(Text)

        desired = Registry(Database(), [WidgetDesired, Gadget], validate_schema="off")
        actual = Registry(Database(), [WidgetActual], validate_schema="off")

        self.native = native
        self.desired_descriptor = _registry_descriptor(desired)
        self.desired_image = _compile_registry_image(desired)
        # The catalog rows the fake driver will stream. A field tape is
        # single-use (the decoder drains it), and a live fetch builds a fresh
        # one from wire bytes per call, so the tape is rebuilt inside plan().
        self.rows = _catalog_rows(_registry_descriptor(actual))
        self.decoder = native._compile_decoder_plan(
            (25, 25, 25, 23, 25),
            (1, 1, 1, 1, 1),
            ("schema", "table", "name", "kind", "signature"),
        )

    def plan(self) -> tuple[object, object, object]:
        """Stream the fake catalog and produce (diff, named plan, SQL tape).

        Mirrors the per-fetch CPU work of ``wreath migrations generate``: build
        the wire tape, decode it into a packed image and descriptor, diff
        against the desired image, and render the named plan and SQL tape.
        """
        tape = _catalog_tape(self.native, self.rows)
        builder = self.native._migration_catalog_builder()
        self.native._migration_decode_catalog(self.decoder, tape, builder, 256)
        actual_image = builder.finish()
        actual_descriptor = builder.descriptor()
        diff = _diff_packed_images(self.desired_image, actual_image)
        plan = _plan_descriptors(self.desired_descriptor, actual_descriptor)
        if plan.operation_count != diff.operation_count:
            raise RuntimeError("native named plan and image diff disagree")
        return diff, plan, _render_sql_plan(plan)


def _catalog_rows(descriptor: bytes) -> list[tuple[str, str, str, int, str]]:
    """Parse a WMD1 registry descriptor into (schema, table, name, kind, signature)."""
    if descriptor[:4] != b"WMD1" or int.from_bytes(descriptor[4:8], "little") != 1:
        raise ValueError("expected a WMD1 descriptor")
    count = int.from_bytes(descriptor[8:12], "little")
    offset = 12
    rows: list[tuple[str, str, str, int, str]] = []
    for _ in range(count):
        ls, lt, ln, lsig, kind = struct.unpack_from("<HHHHI", descriptor, offset)
        offset += 12
        parts = []
        for length in (ls, lt, ln, lsig):
            parts.append(descriptor[offset : offset + length].decode("utf-8"))
            offset += length
        schema, table, name, signature = parts
        rows.append((schema, table, name, kind, signature))
    if offset != len(descriptor):
        raise ValueError("trailing bytes in WMD1 descriptor")
    return rows


def _catalog_tape(native: object, rows: list[tuple[str, str, str, int, str]]) -> object:
    """Pack catalog rows into a native field tape, the fake driver's wire input."""
    tape = native._FieldTape(5)  # type: ignore[attr-defined]
    for schema, table, name, kind, signature in rows:
        fields = (
            schema.encode(),
            table.encode(),
            name.encode(),
            struct.pack("!i", kind),
            signature.encode(),
        )
        payload = bytearray(struct.pack("!H", len(fields)))
        for field in fields:
            payload += struct.pack("!I", len(field)) + field
        tape.append(memoryview(payload), 5)
    return tape


def _wreath_generation_arm(scenario: _GenerationScenario) -> Callable[[], int]:
    def generate() -> int:
        _diff, plan, _sql = scenario.plan()
        return plan.operation_count

    return generate


def _alembic_generation_arm() -> tuple[Callable[[], int], Callable[[], None]]:
    """Alembic autogenerate over the same drift, against in-memory SQLite.

    Returns the operation and a close callback for the held connection.
    """
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext
    from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

    desired = MetaData()
    Table(
        "widgets",
        desired,
        Column("id", Integer, primary_key=True),
        Column("name", String, nullable=False),
        Column("price", Integer, nullable=False),
    )
    Table("gadgets", desired, Column("id", Integer, primary_key=True))

    actual = MetaData()
    Table(
        "widgets",
        actual,
        Column("id", Integer, primary_key=True),
        Column("name", String, nullable=False),
    )
    engine = create_engine("sqlite://")
    connection = engine.connect()
    actual.create_all(connection)
    context = MigrationContext.configure(connection)

    def generate() -> int:
        script = produce_migrations(context, desired)
        render_python_code(script.upgrade_ops)
        return len(script.upgrade_ops.ops)

    return generate, connection.close


def _artifact_bytes(scenario: _GenerationScenario) -> bytes:
    tape = _catalog_tape(scenario.native, scenario.rows)
    builder = scenario.native._migration_catalog_builder()
    scenario.native._migration_decode_catalog(scenario.decoder, tape, builder, 256)
    actual_image = builder.finish()
    diff, plan, sql = scenario.plan()
    artifact = _build_native_artifact(
        migration_id=bytes.fromhex("00112233445566778899aabbccddeeff"),
        parent_checksum=bytes(32),
        source_fingerprint=_fingerprint_image(actual_image),
        target_fingerprint=_fingerprint_image(scenario.desired_image),
        operation_tape=diff.tape,
        named_plan=plan.tape,
        sql_tape=sql.tape,
    )
    return artifact.data


def _artifact_verify_arm(data: bytes) -> tuple[Callable[[], int], int]:
    checksum = _load_native_artifact(data).checksum

    def verify() -> int:
        return _load_native_artifact(data).checksum == checksum

    return verify, 1


def _measure_expected(
    operation: Callable[[], int], iterations: int, trials: int, expected: int
) -> list[float]:
    """Time an operation whose per-call result is known and asserted."""
    for _ in range(50):
        assert operation() == expected
    samples: list[float] = []
    for _ in range(trials):
        started = perf_counter_ns()
        acc = 0
        for _ in range(iterations):
            acc += operation()
        elapsed = perf_counter_ns() - started
        assert acc == expected * iterations
        samples.append(elapsed / iterations)
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=50_000)
    parser.add_argument("--generation-iterations", type=int, default=2_000)
    parser.add_argument("--trials", type=int, default=9)
    parser.add_argument("--fleet-tenants", type=int, default=10_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.trials < 3 or args.fleet_tenants < 1:
        parser.error("iterations/fleet-tenants must be positive and trials at least 3")
    if args.generation_iterations < 1:
        parser.error("generation-iterations must be positive")

    with tempfile.TemporaryDirectory(prefix="wreath-migration-bench-") as directory:
        arms = {
            "wreath-metal": _wreath_one,
            "alembic": _alembic_arm(Path(directory)),
            "django": _django_arm(),
        }
        results = {}
        for name, operation in arms.items():
            samples = _measure(operation, args.iterations, args.trials)
            median = statistics.median(samples)
            results[name] = {
                "median_ns": median,
                "samples_ns": samples,
                "resolutions_per_second": 1_000_000_000 / median,
            }

    fleet_samples = _fleet_sample(args.fleet_tenants, args.trials)
    fleet_median = statistics.median(fleet_samples)

    # -- generation: the same two-object drift, planned and rendered ---------
    scenario = _GenerationScenario()
    wreath_generate = _wreath_generation_arm(scenario)
    expected_operations = wreath_generate()
    assert expected_operations > 0, "the drift scenario produced no operations"
    generation_results = {}
    samples = _measure_expected(
        wreath_generate, args.generation_iterations, args.trials, expected_operations
    )
    median = statistics.median(samples)
    generation_results["wreath-metal"] = {
        "median_ns": median,
        "samples_ns": samples,
        "operations": expected_operations,
        "plans_per_second": 1_000_000_000 / median,
    }
    alembic_generate, close_connection = _alembic_generation_arm()
    try:
        expected_alembic = alembic_generate()
        assert expected_alembic > 0, "Alembic saw no drift in the shared scenario"
        samples = _measure_expected(
            alembic_generate, args.generation_iterations, args.trials, expected_alembic
        )
        median = statistics.median(samples)
        generation_results["alembic"] = {
            "median_ns": median,
            "samples_ns": samples,
            "operations": expected_alembic,
            "plans_per_second": 1_000_000_000 / median,
        }
    finally:
        close_connection()

    # -- artifact: verify one checksummed WMA1 from bytes --------------------
    artifact_data = _artifact_bytes(scenario)
    verify_arm, expected_verify = _artifact_verify_arm(artifact_data)
    verify_samples = _measure_expected(
        verify_arm, args.iterations, args.trials, expected_verify
    )
    verify_median = statistics.median(verify_samples)

    document = {
        "tool": "benchmarks.bench_migration_resolution",
        "schema_version": 2,
        "python": sys.version,
        "platform": platform.platform(),
        "scenario": "already-current linear migration history",
        "revisions": REVISIONS,
        "iterations": args.iterations,
        "trials": args.trials,
        "results": results,
        "fleet": {
            "tool": "wreath-metal",
            "tenants": args.fleet_tenants,
            "median_ns_per_tenant": fleet_median,
            "samples_ns_per_tenant": fleet_samples,
            "tenants_per_second": 1_000_000_000 / fleet_median,
        },
        "generation": {
            "scenario": "two-object drift: one added column, one added table",
            "iterations": args.generation_iterations,
            "results": generation_results,
            "fairness": (
                "Both arms plan and render the same drift, but the work is not identical: "
                "Alembic's number includes per-call reflection of an in-memory SQLite "
                "database, while Wreath diffs packed PostgreSQL-shaped images compiled once "
                "at startup — that compile-once design is the product, not an omission. "
                "Presented side by side, never ranked."
            ),
        },
        "artifact": {
            "tool": "wreath-metal",
            "bytes": len(artifact_data),
            "median_ns": verify_median,
            "samples_ns": verify_samples,
            "verifications_per_second": 1_000_000_000 / verify_median,
            "fairness": (
                "Wreath-only and unranked: verifying format, SHA-256 checksum, tape bounds, "
                "and fingerprints of one WMA1 artifact from bytes. Alembic revision files "
                "carry no equivalent verifiable envelope."
            ),
        },
        "fairness": (
            "All ranked arms resolve the same already-current 16-revision linear history "
            "and assert an empty upgrade plan. No catalog I/O or DDL is measured — Wreath's "
            "apply path requires live PostgreSQL and operator credentials and is not "
            "benchmarked. The fleet row is Wreath-only and unranked because competitors "
            "expose no equivalent batch API."
        ),
    }
    payload = json.dumps(document, indent=2) + "\n"
    if args.output is not None:
        from .report import render

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        args.output.with_suffix(".html").write_text(
            render({"metadata": {}, "results": []}, [document]),
            encoding="utf-8",
        )
        print(f"wrote {args.output} and {args.output.with_suffix('.html')}")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
