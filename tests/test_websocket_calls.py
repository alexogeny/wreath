"""Request/response over a frame pipe, correlated and bounded.

A WebSocket delivers frames in order and pairs nothing. Every protocol that
puts a request/response contract on one grows the same three pieces by hand: an
identifier on the way out, a map of what is outstanding, and a deadline. `Calls`
is those three, and `wreath._correlation.Pending` is the part it shares with
`wreath.entity` rather than writing twice.

What is pinned here is the behaviour that is easy to get wrong in either place:

* a reply for an identifier nobody awaits is ordinary, not an error;
* a second reply must not settle an already-answered call;
* a malformed frame must not end the read loop, because the calls still
  outstanding on that socket are still answerable;
* a closing socket fails its outstanding calls rather than letting each wait
  out a deadline for an answer that provably cannot arrive.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath._correlation import Pending, TooManyPending
from wreath.websocket import Calls, CallsClosed

pytestmark = pytest.mark.asyncio


class FakeSocket:
    """A frame pipe: what is sent is recorded, what is queued is received."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        #: Signalled on every send, so a test waits on an event rather than
        #: spinning on a list -- which is what ASYNC110 is about, and it is
        #: right: a poll loop here would pass on a fast machine and flake on a
        #: loaded one.
        self.wrote = asyncio.Event()

    async def send(self, data: Any) -> None:
        self.sent.append(data)
        self.wrote.set()

    async def wait_for_sends(self, count: int, *, within: float = 2.0) -> None:
        """Wait for `count` frames, or fail rather than hang.

        Bounded because an unbounded wait here is a test that hangs forever when
        the send never happens -- which is exactly what a mutation that breaks
        the send path produces, and a hung test is reported *undecided* rather
        than as the kill it should be.
        """
        async with asyncio.timeout(within):
            while len(self.sent) < count:
                self.wrote.clear()
                await self.wrote.wait()

    def deliver(self, frame: Any) -> None:
        self._inbox.put_nowait(frame)

    def finish(self) -> None:
        self._closed = True
        self._inbox.put_nowait(None)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> Any:
        frame = await self._inbox.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


def _calls(ws: FakeSocket, **kw: Any) -> Calls:
    return Calls(
        ws,  # type: ignore[arg-type]
        reply_to=lambda message: message.get("reply_to"),
        label=lambda identifier, payload: {"id": identifier, **payload},
        **kw,
    )


async def _answer_next(ws: FakeSocket, calls: Calls, body: dict[str, Any]) -> None:
    """Reply to whatever request was sent most recently."""
    await ws.wait_for_sends(1)
    import json

    identifier = json.loads(ws.sent[-1])["id"]
    ws.deliver(json.dumps({"reply_to": identifier, **body}))


# --- the round trip -------------------------------------------------------------------


async def test_a_call_gets_the_reply_that_names_it() -> None:
    ws = FakeSocket()
    async with _calls(ws) as calls:
        task = asyncio.create_task(calls.call({"op": "read"}))
        await _answer_next(ws, calls, {"value": 42})
        assert (await task)["value"] == 42


async def test_two_calls_are_answered_out_of_order() -> None:
    # The entire point of an identifier: frames come back in whatever order the
    # peer produces them, and each caller gets its own answer.
    import json

    ws = FakeSocket()
    async with _calls(ws) as calls:
        first = asyncio.create_task(calls.call({"op": "a"}))
        second = asyncio.create_task(calls.call({"op": "b"}))
        await ws.wait_for_sends(2)
        ids = [json.loads(frame)["id"] for frame in ws.sent]
        ws.deliver(json.dumps({"reply_to": ids[1], "which": "b"}))
        ws.deliver(json.dumps({"reply_to": ids[0], "which": "a"}))
        assert (await first)["which"] == "a"
        assert (await second)["which"] == "b"


async def test_a_call_that_is_never_answered_times_out() -> None:
    ws = FakeSocket()
    async with _calls(ws) as calls:
        with pytest.raises(TimeoutError):
            await calls.call({"op": "read"}, timeout=0.05)
        # And the slot is released, or a timing-out peer would fill the map.
        assert calls.outstanding == 0


