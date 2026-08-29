from __future__ import annotations

import pytest

from wreath.websocket import _valid_close_code


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

    ws = WebSocket({"type": "websocket"}, (lambda: None), _send)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await ws.close(code=1006)
