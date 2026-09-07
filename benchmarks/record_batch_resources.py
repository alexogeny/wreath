from __future__ import annotations

import argparse
import gc
import hashlib
import json
import struct
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns


def resident_bytes() -> dict[str, int]:
    fields = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            fields[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return fields


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--metrics", required=True, type=Path)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument(
        "--scenario", choices=("integer", "text", "mixed", "sort", "template"), required=True
    )
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if args.rows <= 0:
        parser.error("--rows must be positive")
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root))
    from wreath._native import extension
    from wreath._template_tape import MAX_OUTPUT_BYTES, Markup, TemplateRenderError, compile_tape

    pg = extension("_postgres")
    artifact = Path(pg.__file__).resolve()
    if not artifact.is_relative_to(source_root):
        raise RuntimeError(f"PostgreSQL artifact {artifact} is outside {source_root}")
    names = ("a", "b", "c", "d")
    if args.scenario == "integer":
        expected = (1000, -(2**63), 2**63 - 1, 0)
        fields = tuple(struct.pack("!q", value) for value in expected)
        oids = (20,) * 4
    elif args.scenario == "text":
        expected = ("", "a<b&c", "日本語", "hello\x00world")
        fields = tuple(value.encode() for value in expected)
        oids = (25,) * 4
    else:
        expected = (1000, "a<b&c", True, None)
        fields = (struct.pack("!q", 1000), b"a<b&c", b"\x01", None)
        oids = (20, 25, 16, 25)
    payload = struct.pack("!H", 4) + b"".join(
        struct.pack("!i", -1) if value is None else struct.pack("!I", len(value)) + value
        for value in fields
    )
    tape = pg._FieldTape(4)
    view = memoryview(payload)
    for _ in range(args.rows):
        tape.append(view, 4)
    plan = pg._compile_decoder_plan(oids, (1,) * 4, names)
    native_core = extension("_core")
    native_core.template_configure(Markup, TemplateRenderError)
    native_core.template_record_configure(pg._RECORD_C_API)
    program = native_core.template_compile(
        compile_tape("{% for row in rows %}{{ row.a }}={{ row.b }};{% endfor %}")
    )
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    start = process_time_ns()
    batch = pg._decode_field_tape(plan, tape, "fetch_batch", args.rows)
    rendered = None
    if args.scenario == "sort":
        batch.sort_by("b")
    elif args.scenario == "template":
        rendered = native_core.template_render_compiled(program, {"rows": batch}, MAX_OUTPUT_BYTES)
    cpu_ns = process_time_ns() - start
    metrics = {"workload_cpu_ns": cpu_ns}
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update(resident_bytes())
    metrics.update({"before_" + key: value for key, value in before.items()})
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    counts = pg._batch_storage_counts(batch)
    expected_counts = {
        "integer": (0, 0, args.rows * 4, 0, 0),
        "text": (0, 0, 0, args.rows * 4, 0),
    }.get(args.scenario, (0, args.rows * 2, args.rows, args.rows, 0))
    if len(batch) != args.rows or counts != expected_counts:
        raise RuntimeError(f"Wrong batch shape or storage: {len(batch)}, {counts}")
    digest = hashlib.sha256()
    for row in batch:
        actual = tuple(row[name] for name in names)
        if actual != expected:
            raise RuntimeError(f"Wrong decoded row: {actual!r}, expected {expected!r}")
        digest.update(repr(actual).encode())
    if rendered is not None:
        expected_rendered = b"1000=a&lt;b&amp;c;" * args.rows
        if rendered != expected_rendered:
            raise RuntimeError("Native template output differs from the escaped literal oracle")
        digest.update(rendered)
    metrics["artifact"] = str(artifact)
    metrics["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    metrics["source_sha256"] = hashlib.sha256(
        (source_root / "wreath/_native/postgres/record.c").read_bytes()
    ).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "rows": args.rows,
                "scenario": args.scenario,
                "storage_counts": counts,
                "sha256": digest.hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
