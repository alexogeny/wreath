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
    uv run wreath-dup-scan --fragments --min-tokens 50
    uv run wreath-dup-scan --normalization alpha --summary
    uv run wreath-dup-scan --context 2 --format json
    uv run wreath-dup-scan --min-lines 12        # only bigger bodies
    uv run wreath-dup-scan --top 40 --format json
    uv run wreath-dup-scan --path src/wreath/_devtools

`--near` adds a second pass over pairs that are *almost* the same shape, which
an exact hash cannot see at all: one added statement changes the digest and the
pair disappears. That is how those four `read_u32_le` copies stayed invisible.
It is off by default because it costs a second pass and reads noisier.

`--fragments` finds a copied interior even when the containing functions have
different prefixes and suffixes. Its tokenizer, rolling-window index and
maximal-match extension are one operation-local C pass; Python retains source
discovery, function boundaries and reporting. Both `--min-lines` and
`--min-tokens` must clear, which stops a long expression or a run of tiny lines
from gaming either ruler. `--normalization alpha` preserves identifier-use
relationships, attributes and literals while still accepting consistent local
renames. JSON reports exact source ranges, optional `--context`, and every file
that was discovered, scanned or skipped. `--summary` aggregates duplicate
involvement by file and directory.

**This is a report, and deliberately not a gate.** Plenty of its findings are
legitimate near-twins -- `query`/`mutation`, `insert_settled`/
`upsert_correction`, an SSE2 arm beside its AVX2 twin -- where the shared shape
is the point and collapsing them would cost more clarity than it saves. Those
are filtered only by an exact, named set of sites in `INTENTIONAL_GROUPS`, with
a reason. Exact membership is load-bearing: if another copy joins one of those
shapes, the larger group no longer matches the exception and is reported.
Wiring the report into `wreath-check` would train everyone to ignore it. Read it
when a subsystem feels repetitive, and when it names something you did not know
was duplicated, that is the finding.

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
from dataclasses import dataclass, field
from pathlib import Path

from .._native import _dupscan
from .native_lint import repo_root

_fragment_scan = _dupscan.scan

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
NORMALIZATIONS: tuple[str, ...] = ("shape", "alpha")

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


def _alpha_structure(
    value: object,
    out: bytearray,
    names: dict[str, int],
) -> None:
    """Append a higher-precision shape that preserves name relationships.

    Local names are numbered by first appearance, so a consistent rename keeps
    the same image while ``combine(value, value)`` remains different from
    ``combine(value, other)``. Attributes, keyword names and literal values stay
    visible: in those positions the spelling commonly *is* the operation rather
    than an incidental local chosen by the author.
    """
    if isinstance(value, ast.Name):
        out += b"N"
        canonical = names.setdefault(value.id, len(names))
        out += canonical.to_bytes(4)
        _alpha_structure(value.ctx, out, names)
        return
    if isinstance(value, ast.Attribute):
        out += b"R"
        _alpha_structure(value.value, out, names)
        _word(out, value.attr)
        _alpha_structure(value.ctx, out, names)
        return
    if isinstance(value, ast.Constant):
        out += b"C"
        _word(out, type(value.value).__name__)
        _word(out, repr(value.value))
        return
    if isinstance(value, list):
        out += b"L"
        out += len(value).to_bytes(4)
        for item in value:
            _alpha_structure(item, out, names)
        return
    if isinstance(value, ast.arg):
        out += b"G"
        canonical = names.setdefault(value.arg, len(names))
        out += canonical.to_bytes(4)
        return
    if isinstance(value, ast.keyword):
        out += b"K0" if value.arg is None else b"K1"
        if value.arg is not None:
            _word(out, value.arg)
        _alpha_structure(value.value, out, names)
        return
    if isinstance(value, ast.AST):
        kind = type(value)
        head, fields = _GENERIC.get(kind) or _generic(kind)
        out += head
        for prefix, name in fields:
            out += prefix
            _alpha_structure(getattr(value, name), out, names)
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
_C_KEYWORDS = frozenset(
    """
auto break case char const continue default do double else enum extern float
for goto if inline int long register restrict return short signed sizeof static
struct switch typedef union unsigned void volatile while
""".split()
)

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
_C_NOT_A_DEFINITION = frozenset({"if", "for", "while", "switch", "do", "return", "else", "sizeof"})


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
                return src[brace + 1 : index], index
            index += 1
    return src[brace + 1 :], end


def _c_shape(
    body: str,
    normalization: str = "shape",
) -> tuple[bytearray, int]:
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
    names: dict[str, int] = {}
    attribute_follows = False
    for match in _C_TOKEN.finditer(body):
        kind = match.lastgroup
        if kind == "skip":
            continue
        if kind == "literal":
            if normalization == "alpha":
                shape += _token(b"C", match.group())
            else:
                shape += b"C"
            attribute_follows = False
        else:
            text = match.group()
            if kind == "name":
                keyword = _C_KEYWORD_TOKENS.get(text)
                if keyword is not None:
                    shape += keyword
                elif normalization == "shape":
                    shape += b"I"
                elif attribute_follows:
                    shape += _token(b"A", text)
                else:
                    canonical = names.setdefault(text, len(names))
                    shape += b"I" + canonical.to_bytes(4)
                attribute_follows = False
            else:
                token = _C_OPERATOR_TOKENS.get(text)
                if token is None:
                    token = _C_OPERATOR_TOKENS[text] = _token(b"O", text)
                shape += token
                attribute_follows = text in {".", "->"}
        start = match.start()
        if count("\n", scanned, start) or not lines:
            lines += 1
        scanned = start
    return shape, lines


