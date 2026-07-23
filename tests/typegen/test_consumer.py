"""Compile and exercise the generated client with a pinned Node toolchain.

Skipped unless Node and the consumer's ``node_modules`` are present (run
``npm ci`` in ``tests/typegen/consumer`` first). This proves the generated
TypeScript compiles under strict mode against real React + TanStack Query types,
matches the committed golden, and honours Wreath's fetch wire conventions at
runtime.

It is a single test on purpose: it generates into one shared directory, so
splitting it across xdist workers (default ``--dist load`` ignores group marks)
would race a teardown against another worker's write.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from wreath._cli import main

CONSUMER = Path(__file__).parent / "consumer"
GENERATED = CONSUMER / "src" / "generated"
EXPECTED = Path(__file__).parent / "expected"

_node = shutil.which("node")
_npx = shutil.which("npx")


def _node_major() -> int:
    """Major version of the node on PATH, or 0 if it can't be determined."""
    if _node is None:
        return 0
    try:
        out = subprocess.run([_node, "--version"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        return int(out.lstrip("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


# The consumer's mock harness is an ES module ``.mts`` file, which node only
# executes directly from v18 (v16 raises ERR_UNKNOWN_FILE_EXTENSION), so an old
# toolchain is treated the same as a missing one.
pytestmark = pytest.mark.skipif(
    _node is None or _npx is None or not (CONSUMER / "node_modules").exists()
    or _node_major() < 18,
    reason="node>=18 toolchain or consumer node_modules not available",
)


def test_consumer_compiles_and_runs() -> None:
    code = main(
        [
            "typegen",
            "tests.typegen.app:app",
            "--output",
            str(GENERATED),
            "--react-query",
            "--base-url-env",
            "VITE_API_URL",
            "--title",
            "Fixture API",
            "--api-version",
            "2.0.0",
        ]
    )
    assert code == 0
    try:
        # Freshly generated output matches the committed golden byte-for-byte.
        for path in GENERATED.iterdir():
            assert path.read_text() == (EXPECTED / path.name).read_text()

        # Strict compile against real React + TanStack Query types.
        compiled = subprocess.run(
            [_npx, "tsc", "--noEmit"],
            cwd=CONSUMER,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr

        # Runtime transport conventions under a mocked fetch.
        ran = subprocess.run(
            [_node, "src/mock_fetch.mts"],
            cwd=CONSUMER,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert "mock-fetch transport checks passed" in ran.stdout
    finally:
        shutil.rmtree(GENERATED, ignore_errors=True)
