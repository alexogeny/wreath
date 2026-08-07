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
