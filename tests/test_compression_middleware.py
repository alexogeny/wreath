from __future__ import annotations

import zlib
from typing import Any

import pytest

from wreath import JSONResponse, Wreath
from wreath.cache_control import CacheControl
from wreath.compression import GzipCompressor, gzip_compress
from wreath.middleware import (
    CacheControlMiddleware,
    CompressionMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
)
from wreath.response import StreamingResponse
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
