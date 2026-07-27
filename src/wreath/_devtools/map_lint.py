"""Keep the agent-facing map honest.

An agent arriving cold reads four files -- `AGENTS.md`, `repo-map.md`,
`docs/llms.txt`, and `docs/agents/manifest.json` -- and trusts what they say
about where things live. Nothing used to check them, so they drifted: the
manifest lost three subsystems' test lists to a bad patch that wrote
`"subsystems[5]"` as a literal key, `repo-map.md` pointed at `docs/concepts/`,
`docs/native/`, and `docs/internals/` (none of which exist) and named seven
public modules that had since been made private, and twenty subsystems shipped
without a manifest entry at all. Each of those costs an agent a wasted plan.

The docs site is already protected -- `wreath docs check` fails on a page
missing from the nav, a dead internal link, or a broken anchor -- which is
exactly why `wreath_docs.py` is the one map that stays accurate. This is that
gate for the maps the site build does not read.

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
* ``MAP008`` -- a sanitizer build in ``tools/sanitizers/`` no longer compiles
  the same sources as the extension it mirrors. Each ``setup_*.py`` keeps its
  own ``SOURCES`` tuple and says it is "kept in step with setup.py"; nothing
  checked that, and it drifted -- ``cedar.c``, ``jose.c``, and ``scheduler.c``
  were in the shipped ``_core`` extension but had never been built under
  ASan/UBSan. A file missing here is not a broken build: it is C that the
  sanitizer suites silently do not cover, which is the worst way to be wrong
  about memory safety.

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

#: The compact index agents read in place of the site nav.
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


#: `setup.py` names each extension as a dotted module string; the sanitizer
#: builds mirror one extension each and name it the same way.
_EXTENSION_RE = re.compile(r'"(wreath\._(?:native|exp)\._[a-z0-9_]+)"')
_C_SOURCE_RE = re.compile(r'([A-Za-z0-9_/]+\.c)')


def _sources_block(text: str, start: int, end: int) -> str:
    """The text of the ``sources=[...]`` list within ``text[start:end]``.

    Only that list counts. A ``depends=[...]`` beside it names headers *and*
    files that are ``#include``d rather than compiled -- the reactor extension
    lists five such ``.c`` files -- so reading the whole block would demand the
    sanitizer compile units that must not be compiled separately.
    """
    marker = text.find("sources=", start)
    if marker == -1 or marker >= end:
        return ""
    opening = text.find("[", marker)
    if opening == -1 or opening >= end:
        return ""
    depth = 0
    for index in range(opening, end):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return text[opening : index + 1]
    return ""


def _extension_sources(text: str) -> dict[str, set[str]]:
    """Map each extension name to the basenames of the C files it compiles.

    Both build files are read as text rather than imported: `setup.py` runs
    pkg-config for the optional HTTP/3 backend at import time, and a linter that
    needed QUIC libraries installed to check a source list would not run.

    The sanitizer builds hold their file names in a module-level ``SOURCES``
    tuple and interpolate it into ``sources=[...]``, so when the list itself
    names no ``.c`` file the whole module is read instead. Each of those builds
    mirrors exactly one extension, which is what makes that safe.
    """
    sources: dict[str, set[str]] = {}
    matches = list(_EXTENSION_RE.finditer(text))
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        names = {
            Path(name).name
            for name in _C_SOURCE_RE.findall(_sources_block(text, start, end))
        }
        if not names and len(matches) == 1:
            names = {Path(name).name for name in _C_SOURCE_RE.findall(text)}
        sources.setdefault(match.group(1), set()).update(names)
    return sources


def check_sanitizer_sources(root: Path) -> list[Finding]:
    """Every sanitizer build compiles what its real extension compiles.

    Only *missing* sources are reported. A sanitizer build may legitimately add
    a file (a test shim), but a file it omits is C excluded from ASan/UBSan
    coverage while the suite still reports success.
    """
    findings: list[Finding] = []
    setup = root / "setup.py"
    sanitizers = root / "tools/sanitizers"
    if not setup.exists() or not sanitizers.is_dir():
        return findings

    real = _extension_sources(setup.read_text(encoding="utf-8"))
    for build in sorted(sanitizers.glob("setup_*.py")):
        relative = f"tools/sanitizers/{build.name}"
        mirrored = _extension_sources(build.read_text(encoding="utf-8"))
        for extension, listed in mirrored.items():
            expected = real.get(extension)
            if expected is None:
                findings.append(
                    Finding("MAP008", relative, f"builds {extension!r}, which setup.py"
                            " does not define")
                )
                continue
            missing = sorted(expected - listed)
            if missing:
                findings.append(
                    Finding("MAP008", relative, f"{extension} omits {', '.join(missing)};"
                            " those sources ship but are never built under the"
                            " sanitizers, so their suites pass without covering them")
                )
    return findings


def _module_name(source: str) -> str | None:
    """``src/wreath/session_store.py`` -> ``session_store``; nested paths -> None.

    Only a top-level module or package has a conventional test path worth
    guessing at. ``src/wreath/_auth/cedar.py`` does not -- its tests live wherever
    the subsystem that owns ``_auth`` put them.
    """
    if not source.startswith("src/wreath/"):
        return None
    trimmed = source.removeprefix("src/wreath/").rstrip("/")
    if "/" in trimmed:
        return None
    return trimmed.removesuffix(".py") or None


def _conventional_tests(root: Path, module: str) -> list[str]:
    """The test paths named after ``module`` that actually exist.

    Deliberately only the two exact spellings the repository already uses --
    ``tests/test_<module>.py`` and ``tests/<module>/``. A prefix match would sweep
    up ``tests/test_native_lint_readability.py`` under ``native_lint``, and a
    guessed-wrong entry in the manifest is worse than a missing one: an agent
    reads it and runs the wrong suite.
    """
    found = []
    for candidate in (f"tests/test_{module}.py", f"tests/{module}/"):
        if (root / candidate.rstrip("/")).exists():
            found.append(candidate)
    return found


def _listed(values: list[str]) -> set[str]:
    """Path comparison that does not care about a trailing slash."""
    return {value.rstrip("/") for value in values}


def repair(root: Path, adopt: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Apply the mechanically-derivable manifest repairs.

    Two of them, and no more. Both have exactly one right answer, which is what
    separates them from the findings a person has to resolve -- ``MAP002`` cannot
    know where a moved file went, and ``MAP003`` cannot know which subsystem a new
    module belongs to. Guessing at either would produce a manifest that lints
    clean and lies, which is the failure this whole tool exists to prevent.

    * **adopt** -- ``name=src/wreath/x.py`` puts a source under the subsystem you
      name (you supply the judgment) and brings its conventional tests with it
      (the tool supplies the bookkeeping).
    * **conventional tests** -- for every source already listed, attach
      ``tests/test_<module>.py`` / ``tests/<module>/`` when it exists on disk and
      is not listed yet. This is the half that silently rots: the module gets
      mapped when it lands, and the test file added a week later does not.

    Returns ``(changes, refusals)``.
    """
    path = root / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{MANIFEST} cannot be read as JSON: {exc}"]

    subsystems = {s.get("name"): s for s in manifest.get("subsystems", [])}
    changes: list[str] = []
    refusals: list[str] = []

    for name, source in adopt:
        subsystem = subsystems.get(name)
        if subsystem is None:
            known = ", ".join(sorted(n for n in subsystems if n))
            refusals.append(f"no subsystem named {name!r}; known: {known}")
            continue
        if not (root / source.rstrip("/")).exists():
            refusals.append(f"{name}: no such path {source!r} — adopting it would "
                            "add a MAP002 finding, not remove one")
            continue
        if source.rstrip("/") in _listed(subsystem.setdefault("sources", [])):
            continue
        subsystem["sources"].append(source)
        changes.append(f"{name}.sources += {source}")

    for name, subsystem in subsystems.items():
        tests = subsystem.setdefault("tests", [])
        for source in subsystem.get("sources", []):
            module = _module_name(source)
            if module is None:
                continue
            for candidate in _conventional_tests(root, module):
                if candidate.rstrip("/") not in _listed(tests):
                    tests.append(candidate)
                    changes.append(f"{name}.tests += {candidate}")

    if changes:
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return changes, refusals