@dataclass(frozen=True, slots=True)
class Site:
    """One function body, and where to find it."""

    path: str
    name: str
    line: int
    lines: int
    qualname: str = ""
    body_start: int = 0
    body_end: int = 0

    @property
    def identity_name(self) -> str:
        """Qualified Python name, or the native/top-level spelling."""
        return self.qualname or self.name

    def as_dict(self, root: Path | None = None, context: int = 0) -> dict:
        """A stable location, optionally enriched with its exact source range."""
        start = self.body_start or self.line
        end = self.body_end or start + self.lines - 1
        result = {
            "path": self.path,
            "name": self.identity_name,
            "line": self.line,
            "lines": self.lines,
            "start_line": start,
            "end_line": end,
        }
        if root is not None and context >= 0:
            try:
                source_lines = (root / self.path).read_text(encoding="utf-8").splitlines()
            except OSError, UnicodeError:
                result["source"] = ""
            else:
                first = max(1, start - context)
                last = min(len(source_lines), end + context)
                result["source"] = "\n".join(source_lines[first - 1 : last])
        return result


@dataclass(frozen=True)
class Group:
    """Bodies that share a normalised shape."""

    digest: str
    sites: tuple[Site, ...]

    @property
    def redundant_lines(self) -> int:
        """Lines a collapse would remove: everything after the first copy."""
        return sum(site.lines for site in self.sites[1:])

    def as_dict(self, root: Path | None = None, context: int = 0) -> dict:
        return {
            "digest": self.digest,
            "redundant_lines": self.redundant_lines,
            "copies": len(self.sites),
            "sites": [s.as_dict(root, context) for s in self.sites],
        }


@dataclass(frozen=True)
class Exclusion:
    """One exact group whose parallel structure is an asserted design choice."""

    sites: tuple[tuple[str, str], ...]
    reason: str


def _exclusion(reason: str, *sites: tuple[str, str]) -> Exclusion:
    return Exclusion(tuple(sorted(sites)), reason)


