"""Find statically provable superlinear shapes without a recorded decision.

The scanner reports only operands whose container kind and reuse are visible.
Exact-code waivers record bounded or output-sized work. The discovery baseline
therefore retains exceptional confirmed hotspots instead of scanner guesses.

The C rules complement `wreath-native-lint` with repeated nonconstant loop
bounds and `strlen` in loop conditions. Both scanners accept a named waiver
with a nonempty reason.
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
RECEIVER_LINEAR_METHODS = {
    "index": "index() scans",
    "remove": "remove() scans",
    "count": "count() scans",
    "sort": "sort() is n log n",
    "reverse": "reverse() scans",
    "copy": "copy() is linear",
    "difference": "set difference is linear",
    "union": "set union is linear",
    "intersection": "set intersection is linear",
    "issubset": "subset test is linear",
}
ARGUMENT_LINEAR_METHODS = {
    "update": "update() is linear in its argument",
    "extend": "extend() is linear in its argument",
    "join": "join() walks the whole iterable",
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
        site = hashlib.sha256(self.source.encode()).hexdigest()[:16]
        return f"{self.file}::{self.func}::{self.code}::{site}"

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


def _has_branching_recursion(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    for statement in _owned_statements(function):
        for expression in ast.walk(statement):
            if not isinstance(expression, ast.BinOp):
                continue
            calls = sum(
                1
                for node in ast.walk(expression)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == function.name
            )
            if calls >= 2:
                return True
    return False


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
        self.recursion_findings: list[Finding] = []

    # -- helpers ---------------------------------------------------------
    def _depth(self) -> int:
        """Loop depth, ignoring `for x in (a, b, c)` constant unrolls."""
        return sum(1 for loop in self.loops if not loop.constant)

    def _loop_local(self, name: str) -> bool:
        """Whether the loop rebinds this name, which makes cost per-iteration."""
        return any(name in loop.bound or name in loop.targets for loop in self.loops)

    def _report(self, node: ast.AST, code: str, message: str,
                confidence: str = "high") -> None:
        line = getattr(node, "lineno", 0)
        waiver = re.compile(
            rf"#\s*complexity:\s*allow\s+{re.escape(code)}\s+--\s*\S"
        )
        if any(
            waiver.search(self.lines[index])
            for index in (line - 2, line - 1)
            if 0 <= index < len(self.lines)
        ):
            return
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
        self.generic_visit(node)
        decorators = {_name_of(item) or "" for item in node.decorator_list}
        if (
            not any("cache" in item for item in decorators)
            and _has_branching_recursion(node)
        ):
            self.recursion_findings.append(Finding(
                file=self.display, line=node.lineno, code="SL-RECURSE", func=node.name,
                depth=0, confidence="high",
                message="multiple self-calls in one eagerly evaluated expression",
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
        self.visit(node.iter)
        self._push_loop(node.iter, node.target)
        self.visit(node.target)
        for statement in node.body:
            self.visit(statement)
        self.loops.pop()
        for statement in node.orelse:
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_While(self, node) -> None:
        self._push_loop(None, None)
        self.visit(node.test)
        for statement in node.body:
            self.visit(statement)
        self.loops.pop()
        for statement in node.orelse:
            self.visit(statement)

    def visit_Return(self, node) -> None:
        saved_loops, self.loops = self.loops, []
        if node.value is not None:
            self.visit(node.value)
        self.loops = saved_loops

    def visit_Raise(self, node) -> None:
        saved_loops, self.loops = self.loops, []
        if node.exc is not None:
            self.visit(node.exc)
        if node.cause is not None:
            self.visit(node.cause)
        self.loops = saved_loops

    # -- the linear operations -------------------------------------------
    def visit_Compare(self, node) -> None:
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.In | ast.NotIn):
                continue
            name = _name_of(comparator)
            kind = self.kinds.get(name)
            if (
                self._depth() >= 1
                and kind in SEQUENCED
                and name
                and not self._loop_local(name)
            ):
                self._report(node, "SL-IN-LOOP",
                             f"membership test against `{name}`"
                             f" (a {kind}) inside a loop")
        self.generic_visit(node)

    def visit_Call(self, node) -> None:
        if self._depth() >= 1:
            self._check_call(node)
        self.generic_visit(node)

    def _check_call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            receiver, method = _name_of(func.value), func.attr
            receiver_kind = self.kinds.get(receiver)
            front = (method in FRONT_MUTATORS and node.args
                     and isinstance(node.args[0], ast.Constant)
                     and node.args[0].value == 0)
            if receiver and not self._loop_local(receiver):
                if front:
                    self._report(node, "SL-FRONT-MUTATE",
                                 f"`{receiver}.{method}(0, ...)` inside a loop "
                                 f"shifts every remaining element",
                                 confidence="high")
                elif (
                    method in RECEIVER_LINEAR_METHODS
                    and receiver_kind in HASHED | SEQUENCED
                    and (method not in {"index", "count"} or len(node.args) == 1)
                ):
                    self._report(node, "SL-LINEAR-METHOD",
                                 f"`{receiver}.{method}()` inside a loop: "
                                 f"{RECEIVER_LINEAR_METHODS[method]}")
            if method in ARGUMENT_LINEAR_METHODS and node.args:
                argument = _name_of(node.args[0])
                argument_kind = self.kinds.get(argument)
                receiver_supports_method = (
                    (method == "join" and (
                        receiver_kind in {"str", "bytes"}
                        or isinstance(func.value, ast.Constant)
                        and isinstance(func.value.value, str | bytes)
                    ))
                    or method == "extend" and receiver_kind == "list"
                    or method == "update" and receiver_kind in HASHED
                )
                if (
                    receiver_supports_method
                    and argument
                    and argument_kind in HASHED | SEQUENCED
                    and not self._loop_local(argument)
                ):
                    self._report(
                        node,
                        "SL-LINEAR-METHOD",
                        f"`{method}({argument})` inside a loop: "
                        f"{ARGUMENT_LINEAR_METHODS[method]}",
                    )
            if method == "compile" and _name_of(func.value) in {"re", "regex"}:
                self._report(node, "SL-RECOMPILE",
                             "re.compile() inside a loop rebuilds the pattern",
                             confidence="high")
        elif (
            isinstance(func, ast.Name)
            and func.id in LINEAR_FUNCS
            and len(node.args) == 1
        ):
            argument = _name_of(node.args[0])
            if (
                argument
                and self.kinds.get(argument) in HASHED | SEQUENCED
                and not self._loop_local(argument)
            ):
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
            if target and not self._loop_local(target) and quadratic:
                self._report(node, "SL-ACCUM-ADD",
                             f"`{target} += ...` accumulates inside a loop"
                             f" (`{target}` is a {kind}: quadratic)")
        self._record_binding(node)
        self.generic_visit(node)

    def visit_Subscript(self, node) -> None:
        if self._depth() >= 1 and isinstance(node.slice, ast.Slice):
            name = _name_of(node.value)
            lower = node.slice.lower
            upper = node.slice.upper
            open_ended = lower is None or upper is None
            bounded_prefix = (
                lower is None
                and isinstance(upper, ast.Constant)
                and isinstance(upper.value, int)
            )
            bound = upper if lower is None else lower if upper is None else None
            invariant_bound = bound is None or not any(
                self._loop_local(item.id)
                for item in ast.walk(bound)
                if isinstance(item, ast.Name)
            )
            if (
                name
                and self.kinds.get(name) in SEQUENCED
                and not self._loop_local(name)
                and open_ended
                and not bounded_prefix
                and invariant_bound
            ):
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
        seen: set[str] = set()
        for generator in node.generators:
            name = _name_of(generator.iter)
            if (
                self._depth() >= 1
                and name
                and self.kinds.get(name) in HASHED | SEQUENCED
                and not self._loop_local(name)
            ):
                self._report(node, "SL-COMP-LOOP",
                             f"comprehension over `{name}` inside a loop")
            if name and name in seen and self.kinds.get(name) in HASHED | SEQUENCED:
                self._report(node, "SL-COMP-NEST",
                             f"comprehension re-iterates `{name}`")
            if name:
                seen.add(name)
            self.visit(generator.iter)
            self._push_loop(generator.iter, generator.target)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
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
_FOR = re.compile(r"^\s*for\s*\((.*)\)")
_FUNC = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_ *]*\b\s+)?(\w+)\s*\([^;]*\)\s*\{?\s*$"
)
#: honour the same waiver comments `wreath-native-lint` reads
_WAIVER = re.compile(r"native-lint:\s*allow|complexity:\s*allow")
_STRLEN = re.compile(r"\bstrlen\s*\(")


def _c_for_bound(line: str) -> str | None:
    matched = _FOR.match(line)
    if matched is None:
        return None
    clauses = matched.group(1).split(";", 2)
    if len(clauses) != 3:
        return None
    variable = re.search(r"\b([A-Za-z_]\w*)\s*=", clauses[0])
    if variable is None:
        return None
    name = re.escape(variable.group(1))
    condition = clauses[1].strip()
    upper = re.fullmatch(rf"{name}\s*(?:<|<=|>|>=|!=)\s*(.+)", condition)
    if upper is not None:
        return re.sub(r"\s+", "", upper.group(1))
    lower = re.fullmatch(rf"(.+)\s*(?:<|<=|>|>=|!=)\s*{name}", condition)
    if lower is not None:
        return re.sub(r"\s+", "", lower.group(1))
    return None


def scan_c(path: Path, root: Path) -> list[Finding]:
    """Every candidate in one C translation unit or header."""
    display = str(path.relative_to(root)) if root in path.parents else str(path)
    out: list[Finding] = []
    depth = 0
    func, func_depth = "<file>", None
    loops: list[tuple[int, str | None]] = []
    active_bounds: dict[str, int] = {}
    waived = False                 # a waiver comment covers the next code line

    def found(code: str, message: str, at_depth: int, *,
              number: int, func: str, raw: str) -> Finding:
        # Hoisted out of the loop and given its varying inputs explicitly: a
        # closure over the loop variables is the bug ruff's B023 names.
        return Finding(file=display, line=number, code=code, func=func,
                       depth=at_depth, confidence="high",
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
            bound = _c_for_bound(line)
            fixed_bound = bool(
                bound and re.fullmatch(r"[A-Z_][A-Z0-9_]*", bound)
            )
            if bound is not None and not fixed_bound and bound in active_bounds:
                out.append(found(
                    "CL-NEST-SAME",
                    f"nested loop reuses bound `{bound}`",
                    len(loops) + 1,
                    number=number,
                    func=func,
                    raw=raw,
                ))
            if _STRLEN.search(line):
                out.append(found(
                    "CL-STRLEN-COND",
                    "strlen() in a loop condition is re-evaluated per iteration",
                    len(loops) + 1, number=number, func=func, raw=raw))
            loops.append((depth, bound))
            if bound is not None:
                active_bounds[bound] = active_bounds.get(bound, 0) + 1

        depth += braces
        while loops and depth <= loops[-1][0]:
            _, bound = loops.pop()
            if bound is not None:
                remaining = active_bounds[bound] - 1
                if remaining:
                    active_bounds[bound] = remaining
                else:
                    del active_bounds[bound]
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
