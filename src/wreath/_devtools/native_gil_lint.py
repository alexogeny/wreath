"""Scan Wreath's C for unsafe or unnecessarily expensive GIL behavior."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .native_lint import Finding, _enclosing_function, iter_sources, repo_root, strip_c

DEFAULT_ROOTS = ("src/wreath/_native",)
WAIVER = re.compile(r"native-gil-lint:\s*allow\s+(?P<code>NG\d{3})\s*--\s*(?P<reason>\S.*)")


@dataclass(frozen=True)
class Rule:
    code: str
    summary: str
    hint: str


RULES: dict[str, Rule] = {
    "NG001": Rule(
        "NG001",
        "Python C API called while the GIL is released",
        "Move the Python API operation outside Py_BEGIN_ALLOW_THREADS/Py_END_ALLOW_THREADS, "
        "or reacquire the GIL around the operation.",
    ),
    "NG002": Rule(
        "NG002",
        "potentially blocking native I/O while holding the GIL",
        "Release the GIL around genuinely blocking I/O. If the descriptor is guaranteed "
        "nonblocking, document that invariant with a reasoned waiver.",
    ),
    "NG003": Rule(
        "NG003",
        "borrowed Python reference crosses a GIL-release region",
        "Acquire ownership with Py_NewRef/Py_INCREF before releasing the GIL; another thread can "
        "otherwise invalidate the borrowed pointer.",
    ),
    "NG004": Rule(
        "NG004",
        "PyGILState_Ensure/Release calls are unbalanced",
        "Pair every successful PyGILState_Ensure with PyGILState_Release in the same callback and "
        "on every exit path.",
    ),
    "NG005": Rule(
        "NG005",
        "pthread callback uses Python without acquiring the GIL",
        "Native thread entry points must call PyGILState_Ensure before any Python C API and pair "
        "it with PyGILState_Release before returning.",
    ),
}

# Match the public/private Python object and allocator APIs that require an
# attached thread.  PyMem_Raw* is deliberately excluded: the raw allocator
# domain is the one Python allocator family permitted without an attached
# thread.  A narrow API allowlist previously missed PyMem_Malloc and macros such
# as PyBytes_AS_STRING, allowing unsafe released-region code to pass silently.
PYTHON_API = re.compile(
    r"\b_?Py(?!GILState_)(?!Mem_Raw)[A-Za-z_]\w*\s*\("
)
# The leading lookbehind excludes `x->write(...)` and `x.read(...)`. Those are
# calls through a struct member -- the metal transport's `capi->write`, a
# protocol's `.read` -- and a POSIX syscall is never spelled that way, so `\b`
# alone reported a whole class of function-pointer dispatch as blocking I/O.
# This narrows what matches, not what the rule means: a bare `write(fd, ...)`
# still reports.
BLOCKING_IO = re.compile(
    r"(?<![>.\w])(?:read|write|recv|recvfrom|recvmsg|send|sendto|sendmsg|connect|accept|"
    r"accept4|poll|ppoll|select|pselect|getaddrinfo|fsync|fdatasync)\s*\("
)
BORROWED_ASSIGNMENT = re.compile(
    r"(?:PyObject\s*\*\s*)?(?P<var>[A-Za-z_]\w*)\s*=\s*"
    r"(?:PyDict_GetItem\w*|PyList_(?:GetItem|GET_ITEM)|"
    r"PyTuple_(?:GetItem|GET_ITEM)|PyWeakref_GetObject)\s*\("
)
INCREF = re.compile(r"\b(?:Py_INCREF|Py_XINCREF)\s*\(\s*(?P<var>[A-Za-z_]\w*)\s*\)")
PTHREAD_CREATE = re.compile(
    r"\bpthread_create\s*\([^,]+,[^,]+,\s*(?:\([^)]*\)\s*)?(?P<callback>[A-Za-z_]\w*)\s*,"
)


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


def _uses_variable(line: str, variable: str) -> bool:
    escaped = re.escape(variable)
    return bool(
        re.search(rf"\b{escaped}\s*->|\*\s*{escaped}\b", line)
        or re.search(rf"\bPy[A-Za-z_]\w*\s*\([^;]*\b{escaped}\b", line)
    )


def scan_text(path: str, text: str) -> list[Finding]:
    raw_lines = text.split("\n")
    code_lines = strip_c(text)
    waived = _waivers(raw_lines, code_lines)
    findings: list[Finding] = []
    callbacks = {
        match.group("callback")
        for line in code_lines
        if (match := PTHREAD_CREATE.search(line)) is not None
    }
    function_lines: dict[str, list[int]] = {}
    allow_threads = False
    borrowed: set[str] = set()
    crossed: set[str] = set()
    current_function = ""

    def add(index: int, code: str, message: str | None = None) -> None:
        if code in waived.get(index + 1, ()):
            return
        rule = RULES[code]
        findings.append(Finding(path, index + 1, code, message or rule.summary, rule.hint))

    for index, line in enumerate(code_lines):
        function = _enclosing_function(code_lines, index)
        if function != current_function:
            allow_threads = False
            borrowed.clear()
            crossed.clear()
            current_function = function
        if function:
            function_lines.setdefault(function, []).append(index)

        borrowed_match = BORROWED_ASSIGNMENT.search(line)
        if borrowed_match:
            borrowed.add(borrowed_match.group("var"))
        for incref in INCREF.finditer(line):
            borrowed.discard(incref.group("var"))

        if "Py_BEGIN_ALLOW_THREADS" in line:
            allow_threads = True
            crossed = set(borrowed)
            continue
        if "Py_END_ALLOW_THREADS" in line:
            allow_threads = False
            continue

        if allow_threads and PYTHON_API.search(line):
            add(index, "NG001")
        if not allow_threads and BLOCKING_IO.search(line):
            add(index, "NG002")
        if not allow_threads:
            for variable in list(crossed):
                if _uses_variable(line, variable):
                    add(index, "NG003", f"borrowed reference {variable!r} used after GIL release")
                    crossed.remove(variable)

    for function, indexes in function_lines.items():
        ensure_lines = [index for index in indexes if "PyGILState_Ensure" in code_lines[index]]
        release_lines = [index for index in indexes if "PyGILState_Release" in code_lines[index]]
        if len(ensure_lines) != len(release_lines):
            anchor = ensure_lines[0] if ensure_lines else release_lines[0]
            add(
                anchor,
                "NG004",
                f"{function} has {len(ensure_lines)} Ensure and "
                f"{len(release_lines)} Release call(s)",
            )

        if function in callbacks and not ensure_lines:
            for index in indexes:
                if PYTHON_API.search(code_lines[index]):
                    add(index, "NG005", f"pthread callback {function} calls Python without the GIL")
                    break

    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.code))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-native-gil-lint",
        description="Find unsafe GIL release, blocking I/O, and native-thread callback patterns.",
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
        print(f"wreath-native-gil-lint: no C sources found in {roots}", file=sys.stderr)
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
            f"\nwreath-native-gil-lint: {len(findings)} finding(s) "
            f"across {len(sources)} file(s)."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
