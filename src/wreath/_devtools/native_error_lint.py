"""Scan Wreath's C for CPython exception-protocol mistakes."""

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

#: `iter_sources` and `repo_root` are re-exported rather than merely used.
#: They were part of this module's surface before `main` moved to
#: `run_lint`, and callers import them from
#: here; dropping them because the module body stopped needing them would be a
#: silent narrowing.
__all__ = ["DEFAULT_ROOTS", "WAIVER", "Rule", "iter_sources", "main", "repo_root", "scan_text"]

DEFAULT_ROOTS = ("src/wreath/_native",)
WAIVER = re.compile(r"native-error-lint:\s*allow\s+(?P<code>NE\d{3})\s*--\s*(?P<reason>\S.*)")


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


def _function_contexts(code_lines: list[str]) -> tuple[list[str], list[bool]]:
    """Resolve enclosing function and return protocol once per source line.

    ``_enclosing_function`` deliberately searches backwards, which is useful
    for an isolated finding but quadratic when called for every line in a large
    translation unit. A forward pass sees the same declaration transitions and
    carries their context until the next one.
    """
    functions: list[str] = []
    pyobject_results: list[bool] = []
    current = ""
    returns_pyobject = False
    for index, line in enumerate(code_lines):
        declared = _enclosing_function([line], 0)
        if declared:
            current = declared
            returns_pyobject = _returns_pyobject(code_lines, index, current)
        functions.append(current)
        pyobject_results.append(returns_pyobject)
    return functions, pyobject_results


def scan_text(path: str, text: str) -> list[Finding]:
    raw_lines = text.split("\n")
    code_lines = strip_c(text)
    waived = _waivers(raw_lines, code_lines, WAIVER)
    functions, pyobject_results = _function_contexts(code_lines)
    findings: list[Finding] = []
    previous_code = ""

    def add(index: int, code: str) -> None:
        if code in waived.get(index + 1, ()):
            return
        rule = RULES[code]
        findings.append(Finding(path, index + 1, code, rule.summary, rule.hint))

    for index, line in enumerate(code_lines):
        function = functions[index]

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
            for later_index in range(index + 1, min(len(code_lines), index + 9)):
                if functions[later_index] != function:
                    break
                later = code_lines[later_index]
                if "PyErr_Occurred" in later:
                    checked = True
                    break
                if re.search(rf"\b{re.escape(conversion.group('var'))}\b", later):
                    break
            if not checked:
                add(index, "NE005")

        pyobject_result = pyobject_results[index]
        if pyobject_result and NULL_RETURN.match(line) and ERROR_CLEAR.search(previous_code):
            add(index, "NE003")
        if pyobject_result and SUCCESS_RETURN.match(line) and ERROR_SET.search(previous_code):
            add(index, "NE004")
        if line.strip():
            previous_code = line

    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.code))


def main(argv: list[str] | None = None) -> int:
    return run_lint(
        argv,
        prog="wreath-native-error-lint",
        description="Find CPython exception and return-value protocol mistakes in Wreath's C.",
        rules=RULES,
        scan=scan_text,
        default_roots=DEFAULT_ROOTS,
    )


if __name__ == "__main__":
    raise SystemExit(main())
