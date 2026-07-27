"""Find excessive CPython object traffic in Wreath's native hot paths.

This complements ``wreath-native-lint`` (algorithmic complexity) and
``wreath-native-profile`` (measured runtime attribution). It looks specifically for
static signs of avoidable Python/native-boundary work:

    uv run wreath-native-boundary-lint
    uv run wreath-native-boundary-lint --format json

The rules are intentionally heuristic. Waive an intentional finding in place
with ``native-boundary-lint: allow NB001 -- reason``.
"""

from __future__ import annotations

import re

from .native_lint import (
    Finding,
    Rule,
    _enclosing_function,
    _is_init_function,
    _loop_depth_map,
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
WAIVER = re.compile(
    r"native-boundary-lint:\s*allow\s+(?P<code>NB\d{3})\s*--\s*(?P<reason>\S.*)"
)


RULES: dict[str, Rule] = {
    "NB001": Rule(
        "NB001",
        "Python object operation inside a native loop",
        "Keep the loop in C over borrowed/native data where possible. Batch the conversion, "
        "hoist stable lookups, or cross into Python once after the loop.",
    ),
    "NB002": Rule(
        "NB002",
        "temporary argument containers built for a generic Python call",
        "Avoid PyTuple_Pack/Py_BuildValue plus PyObject_Call on the hot path. Prefer a direct "
        "C helper, vectorcall, PyObject_CallOneArg, or cached empty arguments as appropriate.",
    ),
    "NB003": Rule(
        "NB003",
        "repeated dynamic Python lookups in one native function",
        "Cache stable names/callables at startup or pass resolved values explicitly. Repeated "
        "attribute and dictionary lookups erase much of the native boundary's benefit.",
    ),
    "NB004": Rule(
        "NB004",
        "high aggregate Python-boundary pressure in one native function",
        "Split cold object adaptation from the hot native operation. Reduce temporary objects, "
        "generic calls, dynamic lookups, and repeated C-to-Python conversions, then profile.",
    ),
}

LOOP_OBJECT_API = re.compile(
    r"\b(?:PyObject_(?:Call|GetAttr)|PyObject_CallMethod|PyDict_(?:GetItem|SetItem)|"
    r"PyTuple_(?:New|Pack)|PyList_(?:New|Append)|PyLong_(?:As|From)|"
    r"PyUnicode_(?:As|From)|PyBytes_(?:As|From)|Py_BuildValue)\w*\s*\("
)
ARG_CONTAINER = re.compile(r"\b(?:PyTuple_(?:New|Pack)|PyDict_New|Py_BuildValue)\s*\(")
GENERIC_CALL = re.compile(r"\b(?:PyObject_Call|PyObject_CallObject)\s*\(")
DYNAMIC_LOOKUP = re.compile(
    r"\b(?:PyObject_GetAttr(?:String)?|PyDict_GetItem(?:String|WithError)?|"
    r"PyObject_CallMethod\w*)\s*\("
)

WEIGHTED_APIS: tuple[tuple[re.Pattern[str], int], ...] = (
    (
        re.compile(
            r"\b(?:PyLong_From\w*|PyUnicode_From\w*|PyBytes_From\w*|PyTuple_(?:New|Pack)|"
            r"PyList_New|PyDict_New|Py_BuildValue)\s*\("
        ),
        2,
    ),
    (re.compile(r"\bPyObject_(?:Call\w*|GetAttr\w*)\s*\("), 3),
    (re.compile(r"\bPyObject_CallMethod\w*\s*\("), 3),
    (
        re.compile(
            r"\b(?:PyLong_As\w*|PyUnicode_As\w*|PyBytes_As\w*|"
            r"PyDict_GetItem\w*|PyObject_IsTrue)\s*\("
        ),
        1,
    ),
)
BOUNDARY_SCORE_LIMIT = 20


def scan_text(path: str, text: str) -> list[Finding]:
    raw_lines = text.split("\n")
    code_lines = strip_c(text)
    loop_depth = _loop_depth_map(code_lines)
    waived = _waivers(raw_lines, code_lines, WAIVER)
    findings: list[Finding] = []
    by_function: dict[str, list[int]] = {}
    loop_object_lines: dict[str, list[int]] = {}

    for index, line in enumerate(code_lines):
        function = _enclosing_function(code_lines, index)
        if function and function != "<global>":
            by_function.setdefault(function, []).append(index)
            if loop_depth[index] and LOOP_OBJECT_API.search(line):
                loop_object_lines.setdefault(function, []).append(index)

    for function, indexes in by_function.items():
        expensive_loop_lines = [
            index
            for index in loop_object_lines.get(function, ())
            if "NB001" not in waived.get(index + 1, ())
        ]
        if len(expensive_loop_lines) >= 2:
            rule = RULES["NB001"]
            findings.append(
                Finding(
                    path,
                    expensive_loop_lines[0] + 1,
                    rule.code,
                    f"{rule.summary} ({len(expensive_loop_lines)} operation(s) in {function})",
                    rule.hint,
                )
            )

        lines = [code_lines[index] for index in indexes]
        container_lines = [index for index in indexes if ARG_CONTAINER.search(code_lines[index])]
        call_lines = [index for index in indexes if GENERIC_CALL.search(code_lines[index])]
        if container_lines and call_lines:
            anchor = call_lines[0]
            if "NB002" not in waived.get(anchor + 1, ()):
                rule = RULES["NB002"]
                findings.append(Finding(path, anchor + 1, rule.code, rule.summary, rule.hint))

        lookup_lines = [index for index in indexes if DYNAMIC_LOOKUP.search(code_lines[index])]
        # Module setup resolves its names once for the life of the process, so
        # repeated lookups there are the fix, not the problem. NB004 already
        # excludes these; NB003 was reporting them.
        if len(lookup_lines) >= 3 and not _is_init_function(function):
            anchor = lookup_lines[2]
            if "NB003" not in waived.get(anchor + 1, ()):
                rule = RULES["NB003"]
                findings.append(
                    Finding(
                        path,
                        anchor + 1,
                        rule.code,
                        f"{rule.summary} ({len(lookup_lines)} in {function})",
                        rule.hint,
                    )
                )

        score = sum(
            weight * len(pattern.findall(line))
            for line in lines
            for pattern, weight in WEIGHTED_APIS
        )
        if score >= BOUNDARY_SCORE_LIMIT and not _is_init_function(function):
            anchor = indexes[0]
            if "NB004" not in waived.get(anchor + 1, ()):
                rule = RULES["NB004"]
                findings.append(
                    Finding(
                        path,
                        anchor + 1,
                        rule.code,
                        f"{rule.summary} (score {score} in {function}, "
                        f"limit {BOUNDARY_SCORE_LIMIT})",
                        rule.hint,
                    )
                )

    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.code))


def main(argv: list[str] | None = None) -> int:
    return run_lint(
        argv,
        prog="wreath-native-boundary-lint",
        description="Find excessive Python object and boundary traffic in Wreath's C.",
        rules=RULES,
        scan=scan_text,
        default_roots=DEFAULT_ROOTS,
    )


if __name__ == "__main__":
    raise SystemExit(main())
