from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import tracemalloc
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure replay write input-copy storage.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    module = importlib.import_module("wreath.replay")
    if module.__file__ is None or not Path(module.__file__).is_relative_to(source):
        raise RuntimeError("loaded replay outside requested source")
    transport_type = module._ReplayTransport
    metrics = {}
    outputs = []
    for size, repetitions in ((32, 20000), (4 << 20, 80)):
        payload = (bytes(range(256)) * ((size + 255) // 256))[:size]
        for kind in ("bytes", "bytearray", "view"):
            data = payload if kind == "bytes" else bytearray(payload)
            if kind == "view":
                data = memoryview(data)
            for operation in ("write", "writelines"):
                name = f"{operation}_{kind}_{size}"
                chunks = (data, data)
                expected = payload if operation == "write" else payload * 2
                target = transport_type(("127.0.0.1", 1), ("127.0.0.1", 2))
                tracemalloc.start()
                if operation == "write":
                    target.write(data)
                else:
                    target.writelines(chunks)
                retained, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                metrics[f"{name}_retained_bytes"] = retained
                metrics[f"{name}_peak_bytes"] = peak
                if target.buffer != expected or target.write_count != 1:
                    raise RuntimeError(f"incorrect replay output for {name}")
                started = time.process_time_ns()
                for _ in range(repetitions):
                    target = transport_type(("127.0.0.1", 1), ("127.0.0.1", 2))
                    if operation == "write":
                        target.write(data)
                    else:
                        target.writelines(chunks)
                metrics[f"{name}_cpu_ns"] = time.process_time_ns() - started
                if target.buffer != expected or target.write_count != 1:
                    raise RuntimeError(f"incorrect repeated replay output for {name}")
                outputs.append((name, len(target.buffer), target.write_count))
    args.metrics.write_text(json.dumps(metrics, sort_keys=True) + "\n")
    print(json.dumps(outputs))


if __name__ == "__main__":
    main()
