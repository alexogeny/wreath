from __future__ import annotations

import base64

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer
from wreath.mcp import MCP, PROTOCOL_VERSION, MCPLimits, ToolError
from wreath.testing import TestClient, TestResponse


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

    @mcp.resource(
        "camera://ridge/notes",
        description="Field notes for the ridge camera.",
        mime_type="text/markdown",
    )
    async def ridge_notes(request) -> str:
        return "# Ridge\nQuiet all week."

    @mcp.resource("camera://ridge/latest", description="The latest ridge frame.")
    async def ridge_latest(request) -> bytes:
        return b"\x89PNG\r\n"

    @mcp.resource("camera://index", description="Every camera, as JSON.")
    async def index(request) -> dict:
        return {"cameras": ["ridge", "creek"]}

    return app, mcp


async def test_resources_list_renders_every_declared_resource() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(client, session, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        uris = [entry["uri"] for entry in body["result"]["resources"]]
        assert uris == ["camera://index", "camera://ridge/latest", "camera://ridge/notes"]
        notes = next(e for e in body["result"]["resources"] if e["uri"].endswith("notes"))
        assert notes["description"] == "Field notes for the ridge camera."
        assert notes["mimeType"] == "text/markdown"


async def test_the_listing_is_serialized_once_and_invalidated_by_a_late_resource() -> None:
    _, mcp = build()
    first = mcp._resources.listing()
    assert mcp._resources.listing() is first

    @mcp.resource("camera://creek/notes", description="Declared after the listing.")
    async def creek_notes(request) -> str:
        return ""

    assert b"creek" in mcp._resources.listing()


async def test_text_bytes_and_json_are_the_three_shapes_a_read_can_take() -> None:
    app, mcp = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)

        text = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "camera://ridge/notes"},
            },
        )
        content = text["result"]["contents"][0]
        assert content["text"].startswith("# Ridge")
        assert content["mimeType"] == "text/markdown"
        assert content["uri"] == "camera://ridge/notes"

        binary = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": "camera://ridge/latest"},
            },
        )
        blob = binary["result"]["contents"][0]
        # A client chooses between `text` and `blob` by which key is present,
        # not by the media type, so bytes must never arrive as text.
        assert "text" not in blob
        assert base64.b64decode(blob["blob"]) == b"\x89PNG\r\n"
        assert blob["mimeType"] == "application/octet-stream"

        structured = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": "camera://index"},
            },
        )
        rendered = structured["result"]["contents"][0]
        assert rendered["mimeType"] == "application/json"
        assert "creek" in rendered["text"]
        assert mcp.resource_reads == 3
        assert mcp.resource_errors == 0


async def test_an_unknown_uri_is_the_specification_s_own_code() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "camera://nowhere"},
            },
        )
        # -32002 is the code the resources chapter names, which is why Wreath's
        # own rate-limit and concurrency codes sit below it rather than on it.
        assert body["error"]["code"] == -32002
        assert "camera://nowhere" in body["error"]["message"]


async def test_a_reader_that_says_no_is_not_a_reader_that_broke() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")

    @mcp.resource("camera://gone", description="A camera that was retired.")
    async def gone(request) -> str:
        raise ToolError("that camera was retired last spring")

    @mcp.resource("camera://broken", description="A camera whose reader is broken.")
    async def broken(request) -> str:
        raise ZeroDivisionError("secret operational detail")

    async with TestClient(app) as client:
        session, _ = await initialize(client)
        refused = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "camera://gone"},
            },
        )
        assert refused["error"]["code"] == -32002
        assert "retired last spring" in refused["error"]["message"]

        failed = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": "camera://broken"},
            },
        )
        assert failed["error"]["code"] == -32603
        assert "ZeroDivisionError" in failed["error"]["message"]
        # The message was written for an operator, and whoever is driving the
        # model is not that.
        assert "secret operational detail" not in failed["error"]["message"]
        assert mcp.resource_errors == 2


