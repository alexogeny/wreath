from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, cast

from wreath import Wreath
from wreath.testing import TestClient


async def test_a_slow_client_does_not_hold_an_executor_worker(tmp_path) -> None:
    from wreath.response import FileResponse

    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (1024 * 1024))
    loop = asyncio.get_running_loop()
    concrete_loop = cast(Any, loop)
    previous = concrete_loop._default_executor
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    body_started = asyncio.Event()
    release_body = asyncio.Event()

    async def slow_send(message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            body_started.set()
            await release_body.wait()

    response = asyncio.create_task(FileResponse(path)(slow_send))
    try:
        await asyncio.wait_for(body_started.wait(), timeout=5.0)
        available = await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=1.0)
        assert available == "free"
    finally:
        release_body.set()
        await response
        concrete_loop._default_executor = previous
        executor.shutdown()


class TestFileStreamingStillWorks:
    """Whatever the threading shape, the bytes have to be right."""

    async def test_a_large_file_still_streams_correctly(self, tmp_path):
        body = bytes(range(256)) * 4096  # 1 MiB, several chunks
        (tmp_path / "big.bin").write_bytes(body)

        app = Wreath()
        app.static("/files", str(tmp_path))
        async with TestClient(app) as client:
            response = await client.get("/files/big.bin")
        assert response.status == 200
        assert response.body == body

    async def test_an_empty_file_still_streams(self, tmp_path):
        (tmp_path / "empty.bin").write_bytes(b"")
        app = Wreath()
        app.static("/files", str(tmp_path))
        async with TestClient(app) as client:
            response = await client.get("/files/empty.bin")
        assert response.status == 200
        assert response.body == b""

    async def test_a_range_of_a_large_file_streams(self, tmp_path):
        body = bytes(range(256)) * 4096
        (tmp_path / "big.bin").write_bytes(body)
        app = Wreath()
        app.static("/files", str(tmp_path))
        async with TestClient(app) as client:
            response = await client.get("/files/big.bin", headers={"range": "bytes=1000-1999"})
        assert response.status == 206
        assert response.body == body[1000:2000]


class TestStaticFilesHasItsOwnExecutor:
    """G-84: `_resolve` runs on the default executor, so static file lookups
    compete with every other `to_thread` in the application -- and a burst of
    them starves whatever else needed a thread."""

    def test_static_files_do_not_use_the_default_executor(self, tmp_path):
        import inspect

        from wreath.staticfiles import StaticFiles

        source = inspect.getsource(StaticFiles)
        assert "asyncio.to_thread" not in source, (
            "static lookups still run on the loop's shared default executor"
        )

    async def test_the_executor_is_bounded(self, tmp_path):
        from wreath.staticfiles import StaticFiles

        files = StaticFiles(str(tmp_path))
        executor = files._executor
        assert isinstance(executor, concurrent.futures.ThreadPoolExecutor)
        assert executor._max_workers <= 16

    async def test_lookup_submission_is_bounded_before_the_executor(self, tmp_path, monkeypatch):
        from wreath.staticfiles import StaticFiles

        files = StaticFiles(str(tmp_path), max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def blocked_resolve(self, rest):
            started.set()
            release.wait()
            return None

        monkeypatch.setattr(StaticFiles, "_resolve", blocked_resolve)

        class Request:
            path_params = {"path": "missing"}

        first = asyncio.create_task(files(cast(Any, Request())))
        assert await asyncio.to_thread(started.wait, 5.0)
        second = asyncio.create_task(files(cast(Any, Request())))
        await asyncio.sleep(0)

        assert files._executor._work_queue.qsize() == 0
        assert files._lookup_slots.locked()

        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        files.close()
        assert all(isinstance(result, Exception) for result in results)

    async def test_head_does_not_read_the_file(self, tmp_path, monkeypatch):
        import wreath.response as response_module

        body = bytes(range(256)) * 4096
        (tmp_path / "big.bin").write_bytes(body)

        def unexpected_read(fd, size):
            raise AssertionError("HEAD read file contents")

        monkeypatch.setattr(response_module.os, "read", unexpected_read)
        app = Wreath()
        app.static("/files", str(tmp_path))
        async with TestClient(app) as client:
            response = await client.head("/files/big.bin")

        assert response.status == 200
        assert response.body == b""
        assert response.header("content-length") == str(len(body))

    async def test_ranged_head_does_not_read_the_file(self, tmp_path, monkeypatch):
        import wreath.response as response_module

        (tmp_path / "big.bin").write_bytes(b"x" * 4096)

        def unexpected_read(fd, size):
            raise AssertionError("ranged HEAD read file contents")

        monkeypatch.setattr(response_module.os, "read", unexpected_read)
        app = Wreath()
        app.static("/files", str(tmp_path))
        async with TestClient(app) as client:
            response = await client.head("/files/big.bin", headers={"range": "bytes=100-199"})

        assert response.status == 206
        assert response.body == b""
        assert response.header("content-length") == "100"
        assert response.header("content-range") == "bytes 100-199/4096"

    async def test_files_are_still_served(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        app = Wreath()
        app.static("/files", str(tmp_path))
        async with TestClient(app) as client:
            response = await client.get("/files/a.txt")
        assert response.body == b"hello"

    async def test_concurrent_lookups_all_complete(self, tmp_path):
        for index in range(20):
            (tmp_path / f"f{index}.txt").write_text(str(index))
        app = Wreath()
        app.static("/files", str(tmp_path))
        async with TestClient(app) as client:
            responses = await asyncio.gather(
                *(client.get(f"/files/f{index}.txt") for index in range(20))
            )
        assert [r.body for r in responses] == [str(i).encode() for i in range(20)]

    def test_the_executor_shuts_down(self, tmp_path):
        from wreath.staticfiles import StaticFiles

        files = StaticFiles(str(tmp_path))
        files.close()
        assert files._executor._shutdown
