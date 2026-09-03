from __future__ import annotations

import json
from typing import Any

import pytest

import wreath.policy.request_decompression as decompression_module
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


@pytest.mark.asyncio
async def test_duplicate_content_type_is_refused_before_format_aware_decoding() -> None:
    async def should_not_receive() -> dict[str, Any]:
        raise AssertionError("ambiguous content type must be refused before reading the body")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [
                (b"content-encoding", b"gzip"),
                (b"content-type", b"application/json"),
                (b"content-type", b"application/x-www-form-urlencoded"),
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


def test_request_decompression_validates_format_awareness() -> None:
    with pytest.raises(ValueError, match="format_aware must be a bool"):
        RequestDecompressionPolicy(format_aware=1)


@pytest.mark.parametrize("coding", [None, b"identity"])
async def test_absent_and_identity_codings_skip_body_decoding(coding: bytes | None) -> None:
    async def should_not_receive() -> dict[str, Any]:
        raise AssertionError("an uncompressed request must not read the body")

    headers = [] if coding is None else [(b"content-encoding", coding)]
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": headers},
        should_not_receive,
    )

    assert await RequestDecompressionPolicy()._ingress(request) is None
    assert request.header("content-encoding") is None


@pytest.mark.parametrize(
    ("format_aware", "expected_hint"),
    [(True, "application/json"), (False, "unknown")],
)
async def test_format_awareness_selects_the_native_decompression_hint(
    monkeypatch: pytest.MonkeyPatch,
    format_aware: bool,
    expected_hint: str,
) -> None:
    seen: list[tuple[bytes, int, str]] = []

    def decompress(encoded: bytes, maximum: int, hint: str) -> bytes:
        seen.append((encoded, maximum, hint))
        return b"decoded"

    monkeypatch.setattr(decompression_module._core, "gzip_decompress", decompress)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"encoded", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [
                (b"content-encoding", b"gzip"),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
    )

    assert await RequestDecompressionPolicy(format_aware=format_aware)._ingress(request) is None
    assert seen == [(b"encoded", request._limits.max_body_bytes, expected_hint)]
    assert await request.body() == b"decoded"
