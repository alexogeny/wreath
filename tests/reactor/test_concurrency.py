"""Multi-worker reactors and GIL discipline.

The production model is one reactor per worker thread behind SO_REUSEPORT. These
pin two invariants: independent reactors serve correctly in parallel, and a
single reactor stays correct under many concurrent clients (no lost wakeups, no
torn state) with the GIL released only across the blocking poll.
"""
from __future__ import annotations

import asyncio
import socket
import threading

from .support import reactor_serve, run, sync_ok_app


def _http_get(host, port) -> bytes:
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    return data


def test_single_reactor_handles_many_concurrent_clients(loop):
    n = 40
    results: list[bytes] = []
    lock = threading.Lock()

    async def main():
        h = await reactor_serve(loop, sync_ok_app(), protocols=("http/1.1",))

        def worker():
            data = _http_get(h.host, h.port)
            with lock:
                results.append(data)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await h.aclose()
        return results

    out = run(loop, main())
    assert len(out) == n
    assert all(r.startswith(b"HTTP/1.1 200") for r in out)


def test_two_reactor_workers_serve_independently(make_native_loop):
    """Two loops, two threads, two ports — each answers its own clients."""
    ready = threading.Event()
    stop = threading.Event()
    ports: dict[int, int] = {}
    responses: dict[int, bytes] = {}

    def worker(idx):
        lp = make_native_loop(None)

        async def serve():
            h = await reactor_serve(lp, sync_ok_app(), protocols=("http/1.1",))
            ports[idx] = h.port
            ready_count.append(idx)
            if len(ready_count) == 2:
                ready.set()
            while not stop.is_set():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await h.aclose()

        lp.run_until_complete(serve())

    ready_count: list[int] = []
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    try:
        assert ready.wait(timeout=5), "reactors did not start"
        for idx in (0, 1):
            responses[idx] = _http_get("127.0.0.1", ports[idx])
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)

    assert responses[0].startswith(b"HTTP/1.1 200")
    assert responses[1].startswith(b"HTTP/1.1 200")
