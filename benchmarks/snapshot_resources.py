"""Fixed-work required snapshot lookups against explicit frozen source modules."""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
from pathlib import Path
from time import process_time_ns


class Key:
    def __hash__(self):
        return 42


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=100_000)
    parser.add_argument("--key", choices=("int", "str", "tuple", "custom"), required=True)
    parser.add_argument("--operation", choices=("require", "get", "miss"), required=True)
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    cache_type = runpy.run_path(str(args.source))["SnapshotCache"]
    cache = cache_type()
    key = {"int": 42, "str": "snapshot-key", "tuple": ("snapshot", 42), "custom": Key()}[args.key]
    if args.operation != "miss":
        cache.replace({key: 1})
    checksum = 0
    if args.operation == "get":
        lookup = cache.get
    else:
        lookup = cache.require
    started = process_time_ns()
    if args.operation == "miss":
        for _ in range(args.repeats):
            try:
                lookup(key)
            except KeyError as error:
                if error.args != (key,):
                    raise RuntimeError("missing-key arguments changed") from error
                checksum += 1
    else:
        for _ in range(args.repeats):
            checksum += lookup(key)
    elapsed = process_time_ns() - started
    if checksum != args.repeats:
        raise RuntimeError(f"expected {args.repeats}, got {checksum}")
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )
    print(json.dumps({"checksum": checksum, "generation": cache.generation}, sort_keys=True))


if __name__ == "__main__":
    main()