# These are deliberately site-specific rather than path or name patterns. A
# directory exemption would hide the next real copy in the noisiest parts of
# the tree; an exact set stops matching as soon as a new site joins the group.
INTENTIONAL_GROUPS: tuple[Exclusion, ...] = (
    _exclusion(
        "gzip encode and decode deliberately own independent ISA kernels so neither "
        "direction pulls the other algorithm into its instruction or cache footprint",
        ("src/wreath/_native/gzip/decode/crc32_pclmul.c", "wreath_gzip_decoder_crc32_pclmul"),
        ("src/wreath/_native/gzip/encode/crc32_pclmul.c", "wreath_gzip_encoder_crc32_pclmul"),
    ),
    _exclusion(
        "gzip encode and decode deliberately own independent ISA kernels so neither "
        "direction pulls the other algorithm into its instruction or cache footprint",
        ("src/wreath/_native/gzip/decode/crc32_vpclmul.c", "wreath_gzip_decoder_crc32_vpclmul"),
        ("src/wreath/_native/gzip/encode/crc32_vpclmul.c", "wreath_gzip_encoder_crc32_vpclmul"),
    ),
    _exclusion(
        "gzip encode and decode keep separate runtime dispatch contracts; sharing this "
        "small query would couple otherwise independent codec implementations",
        ("src/wreath/_native/gzip/decode/crc32.c", "wreath_gzip_decoder_crc32_arm_available"),
        ("src/wreath/_native/gzip/encode/crc32.c", "wreath_gzip_encoder_crc32_arm_available"),
    ),
    _exclusion(
        "HTTP-client and PostgreSQL awaitables live in separate extensions with distinct "
        "object layouts; direct field access avoids a generic offset or callback layer",
        ("src/wreath/_native/client_http1.c", "client_request_result"),
        ("src/wreath/_native/postgres/pipeline.c", "statement_completion_result"),
    ),
    _exclusion(
        "HTTP-client and PostgreSQL awaitables live in separate extensions with distinct "
        "object layouts; direct field access avoids a generic offset or callback layer",
        ("src/wreath/_native/client_http1.c", "client_request_exception"),
        ("src/wreath/_native/postgres/pipeline.c", "statement_completion_exception"),
    ),
    _exclusion(
        "normalisation erases the different capsule layouts and owned fields released by "
        "these unrelated destructors",
        ("src/wreath/_native/activate.c", "request_layout_free"),
        ("src/wreath/_native/cedar.c", "cedar_decision_batch_destroy"),
    ),
    _exclusion(
        "object-store read and list preserve distinct streaming APIs; a shared async "
        "generator would add an await/yield layer to every observed item",
        ("src/wreath/_replay_adapters.py", "ObservedObjectStore.read_stream"),
        ("src/wreath/_replay_adapters.py", "ObservedObjectStore.list"),
    ),
    _exclusion(
        "scaffold functions emit unrelated literal artifacts; normalisation erases "
        "the literal content that is their entire behaviour",
        *(
            ("src/wreath/_scaffold.py", name)
            for name in ("_tenants", "_tenancy_tests", "_web_package_json", "_web_index_html")
        ),
    ),
    _exclusion(
        "scaffold functions emit unrelated literal artifacts; normalisation erases "
        "the literal content that is their entire behaviour",
        *(
            ("src/wreath/_scaffold.py", name)
            for name in ("_contracts", "_memory_adapter", "_web_tsconfig", "_web_main")
        ),
    ),
    _exclusion(
        "scaffold functions emit unrelated literal artifacts; normalisation erases "
        "the literal content that is their entire behaviour",
        ("src/wreath/_scaffold.py", "_model_tests"),
        ("src/wreath/_scaffold.py", "_web_app"),
    ),
    _exclusion(
        "scaffold functions emit unrelated literal artifacts; normalisation erases "
        "the literal content that is their entire behaviour",
        ("src/wreath/_scaffold.py", "_agents"),
        ("src/wreath/_scaffold.py", "_models"),
    ),
    _exclusion(
        "the compiled JSON converter closes over annotation-specific child converters; "
        "the Any converter is its independent recursive base case and hot-path oracle",
        ("src/wreath/binding.py", "_jsonable_any"),
        ("src/wreath/binding.py", "_compile_jsonable.<locals>.jsonable_value"),
    ),
    _exclusion(
        "fixed Flight cell kinds have distinct ABI layouts and straight-line ingest "
        "loops; a callback or tagged generic loop would add work per cell",
        *(
            ("src/wreath/_native/flight_project.c", name)
            for name in (
                "flight_ingest_completion",
                "flight_ingest_correlation",
                "flight_ingest_client_facts",
            )
        ),
    ),
    _exclusion(
        "SSE2 and AVX2 are separate measured implementations selected by per-call dispatch",
        ("src/wreath/_native/simd.h", "wreath_find_sse2"),
        ("src/wreath/_native/simd.h", "wreath_find_avx2"),
    ),
    _exclusion(
        "reader and writer registration are symmetric reactor operations over distinct "
        "kernel filters; combining them would hide the selected filter in a branch",
        ("src/wreath/_native/reactor_poller.c", "rp_add_reader"),
        ("src/wreath/_native/reactor_poller.c", "rp_add_writer"),
    ),
    _exclusion(
        "normalisation erases mapping keys and attribute names, making unrelated status "
        "snapshots look identical",
        ("src/wreath/doctor.py", "TracedRequest.as_dict"),
        ("src/wreath/inspector.py", "_projector_loss"),
        ("src/wreath/jobs.py", "JobRunner.stats"),
        ("src/wreath/messaging.py", "MessageBus.stats"),
    ),
    _exclusion(
        "conjunction and disjunction are separate precedence levels in the SCIM grammar; "
        "their mirrored recursive-descent shape makes that precedence visible",
        ("src/wreath/_native/scim.c", "scim_conjunction"),
        ("src/wreath/_native/scim.c", "scim_disjunction"),
    ),
    _exclusion(
        "SSE2 and AVX2 are separate measured implementations selected by per-call dispatch",
        ("src/wreath/_native/simd.h", "wreath_json_run_sse2"),
        ("src/wreath/_native/simd.h", "wreath_json_run_avx2"),
    ),
    _exclusion(
        "policy describe methods populate the shared MiddlewareContract with independent "
        "header semantics; normalisation erases those declarations",
        ("src/wreath/policy/compression.py", "CompressionPolicy.describe"),
        ("src/wreath/policy/cors.py", "CorsPolicy.describe"),
    ),
    _exclusion(
        "the optional HTTP/3 and server extensions resolve different recorder owner "
        "types and cannot share a linked implementation",
        ("src/wreath/_native/http3_connection.c", "wreath_h3_worker_from"),
        ("src/wreath/_native/server_common.c", "wreath_flight_worker_from"),
    ),
    _exclusion(
        "these are iterator protocols for unrelated native object layouts; their common "
        "shape is CPython's StopIteration/error contract",
        ("src/wreath/_native/queue.c", "queue_value_next"),
        ("src/wreath/_native/server_common.c", "value_awaitable_next"),
    ),
    _exclusion(
        "SSE2 and AVX2 are separate measured implementations selected by per-call dispatch",
        ("src/wreath/_native/simd.h", "wreath_html_run_sse2"),
        ("src/wreath/_native/simd.h", "wreath_html_run_avx2"),
    ),
    _exclusion(
        "FIFO queue and deadline heap drains are layout-specialized hot loops; a common "
        "callback loop would add an indirect call per item",
        ("src/wreath/_native/queue.c", "queue_drain"),
        ("src/wreath/_native/queue.c", "heap_drain"),
    ),
    _exclusion(
        "SSE2 and AVX2 are separate measured implementations selected by per-call dispatch",
        ("src/wreath/_native/simd.h", "wreath_dkim_run_sse2"),
        ("src/wreath/_native/simd.h", "wreath_dkim_run_avx2"),
    ),
    _exclusion(
        "the strict step and attempt wrappers already layer type-specific diagnostics over "
        "the canonical _read_one_recording implementation",
        ("src/wreath/_recording_format.py", "read_step_recording"),
        ("src/wreath/_recording_format.py", "read_attempt_recording"),
    ),
    _exclusion(
        "the attempt and data-kernel readers own different binary formats and error "
        "vocabularies; their common bounds-check shape is the wire-decoder contract",
        ("src/wreath/_native/codecs.c", "read_attempt_text"),
        ("src/wreath/_native/data_kernels.c", "data_read_text"),
    ),
    _exclusion(
        "reader and writer removal are symmetric reactor operations over distinct kernel "
        "filters; combining them would hide the selected filter in a branch",
        ("src/wreath/_native/reactor_poller.c", "rp_remove_reader"),
        ("src/wreath/_native/reactor_poller.c", "rp_remove_writer"),
    ),
    _exclusion(
        "SSE2 and AVX2 are separate measured implementations selected by per-call dispatch",
        ("src/wreath/_native/simd.h", "wreath_value_run_sse2"),
        ("src/wreath/_native/simd.h", "wreath_value_run_avx2"),
    ),
    _exclusion(
        "the two migration refusals own different hazards and recovery instructions; "
        "normalisation erases the diagnostic text that distinguishes them",
        ("src/wreath/migrations.py", "DowngradeWouldStrandCode.__init__"),
        ("src/wreath/migrations.py", "DowngradeWouldStrandRecodedData.__init__"),
    ),
    _exclusion(
        "sparse-vector indices and values require different CPython constructors in a "
        "per-element hot loop; a converter callback would add an indirect call per item",
        ("src/wreath/_native/codecs.c", "wreath_sparsevector_indices"),
        ("src/wreath/_native/codecs.c", "wreath_sparsevector_values"),
    ),
    _exclusion(
        "FIFO queue and deadline heap waiting slots are layout-specialized; sharing would "
        "replace direct field access with layout branches",
        ("src/wreath/_native/queue.c", "queue_set_waiting"),
        ("src/wreath/_native/queue.c", "heap_set_waiting"),
    ),
    _exclusion(
        "io_uring start and stop deliberately mirror the registration lifecycle while "
        "calling opposite kernel operations",
        ("src/wreath/_native/reactor_poller.c", "rp_start_uring_receive"),
        ("src/wreath/_native/reactor_poller.c", "rp_stop_uring_receive"),
    ),
    _exclusion(
        "IPv4 and IPv6 indexes use different integer widths and record layouts; a common "
        "inner loop would add width/layout branches per bucket",
        ("src/wreath/_native/client_facts.c", "geo_index_v4"),
        ("src/wreath/_native/client_facts.c", "geo_index_v6"),
    ),
    _exclusion(
        "Prometheus escaping targets the sizing buffer in one pass and the final bytes "
        "writer in another; an adapter callback would add work per escaped run",
        ("src/wreath/_native/observability.c", "prom_label_buffer_value"),
        ("src/wreath/_native/observability.c", "prom_write_label_value"),
    ),
    _exclusion(
        "SSE2 and AVX2 are separate measured implementations selected by per-call dispatch",
        ("src/wreath/_native/simd.h", "wreath_xor_mask_sse2"),
        ("src/wreath/_native/simd.h", "wreath_xor_mask_avx2"),
    ),
    _exclusion(
        "JSON and MessagePack are separately built extension modules; the matching wrapper "
        "shape is their shared Python codec protocol, not shared encoding logic",
        ("src/wreath/_native/json.c", "wreath_json_dumps"),
        ("src/wreath/_native/msgpack.c", "wreath_msgpack_dumps"),
    ),
    _exclusion(
        "FIFO queue and deadline heap close paths are layout-specialized and wake different "
        "waiter structures",
        ("src/wreath/_native/queue.c", "queue_close"),
        ("src/wreath/_native/queue.c", "heap_close"),
    ),
    _exclusion(
        "normalisation erases mapping keys and attribute names, making unrelated report "
        "serializers look identical",
        ("src/wreath/_port/ir.py", "Finding.as_dict"),
        ("src/wreath/doctor.py", "TracedWork.as_dict"),
    ),
    _exclusion(
        "a Flight slab decoder and an HTTP signature builder are unrelated; normalisation "
        "erases the callees and constants that define both operations",
        ("src/wreath/_flight_schema.py", "CaptureSlab.decode"),
        ("src/wreath/signatures.py", "signature_base"),
    ),
    _exclusion(
        "these capsule destructors own unrelated layouts; the common shape is CPython's "
        "capsule validation and cleanup protocol",
        ("src/wreath/_native/graphql.c", "graphql_policy_state_destroy"),
        ("src/wreath/_native/sparse_vector.h", "wreath_sparse_vector_destroy"),
    ),
    _exclusion(
        "GC traverse and clear must enumerate the same plan fields in the same order; their "
        "parallel shape is an ownership invariant",
        ("src/wreath/_native/postgres/plan.c", "plan_traverse"),
        ("src/wreath/_native/postgres/plan.c", "plan_clear"),
    ),
    _exclusion(
        "FIFO queue and deadline heap clear paths are layout-specialized and release "
        "different storage representations",
        ("src/wreath/_native/queue.c", "queue_clear"),
        ("src/wreath/_native/queue.c", "heap_clear"),
    ),
    _exclusion(
        "reader and writer callback removal bind different event-loop methods; the mirrored "
        "shape is the transport's explicit duplex protocol",
        ("src/wreath/_native/reactor_transport.c", "st_remove_reader_cb"),
        ("src/wreath/_native/reactor_transport.c", "st_remove_writer_cb"),
    ),
    _exclusion(
        "these porting helpers copy unrelated framework metadata; normalisation erases the "
        "field names and target API calls that distinguish them",
        ("src/wreath/_port/analyzer/models.py", "_config_extra"),
        ("src/wreath/_port/emit/ormar.py", "_copy_tablename"),
    ),
    _exclusion(
        "trace and log wrappers declare signal-specific types and fields over the shared "
        "_export_projected transport; normalisation erases those declarations",
        ("src/wreath/_export.py", "OtlpHttpExporter.export_projected_logs"),
        ("src/wreath/_export.py", "OtlpHttpExporter.export_projected_traces"),
    ),
)


