"""What a `tools/call` leaves behind, and what it must not.

An MCP server's audit trail is the thing an operator needs six months later:
which model asked for what, on whose behalf, and what happened. That is a
structured record on the Flight Recorder's ring, and the interesting assertions
are about the *absence* of things -- a tool called with a `password` argument
records the call and not the password.

Nothing here is MCP-specific redaction. Argument values follow
`wreath.logging`'s deny-by-default rule (a scalar is written, a string is
fingerprinted) and argument names follow `wreath.crud.SENSITIVE_FIELD`, the same
regular expression that hides a password column from a generated CRUD endpoint.
A name it matches is recorded as present and never as a value -- not even as a
fingerprint, because a fingerprint of a password is an offline guessing oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from wreath import Wreath
from wreath import _flight_schema as fs
from wreath import logging as log
from wreath.binding import Body
from wreath.mcp import MCP, PROTOCOL_VERSION, ToolError
from wreath.testing import TestClient

PASSWORD = "correct-horse-battery-staple"


@dataclass
class Credentials:
    username: str
    password: str


def build() -> tuple[Wreath, MCP]:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.tool(description="Signs a caller in.")
    async def sign_in(request, username: str, password: str, attempts: int = 1) -> dict:
        return {"signed_in": username, "attempts": attempts}

    @mcp.tool(description="Signs a caller in, from a structured argument.")
    async def sign_in_bodily(request, account: Annotated[Credentials, Body()]) -> dict:
        return {"signed_in": account.username}

    @mcp.tool(description="Takes a whole argument whose *name* looks like a secret.")
    async def sign_in_wholesale(
        request, credentials: Annotated[Credentials, Body()]
    ) -> dict:
        return {"signed_in": credentials.username}

    @mcp.tool(description="Fails on purpose.")
    async def refuse(request) -> dict:
        raise ToolError("no")

    @mcp.tool(description="Fails by accident.")
    async def explode(request) -> dict:
        raise ZeroDivisionError

    return app, mcp


def markers(records: list) -> list[dict]:
    return [
        log.attributes(cell)
        for cell in records
        if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
    ]


def fields(records: list) -> dict:
    merged: dict = {}
    for cell in records:
        if cell.flags & fs.LOG_FLAG_EVENT_FIELDS:
            merged.update(log.attributes(cell))
    return merged


async def drive(tool: str, arguments: dict) -> tuple[list[dict], dict]:
    """Call one tool with the recorder running, returning markers and fields."""
    app, _ = build()
    with log.testing_runtime() as records, log.request_scope(request_id=7) as scope:
        async with TestClient(app) as client:
            opened = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION},
                },
            )
            session = dict(opened.headers)[b"mcp-session-id"].decode()
            await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
                headers={"mcp-session-id": session},
            )
        scope.finish(promoted=True)
        # Read the names back while the runtime that interned them is still
        # installed; `log.attributes` resolves through it.
        return markers(records), fields(records)


async def test_a_call_with_a_password_records_the_call_and_not_the_password() -> None:
    seen, attached = await drive(
        "sign_in", {"username": "ada", "password": PASSWORD, "attempts": 2}
    )

    assert len(seen) == 1
    marker = seen[0]
    assert marker["tool"] == "sign_in"
    assert marker["outcome"] == "ok"
    assert marker["duration_ms"] >= 0.0
    assert marker["principal"] == "anonymous"

    # The call is on the record, argument by argument...
    assert attached["mcp.arg.attempts"] == 2
    assert attached["mcp.arg.username"].startswith("#")
    # ...and the password is on it as a name only. Not the value, and not a
    # fingerprint of the value: a fingerprint of a password is guessable
    # offline, which is the whole reason the name rule exists on top of the
    # deny-by-default value rule.
    assert attached["mcp.arg.password"] == "<redacted>"
    assert PASSWORD not in repr(attached)
    assert PASSWORD not in repr(seen)


async def test_a_password_nested_in_a_structured_argument_is_redacted_too() -> None:
    _, attached = await drive(
        "sign_in_bodily",
        {"account": {"username": "ada", "password": PASSWORD}},
    )
    assert attached["mcp.arg.account.password"] == "<redacted>"
    assert attached["mcp.arg.account.username"].startswith("#")
    assert PASSWORD not in repr(attached)


async def test_a_whole_argument_whose_name_looks_like_a_secret_is_withheld() -> None:
    """The name rule applies to the container, not only to its leaves."""
    _, attached = await drive(
        "sign_in_wholesale",
        {"credentials": {"username": "ada", "password": PASSWORD}},
    )
    assert attached == {"mcp.arg.credentials": "<redacted>"}


async def test_the_arguments_of_a_refused_call_are_recorded_too() -> None:
    """A schema rejection is exactly the call an audit wants the arguments of."""
    seen, attached = await drive(
        "sign_in", {"username": "ada", "password": PASSWORD, "invented": "nonsense"}
    )
    assert seen[0]["outcome"] == "schema_rejected"
    assert attached["mcp.arg.password"] == "<redacted>"
    assert attached["mcp.arg.invented"].startswith("#")
    assert PASSWORD not in repr(attached)


async def test_each_outcome_is_named_on_the_marker() -> None:
    for tool, outcome in (("refuse", "tool_error"), ("explode", "raised")):
        seen, _ = await drive(tool, {})
        assert seen[0]["tool"] == tool
        assert seen[0]["outcome"] == outcome


async def test_the_verified_caller_is_on_the_marker() -> None:
    from tests.test_mcp_auth import protection, token

    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", auth=protection())

    @mcp.tool(description="Anything.")
    async def noop(request) -> dict:
        return {}

    with log.testing_runtime() as records, log.request_scope(request_id=7):
        async with TestClient(app) as client:
            bearer = token(subject="ada")
            opened = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION},
                },
                headers={"authorization": f"Bearer {bearer}"},
            )
            session = dict(opened.headers)[b"mcp-session-id"].decode()
            await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "noop"},
                },
                headers={
                    "mcp-session-id": session,
                    "authorization": f"Bearer {bearer}",
                },
            )
        marker = markers(records)[0]
        assert marker["principal"] == "ada"
        # The session id is a bearer credential for that session's in-flight
        # calls, so it correlates and never identifies.
        assert marker["session"].startswith("#")
        assert session not in repr(marker)
        assert mcp.tool_calls == 1


async def test_stats_reports_every_counter_by_name() -> None:
    """The mapping and canonical metrics reading cannot drift apart."""
    app, mcp = build()
    assert mcp.stats() == {
        "tool_calls": 0,
        "tool_errors": 0,
        "schema_rejections": 0,
        "unauthorized_calls": 0,
        "throttled": 0,
        "expired_sessions": 0,
        "resource_reads": 0,
        "resource_errors": 0,
        "prompt_renders": 0,
        "prompt_errors": 0,
        "notifications_dropped": 0,
        "sampling_requests": 0,
        "sampling_refusals": 0,
        "elicitations": 0,
        "elicitation_declines": 0,
        "elicitation_refusals": 0,
        "client_request_timeouts": 0,
        "roots_refusals": 0,
    }
    async with TestClient(app) as client:
        opened = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            },
        )
        session = dict(opened.headers)[b"mcp-session-id"].decode()
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "explode"},
            },
            headers={"mcp-session-id": session},
        )
    counters = mcp.stats()
    assert counters["tool_calls"] == 1
    assert counters["tool_errors"] == 1
    # Every attribute the dict names is the attribute itself, so the two cannot
    # answer differently once one of them is read by an exporter.
    assert counters == {name: getattr(mcp, name) for name in counters}
    assert mcp.counters().values == counters


async def test_a_tool_called_with_no_recorder_running_still_works() -> None:
    """The marker is best-effort observability, never a precondition."""
    app, mcp = build()
    async with TestClient(app) as client:
        opened = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            },
        )
        session = dict(opened.headers)[b"mcp-session-id"].decode()
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "sign_in",
                    "arguments": {"username": "ada", "password": PASSWORD},
                },
            },
            headers={"mcp-session-id": session},
        )
        assert response.json()["result"]["isError"] is False
        assert mcp.tool_calls == 1
