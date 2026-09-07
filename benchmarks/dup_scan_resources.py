"""Native function scanner CPU and newline-count work with equal-size controls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from time import process_time_ns


class SourcePath:
    def __init__(self, source):
        self.source = source

    def read_text(self, *, encoding):
        if encoding != "utf-8":
            raise RuntimeError("unexpected source encoding")
        return self.source


def fixture(count, scenario):
    many = "".join(
        f"static int\nfunction_{index:06d}(int value)\n{{\n    return value + 1;\n}}\n\n"
        for index in range(count)
    )
    if scenario == "many":
        return many, count
    head = "static int\nfunction_{index:06d}(int value)\n{{\n"
    tail = "    return value;\n}\n\n"
    line = "    value += 1;\n"
    overhead = len(head.format(index=0)) + len(tail)
    lines = (len(many) // 4 - overhead) // len(line)
    few = "".join(head.format(index=index) + line * lines + tail for index in range(4))
    return few + " " * (len(many) - len(few)), 4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--functions", type=int, required=True)
    parser.add_argument("--scenario", choices=("many", "few"), required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.functions < 4 or args.repeats <= 0:
        parser.error("--functions must be at least4 and --repeats positive")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath._devtools import dup_scan

    loaded = Path(dup_scan.__file__).resolve()
    if not loaded.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {loaded}, expected {args.source}")
    source, count = fixture(args.functions, args.scenario)
    path = SourcePath(source)
    started = process_time_ns()
    for _ in range(args.repeats):
        bodies = dup_scan._native_bodies(path, "fixture.c", 1)
    elapsed = process_time_ns() - started
    if len(bodies) != count:
        raise RuntimeError(f"expected {count} bodies, got {len(bodies)}")
    expected_sites = []
    name = ""
    start = brace = 0
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith("function_"):
            name, start = line.split("(", 1)[0], number
        elif line == "{":
            brace = number
        elif line == "}":
            expected_sites.append((name, start, brace, number))
    image = hashlib.sha256()
    for body, expected in zip(bodies, expected_sites, strict=True):
        actual = body.site.name, body.site.line, body.site.body_start, body.site.body_end
        if actual != expected:
            raise RuntimeError("scanner site differs from independent source-line oracle")
        image.update(repr(body).encode())
    scanned = []

    class Source(str):
        def count(self, needle, start=0, end=None):
            stop = len(self) if end is None else end
            scanned.append(stop - start)
            return super().count(needle, start, stop)

    counted = dup_scan._native_bodies(SourcePath(Source(source)), "fixture.c", 1)
    if counted != bodies:
        raise RuntimeError("work-count instrumentation changed scan output")
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "source_characters": len(source),
                "counted_characters": sum(scanned),
                "source_sha256": hashlib.sha256(loaded.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )
    print(json.dumps({"bodies": count, "image_sha256": image.hexdigest()}))


if __name__ == "__main__":
    main()
