"""Public command-line entry point for Wreath's server tooling."""

from __future__ import annotations

from collections.abc import Sequence

from ._cli import main as _main


def main(argv: Sequence[str] | None = None) -> int:
    """Run Wreath's command-line interface and return a process exit code."""
    return _main(argv)
