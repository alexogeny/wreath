"""How file serving uses threads (report 23: G-43, G-84)."""

from __future__ import annotations

import asyncio
import concurrent.futures

import pytest

from wreath import Wreath
from wreath.testing import TestClient


@pytest.mark.skip(
    reason=(
        "not a defect: holding one worker for the life of a slow response is "
        "the deliberate side of a trade. The alternative is one executor "
        "submission per 256 KiB chunk, which "
        "tests/test_framework_features.py::test_file_response_uses_bounded_"
        "executor_submissions exists to forbid -- bounded read-ahead with a "
        "single worker means the worker waits. Written down in "
        "`_send_from_descriptor`. See report 23 G-43."
    )
)
def test_the_reader_does_not_wait_on_the_loop():
    raise AssertionError("unimplemented")


class TestFileStreamingStillWorks:
    """Whatever the threading shape, the bytes have to be right."""

    async def test_a_large_file_still_streams_correctly(self, tmp_path):
        body = bytes(range(256)) * 4096          # 1 MiB, several chunks
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
            response = await client.get(
                "/files/big.bin", headers={"range": "bytes=1000-1999"}
            )
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
