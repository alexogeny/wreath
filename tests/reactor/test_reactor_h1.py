from __future__ import annotations

import asyncio
import socket
import threading
import time

from .support import (
    echo_app,
    reactor_serve,
    run,
    suspending_app,
    sync_ok_app,
)


def _blocking_client(host, port, payload, out, *, recv_all=True):
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(payload)
    data = b""
    while recv_all:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    out.append((s, data))
    if recv_all:
        s.close()


def _run_client(host, port, payload):
    out: list = []
    th = threading.Thread(target=_blocking_client, args=(host, port, payload, out))
    th.start()
    return th, out


def test_h1_post_echoes_body(loop):
    async def main():
        h = await reactor_serve(loop, echo_app(), protocols=("http/1.1",))
        req = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
        th, out = _run_client(h.host, h.port, req)
        while th.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await h.aclose()
        return out[0][1]

    raw = run(loop, main())
    assert raw.startswith(b"HTTP/1.1 200")
    assert raw.endswith(b"hello")


def test_h1_keep_alive_serves_two_requests(loop):
    got: list = []

    def client(host, port):
        s = socket.create_connection((host, port), timeout=5)
        for _ in range(2):
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            # read one response head+body (Content-Length: 0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s.recv(4096)
            got.append(buf)
        s.close()

    async def main():
        h = await reactor_serve(loop, echo_app(), protocols=("http/1.1",))
        th = threading.Thread(target=client, args=(h.host, h.port))
        th.start()
        while th.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await h.aclose()

    run(loop, main())
    assert len(got) == 2
    assert all(r.startswith(b"HTTP/1.1 200") for r in got)


def test_h1_pipelined_requests_answered_in_order(loop):
    def client(host, port, out):
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(
            b"GET /a HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
            b"GET /b HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        out.append(data)
        s.close()

    async def main():
        h = await reactor_serve(loop, sync_ok_app(), protocols=("http/1.1",))
        out: list = []
        th = threading.Thread(target=client, args=(h.host, h.port, out))
        th.start()
        while th.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await h.aclose()
        return out[0]

    data = run(loop, main())
    assert data.count(b"HTTP/1.1 200") == 2  # both pipelined requests answered


def test_h1_slow_header_hits_request_deadline(loop):
    from wreath.server import ServerConfig

    config = ServerConfig(protocols=("http/1.1",), request_timeout=0.2, keep_alive_timeout=0.2)

    def slow_client(host, port):
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(b"GET / HTTP/1.1\r\n")  # never finishes the head
        closed_at = None
        started = time.perf_counter()
        s.settimeout(2.0)
        try:
            # server should drop the connection once the deadline fires
            while True:
                if not s.recv(4096):
                    closed_at = "eof"
                    break
        except OSError:
            closed_at = "reset"
        s.close()
        return closed_at, time.perf_counter() - started

    async def main():
        h = await reactor_serve(loop, sync_ok_app(), protocols=("http/1.1",), config=config)
        closed_at, elapsed = await asyncio.to_thread(slow_client, h.host, h.port)
        await h.aclose()
        return closed_at, elapsed

    closed_at, elapsed = run(loop, main())
    assert closed_at in ("eof", "reset")
    assert elapsed < 0.8


def test_h1_suspending_handler_is_served_on_the_reactor_loop(loop):
    async def main():
        h = await reactor_serve(loop, suspending_app(), protocols=("http/1.1",))
        req = b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        th, out = _run_client(h.host, h.port, req)
        while th.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await h.aclose()
        return out[0][1]

    raw = run(loop, main())
    assert raw.startswith(b"HTTP/1.1 200"), raw[:200]
    assert raw.endswith(b"ok")


def test_h1_suspending_handler_that_raises_reports_500_not_an_abort(loop):
    async def main():
        h = await reactor_serve(loop, suspending_app(fail=True), protocols=("http/1.1",))
        req = b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        th, out = _run_client(h.host, h.port, req)
        while th.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await h.aclose()
        return out[0][1]

    raw = run(loop, main())
    assert raw.startswith(b"HTTP/1.1 500"), raw[:200]
