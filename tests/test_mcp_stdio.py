from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from wreath import Wreath, testing
from wreath._mcp import stdio
from wreath._mcp.stdio import _pump
from wreath.mcp import MCP, PROTOCOL_VERSION


class Pipe:
    """A blocking pipe pair, because a `BytesIO` reads EOF immediately.

    The relay reads stdin on a worker thread precisely so a blocking read does
    not stop the stream being written, and a fake that never blocks would test
    the opposite of what runs.
    """

    def __init__(self) -> None:
        read_fd, write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, "rb")
        self.writer = os.fdopen(write_fd, "wb")

    def send(self, payload: dict) -> None:
        self.writer.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.writer.flush()

    def close(self) -> None:
        if not self.writer.closed:
            self.writer.close()


class Sink:
    """Collects the relay's stdout, line by line, as it is written."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self._pending = asyncio.Queue()
        self._loop = asyncio.get_event_loop()

    def write(self, payload: bytes) -> None:
        for line in payload.splitlines():
            if line.strip():
                decoded = json.loads(line)
                self.lines.append(decoded)
                self._pending.put_nowait(decoded)

    def flush(self) -> None:
        return None

    async def next(self, seconds: float = 0.5) -> dict:
        return await asyncio.wait_for(self._pending.get(), timeout=seconds)


@contextlib.asynccontextmanager
async def relay_session() -> AsyncIterator[tuple[Pipe, Sink]]:
    pipe = Pipe()
    sink = Sink()
    process_streams = SimpleNamespace(
        stdin=SimpleNamespace(buffer=io.BytesIO()),
        stdout=SimpleNamespace(buffer=Sink()),
    )
    with mock.patch.object(stdio, "sys", process_streams):
        relay = asyncio.create_task(stdio.serve(build(), stdin=pipe.reader, stdout=sink))
        try:
            yield pipe, sink
        finally:
            pipe.close()
            try:
                await asyncio.wait_for(relay, timeout=0.25)
            except TimeoutError:
                relay.cancel()
                await asyncio.gather(relay, return_exceptions=True)
            pipe.reader.close()


def build() -> Wreath:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.tool(description="Counts sightings of a species.")
    async def count_sightings(request, species: str) -> dict:
        return {"species": species, "count": 3}

    @mcp.tool(description="Summarises, by asking the client's model.", sampling=True)
    async def summarise(request, note: str) -> dict:
        answer = await request.state.mcp.sample(note, max_tokens=16)
        return {"summary": answer["content"]["text"]}

    return app


async def test_relay_applies_backpressure_at_its_inflight_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(stdio, "_MAX_INFLIGHT", 2)
    app = Wreath()
    mcp = MCP(app, name="bounded", version="1.0.0")
    release = asyncio.Event()
    started = 0
    two_started = asyncio.Event()

    @mcp.tool(description="Waits so the relay ceiling is observable.")
    async def wait(request) -> dict:
        nonlocal started
        started += 1
        if started == 2:
            two_started.set()
        await release.wait()
        return {}

    pipe = Pipe()
    sink = Sink()
    relay = asyncio.create_task(stdio.serve(app, stdin=pipe.reader, stdout=sink))
    try:
        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
            }
        )
        await sink.next()
        for identifier in range(2, 5):
            pipe.send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": "tools/call",
                    "params": {"name": "wait"},
                }
            )
        await asyncio.wait_for(two_started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert started == 2
        release.set()
        await asyncio.gather(*(sink.next() for _ in range(3)))
    finally:
        pipe.close()
        await asyncio.wait_for(relay, timeout=0.5)
        pipe.reader.close()


async def test_backpressure_observes_every_completed_failure(monkeypatch) -> None:
    monkeypatch.setattr(stdio, "_MAX_INFLIGHT", 2)
    release = asyncio.Event()
    two_started = asyncio.Event()
    relay_tasks: list[ObservedTask] = []
    calls = 0

    class Response:
        body = b""

        @staticmethod
        def header(name: str) -> str:
            return "session" if name == "mcp-session-id" else ""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return Response()
            if calls == 3:
                two_started.set()
            await release.wait()
            raise RuntimeError(f"relay {calls} failed")

    class ObservedTask(asyncio.Task):
        observed = False

        def result(self):
            self.observed = True
            return super().result()

        def exception(self):
            self.observed = True
            return super().exception()

    async def idle_pump(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    def create_task(coro):
        task = ObservedTask(coro)
        if getattr(coro, "cr_code", None) is not None and coro.cr_code.co_name == "relay":
            relay_tasks.append(task)
        return task

    monkeypatch.setattr(testing, "TestClient", lambda app: Client())
    monkeypatch.setattr(stdio, "_pump", idle_pump)
    monkeypatch.setattr(stdio.asyncio, "create_task", create_task)
    pipe = Pipe()
    sink = Sink()
    serving = asyncio.create_task(stdio.serve(Wreath(), stdin=pipe.reader, stdout=sink))
    try:
        for identifier in range(4):
            pipe.send({"jsonrpc": "2.0", "id": identifier})
        await asyncio.wait_for(two_started.wait(), timeout=0.5)
        release.set()
        with pytest.raises(RuntimeError, match="relay"):
            await asyncio.wait_for(serving, timeout=0.5)
        assert len(relay_tasks) == 2
        assert all(task.observed for task in relay_tasks)
    finally:
        pipe.close()
        if not serving.done():
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        pipe.reader.close()


async def test_stream_pump_reassembles_one_data_line_from_many_fragments() -> None:
    payload = b'{"jsonrpc":"2.0","method":"notifications/progress"}'
    chunks = tuple(bytes((byte,)) for byte in b"data: " + payload + b"\n")
    written: list[bytes] = []

    class FragmentedClient:
        def _scope(self, *_args, **_kwargs):
            return {}, b""

        async def app(self, _scope, _receive, send):
            for chunk in chunks:
                await send({"type": "http.response.body", "body": chunk})

    await _pump(FragmentedClient(), "/rpc", "session", written.append, asyncio.Lock())

    assert written == [payload]


async def test_stream_pump_emits_every_complete_data_line_in_one_chunk() -> None:
    written: list[bytes] = []

    class BatchedClient:
        def _scope(self, *_args, **_kwargs):
            return {}, b""

        async def app(self, _scope, _receive, send):
            await send(
                {
                    "type": "http.response.body",
                    "body": b'data: {"id":1}\n\ndata: {"id":2}\n',
                }
            )

    await _pump(BatchedClient(), "/rpc", "session", written.append, asyncio.Lock())

    assert written == [b'{"id":1}', b'{"id":2}']


async def test_end_of_input_stops_the_relay() -> None:
    pipe = Pipe()
    pipe.close()
    sink = Sink()
    try:
        assert (
            await asyncio.wait_for(
                stdio.serve(build(), stdin=pipe.reader, stdout=sink),
                timeout=0.5,
            )
            == 0
        )
    finally:
        pipe.reader.close()


async def test_a_tool_can_be_called_over_the_pipe() -> None:
    async with relay_session() as (pipe, sink):
        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
            }
        )
        opened = await sink.next()
        assert opened["result"]["serverInfo"]["name"] == "camera-trap"

        pipe.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = await sink.next()
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "count_sightings",
            "summarise",
        ]

        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "count_sightings", "arguments": {"species": "fox"}},
            }
        )
        called = await sink.next()
        assert called["result"]["structuredContent"] == {"species": "fox", "count": 3}


async def test_a_schema_rejection_is_the_same_one_the_http_endpoint_gives() -> None:
    async with relay_session() as (pipe, sink):
        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
            }
        )
        await sink.next()
        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "count_sightings", "arguments": {"invented": 1}},
            }
        )
        refused = await sink.next()
        assert refused["error"]["code"] == -32602
        assert refused["error"]["data"]["errors"][0]["loc"] == ["arguments", "species"]


async def test_a_server_to_client_request_travels_out_and_its_answer_comes_back() -> None:
    async with relay_session() as (pipe, sink):
        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"sampling": {}},
                },
            }
        )
        await sink.next()
        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "summarise", "arguments": {"note": "a fox"}},
            }
        )

        asked = await sink.next()
        assert asked["method"] == "sampling/createMessage"
        pipe.send(
            {
                "jsonrpc": "2.0",
                "id": asked["id"],
                "result": {
                    "role": "assistant",
                    "content": {"type": "text", "text": "one fox"},
                    "model": "a-model",
                },
            }
        )
        answered = await sink.next()
        assert answered["result"]["structuredContent"] == {"summary": "one fox"}


async def test_the_subcommand_is_wired_to_the_relay() -> None:
    from wreath._cli import build_parser

    namespace = build_parser().parse_args(["mcp", "stdio", "app:app", "--path", "/rpc"])
    assert namespace.command == "mcp"
    assert namespace.mcp_action == "stdio"
    assert namespace.target == "app:app"
    assert namespace.path == "/rpc"
