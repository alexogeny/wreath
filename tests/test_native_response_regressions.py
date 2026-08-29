from __future__ import annotations

from typing import Any

import pytest
from test_server_protocol import IMPLS, drive


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_cls", IMPLS)
async def test_response_headers_accept_list_pairs(protocol_cls: type) -> None:
    async def app(scope: dict, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"x-list-pair", b"accepted"]],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    transport = await drive(
        protocol_cls,
        app,
        [b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"],
    )
    assert b"x-list-pair: accepted\r\n" in transport.buffer
