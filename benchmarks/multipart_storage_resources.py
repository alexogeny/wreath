"""Fixed-work multipart decoding and upload reads with independent peak replay."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path
from tempfile import TemporaryFile
from time import process_time_ns

from route_image import resident_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument(
        "--scenario",
        choices=("text", "invalid", "mixed", "file", "upload", "memory"),
        required=True,
    )
    args = parser.parse_args()
    if args.size <= 0 or args.repeats <= 0:
        parser.error("--size and --repeats must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    from wreath import request

    source = Path(request.__file__).resolve()
    if not source.is_relative_to(args.source.resolve()):
        raise RuntimeError(f"loaded {source}, expected {args.source}")
    payload = (b"\xff" if args.scenario == "invalid" else b"x") * args.size
    expected_text = payload.decode("utf-8", "replace")
    pieces = tuple(payload[start : start + 65536] for start in range(0, len(payload), 65536))
    limits = request.RequestLimits(
        max_part_bytes=args.size,
        max_form_memory_bytes=args.size * 2,
        spool_max_bytes=args.size,
    )

    async def chunks():
        names = ["text", "file"] if args.scenario == "mixed" else [args.scenario]
        for name in names:
            disposition = f'Content-Disposition: form-data; name="{name}"'
            if name == "file":
                disposition += '; filename="f.bin"'
            yield b"--B\r\n" + disposition.encode() + b"\r\n\r\n"
            for piece in pieces:
                yield piece
            yield b"\r\n"
        yield b"--B--\r\n"

    async def multipart(repeats):
        for _ in range(repeats):
            result = await request._stream_multipart(chunks(), b"B", limits)
            if args.scenario in {"file", "mixed"}:
                if result.files["file"].data != payload:
                    raise RuntimeError("uploaded bytes changed")
            if args.scenario != "file":
                name = "text" if args.scenario == "mixed" else args.scenario
                if result[name] != expected_text:
                    raise RuntimeError("decoded text changed")
            result.close()
        return result

    with TemporaryFile() as spool, asyncio.Runner() as runner:
        if args.scenario == "upload":
            spool.write(payload)
            upload = request.UploadedFile("file", "f.bin", [], spool=spool, size=args.size)
        else:
            upload = request.UploadedFile("file", "f.bin", [], payload)

        def exercise(repeats):
            if args.scenario in {"upload", "memory"}:
                for _ in range(repeats):
                    result = upload.read()
                    if result != payload:
                        raise RuntimeError("read bytes changed")
                return result
            return runner.run(multipart(repeats))

        gc.collect()
        started = process_time_ns()
        result = exercise(args.repeats)
        elapsed = process_time_ns() - started
        resident = resident_bytes()
        del result
        gc.collect()
        tracemalloc.start()
        try:
            result = exercise(1)
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if args.scenario in {"upload", "memory"}:
            checksum = len(result)
        else:
            checksum = sum(map(len, result.fields.values())) + sum(
                value.size for value in result.files.values()
            )
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
        print(json.dumps({"size": args.size, "checksum": checksum, "repeats": args.repeats}))


if __name__ == "__main__":
    main()
