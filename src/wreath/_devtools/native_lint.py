"""Scan Wreath's C for the complexity patterns that keep turning up in review.

Every rule here encodes a defect that was actually found and fixed in this
repository, not a hypothetical. The point is that the next one gets caught by a
command instead of by a benchmark six months later.

    uv run wreath-native-lint                 # scan the default source roots
    uv run wreath-native-lint --format json   # machine-readable
    uv run wreath-native-lint path/to/file.c  # scan specific files

Exit status is 1 when any finding is reported, 0 otherwise.

This is a heuristic text scanner, not a compiler. It reads C with comments and
string literals stripped, tracks brace depth to know what is inside a loop, and
deliberately prefers a few precise rules over broad ones: a linter that cries
wolf gets switched off. When a match is intentional, waive it in place with a
reason rather than loosening a rule:

    /* native-lint: allow NC001 -- bounded by the connection's slab count */

A bare `native-lint: allow` with no rule code is rejected: waivers must say what
they waive and why.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Scanned when no explicit paths are given, relative to the repository root.
DEFAULT_ROOTS = ("src/wreath/_native",)

WAIVER = re.compile(r"native-lint:\s*allow\s+(?P<code>NC\d{3})\s*--\s*(?P<reason>\S.*)")
WAIVER_BARE = re.compile(r"native-lint:\s*allow(?!\s+NC\d{3}\s*--\s*\S)")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str
    hint: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}\n    {self.hint}"


@dataclass(frozen=True)
class Rule:
    code: str
    summary: str
    hint: str


RULES: dict[str, Rule] = {
    "NC001": Rule(
        "NC001",
        "front deletion from a list",
        "Deleting index 0 shifts every remaining element, so draining a queue is "
        "quadratic. Keep an owned list plus a head index; drop the consumed "
        "prefix in one slice once head >= 64 and head * 2 >= size.",
    ),
    "NC002": Rule(
        "NC002",
        "element removal inside a forward loop over the same list",
        "Removing while iterating forward both skews the index and costs O(n) per "
        "removal. Iterate in reverse, or compact once after the loop.",
    ),
    "NC003": Rule(
        "NC003",
        "additive (non-geometric) buffer growth",
        "Growing by a constant makes n appends cost O(n^2). Grow geometrically "
        "(capacity *= 2, or += capacity >> 1).",
    ),
    "NC004": Rule(
        "NC004",
        "module import inside a per-item function",
        "PyImport_ImportModule per value pays a dict lookup and refcount churn on "
        "every field. Resolve it once at module init, or decode in C.",
    ),
    "NC005": Rule(
        "NC005",
        "string-keyed method dispatch inside a loop",
        "PyObject_CallMethod re-resolves the attribute by name on every "
        "iteration. Hoist the bound method, or use a cached interned name with "
        "PyObject_CallMethodNoArgs.",
    ),
    "NC006": Rule(
        "NC006",
        "rescan from offset zero in an incremental parser",
        "Searching from the start each time more bytes arrive re-examines the "
        "whole buffered prefix, so a byte-at-a-time peer makes parsing "
        "quadratic. Keep a per-state scan cursor and resume from it "
        "(see find_sub_from).",
    ),
    "NC007": Rule(
        "NC007",
        "Python object rebuilt from a constant string table per lookup",
        "Py*_FromString on a constant table element (e.g. STATIC_NAMES[i]) "
        "re-encodes the same immutable literal on every hit. In a per-request "
        "path -- an indexed HPACK header, a fixed status line -- that is one "
        "allocation and one GC object each time. Build the table's objects once "
        "at module init and hand out Py_NewRef; waive the one-time build in "
        "place.",
    ),
}


def strip_c(text: str) -> list[str]:
    """Blank out comments and string/char literals, preserving line numbers.

    Rules must not fire on prose. Several comments in this tree describe the very
    patterns being searched for, so scanning raw text would report itself.
    """
    out: list[list[str]] = [list(line) for line in text.split("\n")]
    row = col = 0
    state = "code"  # code | block | line | string | char
    lines = text.split("\n")

    def blank(r: int, c: int) -> None:
        if c < len(out[r]):
            out[r][c] = " "

    while row < len(lines):
        line = lines[row]
        if col >= len(line):
            if state == "line":
                state = "code"
            row += 1
            col = 0
            continue
        ch = line[col]
        nxt = line[col + 1] if col + 1 < len(line) else ""
        if state == "code":
            if ch == "/" and nxt == "*":
                state = "block"
                blank(row, col)
                blank(row, col + 1)
                col += 2
                continue
            if ch == "/" and nxt == "/":
                state = "line"
                continue
            if ch == '"':
                state = "string"
                col += 1
                continue
            if ch == "'":
                state = "char"
                col += 1
                continue
            col += 1
            continue
        if state == "block":
            if ch == "*" and nxt == "/":
                blank(row, col)
                blank(row, col + 1)
                state = "code"
                col += 2
                continue
            blank(row, col)
            col += 1
            continue
        if state == "line":
            blank(row, col)
            col += 1
            continue
        # inside a literal
        if ch == "\\":
            blank(row, col)
            blank(row, col + 1)
            col += 2
            continue
        if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
            state = "code"
            col += 1
            continue
        blank(row, col)
        col += 1
    return ["".join(r) for r in out]


LOOP_START = re.compile(r"\b(for|while)\s*\(")
LOOP_KEYWORD = re.compile(r"(for|while)\s*\(")
FORWARD_LOOP = re.compile(r"\bfor\s*\([^;]*;[^;]*;[^)]*\+\+")
DEL_ITEM = re.compile(r"\bPySequence_DelItem\s*\(([^,]+),\s*([^)]+)\)")
FRONT_SLICE = re.compile(r"\bPyList_SetSlice\s*\(([^,]+),\s*0\s*,\s*1\s*,\s*NULL\s*\)")
ADDITIVE_GROWTH = re.compile(
    r"\b(cap|capacity|new_cap|size|alloc)\w*\s*=\s*[^;]*\b\1\w*\s*\+\s*\d+\s*;"
)
REALLOC = re.compile(r"\b(PyMem_Realloc|realloc|_PyBytes_Resize|PyByteArray_Resize)\b")
IMPORT_MODULE = re.compile(r"\bPyImport_ImportModule\s*\(")
CALL_METHOD = re.compile(r"\bPyObject_CallMethod\s*\(")
SCAN_FROM_ZERO = re.compile(r"\bfind_sub\s*\(")
# Py*_FromString applied to a subscript of an ALL_CAPS constant table: the
# argument is a compile-time literal, so the resulting object is invariant.
CONST_TABLE_FROMSTRING = re.compile(
    r"\bPy(?:Bytes|Unicode)_FromString\s*\(\s*[A-Z][A-Z0-9_]{2,}\s*\["
)

# Functions where a one-off import is fine: module setup, not a per-item path.
# Matched on underscore-separated tokens -- `\binit\b` never fires inside
# `wreath_pg_codec_init`, because `_` is a word character.
INIT_TOKENS = frozenset(
    {
        "init", "ready", "setup", "module", "exec", "new", "main", "register",
        "fini", "make", "create", "type", "types", "import",
    }
)


def _is_init_function(name: str) -> bool:
    return bool(INIT_TOKENS & set(name.lower().split("_")))


def _waivers(raw_lines: list[str], code_lines: list[str]) -> dict[int, set[str]]:
    """Map line number -> waived rule codes.

    A waiver covers its own line (for trailing comments) and the next line that
    actually contains code. Anything else would force the reason to be a
    one-liner: these waivers have to explain why a match is bounded, and that
    rarely fits beside the statement.
    """
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


# This tree puts the return type on its own line, so the definition begins at
# column zero. Both styles are matched anyway: relying on one house style would
# make the rules silently do nothing the moment someone writes the other.
FUNC_AT_LINE_START = re.compile(r"^([A-Za-z_]\w*)\s*\(")
FUNC_ONE_LINE = re.compile(
    r"^\s*(?:static\s+|inline\s+|const\s+)*[A-Za-z_][\w\s*]*?\b([A-Za-z_]\w*)\s*\([^;]*$"
)
NOT_A_FUNCTION = frozenset({"if", "for", "while", "switch", "return", "sizeof", "else"})


def _enclosing_function(code_lines: list[str], index: int) -> str:
    """Best-effort name of the function containing `index` (0-based)."""
    for i in range(index, max(-1, index - 400), -1):
        line = code_lines[i]
        for pattern in (FUNC_AT_LINE_START, FUNC_ONE_LINE):
            match = pattern.match(line)
            if match and match.group(1) not in NOT_A_FUNCTION:
                return match.group(1)
    return ""


def _loop_depth_map(code_lines: list[str]) -> list[int]:
    """Loop nesting depth at the start of each line.

    Character-wise over the whole file rather than line-wise, because a loop and
    its body routinely share a line (`for (...) { if (...) { ... } }`). The
    loop's opening brace has to be identified when it is reached -- attributing
    it after the fact marks the enclosing function's brace as a loop and leaks
    depth to the end of the function.

    A braceless single-statement loop body is not tracked: bounding it needs
    real parsing, and a rule that quietly does nothing beats one that is wrong.
    """
    text = "\n".join(code_lines)
    n = len(text)
    depth_at = [0] * (n + 1)
    stack: list[bool] = []  # one entry per open brace: True when it opened a loop
    current = 0
    i = 0
    while i < n:
        depth_at[i] = current
        if (
            LOOP_KEYWORD.match(text, i)
            and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_"))
        ):
            # Skip the balanced condition, then look for a braced body.
            open_paren = text.find("(", i)
            balance = 0
            close = open_paren
            while close < n:
                if text[close] == "(":
                    balance += 1
                elif text[close] == ")":
                    balance -= 1
                    if balance == 0:
                        break
                close += 1
            for offset in range(i, min(close + 1, n)):
                depth_at[offset] = current
            probe = close + 1
            while probe < n and text[probe].isspace():
                depth_at[probe] = current
                probe += 1
            if probe < n and text[probe] == "{":
                stack.append(True)
                current += 1
                depth_at[probe] = current
                i = probe + 1
                continue
            i = close + 1
            continue
        ch = text[i]
        if ch == "{":
            stack.append(False)
        elif ch == "}":
            if stack and stack.pop():
                current = max(0, current - 1)
        i += 1
    depth_at[n] = current

    out: list[int] = []
    offset = 0
    for line in code_lines:
        out.append(depth_at[min(offset, n)])
        offset += len(line) + 1
    return out


def scan_text(path: str, text: str) -> list[Finding]:
    raw_lines = text.split("\n")
    code_lines = strip_c(text)
    waived = _waivers(raw_lines, code_lines)
    depth = _loop_depth_map(code_lines)
    findings: list[Finding] = []

    def add(index: int, code: str, message: str) -> None:
        number = index + 1
        if code in waived.get(number, ()):
            return
        findings.append(Finding(path, number, code, message, RULES[code].hint))

    for i, line in enumerate(code_lines):
        # NC001: deleting the front of a list.
        for match in DEL_ITEM.finditer(line):
            target, index_expr = match.group(1).strip(), match.group(2).strip()
            if index_expr == "0":
                add(i, "NC001", f"PySequence_DelItem({target}, 0) shifts the whole list")
            elif depth[i] > 0 and FORWARD_LOOP.search(_loop_header(code_lines, i)):
                add(i, "NC002", f"PySequence_DelItem({target}, ...) inside a forward loop")
        if FRONT_SLICE.search(line):
            add(i, "NC001", "PyList_SetSlice(..., 0, 1, NULL) removes the front element")

        # NC003: additive growth next to a reallocation.
        if ADDITIVE_GROWTH.search(line):
            window = "\n".join(code_lines[max(0, i - 6) : i + 7])
            if REALLOC.search(window):
                add(i, "NC003", "buffer capacity grows by a constant")

        # NC004: importing a module from a per-value path.
        if IMPORT_MODULE.search(line) and not _is_lazy_cached_import(code_lines, i):
            function = _enclosing_function(code_lines, i)
            if function and not _is_init_function(function):
                add(i, "NC004", f"PyImport_ImportModule inside {function}()")

        # NC005: name-based method dispatch inside a loop.
        if depth[i] > 0 and CALL_METHOD.search(line):
            add(i, "NC005", "PyObject_CallMethod inside a loop re-resolves by name")

        # NC006: incremental parsers must resume, not restart.
        if SCAN_FROM_ZERO.search(line) and "find_sub_from" not in line:
            function = _enclosing_function(code_lines, i)
            if function.startswith("drive_"):
                add(i, "NC006", f"find_sub() restarts from zero in {function}()")

        # NC007: an immutable object rebuilt from a constant table on every hit.
        if CONST_TABLE_FROMSTRING.search(line):
            add(i, "NC007", "Py*_FromString of a constant string-table element")

    for number, line in enumerate(raw_lines, 1):
        if WAIVER_BARE.search(line) and not WAIVER.search(line):
            findings.append(
                Finding(
                    path, number, "NC000",
                    "waiver without a rule code and reason",
                    "Write: native-lint: allow NC001 -- why this one is bounded.",
                )
            )
    return findings


STATIC_CACHE = re.compile(r"\bstatic\s+PyObject\s*\*\s*(\w+)\s*=\s*NULL\s*;")


def _is_lazy_cached_import(code_lines: list[str], index: int) -> bool:
    """True when this import is a guarded one-time cache, not per-item work.

        static PyObject *loads = NULL;
        if (loads == NULL) { ... PyImport_ImportModule("json") ... }

    That costs one import for the life of the process, which is exactly what
    NC004 asks for; flagging it would be telling people to do what they did.
    """
    for i in range(index, max(-1, index - 12), -1):
        match = STATIC_CACHE.search(code_lines[i])
        if not match:
            continue
        guard = re.compile(rf"\bif\s*\(\s*{re.escape(match.group(1))}\s*==\s*NULL\s*\)")
        return any(guard.search(code_lines[j]) for j in range(i, index + 1))
    return False


def _loop_header(code_lines: list[str], index: int) -> str:
    """The nearest enclosing loop header above `index`."""
    for i in range(index, max(-1, index - 40), -1):
        if LOOP_START.search(code_lines[i]):
            return code_lines[i]
    return ""


def iter_sources(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*.c")))
            out.extend(sorted(p for p in path.rglob("*.h")))
        elif path.suffix in (".c", ".h"):
            out.append(path)
    return out


def repo_root() -> Path:
    # Installed editable from the repo: src/wreath/_devtools/native_lint.py
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-native-lint",
        description="Scan Wreath's C for known CPU and memory complexity patterns.",
    )
    parser.add_argument("paths", nargs="*", type=Path,
                        help="files or directories (default: src/wreath/_native)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--list-rules", action="store_true",
                        help="print the rules and exit")
    args = parser.parse_args(argv)

    if args.list_rules:
        for rule in RULES.values():
            print(f"{rule.code}  {rule.summary}\n    {rule.hint}\n")
        return 0

    roots = args.paths or [repo_root() / r for r in DEFAULT_ROOTS]
    sources = iter_sources([Path(p) for p in roots])
    if not sources:
        print(f"wreath-native-lint: no C sources found in {[str(r) for r in roots]}",
              file=sys.stderr)
        return 1

    findings: list[Finding] = []
    root = repo_root()
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"wreath-native-lint: cannot read {source}: {exc}", file=sys.stderr)
            return 1
        try:
            display = str(source.relative_to(root))
        except ValueError:
            display = str(source)
        findings.extend(scan_text(display, text))

    if args.format == "json":
        print(json.dumps(
            {"scanned": len(sources),
             "findings": [f.__dict__ for f in findings]}, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        print(
            f"\nwreath-native-lint: {len(findings)} finding(s) across "
            f"{len(sources)} file(s)."
        )
        if not findings:
            print("Waive an intentional match in place, e.g.\n"
                  "    /* native-lint: allow NC001 -- bounded by pipeline depth */")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
