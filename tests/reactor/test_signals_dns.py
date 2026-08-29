from __future__ import annotations

import asyncio
import os
import signal
import socket

import pytest

from .support import run


def test_getaddrinfo_resolves_loopback(loop):
    async def main():
        res = await loop.getaddrinfo("127.0.0.1", 80, type=socket.SOCK_STREAM)
        return any(entry[4][0] == "127.0.0.1" for entry in res)

    assert run(loop, main()) is True


def test_getnameinfo_loopback(loop):
    async def main():
        host, _port = await loop.getnameinfo(("127.0.0.1", 80))
        return isinstance(host, str)

    assert run(loop, main()) is True


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="no SIGUSR1 on platform")
def test_add_and_remove_signal_handler(loop):
    got = []

    async def main():
        loop.add_signal_handler(signal.SIGUSR1, lambda: got.append(1))
        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.sleep(0.02)
        loop.remove_signal_handler(signal.SIGUSR1)
        return got

    assert run(loop, main()) == [1]