async def test_a_resource_is_gated_on_its_own_uri() -> None:
    seen: list[object] = []

    class Recording:
        def is_authorized(self, **request: object) -> bool:
            seen.append((request["action"], request["resource"]))
            return request["resource"] != "camera://private"

    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda _t: Identity("ada")),
        CedarAuthorizer(
            engine=Recording(),
            principal=lambda identity: identity.id,
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
            context=lambda request: {},
        ),
    )
    mcp = MCP(app, name="x", version="1.0.0")

    @mcp.resource("camera://public", description="Public.", action="Camera::read")
    async def public(request) -> str:
        return "visible"

    @mcp.resource("camera://private", description="Private.", action="Camera::read")
    async def private(request) -> str:
        return "should never be read"

    async with TestClient(app, headers={"authorization": "Bearer t"}) as client:
        session, _ = await initialize(client)
        allowed = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "camera://public"},
            },
        )
        assert allowed["result"]["contents"][0]["text"] == "visible"

        denied = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": "camera://private"},
            },
        )
        assert denied["error"]["code"] == -32001
        assert seen == [
            ("Camera::read", "camera://public"),
            ("Camera::read", "camera://private"),
        ]
        # A refusal is not a failure, and never counts as one.
        assert mcp.unauthorized_calls == 1
        assert mcp.resource_errors == 0
        assert mcp.declared_actions() == {"Camera": ("Camera::read",)}


async def test_subscribing_to_an_unknown_resource_is_refused() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "camera://nowhere"},
            },
        )
        assert body["error"]["code"] == -32002


async def test_a_session_is_bounded_in_subscriptions() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", limits=MCPLimits(max_subscriptions=1))

    for name in ("one", "two"):

        @mcp.resource(f"camera://{name}", name=name, description=f"Camera {name}.")
        async def reader(request) -> str:
            return ""

    async with TestClient(app) as client:
        session, _ = await initialize(client)
        first = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "camera://one"},
            },
        )
        assert first["result"] == {}
        # Re-subscribing to something already held is not a second subscription.
        again = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/subscribe",
                "params": {"uri": "camera://one"},
            },
        )
        assert again["result"] == {}
        second = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/subscribe",
                "params": {"uri": "camera://two"},
            },
        )
        assert second["error"]["code"] == -32004
        assert "max_subscriptions" in second["error"]["message"]


async def test_the_capability_is_advertised_only_when_something_is_declared() -> None:
    bare = Wreath()
    MCP(bare, name="x", version="1.0.0")
    async with TestClient(bare) as client:
        _, opened = await initialize(client)
        assert "resources" not in opened.json()["result"]["capabilities"]

    app, _ = build()
    async with TestClient(app) as client:
        _, opened = await initialize(client)
        capabilities = opened.json()["result"]["capabilities"]
        assert capabilities["resources"] == {"subscribe": True, "listChanged": False}


async def test_resource_templates_are_listed_as_none_rather_than_refused() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session, _ = await initialize(client)
        body = await call(
            client, session, {"jsonrpc": "2.0", "id": 2, "method": "resources/templates/list"}
        )
        assert body["result"] == {"resourceTemplates": []}


def test_a_resource_that_takes_arguments_is_refused_at_registration() -> None:
    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(TypeError, match="only the request"):

        @mcp.resource("camera://one", description="Varies by input.")
        async def varying(request, camera: str) -> str:
            return camera


def test_a_resource_without_a_description_is_refused() -> None:
    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(ValueError, match="needs a description"):

        @mcp.resource("camera://one")
        async def undescribed(request) -> str:
            return ""


def test_a_synchronous_reader_is_refused() -> None:
    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(TypeError, match="async"):

        @mcp.resource("camera://one", description="Blocks the loop.")
        def blocking(request) -> str:
            return ""


def test_two_resources_cannot_share_a_uri() -> None:
    mcp = MCP(name="x", version="1.0.0")

    @mcp.resource("camera://one", description="The first.")
    async def first(request) -> str:
        return ""

    with pytest.raises(ValueError, match="already registered"):

        @mcp.resource("camera://one", description="The second.")
        async def second(request) -> str:
            return ""


def test_a_server_refuses_more_resources_than_its_ceiling() -> None:
    mcp = MCP(name="x", version="1.0.0", limits=MCPLimits(max_resources=1))

    @mcp.resource("camera://one", description="The only one.")
    async def first(request) -> str:
        return ""

    with pytest.raises(ValueError, match="max_resources"):

        @mcp.resource("camera://two", description="One too many.")
        async def second(request) -> str:
            return ""
