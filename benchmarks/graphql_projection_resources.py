import argparse
import asyncio
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns
from types import SimpleNamespace


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
    parser.add_argument("--fields", type=int, required=True)
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    if min(args.fields, args.iterations) < 1:
        parser.error("workload sizes must be positive")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from wreath._graphql import execute, resolvers
    from wreath._graphql.parser import Field, parse
    from wreath._graphql.schema import ObjectType, Schema, SchemaField

    sources = [Path(module.__file__).resolve() for module in (execute, resolvers)]
    if any(not source.is_relative_to(root) for source in sources):
        raise RuntimeError("Projection source outside requested root")
    fields = {
        f"field{index}": SchemaField(f"field{index}", "Int", True, False, attribute=f"field{index}")
        for index in range(args.fields - 1)
    }
    resolver = (
        None
        if args.plain
        else resolvers.ResolverSpec("Thing", "wanted", lambda values, info: [7] * len(values))
    )
    fields["wanted"] = SchemaField(
        "wanted", "Int", True, False, attribute="wanted", resolver=resolver
    )
    object_type = ObjectType("Thing", None, fields)
    run = execute._Run(
        Schema(None, {"Thing": object_type}, {}),
        parse("{ wanted }"),
        SimpleNamespace(),
        variables={},
        authorizer=None,
        request=None,
        max_page_size=10,
        on_denied="error",
        action="read",
        policy_schema=None,
    )
    selected = [Field("wanted", "wanted")]
    instances = [SimpleNamespace(wanted=7)]

    async def measure():
        gc.collect()
        before = resident_bytes()
        if args.trace:
            tracemalloc.start()
        start = process_time_ns()
        for _ in range(args.iterations):
            result = await run._project(instances, object_type, selected, ())
        metrics = {"workload_cpu_ns": process_time_ns() - start}
        if args.trace:
            metrics["retained_bytes"], metrics["peak_bytes"] = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        metrics.update(resident_bytes())
        metrics.update({"growth_" + key: metrics[key] - value for key, value in before.items()})
        if result != [{"wanted": 7}]:
            raise RuntimeError("Projection differs from independent scalar oracle")
        metrics["sources"] = {
            str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources
        }
        args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
        print(json.dumps(result, sort_keys=True))

    asyncio.run(measure())


if __name__ == "__main__":
    main()
