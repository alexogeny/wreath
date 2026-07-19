"""Prepare an isolated Wreath package containing an ASan/UBSan flight extension.

The Native Flight Recorder core owns a single-writer SPSC completion ring plus,
since Stage 5, a forensic capture-slab pool with cross-thread commit/return
index rings. Build the sanitized ``_flight`` here and drive the recorder tests
(``tests/test_flight_native.py``, ``tests/test_flight_capture.py``) against it,
with the sink drained from a second thread, to catch any use-after-free, buffer
overflow, or undefined behavior in the ring/slab code.

    python tools/sanitizers/build_flight.py
    ASAN=$(gcc -print-file-name=libasan.so)
    LD_PRELOAD=$ASAN ASAN_OPTIONS=detect_leaks=0 \
        PYTHONPATH=.sanitizers/native-flight/lib \
        python -m pytest tests/test_flight_capture.py tests/test_flight_native.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / ".sanitizers/native-flight"
LIB = OUTPUT / "lib"
TEMP = OUTPUT / "temp"


def main() -> None:
    shutil.rmtree(OUTPUT, ignore_errors=True)
    LIB.mkdir(parents=True)
    shutil.copytree(
        ROOT / "src/wreath",
        LIB / "wreath",
        ignore=shutil.ignore_patterns("_flight*.so", "__pycache__"),
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/sanitizers/setup_flight.py"),
            "build_ext",
            "--build-lib",
            str(LIB),
            "--build-temp",
            str(TEMP),
            "--force",
        ],
        cwd=ROOT,
        check=True,
    )
    extensions = sorted((LIB / "wreath/_native").glob("_flight*.so"))
    if len(extensions) != 1:
        raise RuntimeError(f"expected one sanitized extension, found: {extensions}")
    print(extensions[0])


if __name__ == "__main__":
    main()
