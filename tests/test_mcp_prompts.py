"""Prompts: text a person chooses, with arguments a form can fill.

The distinction that matters is who does the choosing. A tool is picked by the
model and gets a JSON Schema; a prompt is picked by a person and gets a flat map
of strings. The load-bearing assertion here is the registration refusal: a
non-string parameter is a declaration a compliant client cannot satisfy, and
catching it at declaration is the difference between the author seeing it and
whoever clicked the menu entry seeing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer
from wreath.binding import Body
from wreath.mcp import MCP, PROTOCOL_VERSION, MCPLimits, ToolSignatureError
from wreath.testing import TestClient, TestResponse


@dataclass
class Query:
    species: str


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def initialize(client: TestClient) -> tuple[str, TestResponse]:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
    )
    return header(response, "mcp-session-id") or "", response


async def call(client: TestClient, session: str, payload: dict) -> dict:
    response = await client.post("/mcp", json=payload, headers={"mcp-session-id": session})
    return response.json()


def build() -> tuple[Wreath, MCP]:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.prompt(description="Draft a report on a species' recent sightings.")
    async def sighting_report(request, species: str, tone: str = "neutral") -> str:
        return f"Summarise this month's {species} sightings in a {tone} tone."

    @mcp.prompt(description="Open a conversation about the whole reserve.")
    async def reserve_briefing(request) -> list[dict]:
        return [
            {"role": "user", "content": "How is the reserve doing?"},
            {"role": "assistant", "content": {"type": "text", "text": "Let me look."}},
        ]

    return app, mcp


async def test_prompts_list_renders_arguments_and_which_are_required() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(client, session, {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
        names = [entry["name"] for entry in body["result"]["prompts"]]
        assert names == ["reserve_briefing", "sighting_report"]
        report = next(e for e in body["result"]["prompts"] if e["name"] == "sighting_report")
        assert report["description"] == "Draft a report on a species' recent sightings."
        assert report["arguments"] == [
            {"name": "species", "required": True},
            {"name": "tone", "required": False},
        ]
        # A prompt with no parameters carries no `arguments` key at all rather
        # than an empty list a client has to special-case.
        briefing = next(e for e in body["result"]["prompts"] if e["name"] == "reserve_briefing")
        assert "arguments" not in briefing


async def test_a_string_result_becomes_one_user_message() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompts/get",
                "params": {"name": "sighting_report", "arguments": {"species": "fox"}},
            },
        )
        result = body["result"]
        assert result["description"] == "Draft a report on a species' recent sightings."
        assert result["messages"] == [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": "Summarise this month's fox sightings in a neutral tone.",
                },
            }
        ]
        assert mcp.prompt_renders == 1
        assert mcp.prompt_errors == 0


async def test_a_sequence_of_messages_passes_through_with_text_promoted() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompts/get",
                "params": {"name": "reserve_briefing"},
            },
        )
        messages = body["result"]["messages"]
        assert messages[0] == {
            "role": "user",
            "content": {"type": "text", "text": "How is the reserve doing?"},
        }
        assert messages[1]["role"] == "assistant"


async def test_a_missing_argument_is_named_and_never_reaches_the_handler() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompts/get",
                "params": {"name": "sighting_report", "arguments": {"tone": "wry"}},
            },
        )
        assert body["error"]["code"] == -32602
        assert [e["loc"] for e in body["error"]["data"]["errors"]] == [
            ["arguments", "species"]
        ]
        assert mcp.schema_rejections == 1
        assert mcp.prompt_renders == 0


async def test_an_argument_the_prompt_never_declared_is_named() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompts/get",
                "params": {
                    "name": "sighting_report",
                    "arguments": {"species": "fox", "colour": "red"},
                },
            },
        )
        assert [e["loc"] for e in body["error"]["data"]["errors"]] == [
            ["arguments", "colour"]
        ]


async def test_an_unknown_prompt_is_invalid_params() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "nope"}},
        )
        assert body["error"]["code"] == -32602
        assert "nope" in body["error"]["message"]


async def test_a_handler_that_raises_reports_its_type_and_is_counted() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")

    @mcp.prompt(description="Fails in a way nobody planned.")
    async def broken(request) -> str:
        raise ZeroDivisionError("secret operational detail")

    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "broken"}},
        )
        assert body["error"]["code"] == -32603
        assert "ZeroDivisionError" in body["error"]["message"]
        assert "secret operational detail" not in body["error"]["message"]
        assert mcp.prompt_errors == 1


async def test_a_prompt_that_returns_the_wrong_shape_is_a_counted_failure() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")

    @mcp.prompt(description="Returns something that is not a message.")
    async def wrong(request) -> int:
        return 7

    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "wrong"}},
        )
        assert body["error"]["code"] == -32603
        assert mcp.prompt_errors == 1


async def test_a_prompt_can_be_gated_on_its_own_name() -> None:
    class Engine:
        def is_authorized(self, **request: object) -> bool:
            return False

    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda _t: Identity("ada")),
        CedarAuthorizer(
            engine=Engine(),
            principal=lambda identity: identity.id,
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
            context=lambda request: {},
        ),
    )
    mcp = MCP(app, name="x", version="1.0.0")

    @mcp.prompt(description="Only for staff.", action="Prompt::render")
    async def internal_briefing(request) -> str:
        return "should never render"

    async with TestClient(app, headers={"authorization": "Bearer t"}) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompts/get",
                "params": {"name": "internal_briefing"},
            },
        )
        assert body["error"]["code"] == -32001
        assert mcp.unauthorized_calls == 1
        assert mcp.prompt_renders == 0
        assert mcp.declared_actions() == {"Prompt": ("Prompt::render",)}


async def test_the_capability_is_advertised_only_when_a_prompt_is_declared() -> None:
    bare = Wreath()
    MCP(bare, name="x", version="1.0.0")
    async with TestClient(bare) as client:
        _, opened = await initialize(client)
        assert "prompts" not in opened.json()["result"]["capabilities"]

    app, _ = build()
    async with TestClient(app) as client:
        _, opened = await initialize(client)
        assert opened.json()["result"]["capabilities"]["prompts"] == {"listChanged": False}


def test_a_non_string_argument_is_refused_at_registration() -> None:
    """The whole reason prompts are checked separately from tools."""
    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(ToolSignatureError) as caught:

        @mcp.prompt(description="Counts something.")
        async def counted(request, limit: int) -> str:
            return ""

    assert "limit" in str(caught.value)
    assert "map of strings" in str(caught.value)


def test_an_optional_string_argument_is_allowed() -> None:
    mcp = MCP(name="x", version="1.0.0")

    @mcp.prompt(description="Takes an optional note.")
    async def noted(request, note: str | None = None) -> str:
        return note or ""

    assert mcp.prompts[0].arguments == ({"name": "note", "required": False},)


def test_a_structured_body_argument_is_refused() -> None:
    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(ToolSignatureError, match="flat map of strings"):

        @mcp.prompt(description="Takes a whole object.")
        async def structured(request, query: Annotated[Query, Body()]) -> str:
            return ""


def test_a_prompt_without_a_description_is_refused() -> None:
    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(ValueError, match="needs a description"):

        @mcp.prompt
        async def undescribed(request) -> str:
            return ""


def test_a_server_refuses_more_prompts_than_its_ceiling() -> None:
    mcp = MCP(name="x", version="1.0.0", limits=MCPLimits(max_prompts=1))

    @mcp.prompt(description="The only one.")
    async def first(request) -> str:
        return ""

    with pytest.raises(ValueError, match="max_prompts"):

        @mcp.prompt(description="One too many.")
        async def second(request) -> str:
            return ""
