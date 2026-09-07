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
    parser.add_argument("--routes", type=int, default=10000)
    parser.add_argument("--traces", type=int, default=128)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--drains", type=int, default=8)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args()
    if min(args.routes, args.traces, args.batch, args.drains) < 1:
        parser.error("workload sizes must be positive")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath import _export, _otlp
    from wreath._flight_schema import MetadataImage, Protocol, RouteMeta, TerminalStatus
    from wreath._projector import ProjectedTrace

    sources = [Path(module.__file__).resolve() for module in (_export, _otlp)]
    if any(not source.is_relative_to(root) for source in sources):
        raise RuntimeError("Imported export source is outside requested root")
    traversals = 0

    class Rows(tuple):
        def __iter__(self):
            nonlocal traversals
            traversals += 1
            return super().__iter__()

    rows = tuple(
        RouteMeta(index, "GET", f"/route/{index}", f"route_{index}", 0, (), (), (), 0, "python")
        for index in range(args.routes)
    )
    image = MetadataImage(
        version=1,
        routes=Rows(rows) if args.count else rows,
        plans=(),
        dependencies=(),
        middleware=(),
        auth_policies=(),
        serializers=(),
        validators=(),
        limits=(),
        clients=(),
        databases=(),
        models=(),
    )
    traces = [
        ProjectedTrace(
            index + 1,
            1,
            index % args.routes,
            0,
            0,
            10,
            200,
            TerminalStatus.OK,
            Protocol.HTTP1,
            0,
            0,
            0,
            2,
            observed_unix_nano=5_000_000_000,
        )
        for index in range(args.traces)
    ]
    metrics = {}
    requests = []
    sample_cpu_ns = 0

    class Transport:
        def export_traces(self, request):
            nonlocal sample_cpu_ns
            if not metrics:
                start = process_time_ns()
                metrics.update(resident_bytes())
                sample_cpu_ns += process_time_ns() - start
            requests.append(request)

    pipeline = _export.ExportPipeline(
        Transport(),
        image=image,
        queue_capacity=args.traces,
        batch_size=args.batch,
        resource_attributes={"service.name": "lookup-bench"},
    )
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    elapsed = 0
    for _ in range(args.drains):
        requests.clear()
        for trace in traces:
            pipeline.on_trace(trace)
        start = process_time_ns()
        pipeline._export_traces()
        elapsed += process_time_ns() - start
    metrics["workload_cpu_ns"] = elapsed - sample_cpu_ns
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    spans = [
        span
        for request in requests
        for span in request["resourceSpans"][0]["scopeSpans"][0]["spans"]
    ]
    expected_names = [f"GET /route/{index % args.routes}" for index in range(args.traces)]
    if [span["name"] for span in spans] != expected_names:
        raise RuntimeError("Exported spans differ from route-name oracle")
    if any(span["endTimeUnixNano"] != "5000000000" for span in spans):
        raise RuntimeError("Exported spans differ from timestamp oracle")
    if pipeline.stats["exported_traces"] != args.drains * args.traces:
        raise RuntimeError("Export count differs from submitted work")
    metrics["route_traversals"] = traversals
    metrics["sources"] = {
        str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources
    }
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(hashlib.sha256(json.dumps(requests, sort_keys=True).encode()).hexdigest())


if __name__ == "__main__":
    main()
