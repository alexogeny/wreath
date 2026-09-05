from tempfile import TemporaryFile

import pytest

from wreath import request


def test_spooled_read_preserves_chunks_override_and_closed_spool_error():
    class Upload(request.UploadedFile):
        def chunks(self, size=64 * 1024):
            for chunk in super().chunks(size):
                yield chunk.upper()

    with TemporaryFile() as spool:
        spool.write(b"abc")
        upload = Upload("file", "f.bin", [], spool=spool, size=3)
        assert upload.read() == b"ABC"
        assert upload.read() == b"ABC"
    with pytest.raises(ValueError, match="spool has been closed"):
        upload.read()


def test_memory_upload_read_returns_original_bytes():
    payload = b"payload"
    upload = request.UploadedFile("file", "f.bin", [], payload)
    assert upload.read() is payload


async def chunks(parts):
    for name, filename, data in parts:
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        yield b"--B\r\n" + disposition.encode() + b"\r\n\r\n"
        yield data
        yield b"\r\n"
    yield b"--B--\r\n"


async def test_multipart_text_decodes_without_a_temporary_bytes_copy(monkeypatch):
    copied = []

    def counted(value):
        if isinstance(value, bytearray):
            copied.append(len(value))
        return bytes(value)

    monkeypatch.setattr(request, "bytes", counted, raising=False)
    result = await request._stream_multipart(
        chunks([("text", None, b"a" * 10_000)]), b"B", request.RequestLimits()
    )
    assert result["text"] == "a" * 10_000
    assert copied == []


@pytest.mark.parametrize("payload", [b"", b"plain", "café🐈".encode(), b"bad\xff\xc3"])
async def test_multipart_text_matches_standard_utf8_replacement(payload):
    result = await request._stream_multipart(
        chunks([("text", None, payload)]), b"B", request.RequestLimits()
    )
    assert result["text"] == payload.decode("utf-8", "replace")


async def test_multipart_duplicate_text_and_file_preserve_separate_values():
    result = await request._stream_multipart(
        chunks([("same", None, b"first"), ("same", "f.bin", b"\xff"), ("same", None, b"last")]),
        b"B",
        request.RequestLimits(max_form_memory_bytes=10),
    )
    assert result["same"] == "first"
    assert result.getlist("same") == ["first", "last"]
    assert result.files["same"].data == b"\xff"


@pytest.mark.parametrize("budget", [7, 8])
async def test_multipart_text_budget_counts_encoded_bytes_across_duplicate_fields(budget):
    parts = [("same", None, "éé".encode()), ("same", None, "éé".encode())]
    limits = request.RequestLimits(max_form_memory_bytes=budget)
    if budget == 7:
        with pytest.raises(request.PayloadTooLarge, match="form parts exceed 7"):
            await request._stream_multipart(chunks(parts), b"B", limits)
    else:
        result = await request._stream_multipart(chunks(parts), b"B", limits)
        assert result.getlist("same") == ["éé", "éé"]
