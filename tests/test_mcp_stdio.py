from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest import mock

from wreath import Wreath
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
