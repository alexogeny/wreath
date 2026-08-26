"""Find superlinear shapes that no complexity probe has been written for yet.

`wreath-complexity-probe` proves a contract about code somebody already
suspected. This is the other direction: a static sweep for the shapes that are
*provably* superlinear -- a linear operation inside a loop over something the
loop does not shrink -- so a candidate can be triaged into a probe, or waived
with a reason, before anyone measures it.

It is a **discovery** tool and a ratchet, not a gate on its own. Every finding
is a candidate; most are bounded by something the scanner cannot see (a cap, a
partition that already made the loop run once, a collection with three entries).
The value is that the set is *acknowledged*: `docs/agents/complexity-discovery.json`
records every candidate that has been looked at and what was decided, and
`--discover-check` fails only when a **new** one appears. That way a quadratic
someone introduces tomorrow shows up as a diff rather than joining 1,200 lines
of scenery nobody reads.

Two rules the scanners follow, both learned the expensive way:

* **A container whose kind is known to be hashed is not a finding.** `x in s`
  where `s` is a `set`/`frozenset`/`dict` is O(1), and reporting it buried the
  real findings. Kinds come from parameter annotations first (the most reliable
  signal in this tree), then literals and constructors; anything else is
  "unknown" and reported at lower confidence rather than suppressed.
* **Iterating `d.values()` is the loop, not an extra linear op inside one.**
  Treating it as a finding produced several hundred hits on `for x in
  d.values()` and hid everything else.

The C half is deliberately disjoint from `wreath-native-lint`: NC001-NC007
already cover front-deleted lists, additive growth, per-value imports,
name-based dispatch in a loop, parser restarts, and constant-table rebuilds.
This adds the shapes those rules do not read -- loop nests, a linear call inside
a loop, `strlen` in a loop condition, and reallocation inside a loop -- and
honours the same `native-lint: allow` waiver comments.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any

# --- Python -----------------------------------------------------------------

#: membership against these is O(1), so a hit is not a finding at all
HASHED = frozenset({"set", "frozenset", "dict"})
#: membership against these is O(n)
SEQUENCED = frozenset({"list", "tuple", "str", "bytes", "bytearray"})

#: method calls whose cost is linear in the receiver or the argument.
#: `values()`/`keys()`/`items()` are deliberately absent -- see the module
#: docstring; including them buried every real finding.
LINEAR_METHODS = {
    "index": "index() scans",
    "remove": "remove() scans",
    "count": "count() scans",
    "sort": "sort() is n log n",
    "reverse": "reverse() copies",
    "update": "update() is linear in its argument",
    "extend": "extend() is linear in its argument",
    "join": "join() walks the whole iterable",
    "copy": "copy() is linear",
    "difference": "set difference is linear",
    "union": "set union is linear",
    "intersection": "set intersection is linear",
    "issubset": "subset test is linear",
}
LINEAR_FUNCS = {
    "sorted": "sorted() is n log n",
    "min": "min() scans",
    "max": "max() scans",
    "sum": "sum() scans",
    "any": "any() scans",
    "all": "all() scans",
    "list": "list() copies",
    "dict": "dict() copies",
    "set": "set() copies",
    "tuple": "tuple() copies",
    "frozenset": "frozenset() copies",
    "deepcopy": "deepcopy() walks the graph",
    "reversed": "reversed() over a sequence",
}
#: `pop(0)` / `insert(0, ...)` shift every remaining element
FRONT_MUTATORS = frozenset({"pop", "insert"})

#: annotation head -> container kind. `Iterable` is intentionally absent: it
#: says nothing about membership cost.
_ANNOTATION_KINDS = {
    "set": "set", "Set": "set", "AbstractSet": "set", "MutableSet": "set",
    "frozenset": "frozenset", "FrozenSet": "frozenset",
    "dict": "dict", "Dict": "dict", "Mapping": "dict",
    "MutableMapping": "dict", "OrderedDict": "dict", "defaultdict": "dict",
    "Counter": "dict",
    "list": "list", "List": "list", "MutableSequence": "list",
    "tuple": "tuple", "Tuple": "tuple", "Sequence": "tuple",
    "str": "str", "bytes": "bytes", "bytearray": "bytearray",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One candidate superlinear site."""

    file: str
    line: int
    code: str
    func: str
    depth: int
    confidence: str
    message: str
    source: str

    @property
    def key(self) -> str:
        """Identity for the acknowledgement baseline.

        Deliberately **excludes the line number**: adding an import at the top
        of a module would otherwise re-report every finding in it as new.
        """
        return f"{self.file}::{self.func}::{self.code}"

    def document(self) -> dict[str, Any]:
        return {
            "file": self.file, "line": self.line, "code": self.code,
            "function": self.func, "loop_depth": self.depth,
            "confidence": self.confidence, "message": self.message,
            "source": self.source,
        }


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _annotation_kind(annotation: ast.AST | None) -> str | None:
    """`set[type]`, `Sequence[str]`, `frozenset[...]` -> the container kind."""
    if annotation is None:
        return None
    base = annotation
    while isinstance(base, ast.Subscript):
        base = base.value
    name = _name_of(base)
    if not name:
        return None
    return _ANNOTATION_KINDS.get(name.rsplit(".", 1)[-1])


