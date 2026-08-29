from __future__ import annotations

import asyncio

from wreath import Wreath
from wreath._mcp.session import Session, SessionStore
from wreath.mcp import MCP, PROTOCOL_VERSION, MCPLimits
from wreath.testing import TestClient, TestResponse

STREAM = {"accept": "text/event-stream"}


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def initialize(client: TestClient) -> str:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
    )
    return header(response, "mcp-session-id") or ""


async def call(client: TestClient, session: str, payload: dict) -> dict:
    response = await client.post("/mcp", json=payload, headers={"mcp-session-id": session})
    return response.json()


def frames(response: TestResponse) -> list[dict]:
    """Every `message` event on a finished stream, decoded."""
    import json

    out: list[dict] = []
    for block in response.body.decode("utf-8").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def build() -> tuple[Wreath, MCP]:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.resource("camera://ridge", description="The ridge camera's latest note.")
    async def ridge(request) -> str:
        return "quiet"

    return app, mcp


def test_session_creation_does_not_scan_before_expiry_is_possible() -> None:
    class CountingStore(SessionStore):
        visits = 0

        def _expired(self, session: Session, now: float) -> bool:
            self.visits += 1
            return super()._expired(session, now)

    store = CountingStore(max_sessions=10, idle_seconds=10)
    for index in range(5):
        store.create(
            protocol_version=PROTOCOL_VERSION,
            client_info={"index": index},
            now=100 + index,
        )
    assert store.visits == 0

    store.create(protocol_version=PROTOCOL_VERSION, client_info={}, now=110)
    assert store.visits == 5
    assert len(store) == 5


def test_session_subscriber_index_tracks_unsubscribe_and_discard() -> None:
    store = SessionStore(max_sessions=2, idle_seconds=None)
    first = store.create(
        protocol_version=PROTOCOL_VERSION,
        client_info={},
        now=100,
    )
    second = store.create(
        protocol_version=PROTOCOL_VERSION,
        client_info={},
        now=100,
    )
    store.subscribe(first, "camera://ridge")
    store.subscribe(second, "camera://ridge")

    assert store.subscribers("camera://ridge") == [first, second]
    store.unsubscribe(first, "camera://ridge")
    assert store.subscribers("camera://ridge") == [second]
    assert store.discard(second.id)
    assert store.subscribers("camera://ridge") == []


async def test_a_subscriber_is_told_when_the_resource_changes() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session = await initialize(client)
        subscribed = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "camera://ridge"},
            },
        )
        assert subscribed["result"] == {}
        assert mcp.notify_resource_updated("camera://ridge") == 1
        # Nobody is subscribed to this one, so nobody is told about it.
        assert mcp.notify_resource_updated("camera://creek") == 0

        stream = asyncio.ensure_future(
            client.get("/mcp", headers={**STREAM, "mcp-session-id": session})
        )
        await asyncio.sleep(0)
        await client.delete("/mcp", headers={"mcp-session-id": session})
        response = await asyncio.wait_for(stream, timeout=5)

        assert header(response, "content-type") == "text/event-stream"
        assert frames(response) == [
            {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": "camera://ridge"},
            }
        ]


async def test_unsubscribing_stops_the_notifications() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session = await initialize(client)
        await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "camera://ridge"},
            },
        )
        await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/unsubscribe",
                "params": {"uri": "camera://ridge"},
            },
        )
        assert mcp.notify_resource_updated("camera://ridge") == 0


async def test_one_stream_per_session() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session = await initialize(client)
        headers = {**STREAM, "mcp-session-id": session}
        first = asyncio.ensure_future(client.get("/mcp", headers=headers))
        await asyncio.sleep(0)
        second = await client.get("/mcp", headers=headers)
        assert second.status == 409
        assert "one per session" in second.json()["error"]["message"]
        await client.delete("/mcp", headers={"mcp-session-id": session})
        await asyncio.wait_for(first, timeout=5)


async def test_a_closed_stream_can_be_reopened() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session = await initialize(client)
        headers = {**STREAM, "mcp-session-id": session}
        first = asyncio.ensure_future(client.get("/mcp", headers=headers))
        await asyncio.sleep(0)
        # Ending the *stream* rather than the session: the sentinel goes on the
        # queue directly, exactly as a client hanging up would.
        mcp._sessions.get(session).close_stream()
        await asyncio.wait_for(first, timeout=5)

        second = asyncio.ensure_future(client.get("/mcp", headers=headers))
        await asyncio.sleep(0)
        await client.delete("/mcp", headers={"mcp-session-id": session})
        assert (await asyncio.wait_for(second, timeout=5)).status == 200


