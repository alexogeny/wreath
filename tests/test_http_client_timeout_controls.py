from __future__ import annotations

import socket
import ssl

import pytest

import wreath.http_client as http_client_module
from wreath.http_client import HTTPClient


@pytest.mark.asyncio
async def test_connect_https_scheme_and_ssl_context_guard_returns_plaintext_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Writer:
        closed = False

        def close(self) -> None:
            self.closed = True

    address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))
    winner = http_client_module._Connection(object(), Writer())

    async def resolve(_client: HTTPClient) -> list[tuple[object, ...]]:
        return [address]

    async def open_address(
        _client: HTTPClient,
        _address: tuple[object, ...],
        _delay: float,
        _context: ssl.SSLContext | None,
    ) -> object:
        return winner

    def reject_tls_setup(_client: HTTPClient) -> ssl.SSLContext:
        raise AssertionError("plaintext connection built a TLS context")

    monkeypatch.setattr(HTTPClient, "_resolve", resolve)
    monkeypatch.setattr(HTTPClient, "_open_address", open_address)
    monkeypatch.setattr(HTTPClient, "_build_ssl_context", reject_tls_setup)
    client = HTTPClient("plaintext-connect", base_url="http://example.com")

    assert await client._connect() is winner