_STATEMENT_BLOCKS = ("body", "handlers", "orelse", "finalbody", "cases")


def _owned_statements(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
):
    """Statements owned by *fn*, stopping before every nested scope."""
    todo = deque(fn.body)
    while todo:
        node = todo.popleft()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for name in _STATEMENT_BLOCKS:
            block = getattr(node, name, None)
            if block:
                # These statement blocks are disjoint; every statement enters
                # the queue once regardless of nesting depth.
                todo.extend(block)


def infer_kinds(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str | None]:
    """Best-effort container kind per local name, for one function body.

    Annotations beat values, because `seen: set[str] = _build()` is exactly the
    case where the value tells us nothing and the annotation tells us
    everything. A name rebound to two different kinds becomes unknown rather
    than taking the last one.
    """
    kinds: dict[str, str | None] = {}

    def kind_of(value: ast.AST | None) -> str | None:
        match value:
            case ast.SetComp() | ast.Set():
                return "set"
            case ast.DictComp() | ast.Dict():
                return "dict"
            case ast.ListComp() | ast.List():
                return "list"
            case ast.Tuple():
                return "tuple"
            case ast.JoinedStr():
                return "str"
            case ast.Constant(value=str()):
                return "str"
            case ast.Constant(value=bytes()):
                return "bytes"
            case ast.Call(func=ast.Name(id=name)) if name in HASHED | SEQUENCED:
                return name
            case _:
                return None

    args = fn.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                *filter(None, (args.vararg, args.kwarg))):
        kinds[arg.arg] = _annotation_kind(arg.annotation)

    # A nested definition is a new ownership boundary.  Walking ``fn`` with
    # ``ast.walk`` used to descend every nested body here, then do the same work
    # again when the visitor entered that definition.  A chain of N functions
    # therefore visited O(N**2) nodes.  Start at this function's statements and
    # stop at child scopes: each body is inferred exactly once by its owner.
    for node in _owned_statements(fn):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            kinds[node.target.id] = (
                _annotation_kind(node.annotation)
                or (kind_of(node.value) if node.value is not None else None))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    kind = kind_of(node.value)
                    kinds[target.id] = (
                        None if (target.id in kinds and kinds[target.id] != kind)
                        else kind)
    return kinds


@dataclass(slots=True)
class _Loop:
    """One enclosing loop, and what it binds."""

    iterated: str | None
    targets: set[str]
    bound: set[str] = field(default_factory=set)
    constant: bool = False


@dataclass(slots=True)
class _Recursion:
    """One function whose direct self-call sites are being counted."""

    function: ast.FunctionDef | ast.AsyncFunctionDef
    calls: int = 0


