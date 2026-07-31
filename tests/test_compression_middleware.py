from __future__ import annotations

import zlib
from compression import zstd
from typing import Any

import pytest

from wreath import JSONResponse, Wreath
from wreath.cache_control import CacheControl
from wreath.compression import (
    ZSTD_MAX_LEVEL,
    ZSTD_MIN_LEVEL,
    GzipCompressor,
    gzip_compress,
)
from wreath.middleware import (
    CacheControlMiddleware,
    CompressionMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
)
from wreath.response import Response, StreamingResponse
from wreath.testing import TestClient


def test_gzip_facade_round_trip_and_state() -> None:
    payload = b"wreath" * 10_000
    assert zlib.decompress(gzip_compress(payload), wbits=31) == payload
    compressor = GzipCompressor()
    encoded = compressor.compress(payload[:100]) + compressor.compress(payload[100:])
    encoded += compressor.finish()
    assert zlib.decompress(encoded, wbits=31) == payload
    with pytest.raises(RuntimeError):
        compressor.finish()


@pytest.mark.asyncio
async def test_compression_middleware_updates_headers_and_vary() -> None:
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "compress me" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "br, gzip"})
    assert response.header("content-encoding") == "gzip"
    assert "accept-encoding" in (response.header("vary") or "")
    assert int(response.header("content-length") or "0") == len(response.body)
    assert b"compress me" in zlib.decompress(response.body, wbits=31)


@pytest.mark.asyncio
async def test_streaming_compression_does_not_gather_source() -> None:
    advanced: list[int] = []

    async def source():
        for index in range(3):
            advanced.append(index)
            yield (f"chunk-{index}" * 100).encode()

    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=0))

    @app.get("/")
    async def index(request: Any) -> StreamingResponse:
        return StreamingResponse(source(), headers=[(b"content-type", b"text/plain")])

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "gzip"})
    assert advanced == [0, 1, 2]
    assert zlib.decompress(response.body, wbits=31).startswith(b"chunk-0")


@pytest.mark.asyncio
async def test_recommended_policy_order_sees_cookies_before_cache_and_compression() -> None:
    app = Wreath()
    app.add_middleware(SecurityHeadersMiddleware(), priority=0)
    app.add_middleware(CompressionMiddleware(minimum_size=16), priority=10)
    app.add_middleware(
        CacheControlMiddleware(CacheControl(public=True, max_age=60)), priority=20
    )
    app.add_middleware(CSRFMiddleware("s" * 32, secure=False), priority=30)

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "ordered" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "gzip"})

    assert response.header("cache-control") == "private, no-store"
    assert response.header("content-encoding") == "gzip"
    vary = response.header("vary") or ""
    assert "accept-encoding" in vary
    assert response.header("set-cookie") is not None
    assert response.header("x-content-type-options") == "nosniff"


@pytest.mark.asyncio
async def test_zstd_is_served_to_a_client_that_names_it() -> None:
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "compress me" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd, gzip"})
    assert response.header("content-encoding") == "zstd"
    assert "accept-encoding" in (response.header("vary") or "")
    assert int(response.header("content-length") or "0") == len(response.body)
    assert b"compress me" in zstd.decompress(response.body)


@pytest.mark.asyncio
async def test_bare_wildcard_still_means_gzip_not_zstd() -> None:
    """A client sending `*` and no list is likelier old than new.

    RFC 9110 would let `*` stand for consent to zstd, but a client with no zstd
    decoder would receive a body it cannot read and report it as corruption, not
    as a negotiation failure. So the wildcard keeps meaning gzip.
    """
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "wildcard" * 100})

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "*"})
    assert response.header("content-encoding") == "gzip"
    assert zlib.decompress(response.body, wbits=31).startswith(b'{"message"')


@pytest.mark.asyncio
async def test_client_preference_for_gzip_is_honoured_over_zstd() -> None:
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        return JSONResponse({"message": "preference" * 100})

    async with TestClient(app) as client:
        response = await client.get(
            "/", headers={"accept-encoding": "gzip;q=1.0, zstd;q=0.5"}
        )
    assert response.header("content-encoding") == "gzip"


