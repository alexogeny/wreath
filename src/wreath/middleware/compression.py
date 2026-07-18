"""Portable ASGI response compression with optional direct-zlib acceleration."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

from .._headers import find_header
from .._webpolicy import (
    NO_TRANSFORM,
    append_vary,
    cache_control_flags,
    find_response_header,
    is_compressible_content_type,
    replace_content_length,
    select_content_encoding,
)
from ..compression import GzipCompressor, gzip_compress
from ..request import Request
from ..response import FileResponse, Response, StreamingResponse

_BODYLESS = {204, 304}


def _gzip_etag(value: bytes) -> bytes | None:
    prefix = b""
    tag = value
    if tag.startswith(b"W/"):
        prefix, tag = b"W/", tag[2:]
    if len(tag) < 2 or tag[:1] != b'"' or tag[-1:] != b'"':
        return None
    return prefix + tag[:-1] + b"--gzip\""


async def _compressed_stream(source: AsyncIterable[bytes], level: int) -> AsyncIterator[bytes]:
    compressor = GzipCompressor(level)
    try:
        async for chunk in source:
            if not isinstance(chunk, bytes):
                raise TypeError("streaming response chunks must be bytes")
            if chunk:
                output = compressor.compress(chunk)
                if output:
                    yield output
        final = compressor.finish()
        if final:
            yield final
    finally:
        compressor.close()


class CompressionMiddleware:
    """Compress eligible in-memory and streaming responses with gzip."""

    global_scope = True
    __slots__ = ("compress_streaming", "gzip_level", "minimum_size")

    def __init__(
        self,
        *,
        minimum_size: int = 1024,
        gzip_level: int = 5,
        compress_streaming: bool = True,
    ) -> None:
        if minimum_size < 0:
            raise ValueError("minimum_size must be non-negative")
        if not 0 <= gzip_level <= 9:
            raise ValueError("gzip_level must be between 0 and 9")
        self.minimum_size = minimum_size
        self.gzip_level = gzip_level
        self.compress_streaming = compress_streaming

    async def after(self, request: Request, response):
        if request.method == "HEAD" or response.status in _BODYLESS or response.status == 206:
            return response
        headers = response.headers
        accepted = find_header(request.headers, b"accept-encoding")
        if accepted is None or select_content_encoding(accepted) != "gzip":
            return response
        if (
            find_response_header(headers, b"content-encoding") is not None
            or find_response_header(headers, b"content-range") is not None
        ):
            return response
        cache_control = find_response_header(headers, b"cache-control")
        if cache_control is not None and cache_control_flags(cache_control) & NO_TRANSFORM:
            return response
        content_type = find_response_header(headers, b"content-type")
        if content_type is None or not is_compressible_content_type(content_type):
            return response
        etag = find_response_header(headers, b"etag")
        compressed_etag = _gzip_etag(etag) if etag is not None else None
        if etag is not None and compressed_etag is None:
            return response

        if isinstance(response, Response):
            if len(response.body) < self.minimum_size:
                return response
            response.body = gzip_compress(response.body, self.gzip_level)
            replace_content_length(headers, len(response.body))
        elif isinstance(response, StreamingResponse) and self.compress_streaming:
            response.body = _compressed_stream(response.body, self.gzip_level)
            replace_content_length(headers, None)
        elif isinstance(response, FileResponse):
            return response
        else:
            return response

        headers.append((b"content-encoding", b"gzip"))
        append_vary(headers, b"accept-encoding")
        if compressed_etag is not None:
            for index, (name, _value) in enumerate(headers):
                if name.lower() == b"etag":
                    headers[index] = (b"etag", compressed_etag)
                    break
        return response


__all__ = ["CompressionMiddleware"]
