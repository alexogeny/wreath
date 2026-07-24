"""Fixtures for the ``wreath port`` corpus tests.

The ``corpus/`` tree is *input text* for the future ``wreath port`` codemod. It
imports third-party frameworks (fastapi, ormar, sqlmodel, celery, authlib, ...)
that are not installed for the test run, and it is never executed or imported by
these tests — only ever read as source. So it MUST be excluded from pytest
collection, or collection would try to import it and error out.
"""
from pathlib import Path

import pytest

_HERE = Path(__file__).parent

# Never let pytest import/collect the corpus modules.
collect_ignore_glob = ["corpus/*", "corpus/**/*"]


@pytest.fixture
def corpus_root() -> Path:
    return _HERE / "corpus"


@pytest.fixture
def corpus_app_roots(corpus_root) -> list:
    """The app roots a future coverage run globs over (design 07 §5)."""
    return sorted(p for p in corpus_root.iterdir() if p.is_dir())
