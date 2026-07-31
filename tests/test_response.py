"""Response header assembly regressions.

The header lists are built through precomputed content-type pairs and a
cached content-length table; these tests pin the observable output.
"""

from __future__ import annotations

import gc
import os
from typing import Any, cast

import pytest

from wreath.response import (
    FileResponse,
    JSONResponse,
    PreparedResponse,
    Response,
    StreamingResponse,
    TextResponse,
)


@pytest.mark.asyncio
async def test_file_response_prefers_native_descriptor_path(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"native-file")

    class Protocol:
        def __init__(self) -> None:
            self.started = False
            self.finished = False
            self.payload = b""

        async def _asgi_send(self, message) -> None:
            raise AssertionError("native file response used generic ASGI send")

        async def _wreath_file_start(self, status, headers, file, size) -> None:
            self.started = status == 200 and (b"content-length", b"11") in headers
            assert size == 11
            self.payload = file.read()

        async def _wreath_file_finish(self) -> None:
            self.finished = True

    protocol = Protocol()
    await FileResponse(path)(protocol._asgi_send)

    assert protocol.started and protocol.finished
    assert protocol.payload == b"native-file"


@pytest.mark.asyncio
async def test_file_reader_errors_are_relayed_to_the_sender(
    monkeypatch, tmp_path
) -> None:
    import wreath.response as response_module

    path = tmp_path / "asset.bin"
    path.write_bytes(b"broken")
    descriptor = os.open(path, os.O_RDONLY)

    def fail_read(fd: int, size: int) -> bytes:
        raise OSError("reader failed")

    async def send(message) -> None:
        if message["type"] == "http.response.body":
            assert isinstance(message["body"], bytes)

    monkeypatch.setattr(response_module.os, "read", fail_read)
    with pytest.raises(OSError, match="reader failed"):
        await response_module._send_from_descriptor(
            descriptor,
            path.stat().st_size,
            200,
            [],
            send,
        )


def test_default_headers() -> None:
    response = Response(b"abc")
    assert response.status == 200
    assert response.headers == [
        (b"content-type", b"application/octet-stream"),
        (b"content-length", b"3"),
    ]


def test_subclasses_carry_their_media_type() -> None:
    assert TextResponse("hi").headers == [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", b"2"),
    ]
    assert JSONResponse({"a": 1}).headers == [
        (b"content-type", b"application/json"),
        (b"content-length", b"7"),
    ]


def test_content_length_beyond_cache() -> None:
    # The small-value cache covers < 1024; larger bodies format on demand.
    for size in (0, 1, 1023, 1024, 1025, 70000):
        response = Response(b"x" * size)
        assert (b"content-length", str(size).encode()) in response.headers


def test_media_type_override_and_empty() -> None:
    response = Response(b"x", media_type=b"text/csv")
    assert (b"content-type", b"text/csv") in response.headers
    response = Response(b"x", media_type=b"")
    assert not any(key == b"content-type" for key, _ in response.headers)


def test_status_without_body_omits_content_length() -> None:
    for status in (204, 304):
        response = Response(b"", status=status)
        assert not any(key == b"content-length" for key, _ in response.headers)


def test_caller_headers_are_preserved_and_deduplicated() -> None:
    response = Response(
        b"abc",
        headers=[(b"x-custom", b"1"), (b"Content-Type", b"text/html")],
    )
    # Existing content-type (any case) wins; content-length is appended.
    assert response.headers[:2] == [(b"x-custom", b"1"), (b"Content-Type", b"text/html")]
    assert response.headers[2:] == [(b"content-length", b"3")]

    response = Response(b"abc", headers=[(b"CONTENT-LENGTH", b"999")])
    assert (b"content-type", b"application/octet-stream") in response.headers
    assert sum(key.lower() == b"content-length" for key, _ in response.headers) == 1


def test_default_header_lists_are_not_shared_between_instances() -> None:
    first = Response(b"a")
    second = Response(b"bb")
    first.headers.append((b"x-marker", b"1"))
    assert (b"x-marker", b"1") not in second.headers


