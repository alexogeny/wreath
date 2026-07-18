"""Scan Wreath's C for CPython exception-protocol mistakes."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .native_lint import Finding, _enclosing_function, iter_sources, repo_root, strip_c

DEFAULT_ROOTS = ("src/wreath/_native",)
WAIVER = re.compile(r"native-error-lint:\s*allow\s+(?P<code>NE\d{3})\s*--\s*(?P<reason>\S.*)")


@dataclass(frozen=True)
class Rule:
    code: str
    summary: str
    hint: str


RULES: dict[str, Rule] = {
    "NE001": Rule(
        "NE001",
        "integer-status Python API result is ignored",
        "Check for < 0 and propagate the exception. Ignoring mutation/copy status can return "
        "success with an exception set or continue with partial state.",
    ),
    "NE002": Rule(
        "NE002",
        "new-reference Python call result is discarded",
        "Store and check the result, then DECREF it. Discarding it both leaks the result and "
        "ignores any exception raised by the call.",
    ),
    "NE003": Rule(
        "NE003",
        "NULL returned immediately after clearing the exception",
        "A PyObject-returning function must set an exception before returning NULL, unless its "
        "documented C-API contract explicitly uses NULL without an error.",
    ),
    "NE004": Rule(
        "NE004",
        "successful result returned while an exception is set",
        "Clear or propagate the exception; CPython requires a non-NULL result to have no active "
        "exception indicator.",
    ),
    "NE005": Rule(
        "NE005",
        "numeric conversion sentinel is not checked with PyErr_Occurred",
        "PyLong_As* and PyFloat_AsDouble use an ordinary numeric value as their error sentinel. "
        "Check the sentinel together with PyErr_Occurred before using the result.",
    ),
    "NE006": Rule(
        "NE006",
        "PyObject_IsTrue error is treated as true",
        "Store the result and handle -1 before testing truth. Using it directly as a C condition "
        "turns an exception into the true branch.",
    ),
}

IGNORED_STATUS = re.compile(
    r"^\s*(?:PyDict_(?:SetItem|DelItem)(?:String)?|PyList_(?:Append|SetItem|Insert)|"
    r"PySet_(?:Add|Discard)|PyObject_(?:SetAttr|DelAttr)(?:String)?|"
    r"PyMapping_(?:SetItemString|DelItemString)|PySequence_(?:SetItem|DelItem))\s*\(.*\)\s*;\s*$"
)
DISCARDED_CALL = re.compile(
    r"^\s*(?:PyObject_Call\w*|PyObject_Vectorcall\w*|PyObject_CallMethod\w*)\s*\(.*\)\s*;\s*$"
)
CONVERSION = re.compile(
    r"(?:^|[;{}])\s*(?:unsigned\s+)?(?:long(?:\s+long)?|int|Py_ssize_t|double)\s+"
    r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?:PyLong_As\w*|PyFloat_AsDouble)\s*\("
)
ERROR_SET = re.compile(r"\b(?:PyErr_Set\w*|PyErr_Format|PyErr_NoMemory)\s*\(")
ERROR_CLEAR = re.compile(r"\bPyErr_Clear\s*\(")
SUCCESS_RETURN = re.compile(
    r"^\s*(?:Py_RETURN_\w+|return\s+(?!NULL\b)"
    r"(?:Py_None|Py_True|Py_False|[A-Za-z_]\w*))"
)
NULL_RETURN = re.compile(r"^\s*return\s+NULL\s*;")
TRUTH_DIRECT = re.compile(r"\bif\s*\(\s*!?\s*PyObject_IsTrue\s*\(")


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


def _returns_pyobject(code_lines: list[str], index: int, function: str) -> bool:
    declaration = re.compile(rf"\b{re.escape(function)}\s*\(")
    for position in range(index, max(-1, index - 400), -1):
        if declaration.search(code_lines[position]):
            start = max(0, position - 4)
            signature = " ".join(code_lines[start : position + 1])
            return re.search(
                rf"PyObject\s*\*\s*{re.escape(function)}\s*\(", signature
            ) is not None
    return False


def scan_text(path: str, text: str) -> list[Finding]:
    raw_lines = text.split("\n")
    code_lines = strip_c(text)
    waived = _waivers(raw_lines, code_lines)
    findings: list[Finding] = []
    previous_code = ""

    def add(index: int, code: str) -> None:
        if code in waived.get(index + 1, ()):
            return
        rule = RULES[code]
        findings.append(Finding(path, index + 1, code, rule.summary, rule.hint))

    for index, line in enumerate(code_lines):
        function = _enclosing_function(code_lines, index)

        if IGNORED_STATUS.match(line):
            add(index, "NE001")
        if DISCARDED_CALL.match(line) and not previous_code.rstrip().endswith(
            ("=", ":", "?", ",", "(")
        ):
            add(index, "NE002")
        if TRUTH_DIRECT.search(line):
            add(index, "NE006")

        conversion = CONVERSION.search(line)
        if conversion:
            checked = False
            for later in code_lines[index + 1 : index + 9]:
                if _enclosing_function(code_lines, index + 1) != function:
                    break
                if "PyErr_Occurred" in later:
                    checked = True
                    break
                if re.search(rf"\b{re.escape(conversion.group('var'))}\b", later):
                    break
            if not checked:
                add(index, "NE005")

        pyobject_result = _returns_pyobject(code_lines, index, function)
        if pyobject_result and NULL_RETURN.match(line) and ERROR_CLEAR.search(previous_code):
            add(index, "NE003")
        if pyobject_result and SUCCESS_RETURN.match(line) and ERROR_SET.search(previous_code):
            add(index, "NE004")
        if line.strip():
            previous_code = line

    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.code))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-native-error-lint",
        description="Find CPython exception and return-value protocol mistakes in Wreath's C.",
    )
    parser.add_argument("paths", nargs="*", type=Path)
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
        print(f"wreath-native-error-lint: no C sources found in {roots}", file=sys.stderr)
        return 1
    root = repo_root()
    findings: list[Finding] = []
    for source in sources:
        text = source.read_text(encoding="utf-8", errors="replace")
        try:
            display = str(source.relative_to(root))
        except ValueError:
            display = str(source)
        findings.extend(scan_text(display, text))

    payload = {
        "scanned": len(sources),
        "count": len(findings),
        "findings": [finding.__dict__ for finding in findings],
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        print(
            f"\nwreath-native-error-lint: {len(findings)} finding(s) "
            f"across {len(sources)} file(s)."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
