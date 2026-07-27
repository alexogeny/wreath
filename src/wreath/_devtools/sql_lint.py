"""Catch the parameter whose type PostgreSQL infers from something we cannot send.

PostgreSQL decides a placeholder's type at *prepare* time, from its context: a
cast written on the placeholder, or the type of whatever it is compared against.
The decision is then carried by the prepared statement. So a query whose
parameter is inferred as a type this driver has no encoder for **works on its
first call and fails on every call afterwards** -- the first execution goes out
unprepared, and every later one binds against the inferred type and raises.

That is close to the worst shape a defect can take. It survives every smoke
test, every first-run integration check, and every review; it appears only under
load, or on the second request of a process, or the day a code path finally gets
called twice. Six live instances shipped in `wreath/orm/introspection.py`, where
the catalog is made almost entirely of types outside the driver's set --
`nspname`, `relname` and `attname` are `name`, `atttypid` is `oid`, `contype` is
`"char"`, `indkey` is `int2vector`. The fix in every case was to pin the
parameter with an explicit cast the driver can encode: `n.nspname = $1::text`.

Findings:

* `SQL001` -- a placeholder is cast to a type the driver cannot encode
  (`$1::regclass`). The cast decides the inference, and decides it wrong.
* `SQL002` -- a placeholder is compared against a catalog column whose type the
  driver cannot encode, with no cast on either side. Inference falls to the
  column, with the same result and no syntax to hint at it.

The encodable set is **derived from the driver**, not restated here: this module
reads `wreath/_pure/postgres.py` and extracts the type constants its codec
functions actually dispatch on. Adding a codec therefore relaxes this lint by
itself, which is the only arrangement that cannot drift into refusing something
the driver has since learned to send.

The catalog column types are PostgreSQL's, not wreath's -- `pg_namespace.nspname`
has been `name` for decades and will not change under us. They are listed here
because there is nowhere else to read them from without a live server, and a
lint that needed a database would not run in the gate.

Run it with `uv run wreath-sql-lint`; `0` means clean.
"""

from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .native_lint import repo_root

DRIVER = "src/wreath/_pure/postgres.py"

#: Codec dispatch lives in these three functions; every branch names the type
#: constants the driver can encode or decode.
_CODEC_FUNCTIONS = frozenset({"_encode_text", "_encode_binary", "_decode_value"})

#: PostgreSQL catalog columns whose declared type no driver of this shape can
#: encode. Only columns wreath's own SQL touches are listed -- a lint that
#: enumerated the whole catalog would flag queries nobody here writes.
_CATALOG_COLUMN_TYPES: dict[str, str] = {
    "nspname": "name",
    "relname": "name",
    "attname": "name",
    "conname": "name",
    "typname": "name",
    "proname": "name",
    "amname": "name",
    "collname": "name",
    "datname": "name",
    "rolname": "name",
    "atttypid": "oid",
    "typelem": "oid",
    "relnamespace": "oid",
    "reltype": "oid",
    "conrelid": "oid",
    "confrelid": "oid",
    "indexrelid": "oid",
    "indrelid": "oid",
    "adrelid": "oid",
    "attrelid": "oid",
    "oid": "oid",
    "relkind": "char",
    "contype": "char",
    "typtype": "char",
    "attidentity": "char",
    "attgenerated": "char",
    "indkey": "int2vector",
    "conkey": "int2array",
    "confkey": "int2array",
}

#: SQL spells several of these types more than one way, and the standard
#: spellings are the common ones in hand-written SQL. Normalising here rather
#: than widening the derived set keeps the driver the single source of what can
#: actually be encoded; this table only says which words mean the same type.
_TYPE_ALIASES: dict[str, str] = {
    "int": "int4",
    "integer": "int4",
    "smallint": "int2",
    "bigint": "int8",
    "boolean": "bool",
    "real": "float4",
    "double precision": "float8",
    "decimal": "numeric",
    "character varying": "varchar",
    "character": "text",
    "char": "text",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "time with time zone": "timetz",
    "time without time zone": "time",
}

#: The two-word type names, longest first so `timestamp with time zone` wins
#: over `timestamp`.
_MULTIWORD = "|".join(
    re.escape(name) for name in sorted(_TYPE_ALIASES, key=len, reverse=True) if " " in name
)

