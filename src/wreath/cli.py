"""Public command-line entry point for Wreath's server tooling."""

from __future__ import annotations

from collections.abc import Sequence

from ._cli import main as _main


def main(argv: Sequence[str] | None = None) -> int:
    """Run Wreath's command-line interface and return a process exit code."""
    return _main(argv)


# `python -m wreath.cli ...` must run the CLI rather than import it and exit 0.
# A module executed with -m and no guard is silent success, which reads exactly
# like a passing check. See the same guard in `_cli.py`.
if __name__ == "__main__":
    raise SystemExit(main())
