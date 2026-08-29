from __future__ import annotations

import json
from typing import Any

import pytest

from wreath import Wreath
from wreath.compression import gzip_compress
from wreath.policy import HttpPolicy, RequestDecompressionPolicy
from wreath.request import Request
from wreath.testing import TestClient


@pytest.mark.asyncio
async def test_gzip_request_is_transparently_decoded_once() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            request_decompression=RequestDecompressionPolicy(max_output_bytes=4096)
        )
    )

    @app.post("/")
    async def receive(request):
        return {
            "document": await request.json(),
            "encoding": request.header("content-encoding"),
            "length": request.header("content-length"),
        }

    payload = json.dumps({"message": "compressed"}).encode()
    encoded = gzip_compress(payload, format="application/json")
    async with TestClient(app) as client:
        response = await client.post(
            "/",
            content=encoded,
            headers={
                "content-encoding": "gzip",
                "content-type": "application/json",
            },
        )

    assert response.status == 200
    assert response.json() == {
        "document": {"message": "compressed"},
        "encoding": None,
        "length": None,
    }


@pytest.mark.asyncio
async def test_request_decompression_refuses_unsupported_and_bad_members() -> None:
    app = Wreath(http_policy=HttpPolicy(request_decompression=RequestDecompressionPolicy()))

    @app.post("/")
    async def receive(request):
        return await request.body()

    async with TestClient(app) as client:
        unsupported = await client.post("/", content=b"payload", headers={"content-encoding": "br"})
        stacked = await client.post(
            "/", content=b"payload", headers={"content-encoding": "gzip, br"}
        )
        malformed = await client.post(
            "/", content=b"not gzip", headers={"content-encoding": "gzip"}
        )

    assert unsupported.status == 415
    assert stacked.status == 415
    assert malformed.status == 400


@pytest.mark.asyncio
async def test_request_decompression_bounds_expansion() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            request_decompression=RequestDecompressionPolicy(max_output_bytes=1024)
        )
    )

    @app.post("/")
    async def receive(request):
        return await request.body()

    encoded = gzip_compress(b"0" * 8192)
    async with TestClient(app) as client:
        response = await client.post("/", content=encoded, headers={"content-encoding": "gzip"})

    assert response.status == 413


@pytest.mark.asyncio
async def test_duplicate_content_encoding_is_refused_before_body_read() -> None:
    async def should_not_receive() -> dict[str, Any]:
        raise AssertionError("ambiguous encoding must be refused before reading the body")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [
                (b"content-encoding", b"gzip"),
                (b"content-encoding", b"gzip"),
            ],
        },
        should_not_receive,
    )

    response = await RequestDecompressionPolicy()._ingress(request)
    assert response is not None and response.status == 415


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
def test_request_decompression_validates_its_expansion_bound(maximum: Any) -> None:
    with pytest.raises(ValueError, match="max_output_bytes"):
        RequestDecompressionPolicy(max_output_bytes=maximum)