async def test_a_reply_nobody_awaits_is_ignored() -> None:
    import json

    ws = FakeSocket()
    async with _calls(ws) as calls:
        ws.deliver(json.dumps({"reply_to": "never-issued", "value": 1}))
        await asyncio.sleep(0.01)
        assert calls.outstanding == 0  # and no exception escaped the read loop


async def test_a_second_reply_does_not_disturb_the_first() -> None:
    import json

    ws = FakeSocket()
    async with _calls(ws) as calls:
        task = asyncio.create_task(calls.call({"op": "read"}))
        await _answer_next(ws, calls, {"value": "first"})
        identifier = json.loads(ws.sent[-1])["id"]
        ws.deliver(json.dumps({"reply_to": identifier, "value": "second"}))
        await asyncio.sleep(0.01)
        assert (await task)["value"] == "first"


# --- the peer's own requests ----------------------------------------------------------


async def test_a_peer_initiated_frame_reaches_the_handler() -> None:
    import json

    ws = FakeSocket()
    calls = _calls(ws)
    seen: list[Any] = []

    @calls.on_request
    async def handle(message: Any) -> Any:
        seen.append(message)
        return {"reply_to": message["id"], "ok": True}

    async with calls:
        ws.deliver(json.dumps({"id": "peer-1", "op": "ping"}))
        await asyncio.sleep(0.01)

    assert seen and seen[0]["op"] == "ping"
    assert json.loads(ws.sent[-1]) == {"reply_to": "peer-1", "ok": True}


async def test_a_handler_returning_none_sends_nothing() -> None:
    # A notification -- a request with no response -- is a real shape, and
    # inventing an empty reply for it would put a frame on the wire the
    # protocol never described.
    import json

    ws = FakeSocket()
    calls = _calls(ws)

    @calls.on_request
    async def handle(message: Any) -> Any:
        return None

    async with calls:
        ws.deliver(json.dumps({"id": "peer-1", "op": "notify"}))
        await asyncio.sleep(0.01)

    assert ws.sent == []


async def test_only_one_request_handler() -> None:
    calls = _calls(FakeSocket())

    @calls.on_request
    async def first(message: Any) -> Any:
        return None

    with pytest.raises(ValueError, match="already has a request handler"):

        @calls.on_request
        async def second(message: Any) -> Any:
            return None


# --- the failure edges ----------------------------------------------------------------


async def test_a_malformed_frame_does_not_end_the_loop() -> None:
    # The socket is still live and the calls outstanding on it are still
    # answerable; closing on garbage is a decision for the protocol, not a
    # default of the demultiplexer.
    ws = FakeSocket()
    async with _calls(ws) as calls:
        ws.deliver("{not json")
        task = asyncio.create_task(calls.call({"op": "read"}))
        await _answer_next(ws, calls, {"value": "still here"})
        assert (await task)["value"] == "still here"


async def test_closing_fails_outstanding_calls_rather_than_stranding_them() -> None:
    ws = FakeSocket()
    calls = _calls(ws)
    await calls.__aenter__()
    task = asyncio.create_task(calls.call({"op": "read"}, timeout=30))
    await ws.wait_for_sends(1)
    await calls.close()
    with pytest.raises(CallsClosed):
        await task


async def test_a_disconnected_socket_fails_outstanding_calls() -> None:
    ws = FakeSocket()
    async with _calls(ws) as calls:
        task = asyncio.create_task(calls.call({"op": "read"}, timeout=30))
        await ws.wait_for_sends(1)
        ws.finish()
        with pytest.raises(CallsClosed):
            await task


async def test_too_many_outstanding_calls_are_refused() -> None:
    ws = FakeSocket()
    async with _calls(ws, max_pending=1) as calls:
        first = asyncio.create_task(calls.call({"op": "a"}, timeout=5))
        await ws.wait_for_sends(1)
        with pytest.raises(TooManyPending):
            await calls.call({"op": "b"}, timeout=5)
        assert calls.refusals == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first


# --- the shared primitive -------------------------------------------------------------


