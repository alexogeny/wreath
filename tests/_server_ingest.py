from __future__ import annotations

import asyncio
from typing import Any


def feed(protocol: Any, data: bytes, *, force_data_received: bool = False) -> None:
    if force_data_received or not isinstance(protocol, asyncio.BufferedProtocol):
        protocol.data_received(data)
        return
    view = memoryview(data)
    while True:
        target = memoryview(protocol.get_buffer(len(view) or -1))
        n = min(len(target), len(view))
        target[:n] = view[:n]
        # Real transports call buffer_updated() before dropping their view;
        # releasing first would abandon the offer.
        protocol.buffer_updated(n)
        target.release()
        view = view[n:]
        if not len(view):
            return
