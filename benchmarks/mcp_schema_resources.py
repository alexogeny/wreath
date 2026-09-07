import argparse
import gc
import hashlib
import json
import sys
import tracemalloc
from dataclasses import make_dataclass
from pathlib import Path
from time import process_time_ns
from typing import Annotated


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
    parser.add_argument("--scenario", choices=("simple", "model", "openapi"), required=True)
    parser.add_argument("--fields", type=int, default=64)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if min(args.fields, args.iterations) < 1:
        parser.error("workload sizes must be positive")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath import Wreath, openapi
    from wreath._mcp import schema
    from wreath.binding import Body

    sources = [Path(module.__file__).resolve() for module in (schema, openapi)]
    if any(not source.is_relative_to(root) for source in sources):
        raise RuntimeError("Schema source outside requested root")
    child = make_dataclass("Child", [("value", int)])
    fields = [f"field{index}" for index in range(args.fields)]
    payload = make_dataclass("Payload", [(name, list[child | None]) for name in fields])

    def model_handler(request, body: Annotated[payload, Body()]):
        return {}

    def simple_handler(request, query: str, limit: int = 20):
        return {}

    handler = simple_handler if args.scenario == "simple" else model_handler
    app = Wreath(ai_scraping="allow")
    app.post("/tool")(handler)
    if args.scenario == "openapi":
        openapi.generate_openapi(app)
    prefix = "#/components/schemas/" if args.scenario == "openapi" else "#/$defs/"
    expected_properties = {
        name: {"type": "array", "items": {"anyOf": [{"$ref": prefix + "Child"}, {"type": "null"}]}}
        for name in fields
    }
    gc.collect()
    before = resident_bytes()
    if args.trace:
        tracemalloc.start()
    start = process_time_ns()
    for _ in range(args.iterations):
        if args.scenario == "openapi":
            result = openapi.generate_openapi(app)
        else:
            result, _ = schema.derive_input_schema(handler, "tool")
    metrics = {"workload_cpu_ns": process_time_ns() - start}
    if args.trace:
        metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    metrics.update(resident_bytes())
    metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
    if args.scenario == "simple":
        expected = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
        if result != expected:
            raise RuntimeError("Simple tool schema differs from declaration oracle")
    else:
        definitions = (
            result["components"]["schemas"] if args.scenario == "openapi" else result["$defs"]
        )
        if definitions["Payload"]["properties"] != expected_properties:
            raise RuntimeError("Nested schema differs from field/reference oracle")
        if definitions["Payload"]["required"] != fields:
            raise RuntimeError("Nested schema omitted required fields")
        if definitions["Child"]["properties"] != {"value": {"type": "integer"}}:
            raise RuntimeError("Child schema differs from declaration oracle")
    metrics["sources"] = {
        str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources
    }
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest())


if __name__ == "__main__":
    main()
