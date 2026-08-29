from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from wreath._cli import main

CONSUMER = Path(__file__).parent / "consumer"
GENERATED = CONSUMER / "src" / "generated"
EXPECTED = Path(__file__).parent / "expected"

MIN_NODE = (22, 18)


def _node_version(command: tuple[str, ...]) -> tuple[int, int] | None:
    """The runtime version, or None when this command is not usable."""
    try:
        completed = subprocess.run(
            [*command, "--version"], capture_output=True, text=True, timeout=10
        )
        major, minor, *_rest = completed.stdout.strip().lstrip("v").split(".")
        return int(major), int(minor)
    except OSError, ValueError, subprocess.SubprocessError:
        return None


def _node_command() -> tuple[str, ...] | None:
    """Select a supported runtime, preferring PATH then the pinned FNM version."""
    active = shutil.which("node")
    if active is not None:
        command = (active,)
        version = _node_version(command)
        if version is not None and version >= MIN_NODE:
            return command
    fnm = shutil.which("fnm")
    if fnm is not None:
        command = (fnm, "exec", "node")
        version = _node_version(command)
        if version is not None and version >= MIN_NODE:
            return command
    return None


_node = _node_command()


# The mock harness is a TypeScript ES module. Native type stripping is stable
# from Node 22.18, so an EOL runtime is treated as missing rather than silently
# becoming this repository's toolchain.
pytestmark = pytest.mark.skipif(
    _node is None or not (CONSUMER / "node_modules").exists(),
    reason="node>=22.18 toolchain or consumer node_modules not available",
)


def test_consumer_compiles_and_runs() -> None:
    if _node is None:
        raise RuntimeError("test ran without the node>=22.18 capability it requires")
    node = _node
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
            [*node, "node_modules/typescript/bin/tsc", "--noEmit"],
            cwd=CONSUMER,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr

        # Runtime transport conventions under a mocked fetch.
        ran = subprocess.run(
            [*node, "src/mock_fetch.mts"],
            cwd=CONSUMER,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert "mock-fetch transport checks passed" in ran.stdout
    finally:
        shutil.rmtree(GENERATED, ignore_errors=True)
