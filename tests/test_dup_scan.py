"""The structural duplicate scanner.

The scan is only worth reading if it sees the duplication this repository
actually produces — the same body under different names, with different locals
and different literals — and stays quiet about bodies that merely share a
statement count.
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
    return result.rowcount


def replace_settled(session, relation, rows):
    """A different docstring entirely."""
    ready = _coerce(rows)
    if not ready:
        return 99
    query = _build(relation, ready)
    outcome = session.execute(query)
    _record(outcome, relation)
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
    return average


def gather(source):
    out = []
    while source:
        out.append(source.pop())
    if not out:
        raise ValueError("empty")
    _log(out)
    return out
'''


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


def test_ranking_is_by_the_lines_a_collapse_would_remove(tree: Path) -> None:
    group = dup_scan.Group("d", (
        dup_scan.Site("a.py", "one", 1, 10),
        dup_scan.Site("b.py", "two", 1, 10),
        dup_scan.Site("c.py", "three", 1, 10),
    ))
    assert group.redundant_lines == 20  # the copies after the first, not all three


def test_it_runs_on_this_repository_and_stays_a_report() -> None:
    """Report, not gate: it must never fail a run, however much it finds."""
    groups, scanned = dup_scan.scan(repo_root(), dup_scan.DEFAULT_ROOTS,
                                    dup_scan.DEFAULT_MIN_LINES)
    assert scanned > 100
    assert all(len(group.sites) > 1 for group in groups)
    assert dup_scan.main(["--top", "1"]) == 0
