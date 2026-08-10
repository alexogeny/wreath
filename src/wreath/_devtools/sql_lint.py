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
reads `wreath/_native/postgres/codec.c` and extracts the type constants its
codec functions actually dispatch on. Adding a codec therefore relaxes this lint
by itself, which is the only arrangement that cannot drift into refusing
something the driver has since learned to send.

Two arrangements were available and only one of them stays a derivation. The
driver could **export** its set at runtime -- a tuple on
`wreath._native._postgres` -- and the lint could import it, which is sturdier to
read but is not sourced from the dispatch: C has no way to enumerate the labels
of a `switch`, so that tuple would be a hand-written list beside the switch, and
adding a codec would mean editing both. That is the arrangement the paragraph
above exists to refuse, moved one file over. So the lint reads the C: it
brace-matches the four codec entry points, blanks their comments and string
literals so no `{` inside `"{1:1.5}/5"` can end a function early, and takes the
`PG_*` constants the `switch (oid)` in each dispatches on. The switch *is* the
registry, so reading it is the only thing that cannot fall out of step with it.
Where the shape it depends on is gone -- a codec function renamed, a switch over
some subject other than `oid` -- it refuses loudly by name rather than deriving
a smaller set and quietly refusing valid SQL.

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

from .native_lint import repo_root, report_findings

DRIVER = "src/wreath/_native/postgres/codec.c"

#: Codec dispatch lives in these four functions; every branch names the type
#: constants the driver can encode or decode. `..._binary_into` writes straight
#: into the send buffer and falls through to `..._binary_value` for everything
#: it does not handle itself, so it can only ever narrow -- it is read anyway,
#: because "the functions that dispatch on an OID" is the rule, and a rule with
#: an exception in it is the thing that drifts.
_CODEC_FUNCTIONS = (
    "wreath_pg_encode_text_value",
    "wreath_pg_encode_binary_value",
    "wreath_pg_encode_binary_into",
    "wreath_pg_decode_value",
)

#: `#define PG_TIMESTAMPTZ 1184`. Only the name is used -- the constants are
#: named for their types -- but matching the OID too is what tells an OID
#: constant apart from the rest of the `PG_`-prefixed macros in the file.
_OID_DEFINE = re.compile(r"^#define[ \t]+(PG_[A-Z0-9_]+)[ \t]+(\d+)[ \t]*$", re.MULTILINE)

_SWITCH_SUBJECT = re.compile(r"\bswitch\s*\(\s*(\w+)\s*\)")
_OID_CASE = re.compile(r"\bcase\s+(PG_\w+)\s*:")
_OID_COMPARISON = re.compile(r"\boid\s*==\s*(PG_\w+)")

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


def _code_only(source: str) -> str:
    """`source` with the inside of every comment and literal blanked out.

    Brace matching a C function has to not see the `{` in `"{1:1.5}/5"` or in a
    comment quoting one, and a `case PG_TEXT:` written inside either is prose
    rather than dispatch. Blanking rather than deleting keeps every offset where
    it was, so line numbers still mean what they did.
    """
    out = list(source)

    def blank(start: int, stop: int) -> None:
        for index in range(start, min(stop, len(source))):
            if source[index] != "\n":
                out[index] = " "

    index, end = 0, len(source)
    while index < end:
        character = source[index]
        if character == "/" and source[index + 1 : index + 2] in ("/", "*"):
            line_comment = source[index + 1] == "/"
            closed = source.find("\n" if line_comment else "*/", index + 2)
            stop = end if closed < 0 else closed + (0 if line_comment else 2)
            blank(index, stop)
            index = stop
        elif character in "\"'":
            cursor = index + 1
            while cursor < end and source[cursor] != character:
                cursor += 2 if source[cursor] == "\\" else 1
            blank(index, cursor + 1)
            index = cursor + 1
        else:
            index += 1
    return "".join(out)


def _function_body(source: str, name: str) -> str:
    """The braced body of C function `name`, definition only.

    A definition starts in column one here; every *call* of these four is
    indented, so anchoring at the line start distinguishes them without needing
    to know a return type.
    """
    signature = re.search(rf"^{re.escape(name)}\s*\(", source, re.MULTILINE)
    if signature is None:
        raise SystemExit(
            f"sql-lint: {DRIVER} no longer defines {name}(), so the encodable type set "
            "would be derived from less than the driver dispatches on"
        )
    start = source.index("{", signature.end())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise SystemExit(f"sql-lint: {name}() in {DRIVER} has an unbalanced body")


