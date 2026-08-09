"""Find duplicated *structure* in the sources, not duplicated text.

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

**Both languages, because both halves are copied from.** Python is normalised
through its AST; C through a token stream, where identifiers, literals and every
comment collapse the same way. Nothing looked at the native tree for a long
time, and it turned out to hold *more* redundancy than the Python tree, not less
-- a four-copy `*_grow`, a five-copy `*_write`, and `read_u32_le` hand-rolled in
four migration modules beside the `wreath_load_u32_le` that already exists in
`wreathcore.h`.

    uv run wreath-dup-scan                       # the default report
    uv run wreath-dup-scan --lang native --near  # the C half, near-copies too
    uv run wreath-dup-scan --min-lines 12        # only bigger bodies
    uv run wreath-dup-scan --top 40 --format json
    uv run wreath-dup-scan --path src/wreath/_devtools

`--near` adds a second pass over pairs that are *almost* the same shape, which
an exact hash cannot see at all: one added statement changes the digest and the
pair disappears. That is how those four `read_u32_le` copies stayed invisible.
It is off by default because it costs a second pass and reads noisier.

**This is a report, and deliberately not a gate.** Plenty of its findings are
legitimate near-twins -- `query`/`mutation`, `insert_settled`/
`upsert_correction`, an SSE2 arm beside its AVX2 twin -- where the shared shape
is the point and collapsing them would cost more clarity than it saves. Wiring
it into `wreath-check` would train everyone to ignore it. Read it when a
subsystem feels repetitive, and when it names something you did not know was
duplicated, that is the finding.

Ranked by *redundant* lines (the copies after the first), because that is what
collapsing the group would actually remove.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import defaultdict, deque
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

#: The languages, and the suffixes each owns.
LANGS: tuple[str, ...] = ("python", "native")
SUFFIXES: dict[str, tuple[str, ...]] = {"python": (".py",), "native": (".c", ".h")}

#: How similar two shapes must be before `--near` calls them a pair. Tuned on
#: this repository: at 0.75 the report is every genuine near-twin and little
#: else, and by 0.62 it is mostly `tp_traverse` stubs agreeing that they visit
#: two fields.
DEFAULT_SIMILARITY = 0.80

#: Shingle width for the near pass. Long enough that a shared `if (x == NULL) {
#: return -1; }` is not a match on its own.
_GRAM = 12

#: Shingles kept per body for candidate lookup. Taking the numerically smallest
#: is a bottom-k sketch: two bodies that share many shingles are overwhelmingly
#: likely to share one of their smallest, so this bounds the pairing to O(n*k)
#: without needing every pair compared. The similarity itself is then computed
#: exactly, on the full shingle sets, so the sketch only ever costs recall.
_SKETCH = 64


def _word(out: bytearray, value: str) -> None:
    raw = value.encode()
    out.extend(len(raw).to_bytes(2))
    out.extend(raw)


def _token(prefix: bytes, value: str) -> bytes:
    """One length-prefixed word, ready to append."""
    out = bytearray(prefix)
    _word(out, value)
    return bytes(out)


#: Per AST class, the constant bytes its generic encoding opens with and the
#: constant bytes preceding each of its fields. A node's class fixes both, so
#: they are built once per class rather than re-encoded for every node -- on
#: this repository that is ~1.4 million `str.encode` and `int.to_bytes` calls
#: saved out of a walk of ~7 MB of shape, and it takes `_structure` over
#: `src/wreath` from 734ms to 324ms.
_GENERIC: dict[type[ast.AST], tuple[bytes, tuple[tuple[bytes, str], ...]]] = {}


def _generic(kind: type[ast.AST]) -> tuple[bytes, tuple[tuple[bytes, str], ...]]:
    names = kind._fields
    head = bytearray(b"A")
    _word(head, kind.__name__)
    head.extend(len(names).to_bytes(2))
    described = (bytes(head), tuple((_token(b"", name), name) for name in names))
    _GENERIC[kind] = described
    return described


def _structure(value: object, out: bytearray) -> None:
    """Append an unambiguous, anonymised AST shape to *out*.

    The former immutable normal form allocated a nested tuple mirroring the
    complete function and then allocated its equally large ``repr`` solely to
    hash it.  Length-prefixed tokens preserve the same equality relation while
    walking the source tree once and allocating one flat buffer.

    The arms are in descending order of how often each kind appears in a real
    body, which is worth doing when the chain runs once per node of every
    function in the tree.
    """
    if isinstance(value, ast.Name):
        out += b"N"
        _structure(value.ctx, out)
        return
    if isinstance(value, ast.Attribute):
        out += b"R"
        _structure(value.value, out)
        _structure(value.ctx, out)
        return
    if isinstance(value, ast.Constant):
        out += b"C"
        return
    if isinstance(value, list):
        out += b"L"
        out += len(value).to_bytes(4)
        for item in value:
            _structure(item, out)
        return
    if isinstance(value, ast.arg):
        # The former transformer constructed a fresh arg with no annotation or
        # type comment, so neither is structural here.
        out += b"G"
        return
    if isinstance(value, ast.keyword):
        out += b"K0" if value.arg is None else b"K1"
        _structure(value.value, out)
        return
    if isinstance(value, ast.AST):
        kind = type(value)
        head, fields = _GENERIC.get(kind) or _generic(kind)
        out += head
        for prefix, name in fields:
            out += prefix
            _structure(getattr(value, name), out)
        return
    out += b"V"
    _word(out, repr(value))


#: One C token. Comments and preprocessor lines are matched so they can be
#: dropped; a `#define` is a declaration, and its body is not this function's.
_C_TOKEN = re.compile(
    r"""(?P<skip>\s+|//[^\n]*|/\*.*?\*/|\#(?:\\\n|[^\n])*)
      | (?P<literal>"(?:\\.|[^"\\])*" | '(?:\\.|[^'\\])*' | \d[\w.]*)
      | (?P<name>[A-Za-z_]\w*)
      | (?P<op><<=|>>=|\.\.\.|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^!~<>=?:;,.(){}\[\]])
    """,
    re.X | re.S,
)

#: C keywords are structure; every other identifier is a name, and a name is
#: exactly what a copy changes.
_C_KEYWORDS = frozenset("""
auto break case char const continue default do double else enum extern float
for goto if inline int long register restrict return short signed sizeof static
struct switch typedef union unsigned void volatile while
""".split())

#: A definition header. The prevailing style here puts the return type on its
#: own line and the name in column one, so both that and the one-line form have
#: to match -- and neither may match an indented `if (...) {`, which is why the
#: pattern is anchored to the start of a line with no leading whitespace.
_C_HEAD = re.compile(
    r"^(?:[A-Za-z_][\w \t*]*[ \t*])?(?P<name>[A-Za-z_]\w*)[ \t]*\((?P<args>[^;{}]*?)\)"
    r"[ \t]*\n?[ \t]*\{",
    re.M | re.S,
)

#: A keyword and an operator encode to the same bytes every time they appear, so
#: the encoding is done once per distinct token rather than once per occurrence.
#: The operator table fills itself: the token pattern's `op` alternation is the
#: closed set of what can land here, and writing it out twice is how the two
#: spellings drift apart.
#:
#: `wreath mutant` reports the `if token is None` miss check below as a
#: survivor, and that is correct rather than a missing test: a memo whose miss
#: path computes exactly what its hit path returns is unobservable from outside
#: by construction, so the only mutant it can carry is an equivalent one. Making
#: it always miss was run against the whole suite and changed nothing.
_C_KEYWORD_TOKENS: dict[str, bytes] = {word: _token(b"K", word) for word in _C_KEYWORDS}
_C_OPERATOR_TOKENS: dict[str, bytes] = {}

#: Control keywords cannot begin a definition, and `#define FOO(x) { ... }` is
#: not one either; the token pattern above has already removed the directive,
#: but a macro *body* left in column one would otherwise read as a header.
_C_NOT_A_DEFINITION = frozenset({"if", "for", "while", "switch", "do", "return",
                                 "else", "sizeof"})


def _c_skip_string(src: str, index: int) -> int:
    """Index just past the quoted run starting at *index*."""
    quote = src[index]
    index += 1
    while index < len(src) and src[index] != quote:
        index += 2 if src[index] == "\\" else 1
    return index


#: The only characters brace matching has to stop on. Everything between two of
#: them is skipped in one C-level jump rather than one interpreted loop step per
#: byte -- 1.9 MB of native bodies is 1.9 MB of steps otherwise, and deleting
#: them is a 6x on this scan's extraction phase (190ms to 32ms over `src/wreath`).
_C_INTERESTING = re.compile(r"""["'{}]|/[*/]""")