#: `$1::text`, `$2 :: bigint`, `$3::int4[]`. A single word unless it is one of
#: the standard multi-word spellings -- matching `[A-Za-z_ ]+` instead ran on
#: past the cast and swallowed the rest of the clause.
_PLACEHOLDER_CAST = re.compile(
    rf"\$\d+\s*::\s*(?:(?P<multi>{_MULTIWORD})|(?P<single>[A-Za-z_][A-Za-z0-9_]*))"
    r"(?P<array>\s*\[\s*\])?",
    re.IGNORECASE,
)

#: `col = $1`, `col <> $2`, `col IN ($3)` -- a bare identifier compared against a
#: placeholder with no cast on either side. A qualifier (`n.nspname`) is allowed.
_BARE_COMPARISON = re.compile(
    r"(?:([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*)?([A-Za-z_][A-Za-z0-9_]*)"
    r"(?!\s*::)\s*(?:=|<>|!=)\s*\$\d+(?!\s*::)"
)

_SQL_HINT = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    code: str
    where: str
    message: str

    def render(self) -> str:
        return f"{self.where}: {self.code} {self.message}"


def encodable_types(root: Path) -> frozenset[str]:
    """Every PostgreSQL type name the driver's codecs dispatch on.

    Read out of the driver rather than listed here. The constants are named for
    their types (`_TIMESTAMPTZ` is `timestamptz`), so the mapping needs no table
    -- and a codec added tomorrow is picked up without editing this file.
    """
    tree = ast.parse((root / DRIVER).read_text(encoding="utf-8"))
    constants = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    named: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in _CODEC_FUNCTIONS:
            continue
        for compare in ast.walk(node):
            if not isinstance(compare, ast.Compare):
                continue
            if not (isinstance(compare.left, ast.Name) and compare.left.id == "oid"):
                continue
            for comparator in compare.comparators:
                candidates = (
                    comparator.elts
                    if isinstance(comparator, (ast.Set, ast.Tuple, ast.List))
                    else [comparator]
                )
                named.update(
                    element.id for element in candidates
                    if isinstance(element, ast.Name) and element.id in constants
                )
    return frozenset(name.lstrip("_").lower() for name in named)


def _sql_literals(source: str) -> list[tuple[int, str]]:
    """Every string constant that reads like SQL carrying a placeholder.

    Docstrings are excluded. Prose about SQL is not SQL, and a rule that could
    not tell the difference would flag this module's own explanation of the
    defect it exists to find -- which it did, on the first run.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    documentation = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in documentation:
            continue
        text = node.value
        if "$" not in text or not _SQL_HINT.search(text):
            continue
        found.append((node.lineno, text))
    return found


def _scan_text(where: str, line: int, sql: str, encodable: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    for match in _PLACEHOLDER_CAST.finditer(sql):
        if match.group("array"):
            # An array cast is a separate question -- the driver's array support
            # is decided by the element codec, and `_infer_oid` owns that path.
            continue
        spelled = (match.group("multi") or match.group("single") or "").strip().lower()
        target = _TYPE_ALIASES.get(spelled, spelled)
        if target and target not in encodable:
            findings.append(
                Finding("SQL001", f"{where}:{line}", f"placeholder cast to {target!r}, which the"
                        " driver cannot encode; the prepared statement carries that inference,"
                        " so this works once and raises on every call after")
            )
    for match in _BARE_COMPARISON.finditer(sql):
        column = match.group(2).lower()
        column_type = _CATALOG_COLUMN_TYPES.get(column)
        if column_type is None or column_type in encodable:
            continue
        findings.append(
            Finding("SQL002", f"{where}:{line}", f"{match.group(0).strip()!r} compares a"
                    f" placeholder against {column!r} ({column_type}), so PostgreSQL infers the"
                    f" parameter as {column_type} -- which the driver cannot encode. Cast it:"
                    f" `{column} = $N::text`")
        )
    return findings


def scan(root: Path) -> list[Finding]:
    encodable = encodable_types(root)
    findings: list[Finding] = []
    package = root / "src" / "wreath"
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        for line, sql in _sql_literals(path.read_text(encoding="utf-8")):
            findings += _scan_text(relative, line, sql, encodable)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-sql-lint",
        description="Report parameters whose PostgreSQL type is inferred as something"
                    " the driver cannot encode.",
    )
    parser.add_argument("--root", default=None, help="repository root (default: detected)")
    parser.add_argument("--show-encodable", action="store_true",
                        help="print the type set derived from the driver and exit")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else repo_root()
    if args.show_encodable:
        print(" ".join(sorted(encodable_types(root))))
        return 0
    findings = scan(root)
    for finding in findings:
        print(finding.render())
    print(f"wreath-sql-lint: {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
