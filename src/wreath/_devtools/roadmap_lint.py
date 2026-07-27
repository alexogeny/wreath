"""Hold the roadmap to the one claim nothing else can check: absence.

Every other statement a document makes about this codebase is verifiable by
something. A link is checked by `wreath docs check`, a cited path by
`wreath-map-lint`, a method name by the executable-documentation floor, a
complexity contract by a probe. A claim that a feature *does not exist* is
verified by nothing at all, so it can only ever rot in one direction:
understating the product, quietly, for as long as nobody rereads it.

That is not hypothetical. `docs/reference/roadmap.md` said the Native Flight
Recorder's capture engine and `WFR1` sink were "not shipped" while
`tests/test_flight_capture_live.py` drove an armed forensic request end to end
and read the capture back out of the file -- six passing tests. The same
sentence had been copied into `docs/llms.txt` twice and into the manifest's
observability policy, and `llms.txt` contradicted itself: one line described
`wreath.orm.TenantContext` as shipping, another listed "isolated tenant session
execution" as unshipped.

**A claim of absence must have nothing to point at.** So a row that claims a
surface is absent names the symbol that would exist if it were not, and this
lint fails when that symbol resolves.

Findings:

* `ROAD001` -- a row makes an absolute claim of absence and names no surface.
  Without this the whole check is optional, and an unmarked row is the same
  unverifiable prose the lint exists to retire.
* `ROAD002` -- a named surface resolves. The claim is false; the feature ships.
* `ROAD003` -- a named surface cannot be checked, because the module holding it
  does not import. A typo in the marker would otherwise pass forever while
  appearing to verify something -- the lint's own version of the failure it
  guards against, so it is a finding rather than a skip.

Only *absolute* claims are in scope. A row saying some object types are "still
being implemented" is describing partial coverage, not absence, and there is no
single symbol whose existence would refute it; demanding one would produce a
marker nobody could write honestly. Scoping to the absolute phrasing keeps every
in-scope row checkable, which is the property that matters.

Markers are HTML comments, so they carry no weight in the rendered page:

    | Tenant-fleet DDL execution | Not shipped. ... <!-- absent: wreath.migrations.apply_fleet --> |

Run it with `uv run wreath-roadmap-lint`; `0` means clean.
"""

from __future__ import annotations

import argparse
import importlib
import re
from dataclasses import dataclass
from pathlib import Path

from .native_lint import repo_root

ROADMAP = "docs/reference/roadmap.md"

#: A row claiming a surface is wholly absent. Partial-coverage prose is out of
#: scope on purpose -- see the module docstring.
_ABSOLUTE = re.compile(r"^(not shipped|not implemented|reserved|unimplemented)\b", re.IGNORECASE)

_MARKER = re.compile(r"<!--\s*absent:\s*([^>]*?)\s*-->")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")


@dataclass(frozen=True)
class Finding:
    code: str
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.code} {self.message}"


@dataclass(frozen=True)
class Row:
    line: int
    surface: str
    status: str


def _rows(text: str) -> list[Row]:
    """Every data row of the page's table, in order.

    Header and delimiter rows are dropped; so is any line that is not a pipe
    table row, which is how the prose beneath the table stays out of scope.
    """
    rows: list[Row] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if set(cells[0]) <= set("-: ") or cells[0].lower() == "surface":
            continue
        rows.append(Row(number, cells[0], cells[1]))
    return rows


def _resolves(dotted: str) -> tuple[bool, bool]:
    """`(module_imports, attribute_exists)` for a dotted symbol path.

    The module is imported and the attribute looked up separately, because the
    two failures mean opposite things: an attribute that is missing confirms the
    claim, while a module that will not import means the claim was never checked.
    """
    module_path, _, attribute = dotted.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        return False, False
    return True, hasattr(module, attribute)


def scan(root: Path) -> list[Finding]:
    path = root / ROADMAP
    if not path.exists():
        return [Finding("ROAD001", ROADMAP, "the roadmap page is missing")]
    findings: list[Finding] = []
    for row in _rows(path.read_text(encoding="utf-8")):
        where = f"{ROADMAP}:{row.line}"
        if not _ABSOLUTE.match(row.status):
            continue
        marker = _MARKER.search(row.status)
        named = (
            [part.strip() for part in marker.group(1).split(",") if part.strip()]
            if marker else []
        )
        if not named:
            findings.append(
                Finding("ROAD001", where, f"{row.surface!r} claims absence and names no surface;"
                        " add `<!-- absent: dotted.path -->` naming a symbol that would exist"
                        " if it shipped, or the claim is unverifiable prose")
            )
            continue
        for dotted in named:
            if not _DOTTED.match(dotted):
                findings.append(
                    Finding("ROAD003", where, f"{row.surface!r} names {dotted!r}, which is not a"
                            " dotted symbol path and can never be checked")
                )
                continue
            importable, exists = _resolves(dotted)
            if not importable:
                findings.append(
                    Finding("ROAD003", where, f"{row.surface!r} names {dotted!r}, whose module"
                            " does not import; the marker verifies nothing and would pass"
                            " forever")
                )
            elif exists:
                findings.append(
                    Finding("ROAD002", where, f"{row.surface!r} is claimed absent, but"
                            f" {dotted} resolves; the feature ships and this page understates it")
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-roadmap-lint",
        description="Fail when the roadmap claims a surface is absent and it is not.",
    )
    parser.add_argument("--root", default=None, help="repository root (default: detected)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else repo_root()
    findings = scan(root)
    for finding in findings:
        print(finding.render())
    print(f"wreath-roadmap-lint: {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
