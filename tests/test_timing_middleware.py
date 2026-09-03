from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest

from wreath import Request, Response, Wreath
from wreath._native import _core
from wreath.policy import HttpPolicy, ServerTimingPolicy, elapsed
from wreath.testing import TestClient

formatter = _core.format_server_timing


def test_formatter_renders_milliseconds() -> None:
    assert formatter(b"total", 0.0) == b"total;dur=0.000"
    assert formatter(b"total", 1.5) == b"total;dur=1500.000"
    assert formatter(b"app", 0.0123456) == b"app;dur=12.346"
    assert formatter(b"x", 0.0000001) == b"x;dur=0.000"
    with pytest.raises(ValueError):
        formatter(b"", 1.0)
    with pytest.raises(ValueError):
        formatter(b"n" * 65, 1.0)


def test_formatter_renders_dur_in_milliseconds_to_three_places() -> None:
    for seconds in (0.0, 1e-9, 0.5, 1.23456789, 1000.0):
        assert formatter(b"total", seconds) == f"total;dur={seconds * 1000:.3f}".encode()


async def test_header_reports_a_plausible_duration() -> None:
    app = Wreath(http_policy=HttpPolicy(server_timing=ServerTimingPolicy()))

    @app.get("/")
    async def index(request: Any) -> str:
        await asyncio.sleep(0.02)
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/")

    value = response.header("server-timing")
    assert value is not None
    match = re.fullmatch(r"total;dur=(\d+\.\d{3})", value)
    assert match is not None, value
    assert float(match.group(1)) >= 20.0


async def test_elapsed_is_recorded_for_later_readers() -> None:
    policy = HttpPolicy(server_timing=ServerTimingPolicy(emit_header=False))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "headers": [],
        },
        None,
        None,
    )
    await policy._reference_ingress(request)
    await asyncio.sleep(0)
    response = await policy._reference_egress(request, Response(b"ok"))
    assert all(name != b"server-timing" for name, _value in response.headers)
    assert elapsed(request) > 0.0


async def test_custom_metric_name() -> None:
    app = Wreath(http_policy=HttpPolicy(server_timing=ServerTimingPolicy(metric="app")))

    @app.get("/")
    async def index(request: Any) -> str:
        return "ok"

    async with TestClient(app) as client:
        response = await client.get("/")

    assert response.header("server-timing").startswith("app;dur=")


def test_metric_name_cannot_inject_a_header() -> None:
    for bad in ("", "a b", "total;dur=0", "x" * 65, "a\nb", 'q"'):
        with pytest.raises(ValueError, match="metric must be"):
            ServerTimingPolicy(metric=bad)


def test_elapsed_without_the_middleware_is_an_error() -> None:
    from wreath.request import Request

    with pytest.raises(RuntimeError, match="has not timed"):
        elapsed(Request({"type": "http", "headers": []}, None))


def test_elapsed_prefers_the_native_policy_measurement() -> None:
    request = SimpleNamespace(
        _context=SimpleNamespace(policy_elapsed=1.25),
        state={"_wreath_timing_elapsed": 9.0},
    )

    assert elapsed(request) == 1.25
