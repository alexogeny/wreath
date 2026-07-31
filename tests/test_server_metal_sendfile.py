"""A file response leaves the metal tier over a real socket.

`FileResponse` -- and so every route served by `wreath.staticfiles` -- hands the
descriptor to `loop.sendfile`. asyncio decides how to send it by reading
`_sendfile_compatible` off the transport, defaults that to *unsupported*, and
raises rather than falling back. The native C transport is not one of asyncio's
own, carried no such attribute, and every file response on `--loop metal` died
with `RuntimeError: sendfile is not supported for transport ...` while the same
route worked on every other tier.

Nothing caught it because no test had ever put a file response and the metal
loop in the same process: the file suites run on the default loop, and the
metal suites serve JSON. This one crosses them, and asserts the bytes arrive
rather than merely that nothing raised -- the failure mode was a 0-byte
response, which an exception-only assertion would have called a pass.
"""
from __future__ import annotations

import asyncio
import errno
import importlib

import pytest

from wreath import Wreath
from wreath.response import FileResponse
from wreath.server import ServerConfig, serve

BODY = b"metal-sendfile-" * 512


def _metal_loop_or_skip():
    reactor = importlib.import_module("wreath.reactor")
    try:
        return reactor.metal_event_loop(diagnostics=True)
    except OSError as exc:
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        pytest.skip("io_uring unavailable")


def _app(path: str) -> Wreath:
    app = Wreath()

    @app.get("/file")
    async def file_route(request):
        return FileResponse(path, media_type=b"application/octet-stream")

    @app.get("/json")
    async def json_route(request):
        return {"ok": True}

    return app


async def _request(port: int, target: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(-1), 5)
    finally:
        writer.close()


def _run_on(loop, path: str, target: str) -> bytes:
    async def main() -> bytes:
        server = await serve(_app(path), ServerConfig(host="127.0.0.1", port=0))
        port = server.sockets[0].getsockname()[1]
        try:
            return await _request(port, target)
        finally:
            await server.close()

    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(asyncio.wait_for(main(), 10))
    finally:
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "payload.bin"
    path.write_bytes(BODY)
    return str(path)


def test_file_response_over_metal(sample):
    """The regression: the body arrives, not just the head."""
    response = _run_on(_metal_loop_or_skip(), sample, "/file")
    head, _, body = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK"), response[:200]
    assert f"content-length: {len(BODY)}".encode() in head.lower()
    assert body == BODY


def _comparable(response: bytes) -> tuple[list[bytes], bytes]:
    """The parts of a response the tier is answerable for.

    `date:` is dropped: the two runs happen a moment apart and the header is
    generated per response, so comparing it asserts that the clock did not tick
    -- which it does, and which says nothing about how the bytes were sent.
    """
    head, _, body = response.partition(b"\r\n\r\n")
    lines = [
        line for line in head.split(b"\r\n")
        if not line.lower().startswith((b"date:", b"server:"))
    ]
    return lines, body


def test_file_response_matches_the_default_loop(sample):
    """Metal and stock send the same response.

    Sending the file is the tier's job; what is sent is not, so any difference
    between the two is a defect in the tier by definition.
    """
    metal = _run_on(_metal_loop_or_skip(), sample, "/file")
    stock = _run_on(asyncio.new_event_loop(), sample, "/file")
    assert _comparable(metal) == _comparable(stock)


def test_metal_still_serves_an_ordinary_response(sample):
    """The sendfile path must not have disturbed the common one."""
    response = _run_on(_metal_loop_or_skip(), sample, "/json")
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert response.endswith(b'{"ok":true}')
