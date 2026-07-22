"""Poller backends: epoll (stage 1) and io_uring (stage 4) must be swappable and
observably identical at the loop-behaviour level.

`backend=None` picks the best available. Selecting a backend the host cannot
provide must raise a clear error rather than silently degrade.
"""
from __future__ import annotations

import asyncio

import pytest

from .conftest import require_reactor
from .support import socketpair


def test_epoll_is_always_available():
    r = require_reactor()  # RED until built
    assert "epoll" in r.available_backends()


def test_default_backend_is_reported(make_native_loop):
    loop = make_native_loop(None)
    assert loop.reactor_backend() in ("epoll", "io_uring")


@pytest.mark.parametrize("backend", ["epoll", "io_uring"])
def test_selected_backend_is_honoured(make_native_loop, backend):
    r = require_reactor()
    if backend not in r.available_backends():
        with pytest.raises(ValueError):
            make_native_loop(backend)
        return
    loop = make_native_loop(backend)
    assert loop.reactor_backend() == backend


def test_readiness_is_identical_across_backends(make_native_loop):
    r = require_reactor()
    results = {}
    for backend in r.available_backends():
        loop = make_native_loop(backend)
        a, b = socketpair()
        got: list[bytes] = []

        async def main(loop=loop, a=a, b=b, got=got):
            def on_read():
                got.append(a.recv(10))
                loop.remove_reader(a.fileno())

            loop.add_reader(a.fileno(), on_read)
            b.send(b"z")
            await asyncio.sleep(0.02)
            return got

        try:
            results[backend] = loop.run_until_complete(main())
        finally:
            a.close()
            b.close()

    assert results  # at least epoll
    assert all(v == [b"z"] for v in results.values())