class _PythonScanner(ast.NodeVisitor):
    """Report linear operations sitting inside a loop that does not shrink."""

    def __init__(self, path: Path, source: str, root: Path) -> None:
        self.path = path
        self.display = str(path.relative_to(root)) if root in path.parents else str(path)
        self.lines = source.splitlines()
        self.findings: list[Finding] = []
        self.loops: list[_Loop] = []
        self.functions: list[str] = []
        self.kinds: dict[str, str | None] = {}
        self.recursion_frames: list[_Recursion] = []
        self.recursion_findings: list[Finding] = []

    # -- helpers ---------------------------------------------------------
    def _depth(self) -> int:
        """Loop depth, ignoring `for x in (a, b, c)` constant unrolls."""
        return sum(1 for loop in self.loops if not loop.constant)

    def _loop_local(self, name: str) -> bool:
        """Whether the loop rebinds this name, which makes cost per-iteration."""
        return any(name in loop.bound or name in loop.targets for loop in self.loops)

    def _report(self, node: ast.AST, code: str, message: str,
                confidence: str = "medium") -> None:
        line = getattr(node, "lineno", 0)
        self.findings.append(Finding(
            file=self.display, line=line, code=code,
            func=self.functions[-1] if self.functions else "<module>",
            depth=self._depth(), confidence=confidence, message=message,
            source=self.lines[line - 1].strip()[:140] if 0 < line <= len(self.lines) else "",
        ))

    @staticmethod
    def _targets_of(target: ast.AST) -> set[str]:
        return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}

    def _push_loop(self, iter_node: ast.AST | None, target: ast.AST | None) -> None:
        self.loops.append(_Loop(
            iterated=_name_of(iter_node) if iter_node is not None else None,
            targets=self._targets_of(target) if target is not None else set(),
            constant=isinstance(iter_node, ast.Tuple | ast.List | ast.Set)
            and len(getattr(iter_node, "elts", ())) <= 8,
        ))

    def _record_binding(self, node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> None:
        if not self.loops:
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            self.loops[-1].bound |= self._targets_of(target)

    # -- scopes ----------------------------------------------------------
    def visit_FunctionDef(self, node) -> None:
        self.functions.append(node.name)
        saved_loops, saved_kinds = self.loops, self.kinds
        self.loops, self.kinds = [], infer_kinds(node)   # a def is its own cost universe
        recursion = _Recursion(node)
        self.recursion_frames.append(recursion)
        self.generic_visit(node)
        self.recursion_frames.pop()
        decorators = {_name_of(item) or "" for item in node.decorator_list}
        if recursion.calls >= 2 and not any("cache" in item for item in decorators):
            self.recursion_findings.append(Finding(
                file=self.display, line=node.lineno, code="SL-RECURSE", func=node.name,
                depth=0, confidence="low",
                message=f"{recursion.calls} self-recursive call sites, no cache decorator",
                source="",
            ))
        self.loops, self.kinds = saved_loops, saved_kinds
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node) -> None:
        self._record_binding(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node) -> None:
        self._record_binding(node)
        self.generic_visit(node)

    # -- loops -----------------------------------------------------------
    def visit_For(self, node) -> None:
        iterated = _name_of(node.iter)
        if iterated and any(loop.iterated == iterated and not loop.constant
                            for loop in self.loops):
            self._report(node, "SL-NEST-SAME",
                         f"nested loop re-iterates `{iterated}`, already being "
                         f"iterated outside", confidence="high")
        self._push_loop(node.iter, node.target)
        self.generic_visit(node)
        self.loops.pop()

    visit_AsyncFor = visit_For

    def visit_While(self, node) -> None:
        self._push_loop(None, None)
        self.generic_visit(node)
        self.loops.pop()

    # -- the linear operations -------------------------------------------
    def visit_Compare(self, node) -> None:
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.In | ast.NotIn):
                continue
            name = _name_of(comparator)
            kind = self.kinds.get(name)
            if kind in HASHED or isinstance(comparator, ast.Set | ast.Dict):
                continue                      # O(1) membership: not a finding
            if self._depth() >= 1 and name and not self._loop_local(name):
                self._report(node, "SL-IN-LOOP",
                             f"membership test against `{name}`"
                             + (f" (a {kind})" if kind else " (kind unknown)")
                             + " inside a loop",
                             confidence="high" if kind in SEQUENCED else "medium")
        self.generic_visit(node)

    def visit_Call(self, node) -> None:
        if isinstance(node.func, ast.Name):
            for recursion in self.recursion_frames:
                if node.func.id == recursion.function.name:
                    recursion.calls += 1
        if self._depth() >= 1:
            self._check_call(node)
        self.generic_visit(node)

    def _check_call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            receiver, method = _name_of(func.value), func.attr
            front = (method in FRONT_MUTATORS and node.args
                     and isinstance(node.args[0], ast.Constant)
                     and node.args[0].value == 0)
            if receiver and not self._loop_local(receiver):
                if front:
                    self._report(node, "SL-FRONT-MUTATE",
                                 f"`{receiver}.{method}(0, ...)` inside a loop "
                                 f"shifts every remaining element",
                                 confidence="high")
                elif method in LINEAR_METHODS:
                    self._report(node, "SL-LINEAR-METHOD",
                                 f"`{receiver}.{method}()` inside a loop: "
                                 f"{LINEAR_METHODS[method]}")
            if method == "compile" and _name_of(func.value) in {"re", "regex"}:
                self._report(node, "SL-RECOMPILE",
                             "re.compile() inside a loop rebuilds the pattern",
                             confidence="high")
        elif isinstance(func, ast.Name) and func.id in LINEAR_FUNCS and node.args:
            argument = _name_of(node.args[0])
            if argument and not self._loop_local(argument):
                self._report(node, "SL-LINEAR-CALL",
                             f"`{func.id}({argument})` inside a loop: "
                             f"{LINEAR_FUNCS[func.id]}")

    def visit_AugAssign(self, node) -> None:
        # Check BEFORE recording the binding: `s += p` binds `s`, and recording
        # first makes the accumulator look loop-local and suppresses its own
        # finding. That bug hid every quadratic string build in the tree.
        if self._depth() >= 1 and isinstance(node.op, ast.Add):
            target = _name_of(node.target)
            kind = self.kinds.get(target)
            quadratic = kind in {"str", "bytes", "tuple"}
            if target and not self._loop_local(target) and kind != "list":
                self._report(node, "SL-ACCUM-ADD",
                             f"`{target} += ...` accumulates inside a loop"
                             + (f" (`{target}` is a {kind}: quadratic)" if quadratic
                                else " (quadratic if str/bytes/tuple)"),
                             confidence="high" if quadratic else "low")
        self._record_binding(node)
        self.generic_visit(node)

    def visit_Subscript(self, node) -> None:
        if self._depth() >= 1 and isinstance(node.slice, ast.Slice):
            name = _name_of(node.value)
            if name and not self._loop_local(name):
                self._report(node, "SL-SLICE-LOOP",
                             f"slice of `{name}` inside a loop copies each time")
        self.generic_visit(node)

    def visit_Delete(self, node) -> None:
        if self._depth() >= 1:
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == 0):
                    self._report(node, "SL-FRONT-MUTATE",
                                 f"`del {_name_of(target.value)}[0]` inside a loop "
                                 f"shifts every remaining element",
                                 confidence="high")
        self.generic_visit(node)

    # -- comprehensions are loops ----------------------------------------
    def visit_ListComp(self, node) -> None:
        self._comprehension(node)

    def visit_SetComp(self, node) -> None:
        self._comprehension(node)

    def visit_DictComp(self, node) -> None:
        self._comprehension(node)

    def visit_GeneratorExp(self, node) -> None:
        self._comprehension(node)

    def _comprehension(self, node) -> None:
        if self._depth() >= 1:
            for generator in node.generators:
                name = _name_of(generator.iter)
                if name and not self._loop_local(name):
                    self._report(node, "SL-COMP-LOOP",
                                 f"comprehension over `{name}` inside a loop")
        if len(node.generators) >= 2:
            self._report(node, "SL-COMP-NEST",
                         f"comprehension with {len(node.generators)} generators")
        # Push a frame per generator: a comprehension IS a loop, and treating it
        # as anything else missed `[seq.index(x) for x in items]` entirely.
        for generator in node.generators:
            self._push_loop(generator.iter, generator.target)
        self.generic_visit(node)
        for _ in node.generators:
            self.loops.pop()


