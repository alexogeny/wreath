"""Report whether importing Wreath's native package enables the GIL.

Free-threaded CPython may enable the GIL when it imports an extension that has
not declared no-GIL support.  This probe runs before importing ``wreath._native``
so that the transition is observable.  It is diagnostic only: passing the probe
does not prove that the extensions are data-race safe.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import sysconfig
from dataclasses import asdict, dataclass
from typing import Literal

Verdict = Literal[
    "import-error",
    "standard-build",
    "status-unavailable",
    "gil-enabled-by-import",
    "gil-remained-disabled",
    "gil-already-enabled",
]


@dataclass(frozen=True, slots=True)
class Probe:
    module: str
    free_threaded: bool
    status_available: bool
    gil_before: bool | None
    gil_after: bool | None
    imported: bool
    error: str | None


def _gil_status() -> tuple[bool, bool | None]:
    checker = getattr(sys, "_is_gil_enabled", None)
    if checker is None:
        return False, None
    return True, bool(checker())


def probe_import(module: str = "wreath._native") -> Probe:
    """Import *module* and capture any observable GIL-state transition."""
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    status_available, before = _gil_status()
    imported = False
    error: str | None = None
    try:
        importlib.import_module(module)
        imported = True
    except Exception as exc:  # noqa: BLE001 - diagnostics must report loader failures
        error = f"{type(exc).__name__}: {exc}"
    _, after = _gil_status()
    return Probe(
        module=module,
        free_threaded=free_threaded,
        status_available=status_available,
        gil_before=before,
        gil_after=after,
        imported=imported,
        error=error,
    )


def evaluate_probe(probe: Probe) -> Verdict:
    """Classify one probe without claiming extension thread safety."""
    if not probe.imported:
        return "import-error"
    if not probe.free_threaded:
        return "standard-build"
    if not probe.status_available or probe.gil_before is None or probe.gil_after is None:
        return "status-unavailable"
    if probe.gil_before:
        return "gil-already-enabled"
    if probe.gil_after:
        return "gil-enabled-by-import"
    return "gil-remained-disabled"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-native-gil-status",
        description="Report whether a Wreath native import enables the GIL.",
    )
    parser.add_argument("--module", default="wreath._native")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail unless a free-threaded build observably remains GIL-disabled",
    )
    args = parser.parse_args(argv)

    probe = probe_import(args.module)
    verdict = evaluate_probe(probe)
    if args.as_json:
        print(json.dumps({**asdict(probe), "verdict": verdict}, indent=2))
    else:
        print(f"module: {probe.module}")
        print(f"free-threaded build: {probe.free_threaded}")
        print(f"GIL before import: {probe.gil_before}")
        print(f"GIL after import: {probe.gil_after}")
        print(f"verdict: {verdict}")
        if probe.error is not None:
            print(f"error: {probe.error}")
        if verdict == "gil-remained-disabled":
            print("warning: this transition probe does not prove data-race safety")

    if args.check and verdict != "gil-remained-disabled":
        return 1
    return 0 if probe.imported else 1


if __name__ == "__main__":
    raise SystemExit(main())
