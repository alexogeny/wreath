from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import SplitResult, urlsplit

from ..http_client import HTTPClient

if TYPE_CHECKING:
    from .remote_mcp import MCPHTTPResponse

__all__ = ["HTTPClientTransport", "MCPHTTPClientTransport"]


class _StreamingResponse(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> tuple[tuple[bytes, bytes], ...]: ...

    def iter_bytes(self) -> AsyncIterator[bytes]: ...


class _StreamContext(Protocol):
    async def __aenter__(self) -> _StreamingResponse: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> bool: ...


class _OwnedBody:
    __slots__ = ("_closed", "_context", "_iterator")

    def __init__(self, context: _StreamContext, response: _StreamingResponse) -> None:
        self._context = context
        self._iterator = response.iter_bytes()
        self._closed = False

    def __aiter__(self) -> _OwnedBody:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await anext(self._iterator)
        finally:
            error = sys.exception()
            if isinstance(error, StopAsyncIteration):
                await self._exit(None, None)
            elif error is not None:
                await self._exit(type(error), error)

    async def aclose(self) -> None:
        if self._closed:
            return
        close = getattr(self._iterator, "aclose", None)
        try:
            if callable(close):
                await close()
        finally:
            await self._exit(GeneratorExit, None)

    async def _exit(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        await self._context.__aexit__(error_type, error, None)


class HTTPClientTransport:
    __slots__ = ("_base_path", "_client", "_origin")

    def __init__(self, client: HTTPClient, *, base_url: str) -> None:
        parsed = _absolute_url(base_url, label="base_url")
        if parsed.query:
            raise ValueError("model transport base_url must not include a query")
        origin = _origin(parsed)
        if client.origin != origin:
            raise ValueError(
                f"model transport base_url origin {origin!r} must match "
                f"HTTPClient origin {client.origin!r}"
            )
        self._client = client
        self._origin = origin
        self._base_path = parsed.path.rstrip("/")

    async def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, Mapping[str, str], _OwnedBody]:
        parsed = _absolute_url(url, label="request URL")
        if _origin(parsed) != self._origin:
            raise ValueError(
                f"model transport request URL must use configured origin {self._origin!r}"
            )
        target = self._target(parsed)
        encoded_headers = _request_headers(headers)
        context = self._client.stream(
            method,
            target,
            headers=encoded_headers,
            body=body,
        )
        response = await context.__aenter__()
        try:
            response_headers = _response_headers(response.headers)
        except ValueError as error:
            await context.__aexit__(ValueError, error, error.__traceback__)
            raise
        return response.status, response_headers, _OwnedBody(context, response)

    def _target(self, parsed: SplitResult) -> str:
        path = parsed.path or "/"
        base_path = self._base_path
        if base_path and path != base_path and not path.startswith(f"{base_path}/"):
            raise ValueError("model transport request URL must be within its configured base URL")
        target = path[len(base_path) :] if base_path else path
        if not target:
            target = "/"
        return f"{target}?{parsed.query}" if parsed.query else target


class MCPHTTPClientTransport:
    __slots__ = ("_client", "_endpoint", "_target", "origin")

    def __init__(self, client: HTTPClient, *, endpoint: str) -> None:
        parsed = _absolute_url(endpoint, label="MCP endpoint")
        if parsed.scheme != "https":
            raise ValueError("MCP HTTP endpoint must use HTTPS")
        origin = _origin(parsed)
        if client.origin != origin:
            raise ValueError(
                f"MCP HTTP endpoint origin {origin!r} must match HTTPClient origin "
                f"{client.origin!r}"
            )
        target = parsed.path or "/"
        self._client = client
        self._endpoint = endpoint
        self._target = f"{target}?{parsed.query}" if parsed.query else target
        self.origin = origin

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        max_response_bytes: int,
    ) -> MCPHTTPResponse:
        if url != self._endpoint:
            raise ValueError(
                f"MCP HTTP request URL must equal configured endpoint {self._endpoint!r}"
            )
        if max_response_bytes < 1:
            raise ValueError("MCP HTTP max_response_bytes must be positive")
        encoded_headers = _request_headers(headers)
        async with self._client.stream(
            method,
            self._target,
            headers=encoded_headers,
            body=b"" if body is None else body,
        ) as response:
            parts: list[bytes] = []
            total = 0
            async for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_response_bytes:
                    raise ValueError(f"MCP HTTP response exceeds {max_response_bytes} bytes")
                parts.append(chunk)
            response_headers = _response_headers(response.headers)
            status = response.status
        from .remote_mcp import MCPHTTPResponse

        return MCPHTTPResponse(url, status, response_headers, b"".join(parts))

    async def close(self) -> None:
        return None


def _absolute_url(value: str, *, label: str) -> SplitResult:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"model transport {label} must be an absolute HTTP URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and port <= 0
        or parsed.fragment
    ):
        raise ValueError(f"model transport {label} must be an absolute HTTP URL")
    return parsed


def _origin(parsed: SplitResult) -> str:
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if host is None:
        raise ValueError("model transport URL must include a host")
    normalized_host = host.encode("idna").decode("ascii").lower()
    display_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    port = parsed.port
    default_port = 443 if scheme == "https" else 80
    authority = display_host if port in {None, default_port} else f"{display_host}:{port}"
    return f"{scheme}://{authority}"


def _request_headers(headers: Mapping[str, str]) -> tuple[tuple[bytes, bytes], ...]:
    converted: list[tuple[bytes, bytes]] = []
    for name, value in headers.items():
        try:
            encoded_name = name.encode("ascii")
            encoded_value = value.encode("latin-1")
        except UnicodeEncodeError as error:
            raise ValueError(
                "model transport header names must be ASCII and values must be "
                "ISO-8859-1 representable"
            ) from error
        converted.append((encoded_name, encoded_value))
    return tuple(converted)


def _response_headers(headers: tuple[tuple[bytes, bytes], ...]) -> Mapping[str, str]:
    try:
        return {name.decode("ascii"): value.decode("latin-1") for name, value in headers}
    except UnicodeDecodeError as error:
        raise ValueError("HTTPClient response header names must be ASCII representable") from error