def _c_body(src: str, brace: int) -> tuple[str, int]:
    """The text between *brace* and its match, and the index of that match.

    Braces inside strings, character constants and comments are not braces, and
    a scanner that thinks they are walks off the end of the first file holding a
    `"}"`.
    """
    depth = 0
    index = brace
    end = len(src)
    search = _C_INTERESTING.search
    while (match := search(src, index)) is not None:
        index = match.start()
        char = src[index]
        if char in "\"'":
            index = _c_skip_string(src, index) + 1
        elif char == "/":
            if src[index + 1] == "*":
                found = src.find("*/", index)
                index = end if found < 0 else found + 2
            else:
                found = src.find("\n", index)
                index = end if found < 0 else found
        elif char == "{":
            depth += 1
            index += 1
        else:
            depth -= 1
            if depth == 0:
                return src[brace + 1:index], index
            index += 1
    return src[brace + 1:], end


def _c_shape(body: str) -> tuple[bytearray, int]:
    """The anonymised token shape of a C body, and its significant line count.

    Significant lines are those carrying at least one token, so a body's weight
    is its code rather than its comments -- the same ruler `_body_lines` applies
    to Python, and for the same reason.
    """
    shape = bytearray()
    lines = 0
    # Walking the newline count forward with the match position keeps this
    # linear; recounting from the start of the body per token made it quadratic,
    # which on a 4,484-line module is the difference between a report and a wait.
    scanned = 0
    count = body.count
    for match in _C_TOKEN.finditer(body):
        kind = match.lastgroup
        if kind == "skip":
            continue
        if kind == "literal":
            shape += b"C"
        else:
            text = match.group()
            if kind == "name":
                shape += _C_KEYWORD_TOKENS.get(text) or b"I"
            else:
                token = _C_OPERATOR_TOKENS.get(text)
                if token is None:
                    token = _C_OPERATOR_TOKENS[text] = _token(b"O", text)
                shape += token
        start = match.start()
        if count("\n", scanned, start) or not lines:
            lines += 1
        scanned = start
    return shape, lines


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

    def as_dict(self) -> dict:
        return {
            "digest": self.digest,
            "redundant_lines": self.redundant_lines,
            "copies": len(self.sites),
            "sites": [
                {"path": s.path, "name": s.name, "line": s.line, "lines": s.lines}
                for s in self.sites
            ],
        }