async def test_an_idle_stream_says_something() -> None:
    app = Wreath()
    mcp = MCP(
        app,
        name="x",
        version="1.0.0",
        limits=MCPLimits(stream_keepalive_seconds=0.01),
    )

    @mcp.resource("camera://ridge", description="Anything.")
    async def ridge(request) -> str:
        return ""

    async with TestClient(app) as client:
        session = await initialize(client)
        stream = asyncio.ensure_future(
            client.get("/mcp", headers={**STREAM, "mcp-session-id": session})
        )
        await asyncio.sleep(0.05)
        await client.delete("/mcp", headers={"mcp-session-id": session})
        response = await asyncio.wait_for(stream, timeout=5)
        assert b": keep-alive" in response.body


async def test_a_notification_nobody_reads_is_dropped_and_counted() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", limits=MCPLimits(max_pending_notifications=2))

    @mcp.resource("camera://ridge", description="Anything.")
    async def ridge(request) -> str:
        return ""

    async with TestClient(app) as client:
        session = await initialize(client)
        await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "camera://ridge"},
            },
        )
        for _ in range(5):
            mcp.notify_resource_updated("camera://ridge")
        assert mcp.notifications_dropped == 3
        assert mcp.stats()["notifications_dropped"] == 3
        assert mcp._sessions.get(session).dropped == 3


async def test_a_tool_s_progress_reaches_the_client_that_asked_for_it() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", progress_interval=0.01)

    @mcp.tool(description="Takes a while, and says so.")
    async def slow_import(request) -> dict:
        reporter = request.state.mcp.progress
        reporter.update(50.0, "halfway")
        queue = mcp._sessions.get(request.state.mcp.session_id).notifications
        for _ in range(500):
            if any(b"halfway" in item for item in queue.snapshot()):
                break
            await asyncio.sleep(0.005)
        return {"imported": 1}

    async with TestClient(app) as client:
        session = await initialize(client)
        called = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "slow_import", "_meta": {"progressToken": "abc"}},
            },
        )
        assert called["result"]["structuredContent"] == {"imported": 1}

        stream = asyncio.ensure_future(
            client.get("/mcp", headers={**STREAM, "mcp-session-id": session})
        )
        await asyncio.sleep(0)
        await client.delete("/mcp", headers={"mcp-session-id": session})
        response = await asyncio.wait_for(stream, timeout=5)

        reports = [f for f in frames(response) if f["method"] == "notifications/progress"]
        assert reports, "the progress the tool reported never reached the stream"
        assert all(f["params"]["progressToken"] == "abc" for f in reports)
        assert reports[-1]["params"]["progress"] == 50.0
        assert reports[-1]["params"]["message"] == "halfway"
        assert reports[-1]["params"]["total"] == 100.0


async def test_progress_with_no_message_carries_no_message_key() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", progress_interval=0.01)

    @mcp.tool(description="Counts, quietly.")
    async def quiet_import(request) -> dict:
        reporter = request.state.mcp.progress
        reporter.update(50.0)
        queue = mcp._sessions.get(request.state.mcp.session_id).notifications
        for _ in range(500):
            if any(b"50.0" in item for item in queue.snapshot()):
                break
            await asyncio.sleep(0.005)
        return {"imported": 1}

    async with TestClient(app) as client:
        session = await initialize(client)
        await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "quiet_import", "_meta": {"progressToken": "abc"}},
            },
        )
        stream = asyncio.ensure_future(
            client.get("/mcp", headers={**STREAM, "mcp-session-id": session})
        )
        await asyncio.sleep(0)
        await client.delete("/mcp", headers={"mcp-session-id": session})
        response = await asyncio.wait_for(stream, timeout=5)

        reports = [f for f in frames(response) if f["method"] == "notifications/progress"]
        assert reports, "the progress the tool reported never reached the stream"
        assert all("message" not in f["params"] for f in reports)
        assert 50.0 in [f["params"]["progress"] for f in reports]


async def test_a_tool_may_report_progress_with_nobody_listening() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")
    seen: list[object] = []

    @mcp.tool(description="Reports whether it was asked to.")
    async def quiet(request) -> dict:
        context = request.state.mcp
        seen.append(context.progress_token)
        context.progress.update(10.0, "started")
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        await call(
            client,
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "quiet"}},
        )
        assert seen == [None]
        # Written to the registry regardless, which is what makes the same
        # reporter usable from a status route or a durable job.
        recorded = [mcp.progress.get(key) for key in (f"mcp:{session}:2",)]
        assert recorded[0] is not None
        assert recorded[0].percent == 10.0
        # Nothing was relayed, because nobody asked to be told.
        assert len(mcp._sessions.get(session).notifications) == 0


async def test_the_stream_belongs_to_the_session_that_opened_it() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        await initialize(client)
        response = await client.get("/mcp", headers={**STREAM, "mcp-session-id": "not-a-session"})
        assert response.status == 404
