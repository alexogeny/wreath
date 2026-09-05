"""Python Bind assembly cost, peak allocation, and encoded-value concatenations."""

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
    parser.add_argument("--size", type=int, default=8388608)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--kind", choices=("bytea", "text", "integer", "null"), required=True)
    parser.add_argument("--binary", action="store_true")
    args = parser.parse_args()
    if args.size <= 0 or args.repeats <= 0:
        parser.error("--size and --repeats must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath import _pgdriver

    source = Path(_pgdriver.__file__).resolve()
    if not source.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {source}, expected {args.source}")
    if args.kind == "bytea":
        value, oid = b"x" * args.size, 17
        encoded = value if args.binary else b"\\x" + value.hex().encode()
    elif args.kind == "text":
        value, oid = "x" * args.size, 25
        encoded = value.encode()
    elif args.kind == "integer":
        value, oid = 42, 23
        encoded = (42).to_bytes(4, "big", signed=True) if args.binary else b"42"
    else:
        value, oid, encoded = None, 25, None
    expected = b"\0s\0" + (b"\0\1\0\1" if args.binary else b"\0\0") + b"\0\1"
    expected += b"\xff" * 4 if encoded is None else len(encoded).to_bytes(4, "big") + encoded
    expected += b"\0\0"

    def build():
        return _pgdriver._bind_payload(
            b"s", (value,), (oid,), binary_parameters=args.binary, binary_results=False
        )

    gc.collect()
    started = process_time_ns()
    for _ in range(args.repeats):
        packet = build()
        if packet != expected:
            raise RuntimeError("packet differs from independent Bind byte oracle")
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
        raise RuntimeError("traced packet differs from independent Bind byte oracle")
    copied = []

    class Encoded(bytes):
        def __radd__(self, prefix):
            copied.append(len(self))
            return bytes(prefix) + bytes(self)

    encoder_name = "_encode_binary" if args.binary else "_encode_text"
    original = getattr(_pgdriver, encoder_name)

    def counted(value, oid):
        data = original(value, oid)
        return None if data is None else Encoded(data)

    setattr(_pgdriver, encoder_name, counted)
    try:
        counted_packet = build()
    finally:
        setattr(_pgdriver, encoder_name, original)
    if counted_packet != packet:
        raise RuntimeError("copy instrumentation changed packet bytes")
    args.metrics.write_text(
        json.dumps(
            {
                "cpu_ns": elapsed,
                "retained_bytes": retained,
                "peak_bytes": peak,
                "concatenated_value_bytes": sum(copied),
                "concatenation_calls": len(copied),
                **resident,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )
    print(json.dumps({"packet_sha256": hashlib.sha256(packet).hexdigest(), "size": len(packet)}))


if __name__ == "__main__":
    main()
