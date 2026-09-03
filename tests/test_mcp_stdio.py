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


class StubResponse:
    def __init__(self, body: bytes = b"", session: str | None = None) -> None:
        self.body = body
        self.session = session

    def header(self, name: str) -> str | None:
        return self.session if name == "mcp-session-id" else None


class StubClient:
    def __init__(self, *responses: StubResponse) -> None:
        self.responses = list(responses)
        self.posts: list[bytes] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, _path: str, *, content: bytes, headers: dict) -> StubResponse:
        self.posts.append(content)
        return self.responses.pop(0)


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


async def test_default_process_streams_are_used_when_not_overridden(monkeypatch) -> None:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
    }
    source = io.BytesIO(json.dumps(message).encode() + b"\n")
    sink = io.BytesIO()
    monkeypatch.setattr(
        stdio,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(buffer=source),
            stdout=SimpleNamespace(buffer=sink),
        ),
    )

    assert await stdio.serve(build()) == 0
    assert json.loads(sink.getvalue())["id"] == 1


async def test_blank_input_lines_are_ignored(monkeypatch) -> None:
    client = StubClient()
    monkeypatch.setattr(testing, "TestClient", lambda app: client)

    assert await stdio.serve(object(), stdin=io.BytesIO(b" \r\n"), stdout=io.BytesIO()) == 0
    assert client.posts == []


async def test_an_empty_initial_response_writes_no_blank_frame(monkeypatch) -> None:
    client = StubClient(StubResponse())
    sink = io.BytesIO()
    monkeypatch.setattr(testing, "TestClient", lambda app: client)

    assert await stdio.serve(object(), stdin=io.BytesIO(b"{}\n"), stdout=sink) == 0
    assert sink.getvalue() == b""


async def test_a_response_without_a_session_opens_no_stream(monkeypatch) -> None:
    client = StubClient(StubResponse(body=b"{}"))
    pumps = 0

    async def pump(*args: Any, **kwargs: Any) -> None:
        nonlocal pumps
        pumps += 1

    monkeypatch.setattr(testing, "TestClient", lambda app: client)
    monkeypatch.setattr(stdio, "_pump", pump)

    assert await stdio.serve(object(), stdin=io.BytesIO(b"{}\n"), stdout=io.BytesIO()) == 0
    assert pumps == 0


async def test_an_empty_relay_response_writes_no_blank_frame(monkeypatch) -> None:
    second_posted = asyncio.Event()

    class Client(StubClient):
        async def post(self, path: str, *, content: bytes, headers: dict) -> StubResponse:
            response = await super().post(path, content=content, headers=headers)
            if len(self.posts) == 2:
                second_posted.set()
            return response

    async def idle_pump(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    client = Client(StubResponse(body=b'{"id":1}', session="s"), StubResponse())
    pipe = Pipe()
    sink = io.BytesIO()
    monkeypatch.setattr(testing, "TestClient", lambda app: client)
    monkeypatch.setattr(stdio, "_pump", idle_pump)
    serving = asyncio.create_task(stdio.serve(object(), stdin=pipe.reader, stdout=sink))
    try:
        pipe.writer.write(b"{}\n{}\n")
        pipe.writer.flush()
        await asyncio.wait_for(second_posted.wait(), timeout=0.5)
        pipe.close()
        assert await asyncio.wait_for(serving, timeout=0.5) == 0
        assert sink.getvalue() == b'{"id":1}\n'
    finally:
        pipe.close()
        if not serving.done():
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        pipe.reader.close()


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


async def test_stream_pump_accepts_native_response_messages() -> None:
    written: list[bytes] = []

    class NativeClient:
        def _scope(self, *_args, **_kwargs):
            return {}, b""

        async def app(self, _scope, _receive, send):
            await send({"type": "wreath.response", "body": b'data: {"id":1}\n'})

    await _pump(NativeClient(), "/rpc", "session", written.append, asyncio.Lock())

    assert written == [b'{"id":1}']


async def test_stream_pump_discards_consumed_bytes_between_chunks() -> None:
    written: list[bytes] = []

    class SegmentedClient:
        def _scope(self, *_args, **_kwargs):
            return {}, b""

        async def app(self, _scope, _receive, send):
            await send({"type": "http.response.body", "body": b'data: {"id":1}\ndata: '})
            await send({"type": "http.response.body", "body": b'{"id":2}\n'})

    await _pump(SegmentedClient(), "/rpc", "session", written.append, asyncio.Lock())

    assert written == [b'{"id":1}', b'{"id":2}']


async def test_empty_session_headers_do_not_claim_a_session() -> None:
    assert "mcp-session-id" not in stdio._headers("")


async def test_normal_shutdown_propagates_a_stream_failure(monkeypatch) -> None:
    failed = asyncio.Event()

    async def failing_pump(*args: Any, **kwargs: Any) -> None:
        failed.set()
        raise RuntimeError("stream failed")

    client = StubClient(StubResponse(body=b"{}", session="s"))
    pipe = Pipe()
    monkeypatch.setattr(testing, "TestClient", lambda app: client)
    monkeypatch.setattr(stdio, "_pump", failing_pump)
    serving = asyncio.create_task(stdio.serve(object(), stdin=pipe.reader, stdout=io.BytesIO()))
    try:
        pipe.send({})
        await asyncio.wait_for(failed.wait(), timeout=0.5)
        pipe.close()
        with pytest.raises(RuntimeError, match="stream failed"):
            await asyncio.wait_for(serving, timeout=0.5)
    finally:
        pipe.close()
        if not serving.done():
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        pipe.reader.close()


async def test_cancellation_is_not_replaced_by_a_stream_failure(monkeypatch) -> None:
    failed = asyncio.Event()

    async def failing_pump(*args: Any, **kwargs: Any) -> None:
        failed.set()
        raise RuntimeError("stream failed")

    client = StubClient(StubResponse(body=b"{}", session="s"))
    pipe = Pipe()
    monkeypatch.setattr(testing, "TestClient", lambda app: client)
    monkeypatch.setattr(stdio, "_pump", failing_pump)
    serving = asyncio.create_task(stdio.serve(object(), stdin=pipe.reader, stdout=io.BytesIO()))
    try:
        pipe.send({})
        await asyncio.wait_for(failed.wait(), timeout=0.5)
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
    finally:
        pipe.close()
        if not serving.done():
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        pipe.reader.close()


async def test_shutdown_tolerates_an_absent_current_task(monkeypatch) -> None:
    client = StubClient()
    monkeypatch.setattr(testing, "TestClient", lambda app: client)

    with mock.patch.object(stdio.asyncio, "current_task", return_value=None):
        assert await stdio.serve(object(), stdin=io.BytesIO(), stdout=io.BytesIO()) == 0


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
