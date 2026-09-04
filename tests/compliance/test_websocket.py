from __future__ import annotations

from typing import cast

import pytest

from wreath.websocket import _valid_close_code


async def _receive() -> dict[str, str]:
    return {"type": "websocket.disconnect"}


def test_reserved_and_out_of_range_close_codes_are_rejected() -> None:
    # RFC 6455 §7.4.1 — 1004/1005/1006/1015 and <1000 / 1016–2999 are not sendable.
    for bad in (1004, 1005, 1006, 1015, 999, 0, 1016, 2999, 5000):
        assert not _valid_close_code(bad), bad


def test_assigned_and_application_close_codes_are_accepted() -> None:
    for ok in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 3000, 4000, 4999):
        assert _valid_close_code(ok), ok


@pytest.mark.asyncio
async def test_close_rejects_an_invalid_code() -> None:
    from wreath.websocket import WebSocket

    async def _send(_message: dict) -> None: ...

    ws = WebSocket({"type": "websocket"}, _receive, _send)
    with pytest.raises(ValueError):
        await ws.close(code=1006)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [True, 1000.0, "1000"])
async def test_close_requires_an_exact_integer_code(code: object) -> None:
    from wreath.websocket import WebSocket

    async def _send(_message: dict) -> None: ...

    ws = WebSocket({"type": "websocket"}, _receive, _send)
    with pytest.raises(ValueError, match="integer"):
        await ws.close(code=cast(int, code))


@pytest.mark.asyncio
async def test_close_refuses_a_reason_too_large_for_the_control_frame() -> None:
    from wreath.websocket import WebSocket

    async def _receive() -> dict[str, str]:
        return {"type": "websocket.connect"}

    async def _send(_message: dict) -> None: ...

    ws = WebSocket({"type": "websocket"}, _receive, _send)
    with pytest.raises(ValueError, match="123 UTF-8 bytes"):
        await ws.close(reason="é" * 62)


@pytest.mark.asyncio
async def test_close_requires_a_string_reason() -> None:
    from wreath.websocket import WebSocket

    async def _receive() -> dict[str, str]:
        return {"type": "websocket.connect"}

    async def _send(_message: dict) -> None: ...

    ws = WebSocket({"type": "websocket"}, _receive, _send)
    with pytest.raises(ValueError, match="reason must be a string"):
        await ws.close(reason=cast(str, b"not text"))


@pytest.mark.asyncio
async def test_close_allows_a_reason_at_the_control_frame_limit() -> None:
    from wreath.websocket import WebSocket

    incoming = iter(({"type": "websocket.connect"},))
    sent: list[dict] = []

    async def _receive() -> dict:
        return next(incoming)

    async def _send(message: dict) -> None:
        sent.append(message)

    reason = "é" * 61 + "a"
    ws = WebSocket({"type": "websocket"}, _receive, _send)
    await ws.close(reason=reason)
    assert sent == [{"type": "websocket.close", "code": 1000, "reason": reason}]
