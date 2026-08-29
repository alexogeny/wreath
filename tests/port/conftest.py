from pathlib import Path

import pytest

_HERE = Path(__file__).parent

# Never let pytest import/collect the fixture trees. `foreign/` is the same kind
# of input text as `corpus/` — read, never run — and imports flask, django,
# tornado, pyramid, aiohttp, bottle and gevent, none of which are installed.
collect_ignore_glob = ["corpus/*", "corpus/**/*", "foreign/*", "foreign/**/*"]


@pytest.fixture
def corpus_root() -> Path:
    return _HERE / "corpus"


@pytest.fixture
def foreign_root() -> Path:
    """Applications in frameworks `wreath port` does not *target*.

    Separate from `corpus/` because the contract is different, and narrower than
    it once was. When these fixtures arrived, a foreign framework was entirely
    unportable, so "no findings" and "correct" were the same sentence. They are
    not any more: Flask, aiohttp, Tornado, Pyramid and Bottle constructs with an
    exact wreath spelling are translated now, so a root here yielding
    `translated` findings is the tool working rather than regressing.

    Two properties are what the guard was ever protecting, and both are asserted
    in `test_foreign_frameworks.py`:

    * `detection.portable` stays `False`, with warnings — a foreign or
      monkeypatched tree must never be reported as portable; and
    * **no finding in the `routing` category.** That is the exact failure: a
      Bottle application once scored 1.00 coverage because `@app.get("/x")` is
      spelled the way FastAPI spells it and `route.method` fired on a framework
      the tool had never identified. `foreign.flask.route` is a different rule in
      a different category and does not threaten it — every foreign-framework
      finding stays in the `foreign` category whatever its verdict, which is what
      makes that property structural rather than a coincidence.
    """
    return _HERE / "foreign"


@pytest.fixture
def foreign_app_roots(foreign_root) -> list:
    return sorted(p for p in foreign_root.iterdir() if p.is_dir())


@pytest.fixture
def corpus_app_roots(corpus_root) -> list:
    """The app roots a future coverage run globs over (design 07 §5)."""
    return sorted(p for p in corpus_root.iterdir() if p.is_dir())