def intentional_reason(group: Group) -> str | None:
    """The reason `group` is filtered, only when its full site set matches."""
    sites = tuple(sorted((site.path, site.identity_name) for site in group.sites))
    for exclusion in INTENTIONAL_GROUPS:
        if sites == exclusion.sites:
            return exclusion.reason
    return None


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

    def as_dict(self, root: Path | None = None, context: int = 0) -> dict:
        return {
            "similarity": round(self.similarity, 3),
            "left": self.left.as_dict(root, context),
            "right": self.right.as_dict(root, context),
        }


@dataclass(frozen=True, slots=True)
class Body:
    """A scanned body: where it is, what shape it has, and that shape's bytes."""

    site: Site
    digest: str
    shape: bytes
    fragment_source: bytes = b""
    fragment_start: int = 0
    language: int = 0


@dataclass(frozen=True)
class Fragment:
    """One maximal equal token run inside two otherwise distinct bodies."""

    left: Site
    right: Site
    tokens: int

    @property
    def lines(self) -> int:
        return min(self.left.lines, self.right.lines)

    def as_dict(self, root: Path | None = None, context: int = 0) -> dict:
        return {
            "lines": self.lines,
            "tokens": self.tokens,
            "left": self.left.as_dict(root, context),
            "right": self.right.as_dict(root, context),
        }


@dataclass(frozen=True)
class SkippedSource:
    path: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass
class Coverage:
    """What one CLI scan did and did not manage to read."""

    discovered_files: int = 0
    scanned_files: int = 0
    skipped_files: list[SkippedSource] = field(default_factory=list)

    def skip(self, path: str, reason: str) -> None:
        self.skipped_files.append(SkippedSource(path, reason))

    def as_dict(self) -> dict:
        return {
            "discovered_files": self.discovered_files,
            "scanned_files": self.scanned_files,
            "skipped_files": [item.as_dict() for item in self.skipped_files],
        }


def _sources(
    root: Path, relatives: tuple[str, ...], langs: tuple[str, ...]
) -> list[tuple[Path, str]]:
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


