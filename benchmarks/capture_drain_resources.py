from __future__ import annotations

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
    parser.add_argument("--scenario", choices=("empty", "sparse", "full"), required=True)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath._flight_schema import CaptureFieldClass
    from wreath._native import _flight

    artifact = Path(_flight.__file__).resolve()
    source = root / "wreath/_native/_flightmodule.c"
    if not artifact.is_relative_to(root):
        raise RuntimeError("Flight artifact outside requested source root")
    recorder = _flight.Recorder(
        _flight.MODE_FORENSIC,
        ring_records=16,
        active_requests=8,
        capture_slabs=8,
        slab_bytes=1024 * 1024,
        detailed_sample_rate=1.0,
        capture_hash_key=(1, 2),
    )
    count = {"empty": 0, "sparse": 1, "full": 8}[args.scenario]
    gc.collect()
    before = resident_bytes()
    elapsed = 0
    peak = 0
    retained = 0
    total = 0
    for iteration in range(args.iterations):
        for index in range(count):
            request = recorder.begin(connection_id=index, start_ns=iteration)
            request.capture(int(CaptureFieldClass.REQUEST_BODY), 0, _flight.CAP_RAW, bytes([index]))
            request.finish(now_ns=iteration + 1, status=200)
        if args.trace:
            tracemalloc.start()
        start = process_time_ns()
        slabs = recorder.drain_captures()
        elapsed += process_time_ns() - start
        if args.trace:
            current, high = tracemalloc.get_traced_memory()
            retained = max(retained, current)
            peak = max(peak, high)
            tracemalloc.stop()
        if len(slabs) != count or [slab[-4] for slab in slabs] != list(range(count)):
            raise RuntimeError("Capture count/order/payload differs from submitted oracle")
        total += len(slabs)
        recorder.drain()
    metrics = resident_bytes()
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    metrics.update(
        workload_cpu_ns=elapsed,
        peak_bytes=peak,
        retained_bytes=retained,
        list_bytes=sys.getsizeof(slabs),
        source=str(source),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        artifact=str(artifact),
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps({"slabs": total, "last_payloads": [slab[-4] for slab in slabs]}))


if __name__ == "__main__":
    main()
