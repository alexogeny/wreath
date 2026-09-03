from __future__ import annotations

import asyncio

import pytest

from wreath import Wreath
from wreath.mcp import MCP, PROTOCOL_VERSION, MCPLimits
from wreath.testing import TestClient, TestResponse


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def initialize(client: TestClient, **kwargs) -> tuple[str, TestResponse]:
    """Open a session, returning its id (empty when the server refused) and the reply."""
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
        **kwargs,
    )
    return header(response, "mcp-session-id") or "", response


async def request_with_repeated_mcp_header(
    client: TestClient,
    method: str,
    repeated: tuple[bytes, bytes, bytes],
    *,
    session: str | None = None,
) -> TestResponse:
    payload = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    headers = {"accept": "text/event-stream"} if method == "GET" else {}
    if session is not None:
        headers["mcp-session-id"] = session
    scope, body = client._scope(
        method,
        "/mcp",
        headers=headers,
        json=payload if method == "POST" else None,
    )
    name, first, second = repeated
    scope["headers"].extend(((name, first), (name, second)))
    sent: list[dict] = []

    async def receive() -> dict:
        nonlocal body
        chunk, body = body, b""
        return {"type": "http.request", "body": chunk, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await client.app(scope, receive, send)
    first_message = sent[0]
    if first_message["type"] == "wreath.response":
        return TestResponse(
            first_message["status"],
            list(first_message["headers"]),
            first_message.get("body", b""),
        )
    response_body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return TestResponse(first_message["status"], list(first_message["headers"]), response_body)


@pytest.mark.parametrize(
    ("method", "control"),
    (
        ("POST", "session"),
        ("GET", "session"),
        ("DELETE", "session"),
        ("POST", "version"),
        ("GET", "version"),
        ("DELETE", "version"),
    ),
)
async def test_repeated_mcp_control_headers_are_refused(method: str, control: str) -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        if control == "session":
            repeated = (
                b"mcp-session-id",
                session.encode("ascii") if method != "GET" else b"unknown",
                b"unknown" if method != "GET" else session.encode("ascii"),
            )
            single_session = None
        else:
            repeated = (
                b"mcp-protocol-version",
                PROTOCOL_VERSION.encode("ascii"),
                b"1999-01-01",
            )
            single_session = session if method == "POST" else "unknown"

        response = await request_with_repeated_mcp_header(
            client,
            method,
            repeated,
            session=single_session,
        )

        assert response.status == 400
        assert "more than once" in response.json()["error"]["message"]


async def test_repeated_content_type_is_refused_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    authenticated = False

    async def authenticate(self: MCP, request: object) -> None:
        nonlocal authenticated
        authenticated = True

    monkeypatch.setattr(MCP, "_authenticate", authenticate)
    async with TestClient(app) as client:
        scope, body = client._scope(
            "POST",
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
        scope["headers"] = [header for header in scope["headers"] if header[0] != b"content-type"]
        scope["headers"].extend(
            ((b"content-type", b"application/json"), (b"content-type", b"text/plain"))
        )
        sent: list[dict] = []

        async def receive() -> dict:
            nonlocal body
            chunk, body = body, b""
            return {"type": "http.request", "body": chunk, "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        await client.app(scope, receive, send)

    assert authenticated is False
    assert sent[0]["status"] == 400
    assert (
        "Content-Type"
        in b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        ).decode()
    )


async def test_a_message_without_a_session_is_refused_with_a_reason() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert response.status == 400
        assert "Mcp-Session-Id" in response.json()["error"]["message"]


async def test_an_unknown_session_is_a_404_so_the_client_re_initializes() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"mcp-session-id": "not-a-session"},
        )
        assert response.status == 404
        assert "initialize" in response.json()["error"]["message"]


async def test_delete_ends_the_session() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        ended = await client.delete("/mcp", headers={"mcp-session-id": session_id})
        assert ended.status == 204
        again = await client.delete("/mcp", headers={"mcp-session-id": session_id})
        assert again.status == 404
        after = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
            headers={"mcp-session-id": session_id},
        )
        assert after.status == 404


async def test_delete_without_a_session_header_is_refused() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        response = await client.delete("/mcp")
        assert response.status == 400


async def test_an_unimplemented_protocol_version_header_is_named() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "ping"},
            headers={"mcp-session-id": session_id, "mcp-protocol-version": "1999-01-01"},
        )
        assert response.status == 400
        message = response.json()["error"]["message"]
        assert "1999-01-01" in message
        assert PROTOCOL_VERSION in message