def _definitions(
    tree: ast.Module,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
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
    found: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
    todo = deque([(tree, "")])
    while todo:
        node, parent = todo.popleft()
        child_parent = parent
        if isinstance(node, ast.ClassDef):
            child_parent = f"{parent}.{node.name}" if parent else node.name
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            qualified = f"{parent}.{node.name}" if parent else node.name
            found.append((node, qualified))
            child_parent = f"{qualified}.<locals>"
        for name in _BLOCKS:
            block = getattr(node, name, None)
            if block:
                todo.extend((item, child_parent) for item in block)
    return found


def _scope_catalog(
    tree: ast.Module,
) -> tuple[
    list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]],
    dict[int, list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]],
]:
    """Definitions plus direct child scopes from one statement-block walk."""
    found: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
    children: dict[
        int,
        list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef],
    ] = defaultdict(list)
    todo = deque([(tree, "", None)])
    while todo:
        node, parent, owner = todo.popleft()
        child_parent = parent
        child_owner = owner
        if isinstance(node, ast.ClassDef):
            if owner is not None:
                children[id(owner)].append(node)
            child_parent = f"{parent}.{node.name}" if parent else node.name
            child_owner = node
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if owner is not None:
                children[id(owner)].append(node)
            qualified = f"{parent}.{node.name}" if parent else node.name
            found.append((node, qualified))
            child_parent = f"{qualified}.<locals>"
            child_owner = node
        for name in _BLOCKS:
            block = getattr(node, name, None)
            if block:
                todo.extend((item, child_parent, child_owner) for item in block)
    return found, children


def _python_bodies(
    path: Path,
    relative: str,
    min_lines: int,
    normalization: str = "shape",
    coverage: Coverage | None = None,
    build_structure: bool = True,
) -> list[Body]:
    try:
        source = path.read_bytes()
        tree = ast.parse(source)
    except OSError as error:
        # A file that cannot be read or parsed contributes no functions. It is
        # not a duplication finding, and failing the scan over one would make the
        # tool unusable on a tree mid-edit.
        if coverage is not None:
            coverage.skip(relative, f"could not read source: {error.strerror or error}")
        return []
    except SyntaxError as error:
        if coverage is not None:
            line = error.lineno or 1
            coverage.skip(relative, f"invalid Python syntax at line {line}")
        return []
    except ValueError as error:
        if coverage is not None:
            coverage.skip(relative, f"invalid Python source: {error}")
        return []
    source_lines = source.splitlines(keepends=True)
    definitions = _definitions(tree)
    deep_scopes = any(qualname.count(".<locals>.") >= 8 for _, qualname in definitions)
    if not build_structure or not deep_scopes:
        bodies = []
        for node, qualname in definitions:
            body = _significant_body(node)
            if not body or _is_stub(body):
                continue
            span = _body_lines(body)
            if span < min_lines:
                continue
            shape = bytearray()
            if build_structure:
                if normalization == "alpha":
                    _alpha_structure(body, shape, {})
                else:
                    _structure(body, shape)
            body_start = body[0].lineno
            body_end = body[-1].end_lineno or body[-1].lineno
            fragment_source = b"".join(source_lines[body_start - 1 : body_end])
            bodies.append(
                Body(
                    Site(
                        relative,
                        node.name,
                        node.lineno,
                        span,
                        qualname,
                        body_start,
                        body_end,
                    ),
                    _digest(shape),
                    bytes(shape),
                    fragment_source,
                    body_start,
                    0,
                )
            )
        return bodies

    # Deep chains use fixed-size child images.  Ordinary source stays on the
    # tighter direct normalizer above: this branch pays for its catalog only
    # once nesting is deep enough for repeated descendant walks to dominate.
    definitions, scope_children = _scope_catalog(tree)
    function_bodies = {id(defined): _significant_body(defined) for defined, _ in definitions}
    structures: dict[int, bytes] = {}
    scope_images: dict[int, bytes] = {}
    if build_structure:

        def build_scope(defined: ast.AST) -> bytes:
            existing = scope_images.get(id(defined))
            if existing is not None:
                return existing
            if isinstance(defined, ast.FunctionDef | ast.AsyncFunctionDef):
                significant = function_bodies[id(defined)]
            elif isinstance(defined, ast.ClassDef):
                significant = list(defined.body)
            else:
                raise TypeError(
                    f"nested scope must be a function or class, got {type(defined).__name__}"
                )
            for child in scope_children.get(id(defined), ()):
                child_image = build_scope(child)
                # The parsed tree belongs to this one collection operation.
                # Replacing only the child's body with its immutable image lets
                # the ordinary normalizer retain its tight two-argument loop;
                # the saved body lists above still own each function's source.
                child.__dict__["body"] = child_image
            shape = bytearray()
            if normalization == "alpha":
                _alpha_structure(significant, shape, {})
            else:
                _structure(significant, shape)
            frozen = bytes(shape)
            structures[id(defined)] = frozen
            image = hashlib.blake2s(
                frozen,
                digest_size=12,
            ).digest()
            scope_images[id(defined)] = image
            return image

    bodies = []
    for node, qualname in definitions:
        body = function_bodies[id(node)]
        if not body or _is_stub(body):
            continue
        span = _body_lines(body)
        if span < min_lines:
            continue
        if build_structure:
            build_scope(node)
        shape = structures.get(id(node), b"")
        body_start = body[0].lineno
        body_end = body[-1].end_lineno or body[-1].lineno
        fragment_source = b"".join(source_lines[body_start - 1 : body_end])
        bodies.append(
            Body(
                Site(
                    relative,
                    node.name,
                    node.lineno,
                    span,
                    qualname,
                    body_start,
                    body_end,
                ),
                _digest(shape),
                shape,
                fragment_source,
                body_start,
                0,
            )
        )
    return bodies


