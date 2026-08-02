"""Lifecycle of a `StaticFiles` handler: what `close()` releases, and when.

The lookup pool and the pinned root descriptor are the two resources an
instance holds. Both have to go, and they have to go in that order -- a root
descriptor closed while a lookup is still queued would have that lookup call
``openat`` on a number the process may already have handed to something else.
"""

from __future__ import annotations

import os
import threading

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


def test_close_waits_for_work_already_in_the_pool(tmp_path, monkeypatch) -> None:
    """The wait is the precondition for closing the root fd at all."""
    files = StaticFiles(tmp_path, max_workers=1)
    finished: list[str] = []
    worker_started = threading.Event()
    release_worker = threading.Event()
    shutdown_entered = threading.Event()
    original_shutdown = files._executor.shutdown

    def work() -> None:
        worker_started.set()
        release_worker.wait()
        finished.append("done")

    def observed_shutdown(*args, **kwargs) -> None:
        shutdown_entered.set()
        original_shutdown(*args, **kwargs)

    monkeypatch.setattr(files._executor, "shutdown", observed_shutdown)
    files._executor.submit(work)
    assert worker_started.wait(timeout=5.0)
    closer = threading.Thread(target=files.close)
    closer.start()
    assert shutdown_entered.wait(timeout=5.0)
    assert closer.is_alive(), "close returned while pool work was blocked"
    release_worker.set()
    closer.join(timeout=5.0)

    assert finished == ["done"]
    assert not closer.is_alive(), "close stayed blocked after pool work completed"


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


# --- the four response shapes nothing was watching ----------------------------
#
# `wreath mutant` survived every conditional in `__call__` below the resolve:
# the directory redirect, the cache-control header, the conditional 304 and the
# unsatisfiable-range 416. All four are HTTP behaviours a client depends on and
# none of them errors when it stops happening -- the response is simply wrong in
# a way only a browser notices.


def _static_app(root, **kwargs):
    from wreath import Wreath

    app = Wreath()
    app.static("/assets", str(root), **kwargs)
    return app


@pytest.mark.asyncio
async def test_a_directory_without_a_trailing_slash_redirects_canonically(tmp_path) -> None:
    """Serving the index from the un-slashed path breaks every relative link.

    `/assets/docs` and `/assets/docs/` resolve relative URLs one level apart, so
    the index has to be reached at the canonical path or its own `<img src="a.png">`
    asks for `/assets/a.png`. 308 rather than 301 so the method and body survive.
    """
    from wreath.testing import TestClient

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.html").write_bytes(b"<h1>hi</h1>")

    app = _static_app(tmp_path)
    async with TestClient(app) as client:
        response = await client.get("/assets/docs")
        assert response.status == 308
        assert response.header("location") == "/assets/docs/"

        served = await client.get("/assets/docs/")
        assert served.status == 200
        assert served.body == b"<h1>hi</h1>"


@pytest.mark.asyncio
async def test_a_root_static_mount_cannot_emit_a_scheme_relative_redirect(tmp_path) -> None:
    """A doubled leading slash in Location changes the browser's destination.

    Root mounts used to normalize ``/`` to ``//``.  Besides making ordinary
    ``/docs`` miss the mount, that admitted ``//docs`` and reflected it as
    ``Location: //docs/`` -- an authority-form redirect to host ``docs``.
    """
    from wreath import Wreath
    from wreath.testing import TestClient

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.html").write_bytes(b"root index")
    app = Wreath()
    app.static("/", str(tmp_path))

    async with TestClient(app) as client:
        canonical = await client.get("/docs")
        doubled = await client.get("//docs")

    assert canonical.status == 308
    assert canonical.header("location") == "/docs/"
    assert doubled.status == 308
    assert doubled.header("location") == "/docs/"


@pytest.mark.asyncio
async def test_a_missing_file_is_a_404(tmp_path) -> None:
    from wreath.testing import TestClient

    app = _static_app(tmp_path)
    async with TestClient(app) as client:
        assert (await client.get("/assets/nope.txt")).status == 404


@pytest.mark.asyncio
async def test_cache_control_is_sent_when_configured_and_absent_when_not(
    tmp_path,
) -> None:
    """Both halves: asserting only the presence passes with the condition gone."""
    from wreath.staticfiles import CacheControl
    from wreath.testing import TestClient

    (tmp_path / "a.txt").write_bytes(b"hi")

    app = _static_app(tmp_path, cache_control=CacheControl(max_age=60))
    async with TestClient(app) as client:
        response = await client.get("/assets/a.txt")
        assert response.status == 200
        assert b"max-age=60" in response.header("cache-control").encode()

    bare = _static_app(tmp_path, cache_control=None)
    async with TestClient(bare) as client:
        response = await client.get("/assets/a.txt")
        assert response.header("cache-control") is None


@pytest.mark.asyncio
async def test_a_matching_etag_is_answered_304_with_no_body(tmp_path) -> None:
    """The conditional request, which is most of a static server's traffic."""
    from wreath.testing import TestClient

    (tmp_path / "a.txt").write_bytes(b"hello")

    app = _static_app(tmp_path)
    async with TestClient(app) as client:
        first = await client.get("/assets/a.txt")
        etag = first.header("etag")
        assert first.status == 200 and etag

        again = await client.get("/assets/a.txt", headers={"if-none-match": etag})
        assert again.status == 304
        assert again.body == b""
        assert again.header("etag") == etag

        # A stale validator still gets the body, so this is not "304 always".
        stale = await client.get("/assets/a.txt", headers={"if-none-match": '"nope"'})
        assert stale.status == 200 and stale.body == b"hello"


@pytest.mark.asyncio
async def test_a_range_beyond_the_file_is_416_with_the_real_length(tmp_path) -> None:
    """416 must carry `Content-Range: bytes * /size` or the client cannot recover."""
    from wreath.testing import TestClient

    (tmp_path / "a.txt").write_bytes(b"hello")

    app = _static_app(tmp_path)
    async with TestClient(app) as client:
        refused = await client.get("/assets/a.txt", headers={"range": "bytes=99-200"})
        assert refused.status == 416
        assert refused.header("content-range") == "bytes */5"

        # A satisfiable range still works, so this is not "416 always".
        partial = await client.get("/assets/a.txt", headers={"range": "bytes=1-3"})
        assert partial.status == 206
        assert partial.body == b"ell"
        assert partial.header("content-range") == "bytes 1-3/5"
