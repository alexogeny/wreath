"""Prepare an isolated Wreath package containing an ASan/UBSan HTTP/3 extension.

Mirrors build_server.py. See docs/plans/native-server-sanitizers.md for how to
run the suite against the result.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / ".sanitizers/native-http3"
LIB = OUTPUT / "lib"
TEMP = OUTPUT / "temp"


def main() -> None:
    shutil.rmtree(OUTPUT, ignore_errors=True)
    LIB.mkdir(parents=True)
    # Keep the already-built sibling extensions (_core, _server): only _http3 is
    # rebuilt with sanitizers here, so the rest of the package stays importable.
    shutil.copytree(
        ROOT / "src/wreath",
        LIB / "wreath",
        ignore=shutil.ignore_patterns("_http3*.so", "__pycache__"),
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/sanitizers/setup_http3.py"),
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
    extensions = sorted((LIB / "wreath/_native").glob("_http3*.so"))
    if len(extensions) != 1:
        raise RuntimeError(f"expected one sanitized extension, found: {extensions}")
    print(extensions[0])


if __name__ == "__main__":
    main()