def _digest(shape: bytes | bytearray) -> str:
    """A structural digest of a normalised body.

    The immutable tuple walk does not rewrite the parsed tree. An earlier form
    round-tripped through ``ast.parse(ast.unparse(...))`` to get a fresh tree,
    which raised on any body whose first statement cannot stand alone as a
    module -- a bare ``return`` -- and swallowed it in a blanket ``except``.
    Those functions were silently absent from every scan.
    """
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


@dataclass(frozen=True)
class Pair:
    """Two bodies that are *almost* the same shape."""

    left: Site
    right: Site
    similarity: float

    def as_dict(self) -> dict:
        return {
            "similarity": round(self.similarity, 3),
            "left": {"path": self.left.path, "name": self.left.name,
                     "line": self.left.line, "lines": self.left.lines},
            "right": {"path": self.right.path, "name": self.right.name,
                      "line": self.right.line, "lines": self.right.lines},
        }


@dataclass(frozen=True)
class Body:
    """A scanned body: where it is, what shape it has, and that shape's bytes."""

    site: Site
    digest: str
    shape: bytes


def _sources(root: Path, relatives: tuple[str, ...],
             langs: tuple[str, ...]) -> list[tuple[Path, str]]:
    """Every file the scan will read, paired with the language that owns it."""
    wanted = {suffix: lang for lang in langs for suffix in SUFFIXES[lang]}
    files: list[tuple[Path, str]] = []
    for relative in relatives:
        target = root / relative
        if target.is_file():
            if target.suffix in wanted:
                files.append((target, wanted[target.suffix]))
        else:
            files.extend(
                (path, wanted[path.suffix])
                for path in sorted(target.rglob("*"))
                if path.suffix in wanted and path.is_file()
            )
    return files


#: The fields that hold a block of statements, in `_fields` order so the walk
#: below visits definitions in exactly the order `ast.walk` did. `cases` holds
#: `match_case`s and `handlers` holds `ExceptHandler`s, neither of which is a
#: statement, but both carry a `body` and so belong on the queue.
_BLOCKS = ("body", "handlers", "orelse", "finalbody", "cases")


