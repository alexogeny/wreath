"""Fixtures for the native-reactor spec.

The reactor does not exist yet, so the `loop` fixture yields an `_UnbuiltLoop`
that raises on first use. That makes every spec line report as a FAILURE in the
test body (red) rather than a skip or a collection error. Once
`wreath.reactor.new_event_loop()` exists, the fixture yields the real loop and
each test fails instead at its own assertion until that behaviour is built.
"""
from __future__ import annotations

import importlib

import pytest

try:
    reactor = importlib.import_module("wreath.reactor")
except ImportError:  # not built yet — the expected state today
    reactor = None

REASON = (
    "native reactor not built — implement wreath.reactor.new_event_loop(); "
    "this spec line stays RED until then"
)


class _UnbuiltLoop:
    """Yielded before the reactor exists; every use fails the test in-body."""

    def __getattr__(self, name: str):
        raise AssertionError(f"{REASON} (used loop.{name})")


def _new_native_loop(backend: str | None = None):
    if reactor is None or not hasattr(reactor, "new_event_loop"):
        return None
    return reactor.new_event_loop(backend)


def require_reactor():
    """Return the reactor module or fail the calling test red."""
    assert reactor is not None and hasattr(reactor, "new_event_loop"), REASON
    return reactor


@pytest.fixture
def loop():
    """A fresh native reactor loop (or an _UnbuiltLoop that fails on use)."""
    lp = _new_native_loop()
    if lp is None:
        yield _UnbuiltLoop()
        return
    try:
        yield lp
    finally:
        lp.close()


@pytest.fixture
def make_native_loop():
    """Factory for tests needing more than one loop (e.g. multi-worker)."""
    created: list = []

    def _factory(backend: str | None = None):
        lp = _new_native_loop(backend)
        if lp is None:
            return _UnbuiltLoop()
        created.append(lp)
        return lp

    yield _factory
    for lp in created:
        lp.close()
