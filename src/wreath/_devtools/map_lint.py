"""Keep the agent-facing map honest.

An agent arriving cold reads four files -- `AGENTS.md`, `repo-map.md`,
`docs/llms.txt`, and `docs/agents/manifest.json` -- and trusts what they say
about where things live. Nothing used to check them, so they drifted: the
manifest lost three subsystems' test lists to a bad patch that wrote
`"subsystems[5]"` as a literal key, `repo-map.md` pointed at `docs/concepts/`,
`docs/native/`, and `docs/internals/` (none of which exist) and named seven
public modules that had since been made private, and twenty subsystems shipped
without a manifest entry at all. Each of those costs an agent a wasted plan.

The docs site is already protected -- `mkdocs build --strict` fails on a missing
nav entry or a broken autodoc target -- which is exactly why `mkdocs.yml` was
the one map still accurate. This is that gate for the maps mkdocs does not read.

Findings:

* ``MAP001`` -- the manifest has a key the schema does not define. This is the
  one that catches the `"subsystems[5]"` class of damage: a patch that means to
  edit an array element and instead adds a sibling key is silent, and the data
  it added is unreachable by anything that walks `subsystems`.
* ``MAP002`` -- the manifest cites a path that does not exist.
* ``MAP003`` -- a public module under ``src/wreath`` appears in no subsystem's
  ``sources``. A subsystem nobody mapped is a subsystem an agent finds by grep.
* ``MAP004`` -- a subsystem is missing a required field.
* ``MAP005`` -- a prose map cites a repo path that does not exist.
* ``MAP006`` -- a ``docs/llms.txt`` link points at a page that does not exist.
* ``MAP007`` -- a guide is missing from ``docs/llms.txt``, the compact index
  agents read instead of the nav.

Run it with ``uv run wreath-map-lint``; ``0`` means clean.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .native_lint import repo_root

#: Prose maps whose inline-code repo paths must resolve.
PROSE_MAPS: tuple[str, ...] = ("AGENTS.md", "README.md", "repo-map.md")

#: The compact index agents read in place of the mkdocs nav.
LLMS_TXT = "docs/llms.txt"

MANIFEST = "docs/agents/manifest.json"

#: Top-level keys the manifest may define.
MANIFEST_KEYS = frozenset(
    {"schema_version", "project", "python", "status", "entrypoints", "commands",
     "subsystems", "invariants", "playbooks"}
)

#: Keys a subsystem entry may define, and the ones it must.
SUBSYSTEM_KEYS = frozenset({"name", "guides", "reference", "sources", "tests", "policy",
                            "decisions"})
SUBSYSTEM_REQUIRED = ("name", "guides", "sources", "tests")

#: Fields holding lists of repository paths.
PATH_FIELDS = ("guides", "reference", "sources", "tests", "decisions")

#: Directories under `src/wreath` that are implementation, not a public surface.
PRIVATE_PREFIX = "_"

#: Modules that are public but deliberately not a subsystem of their own.
NOT_A_SUBSYSTEM = frozenset({"__init__", "__main__"})

#: A repo path cited in prose starts with one of these.
REPO_ROOTS = ("src/", "tests/", "docs/", "benchmarks/", "tools/")

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


@dataclass(frozen=True)
class Finding:
    code: str
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.code} {self.message}"


def _is_repo_path(text: str) -> bool:
    """True when an inline-code span is meant to name a file in this repository.

    Deliberately conservative: prose is full of backticked things that are not
    paths (`uv sync --group X`, `dict`, `wreath.router`), and a linter that
    cries about those gets turned off rather than fixed.
    """
    if not text.startswith(REPO_ROOTS):
        return False
    return not any(char in text for char in "*<> \t|")


def _public_modules(root: Path) -> list[str]:
    """Every public surface under `src/wreath`, module or package."""
    package = root / "src" / "wreath"
    names: list[str] = []
    for entry in sorted(package.iterdir()):
        if entry.name.startswith(PRIVATE_PREFIX) or entry.name in NOT_A_SUBSYSTEM:
            continue
        if entry.is_dir() and (entry / "__init__.py").exists():
            names.append(entry.name)
        elif entry.suffix == ".py":
            names.append(entry.stem)
    return names


def check_manifest(root: Path) -> list[Finding]:
    path = root / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [Finding("MAP001", MANIFEST, f"cannot be read as JSON: {exc}")]

    findings: list[Finding] = []
    for key in sorted(set(manifest) - MANIFEST_KEYS):
        findings.append(
            Finding("MAP001", MANIFEST, f"unexpected top-level key {key!r}; a subsystem"
                    " field belongs inside its entry in the `subsystems` array")
        )

    cited: list[tuple[str, str]] = [
        (f"entrypoints.{name}", value) for name, value in manifest.get("entrypoints", {}).items()
    ]
    covered: set[str] = set()

    for index, subsystem in enumerate(manifest.get("subsystems", [])):
        name = subsystem.get("name", f"subsystems[{index}]")
        for key in sorted(set(subsystem) - SUBSYSTEM_KEYS):
            findings.append(Finding("MAP001", MANIFEST, f"{name}: unexpected key {key!r}"))
        for key in SUBSYSTEM_REQUIRED:
            if not subsystem.get(key):
                findings.append(Finding("MAP004", MANIFEST, f"{name}: missing {key!r}"))
        for field in PATH_FIELDS:
            for value in subsystem.get(field, []):
                cited.append((f"{name}.{field}", value))
        for source in subsystem.get("sources", []):
            trimmed = source.removeprefix("src/wreath/").rstrip("/")
            covered.add(trimmed.removesuffix(".py"))

    for where, value in cited:
        if not (root / value).exists():
            findings.append(Finding("MAP002", MANIFEST, f"{where}: no such path {value!r}"))

    for module in _public_modules(root):
        if module not in covered:
            findings.append(
                Finding("MAP003", MANIFEST, f"public module `wreath.{module}` is in no"
                        " subsystem's `sources`; add it so it can be found without grep")
            )
    return findings


def check_prose(root: Path, relative: str) -> list[Finding]:
    """Every repo path a prose map cites in inline code must resolve."""
    path = root / relative
    if not path.exists():
        return [Finding("MAP005", relative, "map file is missing")]
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    findings: list[Finding] = []
    seen: set[str] = set()
    for match in _INLINE_CODE.finditer(text):
        cited = match.group(1)
        if not _is_repo_path(cited) or cited in seen:
            continue
        seen.add(cited)
        if not (root / cited.rstrip("/")).exists():
            findings.append(Finding("MAP005", relative, f"cites {cited!r}, which does not exist"))
    return findings


def check_llms_txt(root: Path) -> list[Finding]:
    """Links in the compact index resolve, and every guide is listed."""
    path = root / LLMS_TXT
    if not path.exists():
        return [Finding("MAP006", LLMS_TXT, "index is missing")]
    text = path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    docs = root / "docs"
    listed: set[str] = set()
    for match in _MD_LINK.finditer(text):
        target = match.group(1).split("#", 1)[0]
        if not target or target.startswith(("http://", "https://")):
            continue
        listed.add(target)
        if not (docs / target).exists():
            findings.append(
                Finding("MAP006", LLMS_TXT, f"links to {target!r}, which does not exist")
            )

    for guide in sorted((docs / "guides").glob("*.md")):
        relative = f"guides/{guide.name}"
        if relative not in listed:
            findings.append(
                Finding("MAP007", LLMS_TXT, f"{relative} is not listed; agents read this index"
                        " instead of the nav, so an unlisted guide is an invisible one")
            )
    return findings


def scan(root: Path) -> list[Finding]:
    findings = check_manifest(root)
    for relative in PROSE_MAPS:
        findings.extend(check_prose(root, relative))
    findings.extend(check_llms_txt(root))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-map-lint",
        description="Check that the agent-facing maps still describe this repository.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = repo_root()
    findings = scan(root)

    if args.format == "json":
        print(json.dumps({"count": len(findings),
                          "findings": [finding.__dict__ for finding in findings]}, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        print(f"\nwreath-map-lint: {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
