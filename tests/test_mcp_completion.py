from __future__ import annotations

import enum
from typing import Literal

from wreath import Wreath
from wreath._mcp.completion import MAX_VALUES, _answer
from wreath.mcp import MCP, PROTOCOL_VERSION
from wreath.testing import TestClient, TestResponse


class Season(enum.Enum):
    SPRING = "spring"
    AUTUMN = "autumn"


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def initialize(client: TestClient) -> tuple[str, dict]:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
    )
    return header(response, "mcp-session-id") or "", response.json()["result"]


def build() -> tuple[Wreath, MCP]:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.prompt(description="Draft a report on one trail in one season.")
    async def trail_report(
        request,
        trail: Literal["ridge", "creek", "ridgeway"],
        season: Season,
        note: str = "",
    ) -> str:
        return f"Report on {trail} in {season}."

    return app, mcp


async def call(client: TestClient, session: str, payload: dict) -> dict:
    response = await client.post("/mcp", json=payload, headers={"mcp-session-id": session})
    return response.json()


def completion(session_payload: dict) -> dict:
    return session_payload["result"]["completion"]


def test_completion_answers_stop_at_the_protocol_ceiling() -> None:
    values = tuple(f"value-{index}" for index in range(MAX_VALUES + 1))
    assert _answer(values)["completion"] == {
        "values": list(values[:MAX_VALUES]),
        "total": MAX_VALUES + 1,
        "hasMore": True,
    }


async def test_the_values_are_the_ones_the_annotation_declared() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        answered = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "completion/complete",
                "params": {
                    "ref": {"type": "ref/prompt", "name": "trail_report"},
                    "argument": {"name": "trail", "value": ""},
                },
            },
        )
        assert completion(answered) == {
            "values": ["ridge", "creek", "ridgeway"],
            "total": 3,
            "hasMore": False,
        }


async def test_an_enum_argument_completes_too() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        answered = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "completion/complete",
                "params": {
                    "ref": {"type": "ref/prompt", "name": "trail_report"},
                    "argument": {"name": "season", "value": "a"},
                },
            },
        )
        assert completion(answered)["values"] == ["autumn"]


async def test_what_has_been_typed_narrows_it() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        answered = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "completion/complete",
                "params": {
                    "ref": {"type": "ref/prompt", "name": "trail_report"},
                    "argument": {"name": "trail", "value": "ridg"},
                },
            },
        )
        assert completion(answered)["values"] == ["ridge", "ridgeway"]


async def test_an_argument_that_declared_nothing_completes_to_nothing() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        answered = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "completion/complete",
                "params": {
                    "ref": {"type": "ref/prompt", "name": "trail_report"},
                    "argument": {"name": "note", "value": ""},
                },
            },
        )
        assert completion(answered) == {"values": [], "total": 0, "hasMore": False}


async def test_a_resource_reference_completes_to_nothing_because_there_are_no_templates() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        answered = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "completion/complete",
                "params": {
                    "ref": {"type": "ref/resource", "uri": "camera://{name}"},
                    "argument": {"name": "name", "value": ""},
                },
            },
        )
        assert completion(answered)["values"] == []


async def test_an_unknown_prompt_is_named() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        answered = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "completion/complete",
                "params": {
                    "ref": {"type": "ref/prompt", "name": "nope"},
                    "argument": {"name": "trail", "value": ""},
                },
            },
        )
        assert answered["error"]["code"] == -32602
        assert "unknown prompt 'nope'" in answered["error"]["message"]


async def test_the_capability_is_advertised_with_the_prompts() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        _, result = await initialize(client)
        assert result["capabilities"]["completions"] == {}

    bare = Wreath()
    MCP(bare, name="x", version="1.0.0")
    async with TestClient(bare) as client:
        _, result = await initialize(client)
        assert "completions" not in result["capabilities"]


async def test_a_declared_enum_is_still_a_string_argument() -> None:
    _, mcp = build()
    (prompt,) = mcp.prompts
    assert [argument["name"] for argument in prompt.arguments] == [
        "trail",
        "season",
        "note",
    ]
    assert prompt.completions["trail"] == ("ridge", "creek", "ridgeway")
    assert "note" not in prompt.completions