async def test_a_slot_is_released_even_when_the_body_raises() -> None:
    # A map that leaks on the failure path fills up exactly when it matters.
    pending = Pending(limit=4)
    with pytest.raises(RuntimeError):
        async with pending.slot():
            raise RuntimeError("boom")
    assert len(pending) == 0


async def test_settling_an_unknown_identifier_reports_false() -> None:
    pending = Pending(limit=4)
    assert pending.settle("nobody", 1) is False
    assert pending.fail("nobody", RuntimeError()) is False


async def test_a_duplicate_identifier_is_refused() -> None:
    pending = Pending(limit=4)
    async with pending.slot(identifier="x"):
        with pytest.raises(ValueError, match="already in flight"):
            async with pending.slot(identifier="x"):
                pass


async def test_a_pending_map_must_admit_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Pending(limit=0)


async def test_fail_all_reports_how_many_were_waiting() -> None:
    pending = Pending(limit=4)
    async with pending.slot() as (_a, first), pending.slot() as (_b, second):
        assert pending.fail_all(CallsClosed("gone")) == 2
        for waiter in (first, second):
            with pytest.raises(CallsClosed):
                await waiter
        # Already-failed futures are not failed twice.
        assert pending.fail_all(CallsClosed("gone")) == 0


async def test_settling_an_already_answered_slot_reports_false() -> None:
    # Without the `done()` check this raises InvalidStateError from inside a
    # read loop, which takes the loop down and strands every other call on the
    # socket. A late second answer is ordinary: a superseded peer can send one.
    pending = Pending(limit=4)
    async with pending.slot(identifier="x") as (_key, waiter):
        assert pending.settle("x", "first") is True
        assert pending.settle("x", "second") is False
        assert pending.fail("x", RuntimeError("late")) is False
        assert await waiter == "first"


# --- controls `wreath mutant` found nothing watching ----------------------------------


async def test_closing_a_calls_that_never_started_does_not_raise() -> None:
    # `close` is reachable without `__aenter__` -- a handler that fails before
    # entering the block still unwinds through it -- and cancelling a task that
    # was never created would raise from inside the cleanup path.
    ws = FakeSocket()
    calls = _calls(ws)
    await calls.close()


async def test_an_explicit_timeout_is_used_rather_than_the_default() -> None:
    # Asserted by *elapsed time*, not merely by raising: with the per-call
    # deadline discarded this still raises TimeoutError, just a second later.
    # A test that only checked the exception would pass either way, and one
    # that waited out the default would take longer than a mutant is given.
    ws = FakeSocket()
    async with _calls(ws, timeout=1.0) as calls:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(TimeoutError):
            await calls.call({"op": "read"}, timeout=0.02)
        assert loop.time() - started < 0.5


async def test_the_constructor_default_applies_when_no_timeout_is_given() -> None:
    ws = FakeSocket()
    async with _calls(ws, timeout=0.02) as calls:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(TimeoutError):
            await calls.call({"op": "read"})
        assert loop.time() - started < 0.5


async def test_a_peer_request_with_no_handler_is_dropped() -> None:
    # Registering `on_request` is optional: a caller may only ever make calls.
    # An unhandled peer frame must not take the read loop down with it.
    import json

    ws = FakeSocket()
    async with _calls(ws) as calls:
        ws.deliver(json.dumps({"id": "peer-1", "op": "ping"}))
        await asyncio.sleep(0.01)
        task = asyncio.create_task(calls.call({"op": "read"}))
        await _answer_next(ws, calls, {"value": "loop alive"})
        assert (await task)["value"] == "loop alive"


async def test_close_stops_the_read_loop() -> None:
    # `close` cancels the reader. Without that the loop outlives the object and
    # keeps answering on a socket its owner believes it has finished with.
    import json

    ws = FakeSocket()
    calls = _calls(ws)
    seen: list[Any] = []

    @calls.on_request
    async def handle(message: Any) -> Any:
        seen.append(message)
        return None

    await calls.__aenter__()
    ws.deliver(json.dumps({"id": "before", "op": "x"}))
    await asyncio.sleep(0.01)
    await calls.close()
    ws.deliver(json.dumps({"id": "after", "op": "x"}))
    await asyncio.sleep(0.01)

    assert [message["id"] for message in seen] == ["before"]