@pytest.mark.asyncio
async def test_streaming_response_unchanged() -> None:
    async def chunks():
        yield b"a"
        yield b"b"

    sent = []

    async def send(message):
        sent.append(message)

    await StreamingResponse(chunks(), headers=[(b"content-type", b"text/plain")])(send)
    assert sent[0]["headers"] == [(b"content-type", b"text/plain")]
    assert [m.get("body") for m in sent[1:]] == [b"a", b"b", b""]


# --- PreparedResponse -----------------------------------------------------------


def test_prepared_text_headers_and_body() -> None:
    response = PreparedResponse.text("service healthy")
    assert response.status == 200
    assert response.body == b"service healthy"
    assert (b"content-type", b"text/plain; charset=utf-8") in response.headers
    assert (b"content-length", b"15") in response.headers
    assert response.background is None


def test_prepared_json_and_html() -> None:
    j = PreparedResponse.json({"ok": True})
    assert j.body == b'{"ok":true}'
    assert (b"content-type", b"application/json") in j.headers
    h = PreparedResponse.html("<b>hi</b>")
    assert h.body == b"<b>hi</b>"
    assert (b"content-type", b"text/html; charset=utf-8") in h.headers


def test_prepared_no_body_status_drops_length() -> None:
    response = PreparedResponse(b"ignored", status=204)
    assert response.body == b""
    assert all(name != b"content-length" for name, _ in response.headers)


@pytest.mark.asyncio
async def test_prepared_emits_two_messages() -> None:
    response = PreparedResponse.text("hi")
    sent = []

    async def send(message):
        sent.append(message)

    await response(send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1] == {"type": "http.response.body", "body": b"hi"}


@pytest.mark.asyncio
async def test_prepared_is_reusable_and_immutable() -> None:
    response = PreparedResponse.text("shared")
    first_headers = response.headers

    async def collect():
        sent = []

        async def send(message):
            sent.append(message)

        await response(send)
        return sent

    a = await collect()
    b = await collect()
    # Same bytes each time and no per-call mutation of the shared instance.
    assert a[1]["body"] == b[1]["body"] == b"shared"
    assert response.headers is first_headers


def test_prepared_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        PreparedResponse(cast(Any, "not bytes"))


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_from_descriptor_closes_the_file_when_the_response_is_never_sent(tmp_path) -> None:
    """`from_descriptor` takes ownership of an open descriptor, but only the
    reader ever closed it -- and the reader runs when the response is *sent*.
    A response built and then dropped (a handler that raises, a middleware that
    replaces it, a conditional 304 answered instead) leaked the descriptor for
    the life of the process."""
    path = tmp_path / "asset.bin"
    path.write_bytes(b"payload")
    fd = os.open(path, os.O_RDONLY)

    response = FileResponse.from_descriptor(fd, os.stat(fd), "asset.bin")
    assert _fd_is_open(fd)

    del response
    gc.collect()

    assert not _fd_is_open(fd)


def test_from_descriptor_close_is_explicit_and_idempotent(tmp_path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"payload")
    fd = os.open(path, os.O_RDONLY)

    response = FileResponse.from_descriptor(fd, os.stat(fd), "asset.bin")
    response.close()
    assert not _fd_is_open(fd)
    response.close()  # a second close must not touch a now-reused descriptor
    del response
    gc.collect()


@pytest.mark.asyncio
async def test_a_sent_descriptor_response_is_not_closed_twice(tmp_path) -> None:
    """The reader still owns the descriptor once the send starts, so the
    ownership handover must leave nothing for `__del__` to close."""
    path = tmp_path / "asset.bin"
    path.write_bytes(b"payload")
    fd = os.open(path, os.O_RDONLY)

    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    response = FileResponse.from_descriptor(fd, os.stat(fd), "asset.bin")
    await response(send)

    assert response._fd is None
    assert b"".join(m.get("body", b"") for m in sent[1:]) == b"payload"
    del response
    gc.collect()