def _native_bodies(
    path: Path,
    relative: str,
    min_lines: int,
    normalization: str = "shape",
    coverage: Coverage | None = None,
    build_structure: bool = True,
) -> list[Body]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        if coverage is not None:
            coverage.skip(relative, f"could not read source: {error.strerror or error}")
        return []
    except UnicodeError:
        if coverage is not None:
            coverage.skip(relative, "native source is not valid UTF-8")
        return []
    bodies = []
    line = 1
    line_cursor = 0
    for match in _C_HEAD.finditer(source):
        line += source.count("\n", line_cursor, match.start())
        line_cursor = match.start()
        name = match.group("name")
        if name in _C_NOT_A_DEFINITION:
            continue
        brace = source.index("{", match.end() - 1)
        body, end = _c_body(source, brace)
        shape, lines = _c_shape(body, normalization)
        if not build_structure:
            shape.clear()
        if lines < min_lines:
            continue
        body_start = source.count("\n", 0, brace) + 1
        body_end = source.count("\n", 0, end) + 1
        bodies.append(
            Body(
                Site(relative, name, line, lines, name, body_start, body_end),
                _digest(shape),
                bytes(shape),
                body.encode(),
                body_start,
                1,
            )
        )
    return bodies


def collect(
    root: Path,
    relatives: tuple[str, ...],
    min_lines: int,
    langs: tuple[str, ...] = LANGS,
    *,
    normalization: str = "shape",
    coverage: Coverage | None = None,
    build_structure: bool = True,
) -> list[Body]:
    """Every body worth comparing, in both languages."""
    if normalization not in NORMALIZATIONS:
        raise ValueError(
            f"normalization must be one of {', '.join(NORMALIZATIONS)}; got {normalization!r}"
        )
    read = {"python": _python_bodies, "native": _native_bodies}
    sources = _sources(root, relatives, langs)
    if coverage is not None:
        coverage.discovered_files = len(sources)
    bodies = []
    for path, lang in sources:
        relative = str(path.relative_to(root))
        skipped_before = len(coverage.skipped_files) if coverage is not None else 0
        bodies.extend(
            read[lang](
                path,
                relative,
                min_lines,
                normalization,
                coverage,
                build_structure,
            )
        )
        if coverage is not None and len(coverage.skipped_files) == skipped_before:
            coverage.scanned_files += 1
    return bodies


def scan(
    root: Path,
    relatives: tuple[str, ...],
    min_lines: int,
    langs: tuple[str, ...] = LANGS,
    *,
    include_excluded: bool = False,
    normalization: str = "shape",
) -> tuple[list[Group], int]:
    """Group bodies by shape, omitting exact intentional groups by default."""
    bodies = collect(root, relatives, min_lines, langs, normalization=normalization)
    return _scan_bodies(bodies, include_excluded=include_excluded)


def _scan_bodies(
    bodies: list[Body],
    *,
    include_excluded: bool = False,
) -> tuple[list[Group], int]:
    """Group one already-normalised body image.

    ``--near`` needs the same image for its similarity pass. Keeping grouping
    separate from collection lets that command parse and normalise every source
    once without changing the standalone ``scan()`` contract.
    """
    groups: dict[str, list[Site]] = defaultdict(list)
    for body in bodies:
        groups[body.digest].append(body.site)

    found = [
        Group(digest, tuple(sorted(sites, key=lambda s: (s.path, s.line))))
        for digest, sites in groups.items()
        if len(sites) > 1
    ]
    found.sort(key=lambda g: (-g.redundant_lines, g.sites[0].path, g.sites[0].line))
    if not include_excluded:
        found = [group for group in found if intentional_reason(group) is None]
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
    # `_sketch` returns sorted unique tuples of at most `_SKETCH` hashes. Merge
    # them directly: constructing two sets and sorting their union for every
    # candidate pair dominated the near-copy report (roughly half a million
    # pairs on this tree).
    left_at = right_at = union = both = 0
    while union < _SKETCH and (left_at < len(left) or right_at < len(right)):
        if left_at >= len(left):
            right_at += 1
        elif right_at >= len(right):
            left_at += 1
        elif left[left_at] == right[right_at]:
            both += 1
            left_at += 1
            right_at += 1
        elif left[left_at] < right[right_at]:
            left_at += 1
        else:
            right_at += 1
        union += 1
    if not union:
        return 0.0
    return both / union


def near_clones(
    root: Path,
    relatives: tuple[str, ...],
    min_lines: int,
    langs: tuple[str, ...] = LANGS,
    similarity: float = DEFAULT_SIMILARITY,
    *,
    normalization: str = "shape",
) -> list[Pair]:
    """Bodies that are almost, but not exactly, the same shape.

    Exact grouping cannot see these at all -- one added statement changes the
    digest and the pair vanishes -- and they are where the tree's most useful
    findings have been: a primitive re-implemented beside the real one, with a
    line of local adaptation on top.

    Pairs already grouped as exact copies are excluded, because reporting a
    finding twice is how a report stops being read.
    """
    bodies = collect(root, relatives, min_lines, langs, normalization=normalization)
    return _near_bodies(bodies, similarity)


def _near_bodies(bodies: list[Body], similarity: float) -> list[Pair]:
    """Find near copies in one already-normalised body image."""
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
            first, second = sorted(
                (bodies[left].site, bodies[right].site), key=lambda s: (s.path, s.line)
            )
            pairs.append(Pair(first, second, score))
    pairs.sort(
        key=lambda p: (-min(p.left.lines, p.right.lines), -p.similarity, p.left.path, p.left.line)
    )
    return pairs