def scan_python(path: Path, root: Path) -> list[Finding]:
    """Every candidate in one Python module."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    scanner = _PythonScanner(path, source, root)
    scanner.visit(tree)
    return scanner.findings + scanner.recursion_findings


# --- C ----------------------------------------------------------------------

_COMMENT = re.compile(r"/\*.*?\*/")
_LOOP = re.compile(r"^\s*(for|while)\s*\(")
_FUNC = re.compile(r"^[A-Za-z_][A-Za-z0-9_ *]*\b(\w+)\s*\([^;]*\)\s*\{?\s*$")
_LINEAR_CALL = re.compile(
    r"\b(memmove|memcpy|memset|strlen|strcmp|strncmp|strstr|memcmp|"
    r"wreath_memmem|qsort|PySequence_Contains|PyList_Insert|"
    r"PyUnicode_Find|PyBytes_Concat|_PyBytes_Resize|PyDict_Merge)\s*\(")
_REALLOC = re.compile(r"\b(realloc|PyMem_Realloc|PyMem_RawRealloc|PyObject_Realloc)\s*\(")
#: honour the same waiver comments `wreath-native-lint` reads
_WAIVER = re.compile(r"native-lint:\s*allow|complexity:\s*allow")
_STRLEN = re.compile(r"\bstrlen\s*\(")


def scan_c(path: Path, root: Path) -> list[Finding]:
    """Every candidate in one C translation unit or header."""
    display = str(path.relative_to(root)) if root in path.parents else str(path)
    out: list[Finding] = []
    depth = 0
    func, func_depth = "<file>", None
    loops: list[int] = []          # brace depth at which each open loop began
    waived = False                 # a waiver comment covers the next code line

    def found(code: str, message: str, at_depth: int, *,
              number: int, func: str, raw: str) -> Finding:
        # Hoisted out of the loop and given its varying inputs explicitly: a
        # closure over the loop variables is the bug ruff's B023 names.
        return Finding(file=display, line=number, code=code, func=func,
                       depth=at_depth, confidence="medium",
                       message=message, source=raw.strip()[:140])

    for number, raw in enumerate(path.read_text(encoding="utf-8",
                                                errors="replace").splitlines(), 1):
        # Strip both comment forms before matching. A trailing `/* ... */` on a
        # signature line made every function in the tree read as "<file>".
        line = _COMMENT.sub(" ", raw.split("//")[0])
        braces = line.count("{") - line.count("}")

        if _WAIVER.search(raw):
            waived, depth = True, depth + braces
            continue
        if waived and line.strip():
            waived, depth = False, depth + braces
            continue

        signature = _FUNC.match(line.rstrip())
        if signature and depth == 0:
            func, func_depth = signature.group(1), depth

        is_loop = bool(_LOOP.match(line))
        if is_loop:
            if loops:
                out.append(found("CL-NEST", f"loop nested {len(loops) + 1} deep",
                                 len(loops) + 1, number=number, func=func, raw=raw))
            if _STRLEN.search(line):
                out.append(found(
                    "CL-STRLEN-COND",
                    "strlen() in a loop condition is re-evaluated per iteration",
                    len(loops) + 1, number=number, func=func, raw=raw))
            loops.append(depth)
        elif loops:
            out += [
                found(code, f"{hit.group(1)}(): {why}", len(loops),
                      number=number, func=func, raw=raw)
                for pattern, code, why in (
                    (_LINEAR_CALL, "CL-LINEAR-IN-LOOP", "linear call inside a loop"),
                    (_REALLOC, "CL-REALLOC-IN-LOOP", "reallocation inside a loop"),
                )
                if (hit := pattern.search(line))
            ]

        depth += braces
        while loops and depth <= loops[-1]:
            loops.pop()
        if func_depth is not None and depth <= func_depth and "}" in line:
            func, func_depth = "<file>", None
    return out


# --- the sweep --------------------------------------------------------------

def _source_paths(root: Path) -> list[Path]:
    """Every source the discovery rules own, in stable order."""
    return sorted(
        path
        for path in root.rglob("*")
        if path.suffix in {".py", ".c", ".h"}
    )


def _source_fingerprint(path: Path) -> str:
    """Exact content identity for one cached discovery result."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_fingerprints(root: Path) -> dict[str, str]:
    """Content identities for every discovery source, including clean files."""
    return {
        str(path.relative_to(root)): _source_fingerprint(path)
        for path in _source_paths(root)
    }


def discover(
    root: Path,
    *,
    cache: Mapping[str, tuple[str, Sequence[Finding]]] | None = None,
) -> list[Finding]:
    """Every candidate under `root`, ordered for a stable baseline diff.

    An exact-content cache may supply findings for byte-identical files. Files
    absent from it, including every new or edited source, are scanned normally;
    cached files that were deleted are never visited. The caller owns
    invalidating the whole cache when these scanner rules change.
    """
    per_file: list[Sequence[Finding]] = []
    for path in _source_paths(root):
        relative = str(path.relative_to(root))
        cached = cache.get(relative) if cache is not None else None
        if cached is not None and cached[0] == _source_fingerprint(path):
            per_file.append(cached[1])
            continue
        per_file.append(
            scan_python(path, root)
            if path.suffix == ".py"
            else scan_c(path, root)
        )
    return sorted(chain.from_iterable(per_file), key=lambda f: (f.file, f.line, f.code))
