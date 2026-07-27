"""Documented Python that cannot work, caught by shape rather than by execution.

Three doc sites shipped a probe built on `db.pool("read").fetchval(...)` and four
more on `async with db.pool("write").acquire() as conn`. Neither can run: `Pool`
leases connections and has no query methods, and `Pool.acquire` is a plain
coroutine rather than an async context manager. Both failed *quietly* -- the
first as a health check reporting 503 forever, the second only when someone ran
the example -- so nothing surfaced them for as long as they sat there.

Executing every block in the corpus is not on (most need a database, an app, and
a schema). Checking the *shape* of the calls against the real classes is, and it
generalises: the rules below are derived from what `Pool` actually has, so a
`Pool` that grows `fetchval` tomorrow relaxes the rule automatically instead of
leaving a stale assertion behind.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from wreath.postgres import Pool

DOCS = Path(__file__).resolve().parent.parent / "docs"
_PYTHON_BLOCK = re.compile(r"^```python[^\n]*\n(.*?)^```", re.M | re.S)

#: `pool(...)` immediately followed by a method call, e.g. `db.pool("read").fetchval(`.
_POOL_METHOD = re.compile(r"\.pool\(\s*[^)]*\)\s*\.\s*(\w+)")
#: `async with <anything>.acquire(...)`, with or without an `as` clause.
_ASYNC_WITH_ACQUIRE = re.compile(r"async\s+with\s+[^\n]*?\.acquire\s*\(")


def _blocks() -> list[tuple[Path, str]]:
    out = []
    for md in sorted(DOCS.rglob("*.md")):
        for match in _PYTHON_BLOCK.finditer(md.read_text(encoding="utf-8")):
            out.append((md, match.group(1)))
    return out


def test_the_corpus_has_blocks_to_check() -> None:
    """Guards the guard: a regex that silently matched nothing would make every
    assertion below vacuously true."""
    assert len(_blocks()) > 100


def test_no_doc_calls_a_method_pool_does_not_have() -> None:
    """`db.pool("read").fetchval("SELECT 1")` was in two places. Derived from
    `Pool` itself, so this rule tracks the class rather than a frozen list."""
    offenders = []
    for path, source in _blocks():
        for method in _POOL_METHOD.findall(source):
            if not hasattr(Pool, method):
                offenders.append(f"{path.relative_to(DOCS)}: .pool(...).{method}()")
    assert offenders == [], offenders


def test_no_doc_uses_acquire_as_an_async_context_manager() -> None:
    """`async with pool.acquire() as conn` raises `TypeError`; it returns a
    connection, and the pairing release belongs in a `finally`."""
    assert not hasattr(Pool.acquire, "__aenter__")     # the premise, stated
    offenders = [
        str(path.relative_to(DOCS))
        for path, source in _blocks()
        if _ASYNC_WITH_ACQUIRE.search(source)
    ]
    assert offenders == [], offenders


@pytest.mark.parametrize("name", ["fetchval", "fetch", "fetchrow", "execute"])
def test_pool_still_has_no_query_methods(name: str) -> None:
    """If this goes red the two rules above are stale, not the docs."""
    assert not hasattr(Pool, name)


def test_acquire_is_still_a_plain_coroutine_function(name: str = "acquire") -> None:
    assert inspect.iscoroutinefunction(getattr(Pool, name))
