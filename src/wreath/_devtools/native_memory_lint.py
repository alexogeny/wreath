"""Heuristically scan Wreath's C for reference and pointer lifetime hazards.

This tool is separate from complexity and boundary-cost linting. It catches a
small set of high-confidence ownership mistakes before the required sanitizer
run; it does not replace ASan, UBSan, or leak testing.
"""

from __future__ import annotations

import re

from .native_lint import (
    Finding,
    Rule,
    _enclosing_function,
    _waivers,
    iter_sources,
    repo_root,
    run_lint,
    strip_c,
)

#: ``iter_sources`` and ``repo_root`` are re-exported, not merely used -- see the
#: note in :mod:`~wreath._devtools.native_error_lint`.
__all__ = ["DEFAULT_ROOTS", "WAIVER", "Rule", "iter_sources", "main", "repo_root", "scan_text"]

DEFAULT_ROOTS = ("src/wreath/_native",)
WAIVER = re.compile(r"native-memory-lint:\s*allow\s+(?P<code>NM\d{3})\s*--\s*(?P<reason>\S.*)")


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
    waived = _waivers(raw_lines, code_lines, WAIVER)
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
    return run_lint(
        argv,
        prog="wreath-native-memory-lint",
        description="Find likely double releases, use-after-free, and reference ownership bugs.",
        rules=RULES,
        scan=scan_text,
        default_roots=DEFAULT_ROOTS,
    )


if __name__ == "__main__":
    raise SystemExit(main())
