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

import ast
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

def test_every_component_method_is_callable_with_no_arguments() -> None:
    """`Wreath.schema_components` calls `component()`, so nothing may need an argument.

    The collection walk is duck-typed -- `getattr(candidate, "component", None)`
    then `claim()` -- so a table-owning object that requires a keyword is a
    `TypeError` waiting for the first application whose `schema_owners` reaches
    it. That is not hypothetical plumbing: `quota.QuotaRegistry.schema_owners`
    already answers with store objects rather than with itself.

    `component` used to mean four different things: this zero-argument protocol,
    a declaration-level `component(*, name)` with no default, a walk-facing
    `component(*, name=<default>)`, and two module-level factories taking a
    schema. The first and third are indistinguishable at the call site and the
    second would have raised. The declaration level is now `schema_claim`, which
    leaves one meaning per name.

    Read from source rather than by walking a built application: an arity defect
    on a subsystem this test forgot to register would not show up, and the
    signature is the whole claim.
    """
    offenders = []
    root = Path(__file__).resolve().parents[1] / "src" / "wreath"
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        # Sound pre-filter, not a heuristic: the node this looks for is a
        # `FunctionDef` *named* `component`, and a name cannot exist in a tree
        # without its spelling existing in the source. Skipping the files that
        # do not contain the substring therefore skips only provable non-matches
        # -- 68% of the tree's bytes -- while reading stays 22 ms against 1.9 s
        # to parse. The whole tree is still swept, which is the point of the
        # test: a subsystem it forgot to register must not escape.
        if "component" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, ast.FunctionDef) or item.name != "component":
                    continue
                args = item.args
                required = [a.arg for a in args.posonlyargs + args.args if a.arg != "self"]
                required += [
                    a.arg
                    for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                    if d is None
                ]
                optional = [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults,
                                                  strict=True) if d is not None]
                if required or optional:
                    offenders.append(
                        f"{path.relative_to(root)}:{item.lineno} "
                        f"{node.name}.component takes {required + optional}"
                    )
    assert offenders == []
