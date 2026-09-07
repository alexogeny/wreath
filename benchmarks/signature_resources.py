import argparse
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns


def resident_bytes():
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in {"Rss", "Pss"}:
            values[name.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--components", type=int, required=True)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--plans", type=int, default=2000)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if min(args.components, args.plans) < 1 or args.limit < args.components:
        parser.error("positive workload sizes and limit >= components required")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath._native import _core
    from wreath.signatures import SignatureError

    artifact = Path(_core.__file__).resolve()
    if not artifact.is_relative_to(root):
        raise RuntimeError("Core artifact outside requested source root")
    names = tuple(f"x-field-{index}" for index in range(args.components))
    inner = " ".join(f'"{name}"' for name in names)
    signature_input = f'sig1=({inner});created=1;keyid="fixture"'
    expected = ({"created": 1, "keyid": "fixture"}, b"abc", names)
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    start = process_time_ns()
    plans = [
        _core.signature_compile_pair(
            signature_input, "sig1=:YWJj:", SignatureError, 8192, args.limit
        )
        for _ in range(args.plans)
    ]
    metrics = {"workload_cpu_ns": process_time_ns() - start}
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    if len(plans) != args.plans or any(
        _core.signature_plan_facts(plan) != expected for plan in plans
    ):
        raise RuntimeError("Signature facts differ from structured-field declaration oracle")
    metrics["artifact"] = str(artifact)
    metrics["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    metrics["source_sha256"] = hashlib.sha256(
        (root / "wreath/_native/signatures.c").read_bytes()
    ).hexdigest()
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps({"plans": len(plans), "names": names, "signature_hex": "616263"}))


if __name__ == "__main__":
    main()
