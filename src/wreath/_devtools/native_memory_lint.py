"""Heuristically scan Wreath's C for reference and pointer lifetime hazards.

This tool is separate from complexity and boundary-cost linting. It catches a
small set of high-confidence ownership mistakes before the required sanitizer
run; it does not replace ASan, UBSan, or leak testing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .native_lint import Finding, _enclosing_function, iter_sources, repo_root, strip_c

DEFAULT_ROOTS = ("src/wreath/_native",)
WAIVER = re.compile(r"native-memory-lint:\s*allow\s+(?P<code>NM\d{3})\s*--\s*(?P<reason>\S.*)")


@dataclass(frozen=True)
class Rule:
    code: str
    summary: str
    hint: str


RULES: dict[str, Rule] = {
    "NM001": Rule(
        "NM001",
        "possible double release of the same pointer lifetime",
        "Make ownership explicit and release exactly once. Assign a newly owned value or NULL "
        "after release; if independent references are intentional, document a waiver.",
    ),
    "NM002": Rule(
        "NM002",
        "possible use after Py_DECREF/free",
        "Do not dereference or pass a pointer after releasing its last owned reference. Move the "
        "release after the use, or retain an owned reference explicitly.",
    ),
    "NM003": Rule(
        "NM003",
        "released pointer returned to the caller",
        "Return before releasing, transfer ownership without releasing, or return a fresh owned "
        "pointer. A freed/decref'd pointer is never a valid result.",
    ),
    "NM004": Rule(
        "NM004",
        "borrowed reference decref'd without first acquiring ownership",
        "PyDict_GetItem, PyList_GetItem, PyTuple_GetItem, and PyWeakref_GetObject return borrowed "
        "references. Use Py_NewRef/Py_INCREF before taking ownership.",
    ),
    "NM005": Rule(
        "NM005",
        "realloc result overwrites the only pointer",
        "Store realloc into a temporary and replace the original only after success; otherwise an "
        "allocation failure leaks the original block.",
    ),
}

RELEASE = re.compile(
    r"\b(?P<api>Py_DECREF|Py_XDECREF|PyMem_Free|PyObject_Free|PyMem_RawFree|free)"
    r"\s*\(\s*(?P<var>[A-Za-z_]\w*)\s*\)"
)
ASSIGNMENT = re.compile(r"(?:^|[;{}])\s*(?:[A-Za-z_]\w*[\s*]+)?(?P<var>[A-Za-z_]\w*)\s*=")
BORROWED_ASSIGNMENT = re.compile(
    r"(?:^|[;{}])\s*(?:PyObject\s*\*\s*)?(?P<var>[A-Za-z_]\w*)\s*=\s*"
    r"(?:PyDict_GetItem(?:String|WithError)?|PyList_GetItem|PyTuple_GetItem|"
    r"PyWeakref_GetObject)\s*\("
)
INCREF = re.compile(r"\b(?:Py_INCREF|Py_XINCREF)\s*\(\s*(?P<var>[A-Za-z_]\w*)\s*\)")
NEWREF_ASSIGNMENT = re.compile(
    r"(?:^|[;{}])\s*(?:PyObject\s*\*\s*)?(?P<var>[A-Za-z_]\w*)\s*=\s*"
    r"Py_(?:NewRef|XNewRef)\s*\("
)
REALLOC_OVERWRITE = re.compile(
    r"\b(?P<var>[A-Za-z_]\w*)\s*=\s*(?:PyMem_Realloc|PyMem_RawRealloc|realloc)"
    r"\s*\(\s*(?P=var)\s*,"
)
CONTROL_BOUNDARY = re.compile(r"^\s*(?:if|else|for|while|switch|case|default)\b")
LABEL = re.compile(r"^\s*[A-Za-z_]\w*\s*:\s*$")
FLOW_TERMINATOR = re.compile(r"^\s*(?:return|goto|break|continue)\b")


def _waivers(raw_lines: list[str], code_lines: list[str]) -> dict[int, set[str]]:
    found: dict[int, set[str]] = {}
    for number, line in enumerate(raw_lines, 1):
        match = WAIVER.search(line)
        if not match:
            continue
        code = match.group("code")
        found.setdefault(number, set()).add(code)
        for index in range(number, len(code_lines)):
            if code_lines[index].strip():
                found.setdefault(index + 1, set()).add(code)
                break
    return found


def _strong_use(line: str, variable: str) -> bool:
    escaped = re.escape(variable)
    if re.match(rf"^\s*if\s*\(\s*{escaped}\s*[!=]=\s*NULL\s*\)", line):
        return False
    if re.search(rf"\b{escaped}\s*->|\*\s*{escaped}\b", line):
        return True
    call = re.search(rf"\b[A-Za-z_]\w*\s*\([^;]*\b{escaped}\b[^;]*\)", line)
    return call is not None


def scan_text(path: str, text: str) -> list[Finding]:
    raw_lines = text.split("\n")
    code_lines = strip_c(text)
    waived = _waivers(raw_lines, code_lines)
    findings: list[Finding] = []
    released: dict[str, int] = {}
    borrowed: dict[str, int] = {}
    current_function = ""

    def add(index: int, code: str, message: str | None = None) -> None:
        if code in waived.get(index + 1, ()):
            return
        rule = RULES[code]
        findings.append(Finding(path, index + 1, code, message or rule.summary, rule.hint))

    for index, line in enumerate(code_lines):
        function = _enclosing_function(code_lines, index)
        if function != current_function or LABEL.match(line):
            released.clear()
            borrowed.clear()
            current_function = function

        realloc = REALLOC_OVERWRITE.search(line)
        if realloc:
            add(index, "NM005", f"realloc overwrites {realloc.group('var')!r} before NULL check")

        borrowed_match = BORROWED_ASSIGNMENT.search(line)
        if borrowed_match:
            variable = borrowed_match.group("var")
            borrowed[variable] = index
            released.pop(variable, None)

        newref = NEWREF_ASSIGNMENT.search(line)
        if newref:
            borrowed.pop(newref.group("var"), None)
        for incref in INCREF.finditer(line):
            borrowed.pop(incref.group("var"), None)

        assignment = ASSIGNMENT.search(line)
        if assignment and not borrowed_match:
            variable = assignment.group("var")
            released.pop(variable, None)
            if not newref or newref.group("var") != variable:
                borrowed.pop(variable, None)

        releases = list(RELEASE.finditer(line))
        for release in releases:
            variable = release.group("var")
            if variable in released:
                add(
                    index,
                    "NM001",
                    f"{variable!r} released again after line {released[variable] + 1}",
                )
            if variable in borrowed:
                add(
                    index,
                    "NM004",
                    f"borrowed reference {variable!r} released without INCREF/Py_NewRef",
                )
            released[variable] = index
            borrowed.pop(variable, None)

        for variable, released_at in list(released.items()):
            if released_at == index:
                continue
            if re.search(rf"\breturn\s+{re.escape(variable)}\s*;", line):
                add(index, "NM003", f"{variable!r} returned after release")
                released.pop(variable, None)
            elif _strong_use(line, variable):
                add(
                    index,
                    "NM002",
                    f"{variable!r} used after release on line {released_at + 1}",
                )
                released.pop(variable, None)

        # The scanner deliberately reasons only within straight-line basic blocks.
        # Branch-sensitive lifetime analysis belongs to the compiler/sanitizers.
        if (
            CONTROL_BOUNDARY.match(line)
            or FLOW_TERMINATOR.match(line)
            or line.strip() in {"}", "};"}
        ):
            released.clear()
            borrowed.clear()

    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.code))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-native-memory-lint",
        description="Find likely double releases, use-after-free, and reference ownership bugs.",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, help="files or directories (default: src/wreath/_native)"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--list-rules", action="store_true")
    args = parser.parse_args(argv)

    if args.list_rules:
        for rule in RULES.values():
            print(f"{rule.code}  {rule.summary}\n    {rule.hint}\n")
        return 0

    roots = args.paths or [repo_root() / root for root in DEFAULT_ROOTS]
    sources = iter_sources([Path(root) for root in roots])
    if not sources:
        print(f"wreath-native-memory-lint: no C sources found in {roots}", file=sys.stderr)
        return 1

    root = repo_root()
    findings: list[Finding] = []
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"wreath-native-memory-lint: cannot read {source}: {exc}", file=sys.stderr)
            return 1
        try:
            display = str(source.relative_to(root))
        except ValueError:
            display = str(source)
        findings.extend(scan_text(display, text))

    if args.format == "json":
        print(
            json.dumps(
                {
                    "scanned": len(sources),
                    "count": len(findings),
                    "findings": [finding.__dict__ for finding in findings],
                },
                indent=2,
            )
        )
    else:
        for finding in findings:
            print(finding.render())
        print(
            f"\nwreath-native-memory-lint: {len(findings)} finding(s) across "
            f"{len(sources)} file(s)."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
