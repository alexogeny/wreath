"""cProfile targets for browser policy and compression middleware."""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import pstats
from typing import Any

from wreath import JSONResponse, Request, Response
from wreath.cache_control import CacheControl
from wreath.middleware import (
    CacheControlMiddleware,
    CompressionMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    csrf_token,
)


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": headers
            or [(b"host", b"example.test"), (b"accept-encoding", b"gzip")],
        },
        _receive,
    )


async def profile_compression(iterations: int) -> None:
    middleware = CompressionMiddleware(minimum_size=1024)
    request = _request()
    payload = {"message": "profile compression" * 1000}
    for _ in range(iterations):
        await middleware.after(request, JSONResponse(payload))


async def profile_compression_skip(iterations: int) -> None:
    middleware = CompressionMiddleware(minimum_size=1024)
    request = _request(headers=[(b"host", b"example.test")])
    for _ in range(iterations):
        await middleware.after(request, Response(b"tiny"))


async def profile_security(iterations: int) -> None:
    middleware = SecurityHeadersMiddleware(
        hsts_max_age=31_536_000, hsts_include_subdomains=True
    )
    request = _request()
    for _ in range(iterations):
        await middleware.after(request, Response(b"ok"))


async def profile_cache(iterations: int) -> None:
    middleware = CacheControlMiddleware(CacheControl(private=True, no_store=True))
    request = _request()
    for _ in range(iterations):
        await middleware.after(request, Response(b"ok"))


async def profile_csrf(iterations: int) -> None:
    middleware = CSRFMiddleware("s" * 32)
    safe = _request()
    await middleware.before(safe)
    token = csrf_token(safe)
    headers = [
        (b"host", b"example.test"),
        (b"origin", b"https://example.test"),
        (b"cookie", f"wreath_csrf={token}".encode()),
        (b"x-csrf-token", token.encode()),
    ]
    for _ in range(iterations):
        result = await middleware.before(_request("POST", headers))
        if result is not None:
            raise AssertionError("valid CSRF token was rejected")


async def run(name: str, iterations: int) -> None:
    targets = {
        "compression": profile_compression,
        "compression-skip": profile_compression_skip,
        "security": profile_security,
        "cache": profile_cache,
        "csrf": profile_csrf,
    }
    await targets[name](iterations)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario",
        choices=("compression", "compression-skip", "security", "cache", "csrf"),
    )
    parser.add_argument("iterations", type=int)
    args = parser.parse_args()
    profiler = cProfile.Profile()
    profiler.enable()
    asyncio.run(run(args.scenario, args.iterations))
    profiler.disable()
    pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(30)


if __name__ == "__main__":
    main()
