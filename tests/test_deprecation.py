from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from wreath import Wreath
from wreath.openapi import generate_openapi
from wreath.response import StreamingResponse
from wreath.testing import TestClient


@pytest.mark.asyncio
async def test_route_deprecation_emits_lifecycle_headers() -> None:
    app = Wreath()

    @app.get(
        "/legacy",
        deprecated=True,
        deprecated_at=datetime(2023, 6, 30, 23, 59, 59, tzinfo=UTC),
        sunset_at=datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC),
        deprecation_link="https://developer.example/deprecations/legacy",
    )
    async def legacy(request: Any) -> str:
        return "legacy"

    async with TestClient(app) as client:
        response = await client.get("/legacy")

    headers = dict(response.headers)
    assert headers[b"deprecation"] == b"@1688169599"
    assert headers[b"sunset"] == b"Sun, 30 Jun 2024 23:59:59 GMT"
    assert headers[b"link"] == (
        b'<https://developer.example/deprecations/legacy>; rel="deprecation"'
    )


@pytest.mark.asyncio
async def test_route_deprecation_headers_apply_to_streams_without_mutating_them() -> None:
    app = Wreath()

    async def chunks():
        yield b"one"

    shared = StreamingResponse(chunks(), headers=[(b"content-type", b"text/plain")])

    @app.get(
        "/legacy-stream",
        deprecated_at=datetime(2025, 1, 1, tzinfo=UTC),
        response_only=True,
    )
    async def legacy_stream(request: Any) -> StreamingResponse:
        return shared

    async with TestClient(app) as client:
        response = await client.get("/legacy-stream")

    assert dict(response.headers)[b"deprecation"] == b"@1735689600"
    assert all(name.lower() != b"deprecation" for name, _value in shared.headers)


def test_route_deprecation_refuses_an_impossible_lifecycle() -> None:
    app = Wreath()

    with pytest.raises(ValueError, match="sunset_at.*deprecated_at"):

        @app.get(
            "/backwards",
            deprecated_at=datetime(2025, 1, 2, tzinfo=UTC),
            sunset_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        async def backwards(request: Any) -> str:
            return "no"


def test_route_deprecation_refuses_a_naive_datetime() -> None:
    app = Wreath()

    with pytest.raises(ValueError, match="deprecated_at.*timezone-aware"):

        @app.get("/naive", deprecated_at=datetime(2025, 1, 1))
        async def naive(request: Any) -> str:
            return "no"


def test_route_deprecation_refuses_an_unencoded_non_ascii_link() -> None:
    app = Wreath()

    with pytest.raises(ValueError, match="deprecation_link.*ASCII.*percent-encode"):

        @app.get("/link", deprecation_link="https://developer.example/éolienne")
        async def linked(request: Any) -> str:
            return "no"


def test_a_scheduled_deprecation_marks_the_openapi_operation() -> None:
    app = Wreath()

    @app.get("/future", deprecated_at=datetime(2027, 1, 1, tzinfo=UTC))
    async def future(request: Any) -> str:
        return "legacy"

    operation = generate_openapi(app)["paths"]["/future"]["get"]
    assert operation["deprecated"] is True
