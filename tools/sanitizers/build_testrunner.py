"""Build an isolated ASan/UBSan native test executor."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / ".sanitizers/native-testrunner"
LIB = OUTPUT / "lib"
TEMP = OUTPUT / "temp"


def main() -> None:
    shutil.rmtree(OUTPUT, ignore_errors=True)
    LIB.mkdir(parents=True)
    shutil.copytree(
        ROOT / "src/wreath",
        LIB / "wreath",
        ignore=shutil.ignore_patterns("_testrunner*.so", "__pycache__"),
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/sanitizers/setup_testrunner.py"),
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
    extensions = sorted((LIB / "wreath/_native").glob("_testrunner*.so"))
    if len(extensions) != 1:
        raise RuntimeError(f"expected one sanitized extension, found: {extensions}")
    print(extensions[0])


if __name__ == "__main__":
    main()
