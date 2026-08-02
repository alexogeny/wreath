from __future__ import annotations

import socket

import pytest

from wreath.http_client import ConnectError, HTTPClient


@pytest.mark.asyncio
async def test_plain_connection_failures_are_not_misreported_as_tls_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HTTPClient("connect-errors", base_url="http://example.com")
    address = (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("192.0.2.1", 80),
    )
    failure = OSError("connection refused")

    async def resolve(_client: HTTPClient) -> tuple[tuple[object, ...], ...]:
        return (address,)

    async def refuse(*_args: object) -> object:
        raise failure

    monkeypatch.setattr(HTTPClient, "_resolve", resolve)
    monkeypatch.setattr(HTTPClient, "_open_address", refuse)

    with pytest.raises(ConnectError, match="destination connection failed") as caught:
        await client._connect()

    assert caught.value.__cause__ is failure