def encodable_types(root: Path) -> frozenset[str]:
    """Every PostgreSQL type name the driver's codecs dispatch on.

    Read out of the driver rather than listed here. The constants are named for
    their types (`PG_TIMESTAMPTZ` is `timestamptz`), so the mapping needs no
    table -- and a codec added tomorrow is picked up without editing this file.
    """
    source = _code_only((root / DRIVER).read_text(encoding="utf-8"))
    constants = {name for name, _ in _OID_DEFINE.findall(source)}
    named: set[str] = set()
    for function in _CODEC_FUNCTIONS:
        body = _function_body(source, function)
        # Every one of these dispatches with a single `switch (oid)`. A `case`
        # label under any other subject would name a type the driver does not
        # actually encode -- so rather than guess which switch a label belongs
        # to, refuse and say what changed.
        foreign = {match.group(1) for match in _SWITCH_SUBJECT.finditer(body)} - {"oid"}
        if foreign:
            raise SystemExit(
                f"sql-lint: {function}() in {DRIVER} now switches on "
                f"{', '.join(sorted(foreign))} as well as the OID, so a `case PG_...` in "
                "it no longer means the driver can encode that type; teach this lint "
                "which switch to read"
            )
        named |= set(_OID_CASE.findall(body)) | set(_OID_COMPARISON.findall(body))
    undeclared = named - constants
    if undeclared:
        raise SystemExit(
            f"sql-lint: {DRIVER} dispatches on {', '.join(sorted(undeclared))}, which no "
            "`#define PG_<TYPE> <oid>` in that file declares, so the type name cannot be "
            "read off the constant"
        )
    if not named:
        raise SystemExit(
            f"sql-lint: no codec dispatch was found in {DRIVER}, which would leave every "
            "cast looking encodable and this lint reporting a vacuous zero"
        )
    return frozenset(name.removeprefix("PG_").lower() for name in named)


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


#: Matches the relation named by a `FROM`/`JOIN`/`INTO`/`UPDATE` clause.
_RELATION_REFERENCE = re.compile(
    r'(?:FROM|JOIN|INTO|UPDATE)\s+(?P<ref>"?[A-Za-z_][\w.$"]*)', re.IGNORECASE
)


def owned_relations(root: Path) -> frozenset[str]:
    """Every relation wreath creates *in its own schema*, by unqualified name.

    Read out of the `Component(...)` declarations themselves rather than
    restated here: registering a new component extends this rule automatically,
    which is the discipline `encodable_types` already follows for the driver's
    codecs. A hand-kept list is the next thing to drift, and this rule exists
    precisely because a reference drifting from its schema stays invisible until
    it resolves somewhere wrong.

    Only *qualified* components contribute. Five predate the `wreath` schema and
    use an unqualified `wreath_`-prefixed name deliberately, so demanding a
    schema on those would flag every correct reference in the tree.
    """
    schema_module = root / "src" / "wreath" / "schema.py"
    if "class Component" not in schema_module.read_text(encoding="utf-8"):
        raise SystemExit(
            "sql-lint: wreath/schema.py no longer declares Component, so the "
            "owned-relation set would be empty and SQL003 would check nothing"
        )
    names: set[str] = set()
    for path in sorted((root / "src" / "wreath").rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "Component"):
                continue
            qualified, relations = True, ()
            for keyword in node.keywords:
                if keyword.arg == "schema" and isinstance(keyword.value, ast.Constant):
                    qualified = bool(keyword.value.value)
                elif keyword.arg == "relations" and isinstance(
                    keyword.value, (ast.Tuple, ast.List)
                ):
                    relations = tuple(
                        element.value
                        for element in keyword.value.elts
                        if isinstance(element, ast.Constant)
                        and isinstance(element.value, str)
                    )
            if qualified:
                names.update(relations)
    return frozenset(names)


def _sql_statements(source: str) -> list[tuple[int, str]]:
    """Every string constant that reads like SQL, placeholder or not.

    The same docstring exclusion as `_sql_literals` -- prose about SQL is not
    SQL -- without the `$` requirement, because a statement's table reference is
    there whether or not it binds a parameter.
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
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
        and _SQL_HINT.search(node.value)
    ]


def _scan_qualification(
    where: str, line: int, sql: str, owned: frozenset[str]
) -> list[Finding]:
    """SQL003 -- an unqualified reference to a table wreath owns.

    Wreath's own tables are always written `"wreath"."jobs"` and never resolved
    through `search_path`. That is what lets them compose with an isolated
    tenant session, which binds `search_path` to the tenant's schema alone: an
    unqualified `jobs` there resolves to the tenant's schema, or to nothing,
    rather than to wreath's table.
    """
    findings: list[Finding] = []
    for match in _RELATION_REFERENCE.finditer(sql):
        reference = match.group("ref").strip('"')
        if "." in reference or reference not in owned:
            continue
        findings.append(
            Finding(
                "SQL003", f"{where}:{line}",
                f"{reference!r} is a wreath-owned table referenced without its "
                f'schema; write \'"wreath"."{reference}"\' so it resolves '
                "regardless of search_path, which a tenant session rebinds",
            )
        )
    return findings


def scan(root: Path) -> list[Finding]:
    encodable = encodable_types(root)
    owned = owned_relations(root)
    findings: list[Finding] = []
    package = root / "src" / "wreath"
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        for line, sql in _sql_literals(source):
            findings += _scan_text(relative, line, sql, encodable)
        # SQL003 reads a *wider* set than SQL001/SQL002 deliberately. Those two
        # are about placeholders, so `_sql_literals` is right to require a `$`.
        # This one is about table references, which a statement with no
        # parameters has just as much -- `SELECT count(*) FROM jobs` carries no
        # `$` and is exactly the reference the rule exists to catch. Sharing the
        # narrower collector would have made SQL003 quietly check a subset of
        # what it claims to.
        for line, sql in _sql_statements(source):
            findings += _scan_qualification(relative, line, sql, owned)
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
    return report_findings("wreath-sql-lint", scan(root))


if __name__ == "__main__":
    raise SystemExit(main())
