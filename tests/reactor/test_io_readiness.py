"""fd readiness (add_reader/add_writer) and coroutine socket ops.

These exercise the Poller directly (epoll today, io_uring later). Level-trigger
semantics and the sock_* coroutines are the substrate every protocol rides on.
"""
from __future__ import annotations

import asyncio
import socket

from .conftest import require_reactor
from .support import run, socketpair, tcp_listener


def test_add_reader_fires_when_readable(loop):
    a, b = socketpair()
    got = []

    async def main():
        def on_read():
            got.append(a.recv(100))
            loop.remove_reader(a.fileno())

        loop.add_reader(a.fileno(), on_read)
        b.send(b"hello")
        await asyncio.sleep(0.02)
        return got

    try:
        assert run(loop, main()) == [b"hello"]
    finally:
        a.close()
        b.close()


def test_remove_reader_stops_callbacks(loop):
    a, b = socketpair()
    count = {"n": 0}

    async def main():
        def on_read():
            count["n"] += 1
            a.recv(100)

        loop.add_reader(a.fileno(), on_read)
        b.send(b"1")
        await asyncio.sleep(0.02)
        removed = loop.remove_reader(a.fileno())
        b.send(b"2")
        await asyncio.sleep(0.02)
        return removed, count["n"]

    try:
        removed, n = run(loop, main())
        assert removed is True
        assert n == 1  # second send arrived after removal
    finally:
        a.close()
        b.close()


def test_same_batch_event_cannot_reach_replaced_registration():
    loop = require_reactor().metal_event_loop()
    a, b = socketpair()
    calls: list[int] = []

    async def main():
        done = loop.create_future()
        fd = a.fileno()

        def replacement_writer():
            # A later level-triggered writable event is legitimate, but the
            # stale same-batch record must have been counted before it can run.
            calls.append(loop._poller.stale_events)
            loop.remove_writer(fd)

        def original_writer():
            calls.append(-1)
            loop.remove_writer(fd)

        def reader():
            a.recv(1)
            loop.remove_reader(fd)
            loop.remove_writer(fd)
            loop.add_writer(fd, replacement_writer)
            done.set_result(None)

        loop.add_reader(fd, reader)
        loop.add_writer(fd, original_writer)
        b.send(b"x")
        await done
        return calls

    try:
        # The readable callback changes the registration before this epoll
        # record's writable half is dispatched. Neither old readiness nor its
        # stale token may reach the replacement callback.
        observed = run(loop, main())
        assert -1 not in observed
        assert all(stale_count >= 1 for stale_count in observed)
        assert loop._poller.stale_events >= 1
    finally:
        a.close()
        b.close()
        loop.close()


def test_add_writer_fires_when_writable(loop):
    a, b = socketpair()
    fired = []

    async def main():
        def on_write():
            fired.append(True)
            loop.remove_writer(a.fileno())

        loop.add_writer(a.fileno(), on_write)
        await asyncio.sleep(0.02)
        return fired

    try:
        assert run(loop, main()) == [True]
    finally:
        a.close()
        b.close()


def test_sock_sendall_recv_roundtrip(loop):
    a, b = socketpair()

    async def main():
        await loop.sock_sendall(a, b"ping")
        return await loop.sock_recv(b, 4)

    try:
        assert run(loop, main()) == b"ping"
    finally:
        a.close()
        b.close()


def test_sock_accept_and_connect(loop):
    lsock, addr = tcp_listener()

    async def main():
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.setblocking(False)
        accept_fut = loop.sock_accept(lsock)
        await loop.sock_connect(client, addr)
        server_conn, _ = await accept_fut
        await loop.sock_sendall(client, b"z")
        data = await loop.sock_recv(server_conn, 1)
        server_conn.close()
        client.close()
        return data

    try:
        assert run(loop, main()) == b"z"
    finally:
        lsock.close()
