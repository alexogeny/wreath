"""Fixed-work cold PostgreSQL packet construction; no database connection."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from time import process_time_ns

from route_image import resident_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--parameters", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=1000)
    parser.add_argument("--scenario", choices=("cold", "bind"), required=True)
    args = parser.parse_args()
    if not 0 <= args.parameters <= 65535 or args.repeats <= 0:
        parser.error("--parameters must be in 0..65535 and --repeats positive")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath import _pgdriver

    source = Path(_pgdriver.__file__).resolve()
    if not source.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {source}, expected {args.source}")
    values = (None,) * args.parameters
    oids = (25,) * args.parameters

    def build():
        if args.scenario == "cold":
            return _pgdriver._build_cold_query_packet(b"s", "SELECT 1", values, oids, "fetch")
        return _pgdriver._bind_payload(
            b"s", values, oids, binary_parameters=False, binary_results=False
        )

    bind = (
        b"\0s\0\0\0"
        + args.parameters.to_bytes(2, "big")
        + b"\xff" * (4 * args.parameters)
        + b"\0\0"
    )
    expected = bind
    if args.scenario == "cold":
        parse = b"s\0SELECT 1\0" + args.parameters.to_bytes(2, "big") + bytes(4 * args.parameters)
        frames = [
            (b"P", parse),
            (b"D", b"Ss\0"),
            (b"B", bind),
            (b"D", b"P\0"),
            (b"E", bytes(5)),
            (b"S", b""),
        ]
        expected = b"".join(
            kind + (len(body) + 4).to_bytes(4, "big") + body for kind, body in frames
        )
    gc.collect()
    checksum = 0
    started = process_time_ns()
    for _ in range(args.repeats):
        packet = build()
        if packet != expected:
            raise RuntimeError("packet differs from independent wire-byte oracle")
        checksum += len(packet)
    elapsed = process_time_ns() - started
    resident = resident_bytes()
    del packet
    gc.collect()
    tracemalloc.start()
    try:
        packet = build()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if packet != expected:
        raise RuntimeError("traced packet differs from independent wire-byte oracle")
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "retained_bytes": retained,
                "peak_bytes": peak,
                **resident,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )
    print(json.dumps({"checksum": checksum, "packet_sha256": hashlib.sha256(packet).hexdigest()}))


if __name__ == "__main__":
    main()