def scan(root: Path) -> list[Finding]:
    findings = check_manifest(root)
    for relative in PROSE_MAPS:
        findings.extend(check_prose(root, relative))
    findings.extend(check_llms_txt(root))
    findings.extend(check_sanitizer_sources(root))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-map-lint",
        description="Check that the agent-facing maps still describe this repository.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--fix", action="store_true",
        help="apply the mechanical manifest repairs: attach each source's "
             "conventional test paths (tests/test_<module>.py, tests/<module>/) "
             "when they exist on disk and are not listed yet",
    )
    parser.add_argument(
        "--adopt", action="append", metavar="SUBSYSTEM=PATH", default=[],
        help="add PATH to that subsystem's `sources`, with its conventional "
             "tests (repeatable, e.g. --adopt middleware=src/wreath/session_store.py). "
             "Implies --fix.",
    )
    args = parser.parse_args(argv)

    root = repo_root()

    adopt: list[tuple[str, str]] = []
    for spec in args.adopt:
        name, separator, source = spec.partition("=")
        if not separator or not name or not source:
            parser.error(f"--adopt expects SUBSYSTEM=PATH, got {spec!r}")
        adopt.append((name, source))

    if args.fix or adopt:
        changes, refusals = repair(root, adopt)
        for change in changes:
            print(f"fixed  {change}")
        for refusal in refusals:
            print(f"REFUSED  {refusal}")
        if not changes and not refusals:
            print("nothing to fix: every source's conventional tests are already listed.")
        print()
        if refusals:
            return 1

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
