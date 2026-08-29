from __future__ import annotations

import asyncio

from .support import run, socketpair, tcp_listener


def test_pause_and_resume_reading(loop):
    a, b = socketpair()
    got: list[bytes] = []

    class Proto(asyncio.Protocol):
        def data_received(self, data):
            got.append(data)

    async def main():
        transport, _proto = await loop.create_connection(lambda: Proto(), sock=a)
        b.send(b"1")
        await asyncio.sleep(0.02)
        transport.pause_reading()
        b.send(b"2")
        await asyncio.sleep(0.02)
        after_pause = len(got)
        transport.resume_reading()
        await asyncio.sleep(0.02)
        after_resume = len(got)
        transport.close()
        return after_pause, after_resume

    try:
        after_pause, after_resume = run(loop, main())
        assert after_pause == 1  # "2" withheld while paused
        assert after_resume == 2  # delivered on resume
    finally:
        a.close()
        b.close()


def test_write_high_water_pauses_producer(loop):
    lsock, addr = tcp_listener()
    events: list[str] = []

    class Proto(asyncio.Protocol):
        def connection_made(self, transport):
            self.t = transport
            transport.set_write_buffer_limits(high=16 * 1024, low=4 * 1024)

        def pause_writing(self):
            events.append("pause")

        def resume_writing(self):
            events.append("resume")

    async def main():
        transport, proto = await loop.create_connection(lambda: Proto(), *addr)
        # No one reads the accepted socket: fill kernel + transport buffers.
        for _ in range(2000):
            proto.t.write(b"x" * 4096)
            if "pause" in events:
                break
            await asyncio.sleep(0)
        got_size = transport.get_write_buffer_size() > 0
        transport.abort()
        return "pause" in events, got_size

    try:
        paused, buffered = run(loop, main())
        assert paused is True
        assert buffered is True
    finally:
        lsock.close()


def test_write_buffer_limits_roundtrip(loop):
    a, b = socketpair()

    class Proto(asyncio.Protocol):
        def connection_made(self, transport):
            self.t = transport

    async def main():
        transport, proto = await loop.create_connection(lambda: Proto(), sock=a)
        proto.t.set_write_buffer_limits(high=8192, low=2048)
        low, high = proto.t.get_write_buffer_limits()  # asyncio returns (low, high)
        transport.close()
        return high, low

    try:
        high, low = run(loop, main())
        assert high == 8192 and low == 2048
    finally:
        a.close()
        b.close()
