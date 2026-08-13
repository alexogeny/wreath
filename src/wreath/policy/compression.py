"""First-class response compression, backed by the stdlib's native codecs."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

from .._webpolicy import (
    NO_TRANSFORM,
    append_vary,
    cache_control_flags,
    find_response_header,
    is_compressible_content_type,
    replace_content_length,
    select_content_encoding,
)
from ..compression import (
    ZSTD_DEFAULT_LEVEL,
    ZSTD_MAX_LEVEL,
    ZSTD_MIN_LEVEL,
    GzipCompressor,
    ZstdCompressor,
    gzip_compress,
    zstd_compress,
)
from ..request import Request
from ..response import FileResponse, Response, StreamingResponse

_BODYLESS = {204, 304}

# Keyed by what `select_content_encoding` returns, so adding a coding is one
# entry in each map plus the selector -- and a coding the selector can return but
# a map cannot serve would be a KeyError at the one line that knows both names.
_STREAMING = {"gzip": GzipCompressor, "zstd": ZstdCompressor}
_WHOLE_BUFFER = {"gzip": gzip_compress, "zstd": zstd_compress}
_ETAG_SUFFIX = {"gzip": b"--gzip", "zstd": b"--zstd"}


def _encoded_etag(value: bytes, coding: str) -> bytes | None:
    prefix = b""
    tag = value
    if tag.startswith(b"W/"):
        prefix, tag = b"W/", tag[2:]
    if len(tag) < 2 or tag[:1] != b'"' or tag[-1:] != b'"':
        return None
    return prefix + tag[:-1] + _ETAG_SUFFIX[coding] + b'"'


async def _compressed_stream(
    source: AsyncIterable[bytes], coding: str, level: int
) -> AsyncIterator[bytes]:
    compressor = _STREAMING[coding](level)
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


class CompressionPolicy:
    """Compress eligible in-memory and streaming responses with zstd or gzip.

    Global policy, so static files and error responses are compressed on the
    same terms as routed ones. A client that accepts neither coding — by naming
    both with `q=0`, or by offering neither `zstd`, `gzip`, nor a usable `*` —
    gets the response uncompressed.

    **The two codings are not offered on equal terms, deliberately.** `zstd` is
    served only to a client that named it; `gzip` is served to a client that
    named it *or* that sent a bare `*`. RFC 9110 would allow reading `*` as
    consent to zstd, but a request carrying `*` and no explicit list is far more
    likely to come from an old client than a new one, and a missing zstd decoder
    surfaces as a corrupt body rather than as an error anyone can attribute. When
    a client accepts both, the higher `q` wins and a tie goes to zstd, which
    decodes faster and smaller at every level offered here. The practical effect
    is that no request that used to receive gzip receives a coding it did not ask
    for by name.

    zstd is available because `compression.zstd` is in the standard library from
    Python 3.14 (PEP 784). brotli is not offered: it would mean a mandatory
    third-party runtime dependency, which a content coding does not justify.

    A response is compressed only when all of these hold. The request method is
    not `HEAD` and the status is not 204, 304, or 206. The response carries
    neither `Content-Encoding` -- an already-encoded body is never re-encoded --
    nor `Content-Range`. Its `Cache-Control` does not say `no-transform`. Its
    `Content-Type` is compressible, meaning `text/*`, `application/json`,
    `application/problem+json`, `application/javascript`, `application/xml`,
    `image/svg+xml`, or any `application/*+json` or `application/*+xml`; a
    response with no `Content-Type` at all is left alone. And any `ETag` it
    carries is a well-formed quoted tag, strong or weak, since the encoded body
    needs a distinct one.

    An in-memory response is compressed only when its body is at least
    `minimum_size` bytes, and its `Content-Length` is rewritten. A streaming
    response is compressed chunk by chunk as it is produced, and loses its
    `Content-Length`; `minimum_size` cannot apply to it, because the length is
    not known when the decision is made. A `FileResponse` is never compressed.

    A compressed response gains `Content-Encoding` naming the coding used, gains
    `Accept-Encoding` in its `Vary`, and has its `ETag` suffixed with `--gzip` or
    `--zstd` inside the quotes. The suffix is per-coding for the same reason it
    exists at all: a shared cache keys on `Vary: Accept-Encoding`, and two
    differently-encoded bodies under one tag is a cache handing a zstd body to a
    gzip-only client.

    Args:
        minimum_size: Smallest in-memory body to compress, in bytes.
        gzip_level: zlib compression level from 0 to 9.
        zstd_level: zstd compression level, from `ZSTD_MIN_LEVEL` to
            `ZSTD_MAX_LEVEL`; 3 by default, the level whose speed is comparable
            to gzip's default while compressing appreciably better. There is no
            zstd store mode — levels below 1 are libzstd's *fast* modes.
        compress_streaming: Compress streaming responses as chunks are produced.
        compress_authenticated: Opt identified callers into compression despite
            the response-length side channel. False by default.

    Raises:
        ValueError: `minimum_size` is negative, `gzip_level` is outside 0-9, or
            `zstd_level` is outside libzstd's range.
    """

    __slots__ = (
        "compress_authenticated",
        "compress_streaming",
        "gzip_level",
        "minimum_size",
        "zstd_level",
    )

    def __init__(
        self,
        *,
        minimum_size: int = 1024,
        gzip_level: int = 5,
        zstd_level: int = ZSTD_DEFAULT_LEVEL,
        compress_streaming: bool = True,
        compress_authenticated: bool = False,
    ) -> None:
        if minimum_size < 0:
            raise ValueError("minimum_size must be non-negative")
        if not 0 <= gzip_level <= 9:
            raise ValueError("gzip_level must be between 0 and 9")
        if not ZSTD_MIN_LEVEL <= zstd_level <= ZSTD_MAX_LEVEL:
            raise ValueError(f"zstd_level must be between {ZSTD_MIN_LEVEL} and {ZSTD_MAX_LEVEL}")
        self.minimum_size = minimum_size
        self.gzip_level = gzip_level
        self.zstd_level = zstd_level
        self.compress_streaming = compress_streaming
        self.compress_authenticated = compress_authenticated

    def describe(self):
        """What negotiation this policy takes part in.

        No `const` on `Content-Encoding`: which codec is chosen depends on the
        request's `Accept-Encoding` and on the body, so a fixed value would be
        a guess. The header's presence is the contract; its value is not.
        """
        from .base import HeaderSpec, PolicyContract

        return PolicyContract(
            request_headers=(
                HeaderSpec(
                    "Accept-Encoding",
                    description="Codecs the client accepts; `zstd` and `gzip` are served.",
                ),
            ),
            response_headers=(
                (
                    None,
                    HeaderSpec(
                        "Content-Encoding",
                        description="Present when the body was compressed.",
                    ),
                ),
                (
                    None,
                    HeaderSpec(
                        "Vary",
                        description="Includes `Accept-Encoding` once this policy ran.",
                    ),
                ),
            ),
        )

    async def after(self, request: Request, response):
        """Compress the response when every eligibility condition holds.

        Returns the response object either way; an in-memory body is replaced in
        place, and a streaming body is wrapped in a compressing iterator that
        does its work as the response is sent.
        """
        if request.method == "HEAD" or response.status in _BODYLESS or response.status == 206:
            return response
        # Compression length is an oracle when one body contains both secrets and
        # attacker-controlled reflection (BREACH). Identified responses therefore
        # require an explicit opt-in; public responses keep the existing fast path.
        if request.identity is not None and not self.compress_authenticated:
            return response
        headers = response.headers
        accepted = request._header_bytes(b"accept-encoding")
        coding = select_content_encoding(accepted) if accepted is not None else None
        if coding is None:
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
        compressed_etag = _encoded_etag(etag, coding) if etag is not None else None
        if etag is not None and compressed_etag is None:
            return response

        level = self.zstd_level if coding == "zstd" else self.gzip_level
        if isinstance(response, Response):
            if len(response.body) < self.minimum_size:
                return response
            response.body = _WHOLE_BUFFER[coding](response.body, level)
            replace_content_length(headers, len(response.body))
        elif isinstance(response, StreamingResponse) and self.compress_streaming:
            response.body = _compressed_stream(response.body, coding, level)
            replace_content_length(headers, None)
        elif isinstance(response, FileResponse):
            return response
        else:
            return response

        headers.append((b"content-encoding", coding.encode("ascii")))
        append_vary(headers, b"accept-encoding")
        if compressed_etag is not None:
            for index, (name, _value) in enumerate(headers):
                if name.lower() == b"etag":
                    headers[index] = (b"etag", compressed_etag)
                    break
        return response


__all__ = ["CompressionPolicy"]