def _definitions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function defined anywhere in *tree*, in source order.

    `ast.walk` answers this too, and did, at ten times the cost: a definition is
    a *statement*, so no expression can contain one, yet the generic walk visits
    every `Name`, `Load` and `Constant` on the way past -- 793,535 nodes over
    `src/wreath` to reach 7,308 functions. Descending only the statement blocks
    reaches the same 7,308 in the same order for 69ms against 758ms.

    `tests/test_dup_scan.py::test_a_definition_is_found_inside_every_kind_of_block`
    is what keeps `_BLOCKS` complete; a block missing from it makes the scan
    quietly stop reading part of every file that uses it.
    """
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    todo = deque([tree])
    while todo:
        node = todo.popleft()
        for name in _BLOCKS:
            block = getattr(node, name, None)
            if block:
                todo.extend(block)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(node)
    return found


def _python_bodies(path: Path, relative: str, min_lines: int) -> list[Body]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        # A file that cannot be read or parsed contributes no functions. It is
        # not a duplication finding, and failing the scan over one would make the
        # tool unusable on a tree mid-edit.
        return []
    bodies = []
    for node in _definitions(tree):
        body = _significant_body(node)
        if not body or _is_stub(body):
            continue
        span = _body_lines(body)
        if span < min_lines:
            continue
        shape = bytearray()
        _structure(body, shape)
        bodies.append(Body(Site(relative, node.name, node.lineno, span),
                           _digest(shape), bytes(shape)))
    return bodies


def _native_bodies(path: Path, relative: str, min_lines: int) -> list[Body]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    bodies = []
    for match in _C_HEAD.finditer(source):
        name = match.group("name")
        if name in _C_NOT_A_DEFINITION:
            continue
        brace = source.index("{", match.end() - 1)
        body, _ = _c_body(source, brace)
        shape, lines = _c_shape(body)
        if lines < min_lines:
            continue
        line = source.count("\n", 0, match.start()) + 1
        bodies.append(Body(Site(relative, name, line, lines),
                           _digest(shape), bytes(shape)))
    return bodies


def collect(root: Path, relatives: tuple[str, ...], min_lines: int,
            langs: tuple[str, ...] = LANGS) -> list[Body]:
    """Every body worth comparing, in both languages."""
    read = {"python": _python_bodies, "native": _native_bodies}
    return [
        body
        for path, lang in _sources(root, relatives, langs)
        for body in read[lang](path, str(path.relative_to(root)), min_lines)
    ]


def scan(root: Path, relatives: tuple[str, ...], min_lines: int,
         langs: tuple[str, ...] = LANGS) -> tuple[list[Group], int]:
    """Group every function body by shape. Returns (groups, functions scanned)."""
    bodies = collect(root, relatives, min_lines, langs)
    groups: dict[str, list[Site]] = defaultdict(list)
    for body in bodies:
        groups[body.digest].append(body.site)

    found = [
        Group(digest, tuple(sorted(sites, key=lambda s: (s.path, s.line))))
        for digest, sites in groups.items()
        if len(sites) > 1
    ]
    found.sort(key=lambda g: (-g.redundant_lines, g.sites[0].path, g.sites[0].line))
    return found, len(bodies)


def _sketch(shape: bytes) -> tuple[int, ...]:
    """The `_SKETCH` numerically smallest shingle hashes of a shape.

    Shingles are sliding *byte* windows over the normal form rather than token
    windows, which needs no second normaliser and costs nothing: a window inside
    an unchanged run still matches exactly however much shifted around it, which
    is the whole property the near pass rests on.

    The hash is a rolling polynomial rather than `hash()`, because `hash(bytes)`
    is salted per process -- which would make the sketch, and so the report,
    different on every run of the same tree.
    """
    if len(shape) <= _GRAM:
        return ()
    mask = (1 << 64) - 1
    base = 1000003
    drop = pow(base, _GRAM, 1 << 64)
    rolling = 0
    for byte in shape[:_GRAM]:
        rolling = (rolling * base + byte) & mask
    shingles = {rolling}
    for index in range(_GRAM, len(shape)):
        rolling = (rolling * base - shape[index - _GRAM] * drop + shape[index]) & mask
        shingles.add(rolling)
    return tuple(sorted(shingles)[:_SKETCH])


def _similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Jaccard, estimated from two bottom-k sketches.

    The smallest `k` hashes of the union are a uniform sample of the union, so
    the share of them present in both sketches estimates the share of the union
    present in both sets. When a body is small enough that its sketch is its
    whole shingle set, this is not an estimate but the exact answer.
    """
    union = sorted(set(left) | set(right))[:_SKETCH]
    if not union:
        return 0.0
    both = set(left) & set(right)
    return sum(1 for shingle in union if shingle in both) / len(union)


