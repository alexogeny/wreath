"""ServerTimingPolicy: header formatting and the recorded measurement."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from wreath import Request, Response, Wreath
from wreath._native import _core
from wreath._pure.observability import format_server_timing as pure_format
from wreath.policy import HttpPolicy, ServerTimingPolicy, elapsed
from wreath.testing import TestClient

_FORMATTERS = [pure_format]
if _core is not None and hasattr(_core, "format_server_timing"):
    _FORMATTERS.append(_core.format_server_timing)


@pytest.mark.parametrize("formatter", _FORMATTERS)
def test_formatter_renders_milliseconds(formatter: Any) -> None:
    assert formatter(b"total", 0.0) == b"total;dur=0.000"
    assert formatter(b"total", 1.5) == b"total;dur=1500.000"
    assert formatter(b"app", 0.0123456) == b"app;dur=12.346"
    assert formatter(b"x", 0.0000001) == b"x;dur=0.000"
    with pytest.raises(ValueError):
        formatter(b"", 1.0)
    with pytest.raises(ValueError):
        formatter(b"n" * 65, 1.0)


def test_native_formatter_agrees_with_pure_reference() -> None:
    if _core is None or not hasattr(_core, "format_server_timing"):
        pytest.skip("native core unavailable")
    for seconds in (0.0, 1e-9, 0.5, 1.23456789, 1000.0):
        assert _core.format_server_timing(b"total", seconds) == pure_format(b"total", seconds)


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
    """The measurement an access log or tracing exporter will read."""
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
    app = Wreath(
        http_policy=HttpPolicy(server_timing=ServerTimingPolicy(metric="app"))
    )

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
