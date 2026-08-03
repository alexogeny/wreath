"""Find duplicated *structure* in the Python sources, not duplicated text.

Copy-paste in this repository does not survive as copy-paste. It gets renamed:
the same nine-line body appears as `insert_settled` and `replace_settled`, as
`_register_groups` and `_deregister_groups`, with different locals and
different literals. A textual duplicate finder sees nothing; a reader sees it
immediately and then has to fix the same bug twice.

So this normalises before hashing. Every identifier, attribute name, argument
name and constant collapses to a placeholder, leaving only the shape of the
control and call structure, and bodies whose shapes hash equal are grouped. That
is what found four byte-identical `main()` functions across the native lints
after a shared `_waivers` helper had already been hoisted out of the same four
files -- the second half of a de-duplication nobody finished.

    uv run wreath-dup-scan                       # the default report
    uv run wreath-dup-scan --min-lines 12        # only bigger bodies
    uv run wreath-dup-scan --top 40 --format json
    uv run wreath-dup-scan --path src/wreath/_devtools

**This is a report, and deliberately not a gate.** Plenty of its findings are
legitimate near-twins -- `query`/`mutation`, `insert_settled`/
`upsert_correction` -- where the shared shape is the point and collapsing them
would cost more clarity than it saves. Wiring it into `wreath-check` would
train everyone to ignore it. Read it when a subsystem feels repetitive, and when
it names something you did not know was duplicated, that is the finding.

Ranked by *redundant* lines (the copies after the first), because that is what
collapsing the group would actually remove.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .native_lint import repo_root

#: Where the scan runs when no `--path` is given.
DEFAULT_ROOTS: tuple[str, ...] = ("src/wreath",)

#: Bodies shorter than this are trivia. Two four-line functions that both unpack
#: a tuple and return are not a duplication finding, they are Python. Counted
#: over the *significant* body -- see `_body_lines` for why the function's own
#: span is the wrong ruler in a codebase that documents this heavily.
DEFAULT_MIN_LINES = 8

#: How many groups the text report prints before it stops being read.
DEFAULT_TOP = 25


def _word(out: bytearray, value: str) -> None:
    raw = value.encode()
    out.extend(len(raw).to_bytes(2))
    out.extend(raw)


def _structure(value: object, out: bytearray) -> None:
    """Append an unambiguous, anonymised AST shape to *out*.

    The former immutable normal form allocated a nested tuple mirroring the
    complete function and then allocated its equally large ``repr`` solely to
    hash it.  Length-prefixed tokens preserve the same equality relation while
    walking the source tree once and allocating one flat buffer.
    """
    if isinstance(value, ast.Name):
        out.extend(b"N")
        _structure(value.ctx, out)
        return
    if isinstance(value, ast.Attribute):
        out.extend(b"R")
        _structure(value.value, out)
        _structure(value.ctx, out)
        return
    if isinstance(value, ast.arg):
        # The former transformer constructed a fresh arg with no annotation or
        # type comment, so neither is structural here.
        out.extend(b"G")
        return
    if isinstance(value, ast.Constant):
        out.extend(b"C")
        return
    if isinstance(value, ast.keyword):
        out.extend(b"K0" if value.arg is None else b"K1")
        _structure(value.value, out)
        return
    if isinstance(value, ast.AST):
        out.extend(b"A")
        _word(out, type(value).__name__)
        fields = tuple(ast.iter_fields(value))
        out.extend(len(fields).to_bytes(2))
        for name, field in fields:
            _word(out, name)
            _structure(field, out)
        return
    if isinstance(value, list):
        out.extend(b"L")
        out.extend(len(value).to_bytes(4))
        for item in value:
            _structure(item, out)
        return
    out.extend(b"V")
    _word(out, repr(value))


@dataclass(frozen=True)
class Site:
    """One function body, and where to find it."""

    path: str
    name: str
    line: int
    lines: int


@dataclass(frozen=True)
class Group:
    """Bodies that share a normalised shape."""

    digest: str
    sites: tuple[Site, ...]

    @property
    def redundant_lines(self) -> int:
        """Lines a collapse would remove: everything after the first copy."""
        return sum(site.lines for site in self.sites[1:])

    def to_json(self) -> dict:
        return {
            "digest": self.digest,
            "redundant_lines": self.redundant_lines,
            "copies": len(self.sites),
            "sites": [
                {"path": s.path, "name": s.name, "line": s.line, "lines": s.lines}
                for s in self.sites
            ],
        }


def _shape(body: list[ast.stmt]) -> str:
    """A structural digest of a function body.

    The immutable tuple walk does not rewrite the parsed tree. An earlier form
    round-tripped through ``ast.parse(ast.unparse(...))`` to get a fresh tree,
    which raised on any body whose first statement cannot stand alone as a
    module -- a bare ``return`` -- and swallowed it in a blanket ``except``.
    Those functions were silently absent from every scan.
    """
    shape = bytearray()
    _structure(body, shape)
    return hashlib.blake2s(shape, digest_size=12).hexdigest()


def _significant_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """The body without its docstring -- prose is not structure."""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return body


def _body_lines(body: list[ast.stmt]) -> int:
    """How many source lines the *significant* body spans.

    Deliberately not the function's own span. Measuring `node.end_lineno -
    node.lineno` counts the signature and the docstring, and this repository
    documents heavily: a `Protocol` stub whose entire body is `...` under twelve
    lines of prose measured as a twelve-line function, cleared `--min-lines`,
    and then hashed identically to every other stub in the tree. That produced
    the two largest groups in the report -- 27 copies and 29 copies, 551
    "redundant" lines between them -- made up entirely of Protocol methods and
    one-line `@property` accessors that share no code at all.

    It was also the wrong number for the ranking. The report ranks by "lines a
    collapse would remove", and collapsing two functions removes their bodies;
    it never removes their docstrings, because the surviving one still needs
    prose. So this is both the honest filter and the honest weight.
    """
    if not body:
        return 0
    first, last = body[0], body[-1]
    return (last.end_lineno or last.lineno) - first.lineno + 1


def _is_stub(body: list[ast.stmt]) -> bool:
    """Whether the body is a placeholder rather than an implementation.

    `...` for a `Protocol` method or a `@typing.overload`, `pass` for an empty
    hook, and a bare `raise NotImplementedError` for an abstract base. All three
    are *declarations*, and a hundred of them agreeing on shape is the type
    system working rather than a duplication finding. They survive the
    `_body_lines` filter only when somebody writes a multi-statement stub, which
    is rare enough to be worth naming explicitly rather than relying on length.
    """
    if len(body) != 1:
        return False
    only = body[0]
    if isinstance(only, ast.Pass):
        return True
    if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant):
        return only.value.value is Ellipsis
    if isinstance(only, ast.Raise):
        exc = only.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def scan(root: Path, relatives: tuple[str, ...], min_lines: int) -> tuple[list[Group], int]:
    """Group every function body by shape. Returns (groups, functions scanned)."""
    groups: dict[str, list[Site]] = defaultdict(list)
    scanned = 0

    files: list[Path] = []
    for relative in relatives:
        target = root / relative
        if target.is_file():
            files.append(target)
        else:
            files.extend(sorted(target.rglob("*.py")))

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            # A file that cannot be read or parsed contributes no functions. It
            # is not a duplication finding, and failing the scan over one would
            # make the tool unusable on a tree mid-edit.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            body = _significant_body(node)
            if not body or _is_stub(body):
                continue
            span = _body_lines(body)
            if span < min_lines:
                continue
            scanned += 1
            relative_path = str(path.relative_to(root))
            groups[_shape(body)].append(Site(relative_path, node.name, node.lineno, span))

    found = [
        Group(digest, tuple(sorted(sites, key=lambda s: (s.path, s.line))))
        for digest, sites in groups.items()
        if len(sites) > 1
    ]
    found.sort(key=lambda g: (-g.redundant_lines, g.sites[0].path, g.sites[0].line))
    return found, scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-dup-scan",
        description="Report function bodies that share a structure, ranked by "
                    "the lines a collapse would remove. A report, not a gate.",
    )
    parser.add_argument("--path", action="append", metavar="REL",
                        help=f"repo-relative file or directory to scan "
                             f"(repeatable; default {', '.join(DEFAULT_ROOTS)})")
    parser.add_argument("--min-lines", type=int, default=DEFAULT_MIN_LINES,
                        help=f"ignore bodies shorter than this (default {DEFAULT_MIN_LINES})")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help=f"groups to print (default {DEFAULT_TOP}; 0 for all)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = repo_root()
    relatives = tuple(args.path) if args.path else DEFAULT_ROOTS
    groups, scanned = scan(root, relatives, args.min_lines)
    shown = groups if args.top <= 0 else groups[: args.top]

    if args.format == "json":
        print(json.dumps({
            "scanned_functions": scanned,
            "min_lines": args.min_lines,
            "groups": [g.to_json() for g in groups],
        }, indent=2))
        return 0

    print(f"{scanned} function(s) of >= {args.min_lines} lines scanned in "
          f"{', '.join(relatives)}\n")
    if not groups:
        print("no shared structure found.")
        return 0
    print("redundant  copies  group")
    for group in shown:
        first = group.sites[0]
        print(f"{group.redundant_lines:>9}  {len(group.sites):>6}  "
              f"{first.lines} lines each")
        for site in group.sites:
            print(f"{'':>19}{site.path}:{site.line} {site.name}")
    if len(groups) > len(shown):
        print(f"\n... and {len(groups) - len(shown)} more group(s); --top 0 for all.")
    total = sum(g.redundant_lines for g in groups)
    print(f"\nwreath-dup-scan: {len(groups)} group(s), {total} redundant line(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
