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

* `MAP001` -- the manifest has a key the schema does not define. This is the
  one that catches the `"subsystems[5]"` class of damage: a patch that means to
  edit an array element and instead adds a sibling key is silent, and the data
  it added is unreachable by anything that walks `subsystems`.
* `MAP002` -- the manifest cites a path that does not exist.
* `MAP003` -- a public module under `src/wreath` appears in no subsystem's
  `sources`. A subsystem nobody mapped is a subsystem an agent finds by grep.
* `MAP014` -- a public module under `src/wreath` is rendered by no page in
  `docs/reference/`. `MAP003` asks only whether the map *mentions* the module,
  and being mentioned is not being documented: `wreath.response_cache` sat in
  the `cache` subsystem's `sources` -- so `MAP003` passed -- with no reference
  page at all, and its entire `@cached` surface went ungenerated without anyone
  noticing. The two rules share `_public_modules` deliberately, so the question
  "is this module public?" has one answer rather than two that drift.
* `MAP004` -- a subsystem is missing a required field.
* `MAP005` -- a prose map cites a repo path that does not exist.
* `MAP006` -- a `docs/llms.txt` link points at a page that does not exist.
* `MAP007` -- a guide is missing from `docs/llms.txt`, the compact index
  agents read instead of the nav.
* `MAP008` -- a sanitizer build in `tools/sanitizers/` no longer compiles
  the same sources as the extension it mirrors. Each `setup_*.py` keeps its
  own `SOURCES` tuple and says it is "kept in step with setup.py"; nothing
  checked that, and it drifted -- `cedar.c`, `jose.c`, and `scheduler.c`
  were in the shipped `_core` extension but had never been built under
  ASan/UBSan. A file missing here is not a broken build: it is C that the
  sanitizer suites silently do not cover, which is the worst way to be wrong
  about memory safety.
* `MAP010` -- a subsystem's `replaces` is not a list of distribution names.
  Shape, not truth: this runs offline and must not ask PyPI anything, so it
  checks that each entry is a name someone could type after `pip install` --
  `python-jose[cryptography]` and `pydantic (v2)` are not.
