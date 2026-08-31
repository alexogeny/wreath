"""Find native code that treats Python containers as an execution format.

This scanner deliberately ignores ordinary CPython API traffic. Constructing a
public result, invoking a declared Python callback, and adapting one value at a
module boundary are all legitimate seams; counting those operations produced a
large list with no decision behind it. The rules here identify two structural
defects instead: recursive boxed opcode interpreters, and execution functions
that rebuild a native plan from Python declarations on every call.
"""

from __future__ import annotations

import re

from .native_lint import (
    Finding,
    Rule,
    _source_tape,
    _waivers,
    iter_sources,
    lint_entrypoint,
    repo_root,
    waiver_pattern,
)

__all__ = ["DEFAULT_ROOTS", "WAIVER", "Rule", "iter_sources", "main", "repo_root", "scan_text"]

DEFAULT_ROOTS = ("src/wreath/_native",)
WAIVER = waiver_pattern("native-boundary-lint", "NB")

RULES: dict[str, Rule] = {
    "NB001": Rule(
        "NB001",
        "recursive executor decodes opcodes from Python tuples",
        "Compile the declaration once into an operation-owned native node tree and execute "
        "typed fields instead of re-reading boxed opcodes and child tuples.",
    ),
    "NB002": Rule(
        "NB002",
        "execution path reconstructs a native plan from Python declarations",
        "Move plan construction to startup or the owning shape cache and pass an owned native "
        "plan into the per-operation executor.",
    ),
}

BOXED_OPCODE = re.compile(
    r"\b(?:op|opcode)\s*=\s*(?:PyLong_As\w*|[A-Za-z_]\w*program_int)\s*\(\s*"
    r"PyTuple_GET_ITEM\s*\("
)
TUPLE_READ = re.compile(r"\bPyTuple_GET_ITEM\s*\(")
SWITCH = re.compile(r"\bswitch\s*\(")
PLAN_BUILD_CALL = re.compile(
    r"\b([A-Za-z_]\w*(?:plan|plans)[A-Za-z_\d]*_(?:init|compile)|"
    r"[A-Za-z_]\w*_(?:plan|plans)_(?:init|compile))\s*\("
)
EXECUTOR_NAME = re.compile(
    r"(?:assemble|authorize|decode|dispatch|emit|encode|evaluate|execute|filter|"
    r"hydrate|interpret|match|parse|project|render|route|run|validate)"
)


def _builder(function: str) -> bool:
    return (
        "compile" in function
        or function.endswith(("_init", "_ready", "_configure"))
        or function.startswith(("build_", "create_"))
    )


def _only_called_by_builders(
    function: str, by_function: dict[str, list[int]], code_lines: list[str]
) -> bool:
    call = re.compile(rf"\b{re.escape(function)}\s*\(")
    callers = {
        caller
        for caller, indexes in by_function.items()
        if caller != function and any(call.search(code_lines[index]) for index in indexes)
    }
    return bool(callers) and all(_builder(caller) for caller in callers)


def scan_text(path: str, text: str) -> list[Finding]:
    raw_lines = text.split("\n")
    code_lines, _, functions = _source_tape(text)
    waived = _waivers(raw_lines, code_lines, WAIVER)
    by_function: dict[str, list[int]] = {}
    for index, function in enumerate(functions):
        if function and function != "<global>":
            by_function.setdefault(function, []).append(index)

    findings: list[Finding] = []
    for function, indexes in by_function.items():
        body = "\n".join(code_lines[index] for index in indexes)

        recursive_calls = len(re.findall(rf"\b{re.escape(function)}\s*\(", body))
        if (
            not _builder(function)
            and SWITCH.search(body)
            and BOXED_OPCODE.search(body)
            and len(TUPLE_READ.findall(body)) >= 2
            and (recursive_calls >= 2 or EXECUTOR_NAME.search(function))
            and not _only_called_by_builders(function, by_function, code_lines)
        ):
            anchor = next(index for index in indexes if BOXED_OPCODE.search(code_lines[index]))
            if "NB001" not in waived.get(anchor + 1, ()):
                rule = RULES["NB001"]
                findings.append(Finding(path, anchor + 1, rule.code, rule.summary, rule.hint))

        if not _builder(function) and EXECUTOR_NAME.search(function):
            calls: list[tuple[int, str]] = []
            for index in indexes:
                calls.extend(
                    (index, match.group(1))
                    for match in PLAN_BUILD_CALL.finditer(code_lines[index])
                    if match.group(1) != function
                )
            if calls:
                anchor = calls[0][0]
                if "NB002" not in waived.get(anchor + 1, ()):
                    rule = RULES["NB002"]
                    findings.append(Finding(path, anchor + 1, rule.code, rule.summary, rule.hint))

    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.code))


main = lint_entrypoint(
    prog="wreath-native-boundary-lint",
    description="Find boxed execution plans and per-operation native plan rebuilding.",
    rules=RULES,
    scan=scan_text,
    default_roots=DEFAULT_ROOTS,
)


if __name__ == "__main__":
    raise SystemExit(main())
