"""Keep a compiled extension honest about the sources it was built from.

A stale `.so` is importable. That is the whole problem: nothing about a build
that silently did not happen looks different from a build that did, so the
symptoms arrive later and somewhere else. `_http3.cpython-314-x86_64-linux-gnu.so`
was dated 19 July while its sources were 22-23 July -- 336 insertions across
three files, an entire ACK-backpressure commit, absent from the binary. Five
HTTP/3 tests failed against it, and would have been filed as protocol defects
if the agent running them had not thought to check the artifact's date.

It survived because HTTP/3 is opt-in: `WREATH_BUILD_HTTP3=1` gates the
extension, so every ordinary `setup.py build_ext --inplace` skips it and leaves
the previous artifact in place, still importable, still four days behind. The
mechanism is not specific to HTTP/3 -- it recurs for any extension a default
build does not compile.

Findings:

* `BUILD001` -- a built extension is older than a source it compiles. The
  message names the file and the delta, because "stale" without a number
  invites the assumption that it is stale by a second.
* `BUILD002` -- an extension names a source that does not exist. `setup.py`
  kept building `wreath._exp._reactor` from `src/wreath/_exp/` for some time
  after that directory was deleted, so the one flag that would have compiled it
  failed instead. A name without a file is a plan, not a build input.

**Absent is not stale.** An extension that was never built has no artifact to
compare, and reporting it would make the lint useless on a default install --
which is most of them. Only a *present* artifact can be behind its sources.

Both `sources` and `depends` count. A header change invalidates every object
that includes it just as surely as changing the `.c` does, and the reactor
extension lists `.c` files under `depends` because they are `#include`d rather
than compiled separately.

The source list is read out of `setup.py` rather than restated here, so it
cannot drift from what is actually built. `setup.py` is read as text, not
imported: it runs `pkg-config` for the optional HTTP/3 backend at import time,
and a lint that needed QUIC libraries installed in order to check a file date
would not run.

Run it with `uv run wreath-build-lint`; `0` means clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .native_lint import run_root_lint

#: Every extension `setup.py` defines, named as a dotted module string.
_EXTENSION_RE = re.compile(r'"(wreath\._(?:native|exp)\._[a-z0-9_]+)"')

#: A quoted repository path ending in `.c` or `.h`, as the build lists them.
_BUILD_INPUT_RE = re.compile(r'"(src/[A-Za-z0-9_/]+\.[ch])"')


@dataclass(frozen=True)
class Finding:
    code: str
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.code} {self.message}"


def _named_list(text: str, keyword: str, start: int, end: int) -> str:
    """The text of the `<keyword>=[...]` list within `text[start:end]`."""
    marker = text.find(f"{keyword}=", start)
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


def extension_inputs(text: str) -> dict[str, list[str]]:
    """Map each extension to every repository file its build consumes.

    `sources` and `depends` together: a stale object is stale whether the file
    that changed was compiled or included.
    """
    inputs: dict[str, list[str]] = {}
    matches = list(_EXTENSION_RE.finditer(text))
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        listed: list[str] = []
        for keyword in ("sources", "depends"):
            listed += _BUILD_INPUT_RE.findall(_named_list(text, keyword, start, end))
        inputs.setdefault(match.group(1), []).extend(listed)
    return inputs


def _artifacts(root: Path, extension: str) -> list[Path]:
    """Every built `.so`/`.pyd` for a dotted extension name.

    The suffix carries the interpreter version and platform, so it is matched
    by glob rather than reconstructed -- an artifact built by a different
    interpreter is still an artifact, and still stale.

    **All of them, not the first.** A tree accumulates one artifact per
    interpreter that has built it, and free-threading is a separately swept
    execution mode rather than a variant of the default one. Checking a single
    match reported a clean build over a `cpython-314t` `_core` three days behind
    its sources, because `sorted()` puts `cpython-314` first -- a stale artifact
    that is importable, does not contain those sources, and had a passing lint
    in front of it. That is this module's own failure mode, one ABI tag over.
    """
    *package, module = extension.split(".")
    directory = root.joinpath("src", *package)
    return sorted(directory.glob(f"{module}.*.so")) + sorted(directory.glob(f"{module}.*.pyd"))


def _describe(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 60:.0f} minutes"


def scan(root: Path) -> list[Finding]:
    setup = root / "setup.py"
    if not setup.exists():
        return []
    findings: list[Finding] = []
    for extension, listed in sorted(extension_inputs(setup.read_text(encoding="utf-8")).items()):
        missing = [name for name in listed if not (root / name).exists()]
        if missing:
            findings.append(
                Finding(
                    "BUILD002",
                    "setup.py",
                    f"{extension} names {len(missing)} source(s)"
                    f" that do not exist, starting with {missing[0]!r}; the build flag"
                    " that would compile it fails instead",
                )
            )
            continue
        # Not built is not stale, so an extension with no artifact contributes
        # nothing; one with several contributes one finding per stale artifact,
        # because rebuilding the default interpreter's does not touch the others.
        for artifact in _artifacts(root, extension):
            built = artifact.stat().st_mtime
            newer = [
                (name, (root / name).stat().st_mtime)
                for name in listed
                if (root / name).stat().st_mtime > built
            ]
            if not newer:
                continue
            name, changed = max(newer, key=lambda pair: pair[1])
            findings.append(
                Finding(
                    "BUILD001",
                    artifact.relative_to(root).as_posix(),
                    f"built {_describe(changed - built)} before {name}"
                    f" ({len(newer)} source(s) newer than the artifact); it is importable"
                    " and does not contain them -- rebuild, and prove the rebuild landed"
                    " with a sentinel",
                )
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    return run_root_lint(
        argv,
        prog="wreath-build-lint",
        description="Report compiled extensions older than the sources they are built from.",
        scan=scan,
    )


if __name__ == "__main__":
    raise SystemExit(main())
