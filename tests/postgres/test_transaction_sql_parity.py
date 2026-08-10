"""The native transaction-control test must answer exactly what Python does.

`_submit` runs this on every statement, so it decides whether an operation
raises the transaction barrier. Getting it wrong in one direction lets a `BEGIN`
join a flight it should have been a barrier for; in the other it makes an
ordinary `SELECT` serialise the pipeline. Neither shows up as a crash.

The C twin takes an ASCII fast path and falls back to the Python function for
anything else, so the corpus below deliberately includes non-ASCII, leading
whitespace of several kinds, mixed case, and strings that merely *start with*
a keyword without being one.
"""

from __future__ import annotations

import pytest

from wreath._pgdriver import _is_transaction_sql as reference

native = pytest.importorskip("wreath._native._postgres")

CORPUS = [
    # The six keywords, as the reference spells them.
    "BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
    # Case is folded.
    "begin", "Begin", "bEgIn", "commit", "RoLlBaCk",
    # Leading whitespace of every ASCII kind `str.lstrip()` removes.
    " BEGIN", "\tBEGIN", "\nBEGIN", "\rBEGIN", "\x0bBEGIN", "\x0cBEGIN",
    "   \t\n  COMMIT", "\n\n\nROLLBACK TO SAVEPOINT x",
    # A keyword with a tail: the first token is what counts.
    "BEGIN TRANSACTION", "COMMIT;", "ROLLBACK TO SAVEPOINT a",
    "SAVEPOINT my_point", "RELEASE SAVEPOINT my_point",
    "START TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    # Not transaction control, including things that merely start alike.
    "SELECT 1", "select 1", "BEGINNING", "COMMITTED", "STARTS",
    "ROLLBACKS", "SAVEPOINTS", "RELEASED", "UPDATE t SET a = 1",
    'SELECT id FROM "Fortune"', "INSERT INTO t VALUES (1)",
    # `COMMIT;` splits on whitespace, so the semicolon stays attached and this
    # is *not* the bare keyword -- a difference worth pinning rather than
    # assuming.
    "BEGIN;", "begin;",
    # Empty and whitespace-only.
    "", " ", "\t", "\n", "   \t\n ",
    # Non-ASCII: the C twin must hand these to Python rather than guess.
    "SELECT 'フレームワーク'", "БЕГИН", " BEGIN", "　COMMIT",
    "BEGIN TRANSACTION", "ＢＥＧＩＮ",
]


@pytest.mark.parametrize("sql", CORPUS, ids=lambda s: repr(s))
def test_the_native_transaction_test_agrees_with_python(sql: str) -> None:
    assert native._is_transaction_sql(sql) == reference(sql), (
        f"`codec.c` and `_pgdriver` disagree on {sql!r}"
    )


def test_the_corpus_covers_both_answers() -> None:
    """A corpus that was all True or all False would pass a stub either way."""
    answers = {reference(sql) for sql in CORPUS}
    assert answers == {True, False}


@pytest.mark.parametrize("sql", [" BEGIN", "　COMMIT", "ＢＥＧＩＮ"])
def test_non_ascii_is_answered_by_the_python_twin(sql: str) -> None:
    """The fallback is the point, not an implementation detail.

    `\\u00a0` is whitespace to `str.lstrip()` and not to a byte scan, and
    `ＢＥＧＩＮ` uppercases to itself rather than to `BEGIN`. A C fast path that
    tried to handle either would diverge, so it must decline them.
    """
    assert native._is_transaction_sql(sql) == reference(sql)