async def test_the_negotiated_protocol_version_header_is_accepted() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "ping"},
            headers={"mcp-session-id": session_id, "mcp-protocol-version": PROTOCOL_VERSION},
        )
        assert response.status == 200


async def test_a_client_that_only_takes_sse_gets_the_reply_as_one_event() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        session_id, initialized = await initialize(client, headers={"accept": "text/event-stream"})
        assert header(initialized, "content-type") == "text/event-stream"
        assert initialized.body.startswith(b"event: message\ndata: {")
        assert initialized.body.endswith(b"\n\n")
        assert session_id

        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 5, "method": "ping"},
            headers={"mcp-session-id": session_id, "accept": "text/event-stream"},
        )
        assert response.body == (b'event: message\ndata: {"jsonrpc":"2.0","id":5,"result":{}}\n\n')


async def test_a_client_that_takes_both_gets_json() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        session_id, initialized = await initialize(
            client, headers={"accept": "application/json, text/event-stream"}
        )
        assert header(initialized, "content-type") == "application/json"
        assert session_id


async def test_the_notification_stream_needs_a_session_and_an_accept_header() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        plain = await client.get("/mcp")
        assert plain.status == 406
        assert "text/event-stream" in plain.json()["error"]["message"]

        sessionless = await client.get("/mcp", headers={"accept": "text/event-stream"})
        assert sessionless.status == 400
        assert "Mcp-Session-Id" in sessionless.json()["error"]["message"]


async def test_a_cancelled_call_stops_the_tool_and_sends_no_reply() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")
    started = asyncio.Event()
    released = False

    @mcp.tool(description="Waits until something stops it.")
    async def wait_forever(request) -> dict:
        nonlocal released
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released = True
        return {}

    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        call = asyncio.ensure_future(
            client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "wait_forever"},
                },
                headers={"mcp-session-id": session_id},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        cancelled = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 6, "reason": "the user changed their mind"},
            },
            headers={"mcp-session-id": session_id},
        )
        assert cancelled.status == 202
        response = await asyncio.wait_for(call, timeout=5)
        # The specification forbids sending a response to a cancelled request,
        # so there is nothing to encode and the POST carries no body.
        assert response.status == 202
        assert response.body == b""
        assert released is True


async def test_cancelling_an_unknown_request_id_is_a_no_op() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0")
    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 999},
            },
            headers={"mcp-session-id": session_id},
        )
        assert response.status == 202


async def test_a_cancelled_call_joins_async_tool_cleanup() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")
    started = asyncio.Event()
    cleaned = asyncio.Event()

    @mcp.tool(description="Owns asynchronous cleanup.")
    async def wait_forever(request) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
        return {}

    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        call = asyncio.create_task(
            client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 60,
                    "method": "tools/call",
                    "params": {"name": "wait_forever"},
                },
                headers={"mcp-session-id": session_id},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 60},
            },
            headers={"mcp-session-id": session_id},
        )
        await asyncio.wait_for(call, timeout=5)
        assert cleaned.is_set()


async def test_a_completed_call_joins_its_cancelled_progress_watcher(monkeypatch) -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")
    watcher_started = asyncio.Event()
    watcher_cleaned = asyncio.Event()

    async def relay_progress(self, session, task_id, token) -> None:
        watcher_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            watcher_cleaned.set()

    monkeypatch.setattr(MCP, "_relay_progress", relay_progress)

    @mcp.tool(description="Finishes after its progress watcher starts.")
    async def finish(request) -> dict:
        await watcher_started.wait()
        return {}

    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 61,
                "method": "tools/call",
                "params": {"name": "finish", "_meta": {"progressToken": "p"}},
            },
            headers={"mcp-session-id": session_id},
        )

        assert response.status == 200
        assert watcher_cleaned.is_set()


async def test_ending_a_session_cancels_what_it_still_has_in_flight() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")
    started = asyncio.Event()

    @mcp.tool(description="Waits until something stops it.")
    async def wait_forever(request) -> dict:
        started.set()
        await asyncio.Event().wait()
        return {}

    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        call = asyncio.ensure_future(
            client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "wait_forever"},
                },
                headers={"mcp-session-id": session_id},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        ended = await client.delete("/mcp", headers={"mcp-session-id": session_id})
        assert ended.status == 204
        response = await asyncio.wait_for(call, timeout=5)
        assert response.status == 202


async def test_the_tool_sees_its_call_context_on_the_request() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")
    seen: dict = {}

    @mcp.tool(description="Reports what it was told about the call.")
    async def introspect(request) -> dict:
        context = request.state.mcp
        seen["session"] = context.session_id
        seen["request_id"] = context.request_id
        seen["tool"] = context.tool
        seen["progress_token"] = context.progress_token
        return {}

    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "introspect", "_meta": {"progressToken": "abc"}},
            },
            headers={"mcp-session-id": session_id},
        )
        assert seen == {
            "session": session_id,
            "request_id": 8,
            "tool": "introspect",
            "progress_token": "abc",
        }


