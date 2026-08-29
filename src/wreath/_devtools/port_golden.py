"""Check and regenerate the `wreath port` golden emitter output.

`tests/port/golden/<app>/<module>.py.expected` pins the byte-for-byte result of
the declarative emitter for `tests/port/corpus/<app>/<module>.py`, and
`tests/port/test_golden_output.py` compares against it. Regenerating those
files after an intentional emitter change had no entry point: the procedure was a
copy-pasteable snippet in `tests/port/golden/README.md`.

**That snippet had already drifted, and silently.** It carried a hardcoded list of
four `tumbleweed_api` modules; a fifth golden, `summit_ops/intake.py.expected`,
had since been added under a different app. Following the documented procedure
regenerated four of five files and said nothing about the one it skipped, so an
emitter change could land with a stale golden that the next unrelated run would
then blame on whoever touched it next. A glob cannot drift that way, which is why
this walks the golden tree rather than naming its members.

Regeneration is also the moment the cheap emitter invariants are free to check,
so the same pass runs them and refuses to write when one fails:

* **Determinism.** `emit_module` is pure, so emitting twice must give the same
  bytes. A golden written from a non-deterministic emitter pins one of several
  possible outputs and fails intermittently forever after.
* **The output compiles.** `compile()`, not `ast.parse()` -- parsing accepts
  a module that `compile` rejects (a `return` outside a function, a duplicate
  argument name), and the emitter reassembles bodies, which is exactly the shape
  of edit that produces one.
* **No orphans.** A `.expected` whose corpus source has been renamed or deleted
  is dead weight that still passes its own test, because the test parametrizes
  over the goldens and would simply stop generating a case for it.

    uv run wreath-port-golden              # check only; 1 if anything drifted
    uv run wreath-port-golden --update     # rewrite the drifted goldens
    uv run wreath-port-golden --format json

**The pinned set is the golden tree**, so `--update` fills in the goldens that
exist rather than deciding which corpus modules deserve one. To pin a new module,
create the empty `.expected` beside its siblings and run `--update`:

    touch tests/port/golden/summit_ops/intake.py.expected
    uv run wreath-port-golden --update

Exit codes follow `wreath port`'s convention: `0` clean, `1` ran and has
something to report, `2` never ran over anything (no goldens found).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from .native_lint import repo_root

#: Where the pinned emitter output lives, and the corpus it is emitted from.
GOLDEN_DIR = "tests/port/golden"
CORPUS_DIR = "tests/port/corpus"

#: Ran clean: every golden matches a deterministic, compiling emit.
EXIT_OK = 0
#: Ran, and something needs attention: drift, an orphan, or a failed invariant.
EXIT_WORK_REMAINS = 1
#: Never ran over anything -- no `*.py.expected` was found.
EXIT_NOT_RUN = 2

#: What can be wrong with one golden. `drift` is the only one `--update`
#: repairs; the rest need a person, so writing over them would hide the problem.
DRIFT = "drift"
MISSING_SOURCE = "missing-source"
NON_DETERMINISTIC = "non-deterministic"
DOES_NOT_COMPILE = "does-not-compile"
EMIT_FAILED = "emit-failed"


@dataclass(frozen=True)
class GoldenFinding:
    """One golden that is not what a fresh emit would produce, and why."""

    golden: str
    reason: str
    detail: str
    updated: bool = False

    def render(self) -> str:
        mark = "updated" if self.updated else self.reason
        return f"{mark:>17}  {self.golden}" + (f" — {self.detail}" if self.detail else "")

    def as_dict(self) -> dict:
        return {
            "golden": self.golden,
            "reason": self.reason,
            "detail": self.detail,
            "updated": self.updated,
        }


def _sources_for(root: Path) -> list[tuple[Path, Path]]:
    """Every `(golden, corpus source)` pair, found by walking -- never a list.

    Sorted so a run is reproducible and a diff of two runs is readable.
    """
    golden_root = root / GOLDEN_DIR
    corpus_root = root / CORPUS_DIR
    pairs = []
    for golden in sorted(golden_root.rglob("*.py.expected")):
        relative = golden.relative_to(golden_root).with_suffix("")  # drop ".expected"
        pairs.append((golden, corpus_root / relative))
    return pairs


def check(root: Path, *, update: bool = False) -> tuple[list[GoldenFinding], int]:
    """Compare every golden against a fresh emit. Returns (findings, pairs seen)."""
    # Imported here rather than at module scope: this tool is one entry point
    # among many in a shared console-scripts namespace, and `_port` pulls in the
    # whole analyzer/emitter. Nothing else in `_devtools` should pay for it.
    from .._port.emit import emit_module

    pairs = _sources_for(root)
    findings: list[GoldenFinding] = []

    for golden, source in pairs:
        name = str(golden.relative_to(root))
        if not source.exists():
            findings.append(
                GoldenFinding(
                    name,
                    MISSING_SOURCE,
                    f"no corpus source at {source.relative_to(root)}; the golden test "
                    "parametrizes over goldens, so this one silently stopped being checked",
                )
            )
            continue

        try:
            emitted = emit_module(source)
        except Exception as exc:  # noqa: BLE001 -- any emitter failure is a finding
            findings.append(GoldenFinding(name, EMIT_FAILED, f"{type(exc).__name__}: {exc}"))
            continue

        if emit_module(source) != emitted:
            findings.append(
                GoldenFinding(
                    name,
                    NON_DETERMINISTIC,
                    "two emits of the same source differ; a golden written from this "
                    "would pin one of several outputs",
                )
            )
            continue

        try:
            compile(emitted, str(source), "exec")
        except SyntaxError as exc:
            findings.append(GoldenFinding(name, DOES_NOT_COMPILE, str(exc)))
            continue

        current = golden.read_text(encoding="utf-8") if golden.exists() else None
        if current == emitted:
            continue
        if update:
            golden.parent.mkdir(parents=True, exist_ok=True)
            golden.write_text(emitted, encoding="utf-8")
            findings.append(GoldenFinding(name, DRIFT, "rewritten from the emitter", True))
        else:
            findings.append(
                GoldenFinding(
                    name,
                    DRIFT,
                    "differs from a fresh emit; re-run with --update if the change was intended",
                )
            )

    return findings, len(pairs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-port-golden",
        description="Check (or regenerate) the pinned `wreath port` emitter output.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite goldens that differ from a fresh emit (drift only; a golden "
        "with no source, or one whose emit is non-deterministic or does not "
        "compile, is reported and left alone)",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = repo_root()
    findings, seen = check(root, update=args.update)
    blocking = [f for f in findings if not f.updated]

    if args.format == "json":
        print(
            json.dumps(
                {
                    "goldens": seen,
                    "updated": sum(1 for f in findings if f.updated),
                    "findings": [f.as_dict() for f in findings],
                },
                indent=2,
            )
        )
    else:
        for finding in findings:
            print(finding.render())
        if not seen:
            print(f"wreath-port-golden: no *.py.expected under {GOLDEN_DIR}.")
        else:
            updated = sum(1 for f in findings if f.updated)
            summary = f"wreath-port-golden: {seen} golden(s) checked"
            if updated:
                summary += f", {updated} updated"
            if blocking:
                summary += f", {len(blocking)} needing attention"
            print(f"\n{summary}.")

    if not seen:
        return EXIT_NOT_RUN
    return EXIT_WORK_REMAINS if blocking else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
