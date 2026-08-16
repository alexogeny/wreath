"""The structural duplicate scanner.

The scan is only worth reading if it sees the duplication this repository
actually produces — the same body under different names, with different locals
and different literals — and stays quiet about bodies that merely share a
statement count.

Half of that duplication is in C, and for a long time nothing looked at it. The
native half of the tests below pins the C extractor, because a regex that
mistakes an initialiser for a function, or misses a definition whose return type
sits on its own line, reports a clean scan over a file it never read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wreath._devtools import dup_scan
from wreath._devtools.native_lint import repo_root

RENAMED_TWINS = '''
def insert_settled(connection, table, values):
    """One docstring."""
    prepared = _coerce(values)
    if not prepared:
        return 0
    statement = _build(table, prepared)
    result = connection.execute(statement)
    _record(result, table)
    _audit(result)
    return result.rowcount


def replace_settled(session, relation, rows):
    """A different docstring entirely."""
    ready = _coerce(rows)
    if not ready:
        return 99
    query = _build(relation, ready)
    outcome = session.execute(query)
    _record(outcome, relation)
    _audit(outcome)
    return outcome.rowcount
'''

#: Same length, same statement count, different structure.
NOT_TWINS = '''
def counts(items):
    total = 0
    for item in items:
        total += item
    seen = len(items)
    average = total / seen
    _log(average)
    _emit(average)
    return average


def gather(source):
    out = []
    while source:
        out.append(source.pop())
    if not out:
        raise ValueError("empty")
    _log(out)
    _emit(out)
    return out
'''


#: The shape the native tree produces most: a grow-the-buffer helper, copied
#: into every module that needed one and renamed on arrival.
RENAMED_C_TWINS = r"""
static int
writer_grow(Writer *writer, size_t need)
{
    size_t want = writer->len + need;
    if (want <= writer->cap) {
        return 0;
    }
    size_t next = writer->cap ? writer->cap * 2 : 64;
    while (next < want) {
        next *= 2;
    }
    char *block = PyMem_Realloc(writer->data, next);
    if (block == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    writer->data = block;
    writer->cap = next;
    return 0;
}

static int
sink_reserve(Sink *sink, size_t extra)
{
    /* A comment the other copy does not have. */
    size_t target = sink->used + extra;
    if (target <= sink->size) {
        return 0;
    }
    size_t grown = sink->size ? sink->size * 4 : 256;
    while (grown < target) {
        grown *= 4;
    }
    char *fresh = PyMem_Realloc(sink->bytes, grown);
    if (fresh == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    sink->bytes = fresh;
    sink->size = grown;
    return 0;
}
"""

#: The same body twice, once carrying closing braces inside a string, a
#: character constant and two comments. Those are not braces, and a brace
#: matcher that thinks they are stops the first body early and then reads the
#: rest of the file at the wrong depth. Strings collapse to one literal token
#: and comments are dropped, so the two bodies must hash equal.
BRACE_DECOY_C_TWINS = r"""
static int
guarded(Sink *sink, int flag)
{
    /* A brace in a comment is not a brace: } and { again. */
    const char *hint = "expected } to close the block";
    char closer = '}';
    if (flag) {
        wreath_note(sink, hint, closer);   // and one in a line comment: }
        wreath_flush(sink);
        return 1;
    }
    wreath_reset(sink);
    return 0;
}

static int
plain(Sink *sink, int flag)
{
    /* No decoys at all. */
    const char *label = "expected the block to close";
    char marker = 'x';
    if (flag) {
        wreath_note(sink, label, marker);
        wreath_flush(sink);
        return 1;
    }
    wreath_reset(sink);
    return 0;
}
"""

#: Same length, same brace count, different control structure.
NOT_C_TWINS = r"""
static int
tally(const uint8_t *data, Py_ssize_t len)
{
    int total = 0;
    for (Py_ssize_t i = 0; i < len; i++) {
        total += data[i];
    }
    if (total > 255) {
        total = 255;
    }
    wreath_record(total);
    wreath_emit(total);
    return total;
}

static int
drain(Queue *queue, PyObject **out)
{
    PyObject *item = NULL;
    while ((item = queue_pop(queue)) != NULL) {
        *out++ = item;
    }
    switch (queue->state) {
        case 1:
            return -1;
        default:
            break;
    }
    return 0;
}
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "wreath").mkdir(parents=True)
    return tmp_path


def _write(tree: Path, name: str, text: str) -> None:
    (tree / "src" / "wreath" / name).write_text(text)


def test_a_renamed_copy_is_found(tree: Path) -> None:
    """Renaming is how copy-paste survives here; a text differ sees nothing."""
    _write(tree, "settle.py", RENAMED_TWINS)

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned == 2
    assert len(groups) == 1
    assert [site.name for site in groups[0].sites] == ["insert_settled", "replace_settled"]


def test_the_copy_is_found_across_files(tree: Path) -> None:
    _write(tree, "a.py", RENAMED_TWINS.split("def replace_settled")[0])
    _write(tree, "b.py", "def replace_settled" + RENAMED_TWINS.split("def replace_settled")[1])

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert len(groups) == 1
    assert {site.path for site in groups[0].sites} == {"src/wreath/a.py", "src/wreath/b.py"}


def test_same_size_different_shape_is_not_a_finding(tree: Path) -> None:
    """A scanner that fires on 'both are eight lines' gets switched off."""
    _write(tree, "unrelated.py", NOT_TWINS)

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned == 2
    assert groups == []


def test_a_bare_return_body_is_scanned(tree: Path) -> None:
    """The bug the blanket `except` hid.

    An earlier form of this normalised by round-tripping each statement through
    `ast.parse(ast.unparse(...))`. A body starting with a statement that cannot
    stand alone as a module — a bare `return` — raised there, and the exception
    was swallowed, so those functions were silently absent from every scan. On
    this repository the fix recovered five functions and surfaced a three-copy
    group larger than anything the broken version had ever reported.
    """
    _write(tree, "early.py", '''
def first(value):
    return (
        value
        + 1
        + 2
        + 3
        + 4
        + 5
    )


def second(other):
    return (
        other
        + 9
        + 8
        + 7
        + 6
        + 5
    )
''')

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned == 2, "a body whose first statement is `return` must still be scanned"
    assert len(groups) == 1


#: Every statement block a definition can be written inside, as (header, depth,
#: trailer). `_definitions_inside` puts the renamed twins in the block at that
#: depth, so the scan has to descend the block to find either of them.
NESTING = {
    "module": ("", 0, ""),
    "class": ("class Holder:", 1, ""),
    "function": ("def outer():", 1, ""),
    "if": ("if VALUE:", 1, ""),
    "else": ("if VALUE:\n    pass\nelse:", 1, ""),
    "for": ("for item in VALUE:", 1, ""),
    "while": ("while VALUE:", 1, ""),
    "with": ("with VALUE:", 1, ""),
    "try": ("try:", 1, "except OSError:\n    pass"),
    "except": ("try:\n    pass\nexcept OSError:", 1, ""),
    "except-star": ("try:\n    pass\nexcept* OSError:", 1, ""),
    "finally": ("try:\n    pass\nfinally:", 1, ""),
    "match": ("match VALUE:\n    case 1:", 2, ""),
}


def _definitions_inside(block: str) -> str:
    header, depth, trailer = NESTING[block]
    pad = "    " * depth
    body = "\n".join(pad + line if line else line
                     for line in RENAMED_TWINS.strip().splitlines())
    return "\n".join(part for part in ("VALUE = 1", header, body, trailer) if part) + "\n"


@pytest.mark.parametrize("block", sorted(NESTING))
def test_a_definition_is_found_inside_every_kind_of_block(tree: Path, block: str) -> None:
    """Only statements can hold a definition, but *every* statement can.

    The scan reaches definitions by descending the statement blocks rather than
    every node in the tree, which is an order of magnitude cheaper and exactly
    as complete — provided no block is left out. A missing one is invisible:
    the scan reports a clean pass over a file it never fully read, which is the
    same lie as a file it could not parse, in the same direction. `match` and
    `except*` are the two that a hand-written list of block names forgets.
    """
    _write(tree, "nested.py", _definitions_inside(block))

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned >= 2, f"a definition inside a {block} block was never scanned"
    assert [site.name for site in groups[0].sites] == ["insert_settled", "replace_settled"]


def test_short_bodies_are_trivia(tree: Path) -> None:
    _write(tree, "small.py", '''
def one(a):
    return a + 1


def two(b):
    return b + 2
''')

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert (groups, scanned) == ([], 0)


def test_a_docstring_is_not_structure(tree: Path) -> None:
    """Two bodies differing only in their prose are the same body."""
    _write(tree, "settle.py", RENAMED_TWINS)

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert len(groups) == 1


def test_an_unparseable_file_does_not_stop_the_scan(tree: Path) -> None:
    """Usable on a tree mid-edit, or it will not be run."""
    _write(tree, "settle.py", RENAMED_TWINS)
    _write(tree, "broken.py", "def (:\n")

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert len(groups) == 1


def test_a_documented_protocol_stub_is_not_a_finding(tree: Path) -> None:
    """The bug that made the two largest groups in the report meaningless.

    The span was measured over the whole function, so a `Protocol` method whose
    entire body is `...` measured as however many lines its docstring ran to,
    cleared `--min-lines`, and then hashed identically to every other stub in
    the tree. That produced a 27-copy group and a 29-copy group -- 551 claimed
    redundant lines -- out of Protocol methods and `@property` accessors that
    share no code whatsoever.
    """
    _write(tree, "protocol.py", '''
from typing import Protocol


class Store(Protocol):
    async def read(self, key):
        """Read one row.

        Several paragraphs of prose about what a store owes its caller, easily
        long enough to clear the minimum on its own, which is exactly how this
        went unnoticed: the body is one token and the function is twelve lines.
        """
        ...

    async def write(self, key, value):
        """Write one row.

        A different several paragraphs, equally long, saying something entirely
        different about a completely different operation, and sharing not one
        line of implementation with the method above it.
        """
        ...
''')

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert (groups, scanned) == ([], 0)


def test_a_documented_one_line_property_is_not_a_finding(tree: Path) -> None:
    """The other half of the same bug: `return self._x` under a long docstring."""
    _write(tree, "accessors.py", '''
class Bus:
    @property
    def dropped(self):
        """Messages dropped since start.

        Counted rather than logged, because a rate is what an operator reads and
        a log line is what they miss. Prose long enough to clear the minimum.
        """
        return self._dropped

    @property
    def buffered(self):
        """Messages buffered right now.

        A different counter with a different meaning and a different operational
        story, also documented at length, also a single-line body.
        """
        return self._buffered
''')

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert (groups, scanned) == ([], 0)


def test_a_multi_statement_body_under_a_long_docstring_is_still_scanned(
    tree: Path,
) -> None:
    """The filter must key on the body, not on the presence of a docstring.

    The fix would be worthless if it also hid real duplication that happens to
    be well documented -- which, in this repository, is most of it.
    """
    _write(tree, "real.py", '''
def register(subs, table, name):
    """Declare this process's groups.

    A long docstring, because everything here has one, and the point of this
    test is that prose neither creates a finding nor suppresses one.
    """
    pairs = sorted(subs)
    if not pairs:
        return None
    statement = _insert(table)
    connection = _acquire()
    _run(connection, statement, pairs, name)
    _release(connection)
    return None


def deregister(members, relation, bus):
    """Remove this process's groups.

    Also long, also prose, and the body below is the same shape under different
    names -- which is precisely the finding this tool exists to make.
    """
    items = sorted(members)
    if not items:
        return None
    query = _delete(relation)
    handle = _acquire()
    _run(handle, query, items, bus)
    _release(handle)
    return None
''')

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned == 2
    assert len(groups) == 1
    assert [site.name for site in groups[0].sites] == ["register", "deregister"]


def test_reported_lines_are_body_lines_not_function_lines(tree: Path) -> None:
    """The weight has to match the claim: a collapse removes bodies, not prose.

    The surviving copy still needs its docstring, so counting docstrings into
    "lines a collapse would remove" overstated every group in the report.
    """
    _write(tree, "weighted.py", '''
def one(a, b, c):
    """A docstring that is deliberately five lines long.

    Second paragraph.
    Third line.
    """
    first = _step(a)
    second = _step(b)
    third = _step(c)
    joined = _merge(first, second)
    result = _merge(joined, third)
    _record(result)
    _audit(result)
    return result


def two(x, y, z):
    """A one-line docstring."""
    alpha = _step(x)
    beta = _step(y)
    gamma = _step(z)
    pair = _merge(alpha, beta)
    answer = _merge(pair, gamma)
    _record(answer)
    _audit(answer)
    return answer
''')

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert len(groups) == 1
    # Both bodies are eight lines. The docstrings differ by four, and neither
    # length may reach the count.
    assert {site.lines for site in groups[0].sites} == {8}
    assert groups[0].redundant_lines == 8


def test_ranking_is_by_the_lines_a_collapse_would_remove(tree: Path) -> None:
    group = dup_scan.Group("d", (
        dup_scan.Site("a.py", "one", 1, 10),
        dup_scan.Site("b.py", "two", 1, 10),
        dup_scan.Site("c.py", "three", 1, 10),
    ))
    assert group.redundant_lines == 20  # the copies after the first, not all three


def test_an_intentional_group_is_filtered_only_by_its_exact_site_set(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reasoned exception must not become permission for the next copy."""
    _write(tree, "settle.py", RENAMED_TWINS)
    raw, scanned = dup_scan.scan(
        tree,
        ("src/wreath",),
        dup_scan.DEFAULT_MIN_LINES,
        include_excluded=True,
    )
    sites = tuple(
        sorted((site.path, site.qualname or site.name) for site in raw[0].sites)
    )
    monkeypatch.setattr(
        dup_scan,
        "INTENTIONAL_GROUPS",
        (dup_scan.Exclusion(sites, "the two operations are intentionally parallel"),),
    )

    assert dup_scan.scan(
        tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES
    ) == ([], scanned)
    assert dup_scan.intentional_reason(raw[0]) is not None

    expanded = dup_scan.Group(
        raw[0].digest,
        (*raw[0].sites, dup_scan.Site("src/wreath/new.py", "third", 1, 10)),
    )
    assert dup_scan.intentional_reason(expanded) is None


def test_exclusions_distinguish_same_named_methods(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tree, "methods.py", '''
class First:
    def render(self, value):
        one = _step(value)
        two = _step(one)
        three = _step(two)
        four = _step(three)
        five = _step(four)
        _record(five)
        _audit(five)
        return five


class Second:
    def render(self, item):
        alpha = _step(item)
        beta = _step(alpha)
        gamma = _step(beta)
        delta = _step(gamma)
        epsilon = _step(delta)
        _record(epsilon)
        _audit(epsilon)
        return epsilon
''')

    groups, _ = dup_scan.scan(
        tree,
        ("src/wreath",),
        dup_scan.DEFAULT_MIN_LINES,
        include_excluded=True,
    )

    assert len(groups) == 1
    assert {site.qualname for site in groups[0].sites} == {
        "First.render",
        "Second.render",
    }
    identities = tuple(
        sorted((site.path, site.qualname) for site in groups[0].sites)
    )
    monkeypatch.setattr(
        dup_scan,
        "INTENTIONAL_GROUPS",
        (dup_scan.Exclusion(identities, "these two methods intentionally mirror"),),
    )
    assert dup_scan.intentional_reason(groups[0]) is not None
    wrong_methods = dup_scan.Group(
        groups[0].digest,
        tuple(
            dup_scan.Site(
                site.path,
                site.name,
                site.line,
                site.lines,
                site.qualname.replace("render", "other"),
            )
            for site in groups[0].sites
        ),
    )
    assert dup_scan.intentional_reason(wrong_methods) is None


def test_every_repository_exclusion_is_reasoned_and_canonical() -> None:
    for exclusion in dup_scan.INTENTIONAL_GROUPS:
        assert exclusion.reason.strip()
        assert len(exclusion.sites) >= 2
        assert exclusion.sites == tuple(sorted(exclusion.sites))


def test_a_renamed_c_copy_is_found(tree: Path) -> None:
    """The native half of the same finding, and half of this tree is C."""
    _write(tree, "writer.c", RENAMED_C_TWINS)

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned == 2
    assert len(groups) == 1
    assert [site.name for site in groups[0].sites] == ["writer_grow", "sink_reserve"]


def test_a_c_copy_is_found_across_files_and_suffixes(tree: Path) -> None:
    """A `static inline` in a header is as copyable as one in a `.c`."""
    half = RENAMED_C_TWINS.index("static int\nsink_reserve")
    _write(tree, "writer.c", RENAMED_C_TWINS[:half])
    _write(tree, "sink.h", RENAMED_C_TWINS[half:])

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert len(groups) == 1
    assert {site.path for site in groups[0].sites} == {
        "src/wreath/sink.h", "src/wreath/writer.c",
    }


def test_same_size_different_shape_is_not_a_c_finding(tree: Path) -> None:
    _write(tree, "unrelated.c", NOT_C_TWINS)

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned == 2
    assert groups == []


def test_c_comments_and_literals_are_not_structure(tree: Path) -> None:
    """One copy carries a comment and different constants; they are one body."""
    _write(tree, "writer.c", RENAMED_C_TWINS)

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert len(groups) == 1


def test_only_definitions_are_scanned(tree: Path) -> None:
    """Everything at file scope that is not a function must be ignored.

    A prototype, a macro, a designated initialiser and a type declaration all
    end in something that looks enough like a header to fool a lazy regex, and
    counting them would put noise at the top of a report nobody then reads.
    """
    _write(tree, "decls.c", r"""
#include "wreathcore.h"

#define WREATH_ROUND_UP(value, to) (((value) + (to) - 1) & ~((to) - 1))

static int writer_grow(Writer *writer, size_t need);

typedef struct {
    PyObject_HEAD
    char *data;
} Writer;

static PyMethodDef writer_methods[] = {
    {"grow", (PyCFunction)writer_grow, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL},
};

PyDoc_STRVAR(writer_doc, "A writer.");
""")

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert (groups, scanned) == ([], 0)


def test_a_brace_inside_a_string_or_a_comment_is_not_a_brace(tree: Path) -> None:
    """The invariant the brace matcher exists for, and nothing else asserted it.

    A matcher that counts every `}` closes `guarded` inside its own string
    literal and then reads the remainder of the file at the wrong depth. The
    damage is not an exception: it is a body silently truncated to something
    under `--min-lines`, which drops out of the scan the same way a body with
    no duplicate does. So the assertion is that both halves are *found* and
    that they are found to be the same shape, which a truncated first half
    cannot be.
    """
    _write(tree, "decoy.c", BRACE_DECOY_C_TWINS)

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert scanned == 2
    assert [site.name for site in groups[0].sites] == ["guarded", "plain"]


def test_a_c_body_is_weighed_in_code_lines_not_tokens(tree: Path) -> None:
    """The C ruler is code *lines*, and a one-line body is one line.

    Both halves need asserting, and `test_a_short_c_body_is_trivia` below
    asserts neither: its bodies are five tokens long, so they stay under the
    floor whichever ruler is applied. Weighing tokens instead makes a two-line
    body look like a twenty-line one and fills the report with trivia; losing
    the floor makes a body written entirely on one line measure zero and
    vanish from it. Neither shows up as an error -- both just change what the
    report contains, in opposite directions.
    """
    _write(tree, "dense.c", r"""
static int
dense(int a, int b)
{
    int total = a + b; int scaled = total * 3; int shifted = scaled << 1;
    /* a comment carries no token, so it is not a line */
    return shifted + a * b - total / 2;
}

static int tiny(int a) { return a + 1; }
""")

    weighed = {body.site.name: body.site.lines
               for body in dup_scan.collect(tree, ("src/wreath",), 1)}

    assert weighed == {"dense": 2, "tiny": 1}
    # ... and at the real floor both are trivia, which counting tokens is not.
    assert dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES) == ([], 0)


def test_a_short_c_body_is_trivia(tree: Path) -> None:
    _write(tree, "small.c", r"""
static int
one(int a)
{
    return a + 1;
}

static int
two(int b)
{
    return b + 2;
}
""")

    groups, scanned = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert (groups, scanned) == ([], 0)


def test_a_language_can_be_selected(tree: Path) -> None:
    """`--lang` exists so a native session is not made to read the Python half."""
    _write(tree, "writer.c", RENAMED_C_TWINS)
    _write(tree, "settle.py", RENAMED_TWINS)

    both, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)
    native, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES,
                              ("native",))
    python, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES,
                              ("python",))

    assert len(both) == 2
    assert [s.name for s in native[0].sites] == ["writer_grow", "sink_reserve"]
    assert [s.name for s in python[0].sites] == ["insert_settled", "replace_settled"]


def test_a_near_copy_is_reported_as_near_and_not_as_exact(tree: Path) -> None:
    """The finding an exact-shape hash cannot make.

    Two bodies that differ by one statement hash differently and so are invisible
    to the group scan, which is how six hand-rolled re-implementations of
    `wreath_load_u32_le` sat beside the real one without anything noticing.
    """
    drifted = RENAMED_C_TWINS.replace(
        "    sink->bytes = fresh;",
        "    memset(fresh + sink->size, 0, grown - sink->size);\n    sink->bytes = fresh;",
    )
    _write(tree, "writer.c", drifted)

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)
    pairs = dup_scan.near_clones(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert groups == [], "one added statement is a different shape"
    assert [(p.left.name, p.right.name) for p in pairs] == [
        ("writer_grow", "sink_reserve"),
    ]
    assert 0.7 <= pairs[0].similarity < 1.0


def test_an_exact_copy_is_not_also_reported_as_near(tree: Path) -> None:
    """Reporting a pair twice is how a report stops being read."""
    _write(tree, "writer.c", RENAMED_C_TWINS)

    groups, _ = dup_scan.scan(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)
    pairs = dup_scan.near_clones(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES)

    assert len(groups) == 1
    assert pairs == []


def test_unrelated_bodies_are_not_near(tree: Path) -> None:
    _write(tree, "unrelated.c", NOT_C_TWINS)

    assert dup_scan.near_clones(tree, ("src/wreath",), dup_scan.DEFAULT_MIN_LINES) == []


def test_it_runs_on_this_repository_and_stays_a_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report, not gate: it must never fail a run, however much it finds."""
    groups, scanned = dup_scan.scan(repo_root(), dup_scan.DEFAULT_ROOTS,
                                    dup_scan.DEFAULT_MIN_LINES)
    assert scanned > 100
    assert all(len(group.sites) > 1 for group in groups)
    # `main` consumes the real result above. Re-scanning the whole repository
    # here used to double this test from roughly three seconds to six merely to
    # prove that findings do not turn the report into a failing gate.
    monkeypatch.setattr(dup_scan, "scan", lambda *_args: (groups, scanned))
    assert dup_scan.main(["--top", "1"]) == 0
