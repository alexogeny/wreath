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

**Not every absence on that page is a table row.** Several are prose -- a
paragraph explaining why a surface is unbuilt, ending in the same marker on a
line of its own. Those markers went unchecked by anything until one of them went
stale in place: `wreath.organizations.scim_router` shipped while the paragraph
naming it still said it had not. So a marker is checked wherever it appears, and
the table is merely where most of them happen to live.

A prose marker is resolved from the **source tree** rather than by importing it.
That is not a convenience: a prose absence is routinely a *whole module* that
does not exist (`wreath.oauth`), and `importlib` cannot tell that apart from a
typo -- it raises `ImportError` for both. Reading `src/` can: the module holding
the named symbol either is a file or is not, and the difference is exactly the
one `ROAD003` needs to make. It also keeps a linter from importing the framework
it lints. Consequently a marker names either a module (`package.module`, whose
*package* must exist) or a symbol bound at a module's top level
(`package.module.symbol`, whose *module* must exist); anything else is `ROAD003`,
because nothing anchors it.

Run it with `uv run wreath-roadmap-lint`; `0` means clean.
"""

from __future__ import annotations

import argparse
import ast
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


def _module_source(root: Path, parts: list[str]) -> Path | None:
    """The file backing a dotted module path under `src/`, if there is one."""
    base = root / "src"
    for part in parts:
        base = base / part
    package = base / "__init__.py"
    if package.is_file():
        return package
    module = base.with_name(f"{base.name}.py")
    return module if module.is_file() else None


def _bound_names(body: list[ast.stmt], names: set[str]) -> None:
    """Every name a module body binds at import time, without executing it.

    Conditional bodies are descended into -- a facade that imports behind
    `if TYPE_CHECKING` or `try`/`except ImportError` still exports the name --
    while function and class bodies are not, because a local is not an export.
    """
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update(a.asname or a.name.partition(".")[0] for a in node.names)
        elif isinstance(node, ast.If):
            _bound_names(node.body, names)
            _bound_names(node.orelse, names)
        elif isinstance(node, ast.Try):
            _bound_names(node.body, names)
            for handler in node.handlers:
                _bound_names(handler.body, names)
            _bound_names(node.orelse, names)
            _bound_names(node.finalbody, names)


def _top_level_names(path: Path) -> frozenset[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return frozenset()
    names: set[str] = set()
    _bound_names(tree.body, names)
    return frozenset(names)


def _source_resolves(root: Path, dotted: str) -> tuple[bool, bool]:
    """`(checkable, exists)` for a dotted path, read out of `src/`.

    A path that names a module answers on the module's own existence. Otherwise
    the last component is a symbol and everything before it must be a real
    module -- if it is not, nothing anchors the marker and it could never fail.
    """
    parts = dotted.split(".")
    if _module_source(root, parts) is not None:
        return True, True
    holder = _module_source(root, parts[:-1])
    if holder is None:
        return False, False
    return True, parts[-1] in _top_level_names(holder)


def _prose_findings(root: Path, text: str) -> list[Finding]:
    """The same check, for markers that live in the page's prose.

    Table rows are skipped here because `_rows` already owns them, so a marker
    is reported once whichever half of the page carries it.
    """
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            continue
        where = f"{ROADMAP}:{number}"
        for marker in _MARKER.finditer(line):
            named = [part.strip() for part in marker.group(1).split(",") if part.strip()]
            if not named:
                findings.append(
                    Finding("ROAD001", where, "an `absent:` marker names no surface; name a"
                            " symbol that would exist if it shipped, or drop the marker")
                )
                continue
            findings.extend(_marker_findings(root, where, named))
    return findings


def _marker_findings(root: Path, where: str, named: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for dotted in named:
        if not _DOTTED.match(dotted):
            findings.append(
                Finding("ROAD003", where, f"prose names {dotted!r}, which is not a dotted"
                        " symbol path and can never be checked")
            )
            continue
        checkable, exists = _source_resolves(root, dotted)
        if not checkable:
            findings.append(
                Finding("ROAD003", where, f"prose names {dotted!r}, whose module is not in the"
                        " source tree; the marker verifies nothing and would pass forever")
            )
        elif exists:
            findings.append(
                Finding("ROAD002", where, f"prose claims {dotted} is absent, but it resolves in"
                        " the source tree; the feature ships and this page understates it")
            )
    return findings


def scan(root: Path) -> list[Finding]:
    path = root / ROADMAP
    if not path.exists():
        return [Finding("ROAD001", ROADMAP, "the roadmap page is missing")]
    text = path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    for row in _rows(text):
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
    findings.extend(_prose_findings(root, text))
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