def _fragment_bodies(
    bodies: list[Body],
    min_lines: int,
    min_tokens: int,
    normalization: str,
) -> list[Fragment]:
    """Run the native maximal token-window matcher over one body image."""
    mode = NORMALIZATIONS.index(normalization)
    native = _fragment_scan(
        [(body.fragment_source, body.fragment_start, body.language) for body in bodies],
        min_lines,
        min_tokens,
        mode,
    )
    fragments = []
    for (
        left_index,
        left_start,
        left_end,
        right_index,
        right_start,
        right_end,
        tokens,
    ) in native:
        left_body = bodies[left_index]
        right_body = bodies[right_index]
        left = Site(
            left_body.site.path,
            left_body.site.name,
            left_start,
            left_end - left_start + 1,
            left_body.site.qualname,
            left_start,
            left_end,
        )
        right = Site(
            right_body.site.path,
            right_body.site.name,
            right_start,
            right_end - right_start + 1,
            right_body.site.qualname,
            right_start,
            right_end,
        )
        fragments.append(Fragment(left, right, tokens))
    by_pair: dict[tuple[str, str, str, str], list[Fragment]] = defaultdict(list)
    for fragment in fragments:
        key = (
            fragment.left.path,
            fragment.left.identity_name,
            fragment.right.path,
            fragment.right.identity_name,
        )
        by_pair[key].append(fragment)
    maximal = []
    for candidates in by_pair.values():
        candidates.sort(
            key=lambda fragment: (
                fragment.left.line,
                fragment.right.line,
                -fragment.tokens,
            )
        )
        left_end = right_end = 0
        for fragment in candidates:
            if fragment.left.line <= left_end and fragment.right.line <= right_end:
                continue
            maximal.append(fragment)
            left_end = fragment.left.body_end
            right_end = fragment.right.body_end
    maximal.sort(
        key=lambda fragment: (
            -fragment.lines,
            -fragment.tokens,
            fragment.left.path,
            fragment.left.line,
            fragment.right.path,
            fragment.right.line,
        )
    )
    return maximal


def fragment_clones(
    root: Path,
    relatives: tuple[str, ...],
    min_lines: int,
    min_tokens: int,
    langs: tuple[str, ...] = LANGS,
    *,
    normalization: str = "shape",
) -> list[Fragment]:
    """Maximal duplicated token fragments inside otherwise distinct bodies."""
    bodies = collect(
        root,
        relatives,
        min_lines,
        langs,
        normalization=normalization,
        build_structure=False,
    )
    return _fragment_bodies(bodies, min_lines, min_tokens, normalization)