@pytest.mark.asyncio
async def test_each_coding_gets_its_own_etag() -> None:
    """Two encodings of one resource must never share a tag.

    A shared cache keys on `Vary: Accept-Encoding`, and an origin that returned
    one tag for both bodies is a cache one revalidation away from handing a zstd
    body to a gzip-only client.
    """
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "tagged" * 100})
        response.headers.append((b"etag", b'"v1"'))
        return response

    async with TestClient(app) as client:
        plain = await client.get("/", headers={"accept-encoding": "identity"})
        gzipped = await client.get("/", headers={"accept-encoding": "gzip"})
        zstded = await client.get("/", headers={"accept-encoding": "zstd"})

    assert plain.header("etag") == '"v1"'
    assert gzipped.header("etag") == '"v1--gzip"'
    assert zstded.header("etag") == '"v1--zstd"'
    assert len({plain.header("etag"), gzipped.header("etag"), zstded.header("etag")}) == 3


@pytest.mark.asyncio
async def test_weak_etag_keeps_its_prefix_under_zstd() -> None:
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "weak" * 100})
        response.headers.append((b"etag", b'W/"v2"'))
        return response

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd"})
    assert response.header("etag") == 'W/"v2--zstd"'


@pytest.mark.asyncio
async def test_streaming_zstd_is_a_single_readable_frame() -> None:
    async def source():
        for index in range(4):
            yield (f"chunk-{index}" * 100).encode()

    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=0))

    @app.get("/")
    async def index(request: Any) -> StreamingResponse:
        return StreamingResponse(source(), headers=[(b"content-type", b"text/plain")])

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd"})

    assert response.header("content-encoding") == "zstd"
    decoded = zstd.decompress(response.body)
    assert decoded.startswith(b"chunk-0")
    assert decoded.endswith(b"chunk-3" * 1)
    # One frame, not one per chunk: a second frame would decode fine here but
    # arrives as a trailing empty frame in the double-finish failure mode.
    assert response.body.count(b"\x28\xb5\x2f\xfd") == 1


def test_zstd_level_is_validated_against_libzstd() -> None:
    CompressionMiddleware(zstd_level=ZSTD_MIN_LEVEL)
    CompressionMiddleware(zstd_level=ZSTD_MAX_LEVEL)
    with pytest.raises(ValueError, match="zstd_level"):
        CompressionMiddleware(zstd_level=ZSTD_MAX_LEVEL + 1)
    with pytest.raises(ValueError, match="zstd_level"):
        CompressionMiddleware(zstd_level=ZSTD_MIN_LEVEL - 1)


def test_minimum_size_and_gzip_level_are_validated() -> None:
    CompressionMiddleware(minimum_size=0, gzip_level=0)
    CompressionMiddleware(gzip_level=9)
    with pytest.raises(ValueError, match="minimum_size"):
        CompressionMiddleware(minimum_size=-1)
    for level in (-1, 10):
        with pytest.raises(ValueError, match="gzip_level"):
            CompressionMiddleware(gzip_level=level)


@pytest.mark.asyncio
async def test_no_transform_and_incompressible_types_refuse_zstd_too() -> None:
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/no-transform")
    async def no_transform(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "leave me" * 100})
        response.headers.append((b"cache-control", b"no-transform"))
        return response

    @app.get("/png")
    async def png(request: Any) -> Response:
        return Response(b"\x89PNG" + b"\x00" * 4000, media_type=b"image/png")

    async with TestClient(app) as client:
        kept = await client.get("/no-transform", headers={"accept-encoding": "zstd"})
        image = await client.get("/png", headers={"accept-encoding": "zstd"})

    assert kept.header("content-encoding") is None
    assert image.header("content-encoding") is None


@pytest.mark.asyncio
async def test_the_etag_suffix_lands_on_the_etag_and_not_a_neighbour() -> None:
    """`wreath mutant` found the header scan's `name == b"etag"` test unasserted.

    Forcing that comparison always-true rewrites whichever header comes *first* --
    `content-type`, in practice -- with the encoded tag, and the existing tests
    still passed because none of them looked at the other headers afterwards. The
    result would be a response whose content type is a quoted ETag.
    """
    app = Wreath()
    app.add_middleware(CompressionMiddleware(minimum_size=16))

    @app.get("/")
    async def index(request: Any) -> JSONResponse:
        response = JSONResponse({"message": "neighbours" * 100})
        response.headers.append((b"etag", b'"v3"'))
        response.headers.append((b"x-trailing", b"kept"))
        return response

    async with TestClient(app) as client:
        response = await client.get("/", headers={"accept-encoding": "zstd"})

    assert response.header("etag") == '"v3--zstd"'
    assert response.header("content-type") == "application/json"
    assert response.header("x-trailing") == "kept"
    assert response.header("content-encoding") == "zstd"
    # Exactly one etag: a second would let a cache pick either body for one tag.
    assert sum(1 for name, _ in response.headers if name.lower() == b"etag") == 1