* `MAP011` -- a subsystem that ships guides or reference pages has no
  `capability` sentence, or has `"capability": null` (the deliberate "this is
  internal" marker) while still claiming `replaces`. The capability map is
  generated from that field, so a subsystem that never writes one is simply
  missing from the page that exists to show the surface is there -- silently,
  which is how the maps rotted the first time.
* `MAP013` -- a bounded in-process table or queue is built somewhere under
  `src/wreath` and `docs/reference/memory-budgets.md` does not mention the file
  that builds it. That page answers "how much does a worker hold, and where do I
  tune it?", which used to require grepping a dozen constructors whose bounds
  were all named differently. A page like that is worth reading only if
  something makes it true.
* `MAP012` -- `docs/capabilities.md` is gone, or no longer carries the
  `::: capability-map` directive that renders those fields. Requiring data that
  nothing reads is how a field becomes decoration; this keeps the reader and
  the requirement attached to each other. The generated table's *links* need no
  check of their own -- they are the `guides` and `reference` paths `MAP002`
  already resolves.
* `MAP015` -- `src/wreath/_capability_data.py` no longer matches the manifest,
  or is missing. `wreath capabilities` answers from that file rather than from
  `docs/agents/manifest.json`, because the wheel carries the package and not the
  docs; the copy is generated, and a generated copy with no staleness check is
  the same rot every other rule here is about. Compared byte for byte, so a hand
  edit fails exactly where a missed regeneration does, and `--fix` regenerates.
* `MAP009` -- a map cites a path `.gitignore` excludes. This is the one that
  hides best: the file is on disk, every other check resolves it, and the map is
  wrong only for someone who clones the repository. It was found when
  `.gitignore` excluded a whole documentation directory the manifest cited
  twenty paths from, so a fresh checkout had none of them and `MAP002` -- which
  asks only whether the path exists -- passed on every developer machine.
  Existence is not availability.

Run it with `uv run wreath-map-lint`; `0` means clean.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .native_lint import repo_root

#: Prose maps whose inline-code repo paths must resolve.
PROSE_MAPS: tuple[str, ...] = ("AGENTS.md", "README.md", "repo-map.md")

#: The compact index agents read in place of the site nav.
LLMS_TXT = "docs/llms.txt"

MANIFEST = "docs/agents/manifest.json"

#: The page that turns the manifest's `capability`/`replaces` fields into prose,
#: and the directive on it that does the turning.
CAPABILITY_PAGE = "docs/capabilities.md"
CAPABILITY_DIRECTIVE = "::: capability-map"

#: Top-level keys the manifest may define.
MANIFEST_KEYS = frozenset(
    {"schema_version", "project", "python", "status", "entrypoints", "commands",
     "subsystems", "invariants", "playbooks"}
)

#: Keys a subsystem entry may define, and the ones it must.
SUBSYSTEM_KEYS = frozenset({"name", "guides", "reference", "sources", "tests", "policy",
                            "capability", "replaces"})
SUBSYSTEM_REQUIRED = ("name", "guides", "sources", "tests")

#: A distribution name as PEP 503 spells it: letters, digits, and `-._` between
#: them. Extras, versions, and markers are deliberately rejected -- `replaces` is
#: vocabulary for a reader, not a requirement line.
_DISTRIBUTION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")

#: Fields holding lists of repository paths.
PATH_FIELDS = ("guides", "reference", "sources", "tests")

#: Directories under `src/wreath` that are implementation, not a public surface.
PRIVATE_PREFIX = "_"

#: Modules that are public but deliberately not a subsystem of their own.
NOT_A_SUBSYSTEM = frozenset({"__init__", "__main__"})

#: Where a public module's generated API reference lives, and the directive that
#: generates it. `wreath docs` is the renderer; `_docs/apidoc.py` owns the
#: spelling and this mirrors its `_DIRECTIVE` -- a page's `::: wreath.thing`
#: line is what turns the module into reference prose, so it, and not the mere
#: existence of a file named after the module, is what "documented" means.
REFERENCE_DIR = "docs/reference"
REFERENCE_DIRECTIVE = re.compile(r"^:::\s+([\w.]+)\s*$")

#: The package whose modules those directives name.
PACKAGE = "wreath"

#: Public modules that deliberately have no reference page of their own. An
#: explicit list with a reason each, never a pattern: a rule with a silent skip
#: in it is the shape this whole file exists to prevent.
#:
#: * `storage` -- a deprecated alias for `wreath.objects`, re-exporting the same
#:   classes under their former names. Rendering it would generate a second copy
#:   of `docs/reference/objects.md` written in the vocabulary being retired,
#:   which is the opposite of what the deprecation is for.
NOT_A_REFERENCE_PAGE = frozenset({"storage"})

#: A repo path cited in prose starts with one of these.
REPO_ROOTS = ("src/", "tests/", "docs/", "benchmarks/", "tools/")

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_INVALID_REPO_PATH = re.compile(r"[*<> \t|]")


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
    return _INVALID_REPO_PATH.search(text) is None


def _ignored_paths(root: Path, cited: list[str]) -> frozenset[str]:
    """The cited paths excluded by `.gitignore`, in one Git process.

    *Ignored*, deliberately, rather than *untracked*. An untracked file is
    ordinary uncommitted work and will be in the next commit; an ignored one
    never can be, so a map citing it is wrong for everyone but the machine it
    was written on. Flagging untracked paths would fire on every new file and
    get the rule turned off, which is how the drift returns.

    Failure is silence. Outside a checkout, or with no git on `PATH`, whether a
    path is ignored is unknown -- and a lint that fires on everything when it
    cannot tell is worse than one that stays quiet.
    """
    try:
        values = sorted({value.rstrip("/") for value in cited})
        if not values:
            return frozenset()
        result = subprocess.run(
            ("git", "-C", str(root), "check-ignore", "--stdin"),
            input="".join(f"{value}\n" for value in values),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    # 0 = ignored, 1 = not ignored, 128 = not a repository or git unavailable.
    if result.returncode not in (0, 1):
        return frozenset()
    return frozenset(result.stdout.splitlines())


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
        findings.extend(_check_capability(name, subsystem))
        for field in PATH_FIELDS:
            for value in subsystem.get(field, []):
                cited.append((f"{name}.{field}", value))
        for source in subsystem.get("sources", []):
            trimmed = source.removeprefix("src/wreath/").rstrip("/")
            covered.add(trimmed.removesuffix(".py"))

    ignored = _ignored_paths(root, [value for _where, value in cited])
    for where, value in cited:
        if not (root / value).exists():
            findings.append(Finding("MAP002", MANIFEST, f"{where}: no such path {value!r}"))
        elif value.rstrip("/") in ignored:
            findings.append(
                Finding("MAP009", MANIFEST, f"{where}: {value!r} is excluded by .gitignore;"
                        " a fresh clone would not have it")
            )

    for module in _public_modules(root):
        if module not in covered:
            findings.append(
                Finding("MAP003", MANIFEST, f"public module `wreath.{module}` is in no"
                        " subsystem's `sources`; add it so it can be found without grep")
            )
    return findings


def _check_capability(name: str, subsystem: dict) -> list[Finding]:
    """One subsystem's contribution to the capability map, checked for shape.

    `capability` is the sentence the map is written in and `replaces` is the
    vocabulary beside it; between them they are the only part of the manifest a
    *user* ever reads, which is why they are checked here rather than trusted to
    the docs build. The build can only fail on what it renders, and a subsystem
    that omits both renders nothing at all.
    """
    findings: list[Finding] = []
    documented = bool(subsystem.get("guides") or subsystem.get("reference"))
    replaces = subsystem.get("replaces")

    if "capability" not in subsystem:
        if documented:
            findings.append(Finding(
                "MAP011", MANIFEST,
                f"{name}: has documentation but no 'capability'; add the sentence it"
                " belongs on the capability map with, or \"capability\": null to say"
                " it is internal"))
    elif subsystem["capability"] is None:
        if replaces:
            findings.append(Finding(
                "MAP011", MANIFEST,
                f"{name}: 'capability' is null, which keeps it off the capability map,"
                " but it still lists 'replaces' -- a claim nobody will read is a claim"
                " nobody will check"))
    elif not isinstance(subsystem["capability"], str) or not subsystem["capability"].strip():
        findings.append(Finding(
            "MAP011", MANIFEST,
            f"{name}: 'capability' must be a sentence for the capability map's first"
            f" column, not {subsystem['capability']!r}"))

    if replaces is None:
        return findings
    if not isinstance(replaces, list):
        return [*findings, Finding(
            "MAP010", MANIFEST,
            f"{name}: 'replaces' must be a list of distribution names, not"
            f" {type(replaces).__name__}")]
    for entry in replaces:
        if not isinstance(entry, str) or not _DISTRIBUTION.match(entry):
            findings.append(Finding(
                "MAP010", MANIFEST,
                f"{name}: 'replaces' entry {entry!r} is not a distribution name;"
                " write the bare name a reader would recognise, with no extras,"
                " version, or marker"))
    return findings


def check_capability_page(root: Path) -> list[Finding]:
    """The page that renders `capability` and `replaces` still renders them."""
    path = root / CAPABILITY_PAGE
    if not path.is_file():
        return [Finding("MAP012", CAPABILITY_PAGE, "the capability map page is missing;"
                        " the manifest's 'capability' and 'replaces' fields have"
                        " nothing rendering them")]
    text = path.read_text(encoding="utf-8")
    if not any(line.strip() == CAPABILITY_DIRECTIVE for line in text.splitlines()):
        return [Finding("MAP012", CAPABILITY_PAGE, f"no {CAPABILITY_DIRECTIVE!r} directive;"
                        " the table it generates is the only reader the manifest's"
                        " 'capability' and 'replaces' fields have")]
    return []


def _rendered_modules(root: Path) -> set[str]:
    """Every top-level `wreath.<module>` some reference page pulls in with `:::`.

    A submodule counts for its parent -- `docs/reference/orm.md` renders
    `::: wreath.orm.session` among fifteen others, and the module `MAP003`
    knows about is `orm`. The two rules agree on their subject only if this
    reduction matches `_public_modules`, which lists packages by their top
    name.
    """
    rendered: set[str] = set()
    for page in sorted((root / REFERENCE_DIR).rglob("*.md")):
        for line in page.read_text(encoding="utf-8").splitlines():
            match = REFERENCE_DIRECTIVE.match(line)
            if match is None:
                continue
            parts = match.group(1).split(".")
            if len(parts) > 1 and parts[0] == PACKAGE:
                rendered.add(parts[1])
    return rendered


def check_reference_pages(root: Path) -> list[Finding]:
    """Every public module has a reference page that actually renders it."""
    modules = [m for m in _public_modules(root) if m not in NOT_A_REFERENCE_PAGE]
    if not modules:
        # Nothing public, so nothing is owed -- and this is what keeps the rule
        # usable against a synthetic root with no `src/wreath` at all.
        return []
    if not (root / REFERENCE_DIR).is_dir():
        return [Finding("MAP014", REFERENCE_DIR, "the reference section is gone; no public"
                        " module's API is rendered anywhere")]
    rendered = _rendered_modules(root)
    return [
        Finding("MAP014", REFERENCE_DIR, f"no page renders `{PACKAGE}.{module}`; give it one"
                f" carrying `::: {PACKAGE}.{module}`, or its public API ships with no"
                " reference at all -- being named in the manifest is not being documented")
        for module in modules
        if module not in rendered
    ]


def check_prose(root: Path, relative: str) -> list[Finding]:
    """Every repo path a prose map cites in inline code must resolve."""
    path = root / relative
    if not path.exists():
        return [Finding("MAP005", relative, "map file is missing")]
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    findings: list[Finding] = []
    seen: set[str] = set()
    cited_paths: list[str] = []
    for match in _INLINE_CODE.finditer(text):
        cited = match.group(1)
        if not _is_repo_path(cited) or cited in seen:
            continue
        seen.add(cited)
        cited_paths.append(cited)
    ignored = _ignored_paths(root, cited_paths)
    for cited in cited_paths:
        if not (root / cited.rstrip("/")).exists():
            findings.append(Finding("MAP005", relative, f"cites {cited!r}, which does not exist"))
        elif cited.rstrip("/") in ignored:
            findings.append(
                Finding("MAP009", relative, f"cites {cited!r}, which .gitignore excludes;"
                        " a fresh clone would not have it")
            )
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
    """The text of the `sources=[...]` list within `text[start:end]`.

    Only that list counts. A `depends=[...]` beside it names headers *and*
    files that are `#include`d rather than compiled -- the reactor extension
    lists five such `.c` files -- so reading the whole block would demand the
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

    The sanitizer builds hold their file names in a module-level `SOURCES`
    tuple and interpolate it into `sources=[...]`, so when the list itself
    names no `.c` file the whole module is read instead. Each of those builds
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
    if not setup.exists():
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
    """`src/wreath/session_store.py` -> `session_store`; nested paths -> None.

    Only a top-level module or package has a conventional test path worth
    guessing at. `src/wreath/_auth/cedar.py` does not -- its tests live wherever
    the subsystem that owns `_auth` put them.
    """
    if not source.startswith("src/wreath/"):
        return None
    trimmed = source.removeprefix("src/wreath/").rstrip("/")
    if "/" in trimmed:
        return None
    return trimmed.removesuffix(".py") or None


def _conventional_tests(root: Path, module: str) -> list[str]:
    """The test paths named after `module` that actually exist.

    Deliberately only the two exact spellings the repository already uses --
    `tests/test_<module>.py` and `tests/<module>/`. A prefix match would sweep
    up `tests/test_native_lint_readability.py` under `native_lint`, and a
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

    Three of them, and no more. Each has exactly one right answer, which is what
    separates them from the findings a person has to resolve -- `MAP002` cannot
    know where a moved file went, and `MAP003` cannot know which subsystem a new
    module belongs to. Guessing at either would produce a manifest that lints
    clean and lies, which is the failure this whole tool exists to prevent.

    * **adopt** -- `name=src/wreath/x.py` puts a source under the subsystem you
      name (you supply the judgment) and brings its conventional tests with it
      (the tool supplies the bookkeeping).
    * **conventional tests** -- for every source already listed, attach
      `tests/test_<module>.py` / `tests/<module>/` when it exists on disk and
      is not listed yet. This is the half that silently rots: the module gets
      mapped when it lands, and the test file added a week later does not.
    * **the capability index** -- rewrite `src/wreath/_capability_data.py` from
      the manifest (`MAP015`). Wholly derived, so there is nothing to judge.

    Returns `(changes, refusals)`.
    """
    path = root / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{MANIFEST} cannot be read as JSON: {exc}"]

    subsystems = {s.get("name"): s for s in manifest.get("subsystems", [])}
    known_subsystems = ", ".join(sorted(n for n in subsystems if n))
    changes: list[str] = []
    refusals: list[str] = []

    for name, source in adopt:
        subsystem = subsystems.get(name)
        if subsystem is None:
            refusals.append(
                f"no subsystem named {name!r}; known: {known_subsystems}"
            )
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

    # After the manifest is written, never before: the index is derived from it,
    # and regenerating from the pre-repair copy would leave MAP015 standing on
    # the very run that was asked to clear it.
    from .capability_index import DATA_MODULE, write_module

    if write_module(root, manifest):
        changes.append(f"{DATA_MODULE} regenerated from {MANIFEST}")
    return changes, refusals


#: Where the in-process budgets are written down.
BUDGETS = "docs/reference/memory-budgets.md"

#: The bounded primitives whose construction has to be accounted for. Names
#: rather than qualified paths, because a caller imports them by name; the
#: import check below is what stops `asyncio.Queue` from being mistaken for
#: `wreath.queue.Queue`, which a bare name scan does confuse.
BUDGETED = {
    "KV", "Queue", "PriorityQueue", "RoundRobin",
    "BoundedCache", "MemoryStore", "BoundedLogQueue", "BoundedExportQueue",
}

#: Modules those names legitimately come from.
BUDGET_SOURCES = {"kv", "queue", "cache", "store", "_logsink", "_otlp"}

#: The primitives' own modules, which build them for a living.
BUDGET_EXEMPT = {"src/wreath/kv.py", "src/wreath/queue.py"}


def _budget_sites(root: Path) -> list[tuple[str, int, str]]:
    """Every construction of a bounded table or queue under `src/wreath`."""
    sites: list[tuple[str, int, str]] = []
    for path in sorted((root / "src" / "wreath").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in BUDGET_EXEMPT or "/_devtools/" in f"/{relative}":
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # A site is a *call* to a name imported from BUDGET_SOURCES, so the name
        # has to be spelled in this file for one to exist -- skipping the files
        # that spell none of them skips only provable non-matches. Reading the
        # tree costs 22 ms against 1.9 s to parse it, so the cheap test goes
        # first; this scan runs on every `wreath-map-lint` and in the test that
        # drives it.
        if not any(name in source for name in BUDGETED):
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            # A fixture of deliberately invalid source is not a module.
            continue
        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.rsplit(".", 1)[-1] in BUDGET_SOURCES:
                    for alias in node.names:
                        if alias.name in BUDGETED:
                            bound.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in bound
            ):
                sites.append((relative, node.lineno, node.func.id))
    return sites


def check_memory_budgets(root: Path) -> list[Finding]:
    """Every bounded table and queue is named in the budgets page."""
    sites = _budget_sites(root)
    if not sites:
        # Nothing to account for, so nothing is owed. This is also what keeps
        # the rule usable against the synthetic roots the map-lint tests build,
        # which have no `src/wreath` at all.
        return []
    page = root / BUDGETS
    if not page.exists():
        return [Finding("MAP013", BUDGETS, "the in-process memory budgets page is gone")]
    text = page.read_text(encoding="utf-8")
    findings: list[Finding] = []
    seen: set[str] = set()
    for relative, line, kind in sites:
        # Matched on the *module* rather than the exact path, because the page
        # names a subsystem the way an operator thinks of it ("the response
        # cache") and not a line number that would churn on every edit.
        module = relative.removeprefix("src/wreath/").removesuffix(".py")
        stem = module.rsplit("/", 1)[-1].lstrip("_")
        if module in seen:
            continue
        if stem and (stem in text or module in text):
            seen.add(module)
            continue
        findings.append(
            Finding(
                "MAP013",
                f"{relative}:{line}",
                f"builds a bounded {kind} that {BUDGETS} does not account for; add a "
                "row naming what it holds and the knob that bounds it",
            )
        )
        seen.add(module)
    return findings


def check_capability_index(root: Path) -> list[Finding]:
    """MAP015 -- `src/wreath/_capability_data.py` still matches the manifest.

    The index ships inside the wheel because `docs/` does not, so `wreath
    capabilities` answers from a copy. A copy with no staleness check is the
    drift this file exists to catch, one directory over.

    Byte for byte, and **a missing file is a finding rather than a skip**: a
    check that reads nothing and reports nothing clean is indistinguishable
    from one that agreed.
    """
    from .capability_index import DATA_MODULE, render_module

    path = root / DATA_MODULE
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # MAP001 already reports an unreadable manifest; a second voice saying
        # the same thing is noise, and there is nothing to compare against.
        return []
    expected = render_module(manifest)
    try:
        actual = path.read_text(encoding="utf-8")
    except OSError:
        return [Finding("MAP015", DATA_MODULE, "the shipped capability index is "
                        "missing; generate it with `uv run wreath-map-lint --fix`")]
    if actual != expected:
        return [Finding("MAP015", DATA_MODULE, f"no longer matches {MANIFEST}; "
                        "regenerate it with `uv run wreath-map-lint --fix` (do not "
                        "edit it by hand -- the manifest is the map)")]
    return []


def scan(root: Path) -> list[Finding]:
    findings = check_manifest(root)
    findings.extend(check_reference_pages(root))
    findings.extend(check_capability_page(root))
    findings.extend(check_capability_index(root))
    for relative in PROSE_MAPS:
        findings.extend(check_prose(root, relative))
    findings.extend(check_llms_txt(root))
    findings.extend(check_sanitizer_sources(root))
    findings.extend(check_memory_budgets(root))
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
             "when they exist on disk and are not listed yet, and regenerate "
             "the shipped capability index (MAP015)",
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