def summarize(groups: list[Group]) -> dict[str, list[dict]]:
    """Aggregate exact duplicate involvement by source file and directory."""
    files: dict[str, dict] = {}
    directories: dict[str, dict] = {}
    for group_number, group in enumerate(groups):
        for site in group.sites:
            directory = str(Path(site.path).parent)
            for table, path in ((files, site.path), (directories, directory)):
                record = table.setdefault(
                    path,
                    {"path": path, "duplicated_lines": 0, "_groups": set()},
                )
                record["duplicated_lines"] += site.lines
                record["_groups"].add(group_number)

    def ranked(table: dict[str, dict]) -> list[dict]:
        rows = [
            {
                "path": record["path"],
                "duplicated_lines": record["duplicated_lines"],
                "groups": len(record["_groups"]),
            }
            for record in table.values()
        ]
        rows.sort(key=lambda row: (-row["duplicated_lines"], row["path"]))
        return rows

    return {"files": ranked(files), "directories": ranked(directories)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-dup-scan",
        description="Report function bodies that share a structure, ranked by "
        "the lines a collapse would remove. A report, not a gate.",
    )
    parser.add_argument(
        "--path",
        action="append",
        metavar="REL",
        help=f"repo-relative file or directory to scan "
        f"(repeatable; default {', '.join(DEFAULT_ROOTS)})",
    )
    parser.add_argument(
        "--min-lines",
        type=int,
        default=DEFAULT_MIN_LINES,
        help=f"ignore bodies shorter than this (default {DEFAULT_MIN_LINES})",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"groups to print (default {DEFAULT_TOP}; 0 for all)",
    )
    parser.add_argument(
        "--lang",
        choices=("all", *LANGS),
        default="all",
        help="which half of the tree to scan (default all)",
    )
    parser.add_argument(
        "--near",
        action="store_true",
        help="also report pairs that are almost the same shape, which exact grouping cannot see",
    )
    parser.add_argument(
        "--fragments",
        action="store_true",
        help="also report maximal copied token runs inside distinct function bodies",
    )
    parser.add_argument(
        "--min-tokens",
        type=int,
        default=50,
        help="minimum tokens in a --fragments match (default 50)",
    )
    parser.add_argument(
        "--normalization",
        choices=NORMALIZATIONS,
        default="shape",
        help="shape erases names/literals; alpha preserves their relationships",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=0,
        metavar="LINES",
        help="include exact source ranges plus this many surrounding lines",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="aggregate exact duplicate involvement by file and directory",
    )
    parser.add_argument(
        "--show-excluded",
        action="store_true",
        help="also show exact intentional groups and the reason each is filtered",
    )
    parser.add_argument(
        "--similarity",
        type=float,
        default=DEFAULT_SIMILARITY,
        help=f"how alike a --near pair must be (default {DEFAULT_SIMILARITY})",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = repo_root()
    relatives = tuple(args.path) if args.path else DEFAULT_ROOTS
    langs = LANGS if args.lang == "all" else (args.lang,)
    if args.min_lines < 1:
        parser.error("--min-lines must be at least 1")
    if args.min_tokens < 1:
        parser.error("--min-tokens must be at least 1")
    if args.context < 0:
        parser.error("--context must be non-negative")
    if not 0.0 <= args.similarity <= 1.0:
        parser.error("--similarity must be between 0 and 1")
    resolved_root = root.resolve()
    for relative in relatives:
        target = root / relative
        if not target.exists():
            parser.error(f"{relative} does not exist; use a repository-relative file or directory")
        try:
            target.resolve().relative_to(resolved_root)
        except ValueError:
            parser.error(
                f"{relative} is outside the repository; use a repository-relative file or directory"
            )

    # The exact and near passes consume the same normalised bodies. The ordinary
    # exact-only command retains the public ``scan()`` entry point, while
    # richer reports collect once and share the image between every operation.
    detailed = (
        args.near
        or args.fragments
        or args.format == "json"
        or args.summary
        or args.context > 0
        or args.normalization != "shape"
    )
    coverage = Coverage()
    bodies = (
        collect(
            root,
            relatives,
            args.min_lines,
            langs,
            normalization=args.normalization,
            coverage=coverage if args.format == "json" else None,
        )
        if detailed
        else None
    )
    if args.show_excluded:
        all_groups, scanned = (
            _scan_bodies(bodies, include_excluded=True)
            if bodies is not None
            else scan(
                root,
                relatives,
                args.min_lines,
                langs,
                include_excluded=True,
            )
        )
        excluded = [
            (group, reason)
            for group in all_groups
            if (reason := intentional_reason(group)) is not None
        ]
        groups = [group for group in all_groups if intentional_reason(group) is None]
    else:
        groups, scanned = (
            _scan_bodies(bodies)
            if bodies is not None
            else scan(
                root,
                relatives,
                args.min_lines,
                langs,
            )
        )
        excluded = []
    pairs = _near_bodies(bodies, args.similarity) if args.near and bodies is not None else []
    fragments = (
        _fragment_bodies(
            bodies,
            args.min_lines,
            args.min_tokens,
            args.normalization,
        )
        if args.fragments and bodies is not None
        else []
    )
    shown = groups if args.top <= 0 else groups[: args.top]

    if args.format == "json":
        evidence_root = root if args.context > 0 else None
        print(
            json.dumps(
                {
                    "scanned_functions": scanned,
                    "min_lines": args.min_lines,
                    "min_tokens": args.min_tokens,
                    "langs": list(langs),
                    "normalization": args.normalization,
                    "coverage": coverage.as_dict(),
                    "groups": [g.as_dict(evidence_root, args.context) for g in groups],
                    "excluded_groups": [
                        {**group.as_dict(evidence_root, args.context), "reason": reason}
                        for group, reason in excluded
                    ],
                    "near": [p.as_dict(evidence_root, args.context) for p in pairs],
                    "fragments": [
                        fragment.as_dict(evidence_root, args.context) for fragment in fragments
                    ],
                    **({"summary": summarize(groups)} if args.summary else {}),
                },
                indent=2,
            )
        )
        return 0

    print(f"{scanned} function(s) of >= {args.min_lines} lines scanned in {', '.join(relatives)}\n")
    if not groups:
        print("no shared structure found.")
    else:
        print("redundant  copies  group")
        for group in shown:
            first = group.sites[0]
            print(f"{group.redundant_lines:>9}  {len(group.sites):>6}  {first.lines} lines each")
            for site in group.sites:
                print(f"{'':>19}{site.path}:{site.line} {site.identity_name}")
                if args.context > 0:
                    source = site.as_dict(root, args.context).get("source", "")
                    for source_line in source.splitlines():
                        print(f"{'':>21}| {source_line}")
        if len(groups) > len(shown):
            print(f"\n... and {len(groups) - len(shown)} more group(s); --top 0 for all.")
        total = sum(g.redundant_lines for g in groups)
        print(f"\nwreath-dup-scan: {len(groups)} group(s), {total} redundant line(s).")

    if excluded:
        print("\nintentional groups (exact site set)")
        excluded_shown = excluded if args.top <= 0 else excluded[: args.top]
        for group, reason in excluded_shown:
            first = group.sites[0]
            print(
                f"{group.redundant_lines:>9}  {len(group.sites):>6}  "
                f"{first.lines} lines at first site"
            )
            for site in group.sites:
                print(f"{'':>19}{site.path}:{site.line} {site.identity_name}")
                if args.context > 0:
                    source = site.as_dict(root, args.context).get("source", "")
                    for source_line in source.splitlines():
                        print(f"{'':>21}| {source_line}")
            print(f"{'':>19}reason: {reason}")
        if len(excluded) > len(excluded_shown):
            print(
                f"\n... and {len(excluded) - len(excluded_shown)} more intentional "
                "group(s); --top 0 for all."
            )
        print(f"\nwreath-dup-scan: {len(excluded)} intentional group(s) filtered.")

    if args.near:
        near_shown = pairs if args.top <= 0 else pairs[: args.top]
        print(f"\nnear copies (>= {args.similarity:.2f} alike, not exact)")
        for pair in near_shown:
            print(
                f"{pair.similarity:>9.2f}  {pair.left.path}:{pair.left.line} "
                f"{pair.left.identity_name}  <->  "
                f"{pair.right.path}:{pair.right.line} "
                f"{pair.right.identity_name}"
            )
            if args.context > 0:
                for site in (pair.left, pair.right):
                    source = site.as_dict(root, args.context).get("source", "")
                    for source_line in source.splitlines():
                        print(f"{'':>21}| {source_line}")
        if len(pairs) > len(near_shown):
            print(f"\n... and {len(pairs) - len(near_shown)} more pair(s); --top 0 for all.")
        print(f"\nwreath-dup-scan: {len(pairs)} near pair(s).")

    if args.fragments:
        fragment_shown = fragments if args.top <= 0 else fragments[: args.top]
        print(
            f"\nfragments (>= {args.min_lines} lines and {args.min_tokens} tokens, "
            "not whole-body exact)"
        )
        for fragment in fragment_shown:
            print(
                f"{fragment.lines:>9}  {fragment.tokens:>6}  "
                f"{fragment.left.path}:{fragment.left.line} "
                f"{fragment.left.identity_name}  <->  "
                f"{fragment.right.path}:{fragment.right.line} "
                f"{fragment.right.identity_name}"
            )
            if args.context > 0:
                for site in (fragment.left, fragment.right):
                    source = site.as_dict(root, args.context).get("source", "")
                    for source_line in source.splitlines():
                        print(f"{'':>21}| {source_line}")
        if len(fragments) > len(fragment_shown):
            print(
                f"\n... and {len(fragments) - len(fragment_shown)} more fragment(s); "
                "--top 0 for all."
            )
        print(f"\nwreath-dup-scan: {len(fragments)} fragment(s).")

    if args.summary:
        hotspots = summarize(groups)
        file_rows = hotspots["files"] if args.top <= 0 else hotspots["files"][: args.top]
        directory_rows = (
            hotspots["directories"] if args.top <= 0 else hotspots["directories"][: args.top]
        )
        print("\nduplicate hotspots by file")
        for row in file_rows:
            print(f"{row['duplicated_lines']:>9}  {row['groups']:>6}  {row['path']}")
        print("\nduplicate hotspots by directory")
        for row in directory_rows:
            print(f"{row['duplicated_lines']:>9}  {row['groups']:>6}  {row['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
