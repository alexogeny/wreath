"""First-class response compression on Wreath gzip and CPython zstd."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, cast

from .._compression import (
    _dcz_compress,
    _gzip_compress_with,
    _gzip_encoder_new,
    _gzip_fragment_compress_with,
    _prepare_dcz_dictionary,
    _RenderedFragments,
    require_zstd,
)
from .._native import _core
from .._webpolicy import (
    NO_TRANSFORM,
    _select_prepared_content_encoding,
    append_vary,
    cache_control_flags,
    find_response_header,
    is_compressible_content_type,
    replace_content_length,
)
from ..compression import (
    ZSTD_DEFAULT_LEVEL,
    ZSTD_MAX_LEVEL,
    ZSTD_MIN_LEVEL,
    GzipCompressor,
    ZstdCompressor,
    zstd_compress,
)
from ..request import Request
from ..response import Response, StreamingResponse

_BODYLESS = {204, 304}

_ETAG_SUFFIX = {"dcz": b"--dcz", "gzip": b"--gzip", "zstd": b"--zstd"}
_INTEGRITY_FIELDS = frozenset({b"content-digest", b"repr-digest"})
_FORMAT_COUNT = 7


_format_index = _core.gzip_format


def _has_integrity_field(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(name.lower() in _INTEGRITY_FIELDS for name, _value in headers)


def _encoded_etag(value: bytes, coding: str) -> bytes | None:
    prefix = b""
    tag = value
    if tag.startswith(b"W/"):
        prefix, tag = b"W/", tag[2:]
    if len(tag) < 2 or tag[:1] != b'"' or tag[-1:] != b'"':
        return None
    return prefix + tag[:-1] + _ETAG_SUFFIX[coding] + b'"'


async def _compressed_stream(
    source: AsyncIterable[bytes], coding: str, level: int, content_type: bytes
) -> AsyncIterator[bytes]:
    compressor = GzipCompressor(level, content_type) if coding == "gzip" else ZstdCompressor(level)
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
    """Compress eligible responses with DCZ, fragment gzip, gzip, or zstd.

    Global policy, so static files and error responses are compressed on the
    same terms as routed ones. A client that accepts neither coding — by naming
    both with `q=0`, or by offering neither `zstd`, `gzip`, nor a usable `*` —
    gets the response uncompressed.

    **The ordinary codings are not offered on equal terms, deliberately.** `zstd` is
    served only to a client that named it; `gzip` is served to a client that
    named it *or* that sent a bare `*`. RFC 9110 would allow reading `*` as
    consent to zstd, but a request carrying `*` and no explicit list is far more
    likely to come from an old client than a new one, and a missing zstd decoder
    surfaces as a corrupt body rather than as an error anyone can attribute. When
    a client accepts both, the higher `q` wins and a tie goes to zstd, which
    decodes faster and smaller at every level offered here. A bare wildcard
    therefore selects gzip, never zstd. For an eligible HTTPS response whose exact registered
    dictionary appears in `Available-Dictionary`, DCZ is preferred when its
    client quality is at least as high as every ordinary coding. If that exact
    match is unavailable, an equal-quality client that advertised DCZ falls
    through to gzip: an exact prepared fragment is attempted first and the
    format-aware gzip encoder is the final gzip fallback. Explicitly higher
    client quality values are always honoured.

    zstd is available when this Python 3.14 build includes the optional `_zstd`
    extension. Construction refuses immediately when it does not, naming the
    required interpreter capability; brotli is not installed as a silent
    third-party fallback.

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
    response loses its `Content-Length`; zstd emits incrementally, while gzip
    holds the member until finish so its format-aware parser sees the complete
    body. `minimum_size` cannot apply because the length is not known when the
    decision is made. A `FileResponse` is never compressed.

    A compressed response gains `Content-Encoding` naming the coding used, gains
    `Accept-Encoding` in its `Vary`, and has its `ETag` suffixed with `--gzip` or
    `--zstd` inside the quotes. The suffix is per-coding for the same reason it
    exists at all: a shared cache keys on `Vary: Accept-Encoding`, and two
    differently-encoded bodies under one tag is a cache handing a zstd body to a
    gzip-only client.

    Args:
        minimum_size: Smallest in-memory body to compress, in bytes.
        gzip_level: Wreath gzip compression level from 0 to 9.
        zstd_level: zstd compression level, from `ZSTD_MIN_LEVEL` to
            `ZSTD_MAX_LEVEL`; 3 by default, the level whose speed is comparable
            to gzip's default while compressing appreciably better. There is no
            zstd store mode — levels below 1 are libzstd's *fast* modes.
        compress_streaming: Compress streaming response iterables.
        compress_authenticated: Opt identified callers into compression despite
            the response-length side channel. False by default.

    Raises:
        RuntimeError: this CPython build omitted the optional `_zstd` module.
        ValueError: `minimum_size` is negative, `gzip_level` is outside 0-9, or
            `zstd_level` is outside libzstd's range.
    """

    __slots__ = (
        "_dcz_dictionaries",
        "_gzip_fragment_bodies",
        "_gzip_fragments",
        "compress_authenticated",
        "compress_streaming",
        "gzip_level",
        "_gzip_workspace",
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
        if type(minimum_size) is not int:
            raise TypeError("minimum_size must be an integer number of bytes")
        if minimum_size < 0:
            raise ValueError("minimum_size must be non-negative")
        if type(gzip_level) is not int:
            raise TypeError("gzip_level must be an integer between 0 and 9")
        if not 0 <= gzip_level <= 9:
            raise ValueError("gzip_level must be between 0 and 9")
        if type(zstd_level) is not int:
            raise TypeError("zstd_level must be an integer compression level")
        if type(compress_streaming) is not bool:
            raise TypeError("compress_streaming must be true or false")
        if type(compress_authenticated) is not bool:
            raise TypeError("compress_authenticated must be true or false")
        require_zstd()
        if not ZSTD_MIN_LEVEL <= zstd_level <= ZSTD_MAX_LEVEL:
            raise ValueError(f"zstd_level must be between {ZSTD_MIN_LEVEL} and {ZSTD_MAX_LEVEL}")
        self.minimum_size = minimum_size
        self.gzip_level = gzip_level
        self._gzip_workspace = _gzip_encoder_new()
        self._dcz_dictionaries: list[tuple[bytes, bytes, object, object | None] | None] = [
            None
        ] * _FORMAT_COUNT
        self._gzip_fragments: tuple[tuple[int, int, bytes, bytes, int] | None, ...] = (
            None,
        ) * _FORMAT_COUNT
        # The latest exact body and its already-owned dynamic edges per format.
        # Seven strong references are a bounded application-owned provenance
        # table; replacement can only turn a concurrent older body into the
        # checked fallback, never make a different body trusted.
        self._gzip_fragment_bodies: tuple[tuple[bytes, bytes, bytes, object] | None, ...] = (
            None,
        ) * _FORMAT_COUNT
        self.zstd_level = zstd_level
        self.compress_streaming = compress_streaming
        self.compress_authenticated = compress_authenticated

    def _configure_dcz_dictionary(self, format: str | bytes, dictionary: bytes) -> bytes:
        """Install one private, format-owned raw dictionary; return its wire token."""
        prepared = _prepare_dcz_dictionary(dictionary)
        self._dcz_dictionaries[_format_index(format)] = prepared
        return prepared[0]

    def _configure_gzip_fragment(
        self,
        format: str | bytes,
        document: bytes,
        *,
        prefix_bytes: int,
        suffix_bytes: int,
    ) -> None:
        """Prepare one exact stable span as an independently readable gzip member."""
        if type(prefix_bytes) is not int:
            raise TypeError("gzip fragment prefix_bytes must be an integer number of bytes")
        if type(suffix_bytes) is not int:
            raise TypeError("gzip fragment suffix_bytes must be an integer number of bytes")
        if prefix_bytes < 0 or suffix_bytes < 0:
            raise ValueError("gzip fragment prefix_bytes and suffix_bytes must be non-negative")
        if prefix_bytes + suffix_bytes >= len(document):
            raise ValueError("gzip fragment must leave a non-empty stable span")
        middle = bytes(document[prefix_bytes : len(document) - suffix_bytes])
        cached = _gzip_compress_with(
            self._gzip_workspace,
            middle,
            self.gzip_level,
            format,
        )
        fragments = list(self._gzip_fragments)
        fragments[_format_index(format)] = (
            prefix_bytes,
            suffix_bytes,
            middle,
            cached,
            self.gzip_level,
        )
        self._gzip_fragments = tuple(fragments)
        bodies = list(self._gzip_fragment_bodies)
        bodies[_format_index(format)] = None
        self._gzip_fragment_bodies = tuple(bodies)

    def _gzip_fragment_body(
        self,
        format: str | bytes,
        prefix: bytes,
        suffix: bytes = b"",
    ) -> bytes:
        """Join dynamic edges to this policy's exact prepared stable span.

        The policy retains this exact bytes object and its already-owned edges
        as unforgeable provenance for the stable middle. Gzip can therefore
        compress only the two dynamic edges and
        splice the cached member without scanning the full uncompressed middle
        again. A template can render just its dynamic prefix/suffix programs;
        the stable bytes are copied once here rather than emitted and reread.
        """
        index = _format_index(format)
        entry = self._gzip_fragments[index]
        if entry is None:
            raise RuntimeError(
                f"gzip fragment format {format!r} is not prepared; call "
                "_configure_gzip_fragment() for that format first"
            )
        prefix_bytes, suffix_bytes, stable, _cached, prepared_level = entry
        if prepared_level != self.gzip_level:
            raise RuntimeError("prepared gzip fragment no longer matches gzip_level")
        if type(prefix) is not bytes or len(prefix) != prefix_bytes:
            raise ValueError(f"gzip fragment prefix must be exactly {prefix_bytes} bytes")
        if type(suffix) is not bytes or len(suffix) != suffix_bytes:
            raise ValueError(f"gzip fragment suffix must be exactly {suffix_bytes} bytes")
        # join allocates the final wire body once. Chained `+` would allocate
        # and reread the stable middle again when a suffix is present.
        body = b"".join((prefix, stable, suffix))
        bodies = list(self._gzip_fragment_bodies)
        bodies[index] = (body, prefix, suffix, entry)
        self._gzip_fragment_bodies = tuple(bodies)
        return body

    def _gzip_fragment_render(
        self,
        format: str | bytes,
        template: Any,
        context: dict[str, Any],
    ) -> bytes:
        index = _format_index(format)
        entry = self._gzip_fragments[index]
        if entry is None:
            raise RuntimeError(
                f"gzip fragment format {format!r} is not prepared; call "
                "_configure_gzip_fragment() for that format first"
            )
        prefix_bytes, suffix_bytes, stable, _cached, prepared_level = entry
        if prepared_level != self.gzip_level:
            raise RuntimeError("prepared gzip fragment no longer matches gzip_level")
        if suffix_bytes != 0:
            raise ValueError("fused gzip fragment rendering requires a zero-byte suffix")
        body = template._render_bytes_tail(context, stable)
        if len(body) != prefix_bytes + len(stable):
            raise ValueError(f"gzip fragment prefix must be exactly {prefix_bytes} bytes")
        prefix = body[:prefix_bytes]
        bodies = list(self._gzip_fragment_bodies)
        bodies[index] = (body, prefix, b"", entry)
        self._gzip_fragment_bodies = tuple(bodies)
        return body

    def _dcz_fragment_render(
        self,
        request: Request,
        format: str | bytes,
        template: Any,
        context: dict[str, Any],
    ) -> bytes:
        index = _format_index(format)
        dictionary = self._dcz_dictionaries[index]
        fragment = self._gzip_fragments[index]
        accepted = request._header_bytes(b"accept-encoding")
        available = request._header_bytes(b"available-dictionary")
        eligible = (
            dictionary is not None
            and fragment is not None
            and request.scheme == "https"
            and accepted is not None
            and available == dictionary[0]
            and _select_prepared_content_encoding(accepted, True) == "dcz"
            and (request.identity is None or self.compress_authenticated)
        )
        if not eligible:
            return self._gzip_fragment_render(format, template, context)
        prefix_bytes, suffix_bytes, stable, _cached, prepared_level = fragment
        if prepared_level != self.gzip_level:
            raise RuntimeError("prepared gzip fragment no longer matches gzip_level")
        if suffix_bytes != 0:
            raise ValueError("fused DCZ rendering requires a zero-byte suffix")
        prefix = template.render_bytes(context)
        if len(prefix) != prefix_bytes:
            raise ValueError(f"DCZ fragment prefix must be exactly {prefix_bytes} bytes")
        return _RenderedFragments(prefix, stable)

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
        if accepted is None:
            return response
        if (
            find_response_header(headers, b"content-encoding") is not None
            or find_response_header(headers, b"content-range") is not None
            or _has_integrity_field(headers)
        ):
            return response
        cache_control = find_response_header(headers, b"cache-control")
        if cache_control is not None:
            for name, value in headers:
                if (
                    name.lower() == b"cache-control"
                    and cache_control_flags(value) & NO_TRANSFORM
                ):
                    return response
        content_type = find_response_header(headers, b"content-type")
        if content_type is None or not is_compressible_content_type(content_type):
            return response
        format_index = _format_index(content_type)
        dcz_entry = self._dcz_dictionaries[format_index]
        dcz_available = (
            dcz_entry is not None
            and request.scheme == "https"
            and isinstance(response, Response)
            and request._header_bytes(b"available-dictionary") == dcz_entry[0]
        )
        coding = _select_prepared_content_encoding(accepted, dcz_available)
        if coding is None:
            return response
        etag = find_response_header(headers, b"etag")
        compressed_etag = _encoded_etag(etag, coding) if etag is not None else None
        if etag is not None and compressed_etag is None:
            return response

        level = self.zstd_level if coding in {"dcz", "zstd"} else self.gzip_level
        if isinstance(response, Response):
            if len(response.body) < self.minimum_size:
                return response
            prepared_body = self._gzip_fragment_bodies[format_index]
            fragment_parts = None
            if prepared_body is not None and response.body is prepared_body[0]:
                fragment_parts = prepared_body[1:]
            response.body = (
                _dcz_compress(
                    cast(tuple[bytes, bytes, Any, object | None], dcz_entry),
                    response.body,
                    level,
                )
                if coding == "dcz"
                else _gzip_fragment_compress_with(
                    self._gzip_workspace,
                    fragment_parts if fragment_parts is not None else response.body,
                    level,
                    content_type,
                    self._gzip_fragments,
                )
                if coding == "gzip"
                else zstd_compress(
                    response.body.materialize()
                    if isinstance(response.body, _RenderedFragments)
                    else response.body,
                    level,
                )
            )
            replace_content_length(headers, len(response.body))
        elif isinstance(response, StreamingResponse) and self.compress_streaming:
            response.body = _compressed_stream(response.body, coding, level, content_type)
            replace_content_length(headers, None)
        else:
            return response

        headers.append((b"content-encoding", coding.encode("ascii")))
        append_vary(headers, b"accept-encoding")
        if coding == "dcz":
            append_vary(headers, b"available-dictionary")
        if compressed_etag is not None:
            for index, (name, _value) in enumerate(headers):
                if name.lower() == b"etag":
                    headers[index] = (b"etag", compressed_etag)
                    break
        return response


__all__ = ["CompressionPolicy"]
