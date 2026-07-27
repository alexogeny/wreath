"""Large uploads without holding them in memory (report 23: G-44)."""

from __future__ import annotations

import pytest

from wreath.request import Request, RequestLimits


def _multipart(parts: list[tuple[str, bytes, str | None]], boundary: bytes = b"BOUND"):
    body = bytearray()
    for name, data, filename in parts:
        body += b"--" + boundary + b"\r\n"
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body += f"content-disposition: {disposition}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    return bytes(body)


def _request(body: bytes, limits: RequestLimits | None = None, chunks: int = 1):
    pieces = [body[i::chunks] for i in range(chunks)] if chunks > 1 else [body]
    if chunks > 1:
        size = max(1, len(body) // chunks)
        pieces = [body[i:i + size] for i in range(0, len(body), size)]
    queue = list(pieces)

    async def receive():
        if queue:
            return {
                "type": "http.request",
                "body": queue.pop(0),
                "more_body": bool(queue),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"content-type", b'multipart/form-data; boundary=BOUND')],
        },
        receive,
        limits=limits or RequestLimits(),
    )


class TestSpooledUploads:
    """G-44: every part is held in memory, so an upload is hard-capped at
    `max_body_bytes` and costs RAM x concurrency. A file larger than the spool
    threshold should go to disk instead of the heap."""

    async def test_a_large_file_spools_to_disk(self):
        payload = b"x" * (256 * 1024)
        body = _multipart([("upload", payload, "big.bin")])
        limits = RequestLimits(spool_max_bytes=64 * 1024)
        form = await _request(body, limits).form()

        uploaded = form.files["upload"]
        assert uploaded.spooled is True
        assert uploaded.size == len(payload)
        assert uploaded.read() == payload

    async def test_a_small_file_stays_in_memory(self):
        payload = b"y" * 1024
        body = _multipart([("upload", payload, "small.bin")])
        limits = RequestLimits(spool_max_bytes=64 * 1024)
        form = await _request(body, limits).form()

        uploaded = form.files["upload"]
        assert uploaded.spooled is False
        assert uploaded.data == payload

    async def test_a_spooled_file_streams_in_chunks(self):
        payload = bytes(range(256)) * 1024
        body = _multipart([("upload", payload, "big.bin")])
        limits = RequestLimits(spool_max_bytes=4096)
        form = await _request(body, limits).form()

        chunks = [chunk for chunk in form.files["upload"].chunks(4096)]
        assert len(chunks) > 1
        assert b"".join(chunks) == payload

    async def test_a_spooled_file_is_released_with_the_form(self):
        payload = b"z" * (128 * 1024)
        body = _multipart([("upload", payload, "big.bin")])
        limits = RequestLimits(spool_max_bytes=4096)
        form = await _request(body, limits).form()
        uploaded = form.files["upload"]
        assert uploaded.read() == payload

        form.close()
        with pytest.raises(ValueError):
            uploaded.read()

    async def test_fields_still_read_as_text(self):
        body = _multipart([("name", b"ann", None), ("role", b"admin", None)])
        form = await _request(body).form()
        assert form["name"] == "ann"
        assert form.getlist("role") == ["admin"]

    async def test_a_mixed_form_keeps_both(self):
        body = _multipart(
            [("name", b"ann", None), ("upload", b"q" * (128 * 1024), "big.bin")]
        )
        form = await _request(body, RequestLimits(spool_max_bytes=4096)).form()
        assert form["name"] == "ann"
        assert form.files["upload"].size == 128 * 1024

    async def test_the_aggregate_memory_bound_ignores_spooled_bytes(self):
        """The point of spooling: an upload past `max_form_memory_bytes` no
        longer has to be refused, because it is not in memory."""
        payload = b"w" * (2 * 1024 * 1024)
        body = _multipart([("upload", payload, "big.bin")])
        limits = RequestLimits(
            spool_max_bytes=64 * 1024, max_form_memory_bytes=256 * 1024
        )
        form = await _request(body, limits).form()
        assert form.files["upload"].size == len(payload)

    async def test_in_memory_parts_are_still_bounded(self):
        from wreath.exceptions import PayloadTooLarge

        body = _multipart([(f"f{n}", b"v" * 40_000, None) for n in range(10)])
        limits = RequestLimits(max_form_memory_bytes=100_000, spool_max_bytes=1 << 30)
        with pytest.raises(PayloadTooLarge):
            await _request(body, limits).form()

    async def test_a_body_arriving_in_pieces_parses_the_same(self):
        payload = b"p" * (128 * 1024)
        body = _multipart([("name", b"ann", None), ("upload", payload, "big.bin")])
        form = await _request(
            body, RequestLimits(spool_max_bytes=4096), chunks=17
        ).form()
        assert form["name"] == "ann"
        assert form.files["upload"].read() == payload


class TestUploadedFileCompatibility:
    """`.data` is the shipped accessor and has to keep working."""

    async def test_data_still_returns_bytes_for_a_small_file(self):
        body = _multipart([("upload", b"small", "s.bin")])
        form = await _request(body).form()
        assert form.files["upload"].data == b"small"

    async def test_content_type_is_unchanged(self):
        body = _multipart([("upload", b"small", "s.bin")])
        form = await _request(body).form()
        assert form.files["upload"].content_type == "application/octet-stream"