async def test_the_session_store_refuses_past_its_ceiling() -> None:
    app = Wreath()
    MCP(app, name="x", version="1.0.0", limits=MCPLimits(max_sessions=1))
    async with TestClient(app) as client:
        first, _ = await initialize(client)
        assert first
        _, response = await initialize(client)
        assert response.status == 503
        assert "ceiling" in response.json()["error"]["message"]


async def test_an_idle_session_expires_and_frees_its_slot() -> None:
    app = Wreath()
    mcp = MCP(
        app,
        name="x",
        version="1.0.0",
        limits=MCPLimits(max_sessions=1, session_idle_seconds=0.05),
    )
    async with TestClient(app) as client:
        first, _ = await initialize(client)
        # Advance this session's idle age directly. Wall-clock sleeping adds no
        # coverage when the store already records the clock value explicitly.
        session = mcp._sessions._sessions.held(first)
        assert session is not None
        mcp._sessions.get(first, now=session.last_seen - 0.08)
        # The abandoned session is gone, so the ceiling it was holding is free:
        # a client that never sends DELETE cannot wedge the endpoint shut.
        second, response = await initialize(client)
        assert response.status == 200
        assert second and second != first
        assert mcp.expired_sessions == 1

        stale = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
            headers={"mcp-session-id": first},
        )
        assert stale.status == 404
        assert "idle-expired" in stale.json()["error"]["message"]


async def test_traffic_keeps_a_session_alive() -> None:
    app = Wreath()
    mcp = MCP(
        app,
        name="x",
        version="1.0.0",
        limits=MCPLimits(session_idle_seconds=0.15),
    )
    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        session = mcp._sessions._sessions.held(session_id)
        assert session is not None
        for _ in range(4):
            # Without each request touching `last_seen`, the accumulated 240ms
            # crosses the 150ms idle bound on the third pass.
            session.last_seen -= 0.06
            alive = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 10, "method": "ping"},
                headers={"mcp-session-id": session_id},
            )
            # Idle means "no traffic", not "old": a long conversation that keeps
            # talking must never be collected out from under itself.
            assert alive.status == 200


async def test_a_session_is_bounded_in_concurrent_calls() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", limits=MCPLimits(max_concurrent_calls=1))
    started = asyncio.Event()

    @mcp.tool(description="Waits until something stops it.")
    async def wait_forever(request) -> dict:
        started.set()
        await asyncio.Event().wait()
        return {}

    async with TestClient(app) as client:
        session_id, _ = await initialize(client)
        first = asyncio.ensure_future(
            client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "wait_forever"},
                },
                headers={"mcp-session-id": session_id},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        try:
            second = await asyncio.wait_for(
                client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "tools/call",
                        "params": {"name": "wait_forever"},
                    },
                    headers={"mcp-session-id": session_id},
                ),
                timeout=1,
            )
            error = second.json()["error"]
            assert error["code"] == -32004
            assert "in flight" in error["message"]
            assert mcp.throttled == 1
        finally:
            await client.delete("/mcp", headers={"mcp-session-id": session_id})
            await asyncio.wait_for(first, timeout=5)


async def test_a_server_refuses_more_tools_than_its_ceiling() -> None:
    mcp = MCP(name="x", version="1.0.0", limits=MCPLimits(max_tools=1))

    @mcp.tool(description="The only one.")
    async def first(request) -> dict:
        return {}

    with pytest.raises(ValueError, match="max_tools"):

        @mcp.tool(description="One too many.")
        async def second(request) -> dict:
            return {}


def test_a_limit_that_is_not_a_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="max_sessions"):
        MCPLimits(max_sessions=0)
    with pytest.raises(ValueError, match="session_idle_seconds"):
        MCPLimits(session_idle_seconds=0)
    with pytest.raises(ValueError, match="stream_keepalive_seconds"):
        MCPLimits(stream_keepalive_seconds=0)
    with pytest.raises(ValueError, match="client_request_seconds"):
        MCPLimits(client_request_seconds=0)
    assert MCPLimits(session_idle_seconds=None).session_idle_seconds is None


@pytest.mark.parametrize(
    "field",
    ["session_idle_seconds", "stream_keepalive_seconds", "client_request_seconds"],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_mcp_lifecycle_timeouts_must_be_finite(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        MCPLimits(**{field: value})
