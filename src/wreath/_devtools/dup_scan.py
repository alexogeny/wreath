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
import copy
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .native_lint import repo_root

#: Where the scan runs when no `--path` is given.
DEFAULT_ROOTS: tuple[str, ...] = ("src/wreath",)

#: Bodies shorter than this are trivia. Two four-line functions that both unpack
#: a tuple and return are not a duplication finding, they are Python.
DEFAULT_MIN_LINES = 8

#: How many groups the text report prints before it stops being read.
DEFAULT_TOP = 25


class _Anonymise(ast.NodeTransformer):
    """Erase every name, attribute, argument and literal; keep the structure."""

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        return ast.copy_location(ast.Attribute(value=node.value, attr="_", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        return ast.copy_location(ast.arg(arg="_", annotation=None), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return ast.copy_location(ast.Constant(value=None), node)

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        self.generic_visit(node)
        return ast.copy_location(ast.keyword(arg=None if node.arg is None else "_",
                                             value=node.value), node)


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

    The statements are deep-copied before normalising: `NodeTransformer`
    rewrites in place, and this walks the same tree the caller is still
    iterating. (An earlier form of this tool round-tripped through
    `ast.parse(ast.unparse(...))` to get a fresh tree, which raised on any body
    whose first statement cannot stand alone as a module -- a bare `return` --
    and swallowed it in a blanket `except`. Those functions were silently
    absent from every scan.)
    """
    anonymised = [_Anonymise().visit(copy.deepcopy(stmt)) for stmt in body]
    dumped = "".join(ast.dump(stmt) for stmt in anonymised)
    return hashlib.blake2s(dumped.encode(), digest_size=12).hexdigest()


def _significant_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """The body without its docstring -- prose is not structure."""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return body


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
            if not body:
                continue
            span = (node.end_lineno or node.lineno) - node.lineno + 1
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
