"""Lifecycle of a `StaticFiles` handler: what `close()` releases, and when.

The lookup pool and the pinned root descriptor are the two resources an
instance holds. Both have to go, and they have to go in that order -- a root
descriptor closed while a lookup is still queued would have that lookup call
``openat`` on a number the process may already have handed to something else.
"""

from __future__ import annotations

import os
import time

import pytest

from wreath import Wreath
from wreath.staticfiles import StaticFiles
from wreath.testing import TestClient


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_close_releases_the_root_descriptor(tmp_path) -> None:
    files = StaticFiles(tmp_path)
    root_fd = files._root_fd

    files.close()

    assert files._root_fd == -1
    with pytest.raises(OSError):
        os.fstat(root_fd)


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"), reason="needs /proc to count descriptors"
)
def test_closed_instances_do_not_leak_descriptors(tmp_path) -> None:
    """One fd per instance was leaking; twenty instances made it obvious."""
    StaticFiles(tmp_path).close()  # warm any lazy imports the first one does
    before = _fd_count()

    for _ in range(20):
        StaticFiles(tmp_path).close()

    assert _fd_count() <= before + 2


def test_close_is_idempotent(tmp_path) -> None:
    files = StaticFiles(tmp_path)
    files.close()
    files.close()  # must not close a descriptor number somebody else now owns


def test_close_waits_for_work_already_in_the_pool(tmp_path) -> None:
    """The wait is the precondition for closing the root fd at all."""
    files = StaticFiles(tmp_path, max_workers=1)
    finished: list[str] = []
    files._executor.submit(lambda: (time.sleep(0.2), finished.append("done")))

    started = time.monotonic()
    files.close()

    assert finished == ["done"]
    assert time.monotonic() - started >= 0.2


@pytest.mark.asyncio
async def test_serving_still_works_before_close(tmp_path) -> None:
    """A guard on the fd-closing change: lookups must be unaffected."""
    (tmp_path / "hello.txt").write_bytes(b"hi")
    files = StaticFiles(tmp_path, cache_control=None)
    try:
        resolved = files._resolve("hello.txt")
        assert resolved is not None
        fd, stat, name = resolved
        assert isinstance(fd, int)
        assert stat.st_size == 2
        os.close(fd)
    finally:
        files.close()


# --- reachability through the public API -------------------------------------
#
# `Wreath.static()` builds the instance itself and stores it only as an opaque
# route handler, so before lifespan closed it there was no way for an
# application to satisfy `close()`'s own "call it at shutdown" instruction. The
# leak was bounded -- one descriptor per mount, for the process lifetime -- but
# a documented contract nobody can satisfy is worse than an undocumented one.


def _mount_fd(app) -> int:
    (handler,) = [handler for _prefix, handler in app._static_matcher._mounts]
    return handler._root_fd


@pytest.mark.asyncio
async def test_lifespan_shutdown_closes_a_static_mount(tmp_path) -> None:
    app = Wreath()
    app.static("/assets", str(tmp_path))
    root_fd = _mount_fd(app)
    assert os.fstat(root_fd)  # open while the app is up

    async with TestClient(app):
        pass

    assert _mount_fd(app) == -1, "shutdown did not close the mount's descriptor"
    with pytest.raises(OSError):
        os.fstat(root_fd)


@pytest.mark.asyncio
async def test_a_failed_startup_also_closes_a_static_mount(tmp_path) -> None:
    """Startup failure is the path where nothing else ever will: the server is
    told startup failed and never calls shutdown."""
    app = Wreath()
    app.static("/assets", str(tmp_path))

    @app.on_startup
    async def boom(_app) -> None:
        raise RuntimeError("startup refused")

    root_fd = _mount_fd(app)
    sent: list[dict] = []
    messages = iter([{"type": "lifespan.startup"}])

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert sent and sent[0]["type"] == "lifespan.startup.failed"
    assert _mount_fd(app) == -1, "a failed startup left the mount's descriptor open"
    with pytest.raises(OSError):
        os.fstat(root_fd)