def near_clones(root: Path, relatives: tuple[str, ...], min_lines: int,
                langs: tuple[str, ...] = LANGS,
                similarity: float = DEFAULT_SIMILARITY) -> list[Pair]:
    """Bodies that are almost, but not exactly, the same shape.

    Exact grouping cannot see these at all -- one added statement changes the
    digest and the pair vanishes -- and they are where the tree's most useful
    findings have been: a primitive re-implemented beside the real one, with a
    line of local adaptation on top.

    Pairs already grouped as exact copies are excluded, because reporting a
    finding twice is how a report stops being read.
    """
    bodies = collect(root, relatives, min_lines, langs)
    sketches = [_sketch(body.shape) for body in bodies]

    # Two bodies sharing many shingles are overwhelmingly likely to share one of
    # their smallest, so an index over the sketches finds the candidates without
    # comparing every pair.
    index: dict[int, list[int]] = defaultdict(list)
    for position, sketch in enumerate(sketches):
        for shingle in sketch:
            index[shingle].append(position)

    candidates: set[tuple[int, int]] = set()
    for sharing in index.values():
        if len(sharing) > _SKETCH:
            # A shingle common to more shapes than a sketch holds is boilerplate
            # -- a prologue every body in a module opens with -- and pairing on
            # it alone would cost a quadratic blow-up for no finding.
            continue
        candidates.update(
            (sharing[i], sharing[j])
            for i in range(len(sharing))
            for j in range(i + 1, len(sharing))
        )

    pairs = []
    for left, right in candidates:
        if bodies[left].digest == bodies[right].digest:
            continue
        score = _similarity(sketches[left], sketches[right])
        if score >= similarity:
            first, second = sorted((bodies[left].site, bodies[right].site),
                                   key=lambda s: (s.path, s.line))
            pairs.append(Pair(first, second, score))
    pairs.sort(key=lambda p: (-min(p.left.lines, p.right.lines), -p.similarity,
                              p.left.path, p.left.line))
    return pairs


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
    parser.add_argument("--lang", choices=("all", *LANGS), default="all",
                        help="which half of the tree to scan (default all)")
    parser.add_argument("--near", action="store_true",
                        help="also report pairs that are almost the same shape, "
                             "which exact grouping cannot see")
    parser.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY,
                        help=f"how alike a --near pair must be "
                             f"(default {DEFAULT_SIMILARITY})")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = repo_root()
    relatives = tuple(args.path) if args.path else DEFAULT_ROOTS
    langs = LANGS if args.lang == "all" else (args.lang,)
    groups, scanned = scan(root, relatives, args.min_lines, langs)
    pairs = (near_clones(root, relatives, args.min_lines, langs, args.similarity)
             if args.near else [])
    shown = groups if args.top <= 0 else groups[: args.top]

    if args.format == "json":
        print(json.dumps({
            "scanned_functions": scanned,
            "min_lines": args.min_lines,
            "langs": list(langs),
            "groups": [g.as_dict() for g in groups],
            "near": [p.as_dict() for p in pairs],
        }, indent=2))
        return 0

    print(f"{scanned} function(s) of >= {args.min_lines} lines scanned in "
          f"{', '.join(relatives)}\n")
    if not groups:
        print("no shared structure found.")
    else:
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

    if args.near:
        near_shown = pairs if args.top <= 0 else pairs[: args.top]
        print(f"\nnear copies (>= {args.similarity:.2f} alike, not exact)")
        for pair in near_shown:
            print(f"{pair.similarity:>9.2f}  {pair.left.path}:{pair.left.line} "
                  f"{pair.left.name}  <->  {pair.right.path}:{pair.right.line} "
                  f"{pair.right.name}")
        if len(pairs) > len(near_shown):
            print(f"\n... and {len(pairs) - len(near_shown)} more pair(s); "
                  f"--top 0 for all.")
        print(f"\nwreath-dup-scan: {len(pairs)} near pair(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
