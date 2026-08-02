"""Requests that go the other way: sampling, elicitation, roots, and reentrancy.

Everything through stage 3 was a reply to something the client asked for. These
three methods invert that, and the interesting failures are all about a tool
that is awaiting the client while the client is awaiting that tool: it must be
able to finish, it must not be able to park forever, cancelling the outer call
must cancel the inner question, and ending the session must fail every awaiter
rather than leave one holding a future nobody will complete.

The shape of every test here is the same, and it is the shape a real client has:
a `GET` stream open, a POST parked on a tool, and a *second* POST carrying the
answer. If the server ran a call inline on the POST's own coroutine, the second
POST would never be served and every one of these would hang.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from wreath import Wreath
from wreath._fsguard import ContainmentError
from wreath._mcp import roots as mcp_roots
from wreath.mcp import MCP, PROTOCOL_VERSION, ClientRequestError, MCPLimits, ToolError
from wreath.testing import TestClient, TestResponse

FULL = {"sampling": {}, "elicitation": {}, "roots": {"listChanged": True}}


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def initialize(client: TestClient, capabilities: dict | None = None) -> str:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": FULL if capabilities is None else capabilities,
            },
        },
    )
    return header(response, "mcp-session-id") or ""


class Peer:
    """A client that answers the server's questions, on its own POSTs.

    Deliberately not a helper that reaches into the session: the point of every
    test in this file is that the answer arrives the way a real client sends it,
    over HTTP, while the tool that asked is still parked.
    """

    def __init__(self, client: TestClient, session: str, mcp: MCP) -> None:
        self.client = client
        self.session = session
        self.mcp = mcp
        self.asked: list[dict] = []

    async def next_request(self, seconds: float = 5.0) -> dict:
        """Wait for the server to ask something, reading the session's queue.

        The queue is the transport here rather than the SSE stream: a stream
        opened with `TestClient.get` does not return until it closes, and these
        tests need to read one message and answer it while it stays open.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        queue = _session_of(self.mcp, self.session).notifications
        while loop.time() < deadline:
            if len(queue):
                payload = json.loads(await queue.get())
                if payload.get("method") in (
                    "notifications/progress",
                    "notifications/resources/updated",
                ):
                    continue
                self.asked.append(payload)
                return payload
            await asyncio.sleep(0.005)
        raise AssertionError("the server never asked the client anything")

    async def answer(self, identifier: str, result: dict) -> TestResponse:
        return await self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": identifier, "result": result},
            headers={"mcp-session-id": self.session},
        )

    async def refuse(self, identifier: str, message: str) -> TestResponse:
        return await self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {"code": -32000, "message": message},
            },
            headers={"mcp-session-id": self.session},
        )


def _session_of(mcp: MCP, session: str):
    live = mcp._sessions.get(session)
    if live is None:
        raise AssertionError("no such session")
    return live


def build(**kwargs) -> tuple[Wreath, MCP]:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0", **kwargs)
    return app, mcp


async def call(
    client: TestClient, session: str, payload: dict, **headers: str
) -> TestResponse:
    return await client.post(
        "/mcp", json=payload, headers={"mcp-session-id": session, **headers}
    )


def tool_call(identifier: int, name: str, arguments: dict | None = None) -> dict:
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": params}


# -- sampling ---------------------------------------------------------------


async def test_a_tool_may_ask_the_client_s_model_to_generate() -> None:
    """The reentrant path, end to end: the answer arrives on a second POST."""
    app, mcp = build()

    @mcp.tool(description="Summarises a sighting.", sampling=True)
    async def summarise(request, note: str) -> dict:
        answer = await request.state.mcp.sample(f"Summarise: {note}", max_tokens=64)
        return {"summary": answer["content"]["text"]}

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(
            call(client, session, tool_call(2, "summarise", {"note": "a fox"}))
        )
        asked = await peer.next_request()
        assert asked["method"] == "sampling/createMessage"
        assert asked["params"]["maxTokens"] == 64
        assert asked["params"]["messages"] == [
            {"role": "user", "content": {"type": "text", "text": "Summarise: a fox"}}
        ]
        await peer.answer(
            asked["id"],
            {
                "role": "assistant",
                "content": {"type": "text", "text": "one fox"},
                "model": "a-model",
                "stopReason": "endTurn",
            },
        )
        answered = await asyncio.wait_for(parked, timeout=5)
        assert answered.json()["result"]["structuredContent"] == {"summary": "one fox"}
        assert mcp.stats()["sampling_requests"] == 1


async def test_a_tool_that_did_not_declare_sampling_may_not_sample() -> None:
    """Off by default: putting words in the caller's model is a declaration."""
    app, mcp = build()

    @mcp.tool(description="Tries to sample without saying so.")
    async def sneaky(request) -> dict:
        try:
            await request.state.mcp.sample("hello")
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        result = (await call(client, session, tool_call(2, "sneaky"))).json()["result"]
        assert result["isError"] is True
        assert "did not declare `sampling=`" in result["content"][0]["text"]
        assert mcp.stats()["sampling_refusals"] == 1
        assert mcp.stats()["sampling_requests"] == 0


async def test_sampling_is_cedar_gated_per_tool() -> None:
    """The same authorizer, the same entity shapes, a second requirement."""
    from wreath.auth import BearerTokenBackend, Identity
    from wreath.authorization import CedarAuthorizer

    class Engine:
        """Refuses `Model::sample` and permits everything else."""

        def is_authorized(self, **request: object) -> bool:
            return request["action"] != "Model::sample"

    app, mcp = build()
    app.configure_auth(
        BearerTokenBackend(lambda _token: Identity("ada")),
        CedarAuthorizer(
            engine=Engine(),
            principal=lambda identity: f"User::{identity.id}",
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
            context=lambda request: {},
        ),
    )

    @mcp.tool(description="Summarises.", sampling="Model::sample")
    async def summarise(request) -> dict:
        try:
            await request.state.mcp.sample("hello")
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        answered = await call(
            client, session, tool_call(2, "summarise"), authorization="Bearer t"
        )
        result = answered.json()["result"]
        assert result["isError"] is True
        assert "may not 'Model::sample'" in result["content"][0]["text"]
        assert mcp.stats()["sampling_refusals"] == 1
        # A refusal, never a failure -- the distinction the whole counter set exists for.
        assert mcp.stats()["unauthorized_calls"] == 1
        assert mcp.stats()["tool_errors"] == 1


async def test_the_sampling_gate_is_in_declared_actions() -> None:
    """A model can reach it, so the one document that lists what a model can reach says so."""
    _, mcp = build()

    @mcp.tool(description="Summarises.", action="Note::read", sampling="Model::sample")
    async def summarise(request) -> dict:
        return {}

    assert mcp.declared_actions() == {
        "Model": ("Model::sample",),
        "Note": ("Note::read",),
    }


async def test_sampling_spends_the_tool_s_own_rate_limit() -> None:
    """One bucket per tool, and a sampling request is a draw on it like any other."""
    from wreath.mcp import ToolRateLimit

    app, mcp = build()

    @mcp.tool(
        description="Summarises.",
        sampling=True,
        rate_limit=ToolRateLimit(limit=1, window=60.0),
    )
    async def summarise(request) -> dict:
        # The call itself took the only token, so the sampling request cannot.
        try:
            await request.state.mcp.sample("hello")
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        result = (await call(client, session, tool_call(2, "summarise"))).json()["result"]
        assert result["isError"] is True
        assert "spends the same bucket" in result["content"][0]["text"]
        assert mcp.stats()["throttled"] == 1


async def test_sampling_is_on_the_flight_recorder_like_a_call() -> None:
    from wreath import _flight_schema as fs
    from wreath import logging as log

    app, mcp = build()

    @mcp.tool(description="Summarises.", sampling=True)
    async def summarise(request) -> dict:
        answer = await request.state.mcp.sample("hello")
        return {"said": answer["content"]["text"]}

    with log.testing_runtime() as records, log.request_scope(request_id=7):
        async with TestClient(app) as client:
            session = await initialize(client)
            peer = Peer(client, session, mcp)
            parked = asyncio.ensure_future(call(client, session, tool_call(2, "summarise")))
            asked = await peer.next_request()
            await peer.answer(
                asked["id"], {"role": "assistant", "content": {"type": "text", "text": "hi"}}
            )
            await asyncio.wait_for(parked, timeout=5)
        markers = [
            log.attributes(cell)
            for cell in records
            if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]

    outcomes = [marker["outcome"] for marker in markers]
    assert "sampled" in outcomes
    assert "ok" in outcomes
    assert all(marker["tool"] == "summarise" for marker in markers)


async def test_a_gate_that_lives_only_on_sampling_still_names_the_caller() -> None:
    """The gate decides about a caller, so the marker beside it must name one.

    `_tools_call` resolves the identity only when the *tool* is gated or bounded,
    which keeps an ungated tool from ever running the backend. A tool whose only
    gate is on `sampling=` falls through that: the Cedar decision is made against
    `User::ada` and the sampling marker used to say `anonymous` beside it.

    The credentials are withheld from `initialize` on purpose. A session opened
    by a caller the backend recognises binds `session.principal`, and after that
    the ownership check re-resolves the identity on every message -- which would
    hand this test its `ada` for free and leave the resolution inside `_sampling`
    doing nothing observable. Anonymous `initialize` is the one shape where
    `_sampling` is the only thing that can name the caller.
    """
    from wreath import _flight_schema as fs
    from wreath import logging as log
    from wreath.auth import Identity
    from wreath.authorization import CedarAuthorizer

    class _Ada:
        async def authenticate(self, request) -> Identity | None:
            if request.header("x-token") != "ada":
                return None
            return Identity(id="ada", claims={"sub": "ada"})

        def challenge(self, request) -> None:
            return None

    class _Yes:
        def is_authorized(self, **request) -> bool:
            return True

    app, mcp = build()
    app.configure_auth(
        _Ada(),
        CedarAuthorizer(
            engine=_Yes(),
            principal=lambda identity: f"User::{identity.id}",
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
        ),
    )

    @mcp.tool(description="Summarises.", sampling="Model::sample")
    async def summarise(request) -> dict:
        answer = await request.state.mcp.sample("hello")
        return {"said": answer["content"]["text"]}

    with log.testing_runtime() as records, log.request_scope(request_id=7):
        async with TestClient(app) as client:
            session = await initialize(client)
            peer = Peer(client, session, mcp)
            parked = asyncio.ensure_future(
                call(client, session, tool_call(2, "summarise"), **{"x-token": "ada"})
            )
            asked = await peer.next_request()
            await peer.answer(
                asked["id"], {"role": "assistant", "content": {"type": "text", "text": "hi"}}
            )
            await asyncio.wait_for(parked, timeout=5)
        markers = [
            log.attributes(cell)
            for cell in records
            if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]

    sampled = next(marker for marker in markers if marker["outcome"] == "sampled")
    assert sampled["principal"] == "ada"


async def test_a_client_that_never_advertised_sampling_is_refused_not_hung() -> None:
    app, mcp = build()

    @mcp.tool(description="Summarises.", sampling=True)
    async def summarise(request) -> dict:
        try:
            await request.state.mcp.sample("hello")
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client, capabilities={})
        answered = await asyncio.wait_for(
            call(client, session, tool_call(2, "summarise")), timeout=5
        )
        result = answered.json()["result"]
        assert result["isError"] is True
        assert "did not advertise the 'sampling' capability" in result["content"][0]["text"]


# -- elicitation ------------------------------------------------------------


@dataclass
class Confirm:
    reason: str
    approve: bool
    password: str = ""


async def test_an_elicitation_asks_for_the_schema_the_binding_layer_derives() -> None:
    """The requested schema is a tool's `inputSchema` derivation, over a dataclass."""
    app, mcp = build()

    @mcp.tool(description="Deletes a sighting, once someone says so.", elicitation=True)
    async def delete_sighting(request, sighting: str) -> dict:
        answer = await request.state.mcp.elicit(f"Really delete {sighting}?", Confirm)
        if answer is None:
            raise ToolError("nobody confirmed")
        return {"deleted": answer.approve, "reason": answer.reason}

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(
            call(client, session, tool_call(2, "delete_sighting", {"sighting": "7"}))
        )
        asked = await peer.next_request()
        assert asked["method"] == "elicitation/create"
        assert asked["params"]["message"] == "Really delete 7?"
        assert asked["params"]["requestedSchema"] == {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "approve": {"type": "boolean"},
                # The field's declared default travels too, exactly as it does
                # in a tool's `inputSchema` -- the derivation is the same one.
                "password": {"type": "string", "default": ""},
            },
            "additionalProperties": False,
            "required": ["reason", "approve"],
        }
        await peer.answer(
            asked["id"],
            {"action": "accept", "content": {"reason": "duplicate", "approve": True}},
        )
        answered = await asyncio.wait_for(parked, timeout=5)
        assert answered.json()["result"]["structuredContent"] == {
            "deleted": True,
            "reason": "duplicate",
        }
        assert mcp.stats()["elicitations"] == 1


async def test_a_declined_elicitation_is_none_and_is_counted() -> None:
    app, mcp = build()

    @mcp.tool(description="Asks first.", elicitation=True)
    async def ask(request) -> dict:
        return {"answered": await request.state.mcp.elicit("Well?", Confirm) is not None}

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(call(client, session, tool_call(2, "ask")))
        asked = await peer.next_request()
        await peer.answer(asked["id"], {"action": "decline"})
        answered = await asyncio.wait_for(parked, timeout=5)
        assert answered.json()["result"]["structuredContent"] == {"answered": False}
        assert mcp.stats()["elicitation_declines"] == 1


async def test_an_answer_that_misses_the_schema_is_refused_by_the_same_validator() -> None:
    app, mcp = build()

    @mcp.tool(description="Asks first.", elicitation=True)
    async def ask(request) -> dict:
        try:
            await request.state.mcp.elicit("Well?", Confirm)
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(call(client, session, tool_call(2, "ask")))
        asked = await peer.next_request()
        await peer.answer(
            asked["id"],
            {"action": "accept", "content": {"reason": "x", "approve": "not-a-bool"}},
        )
        answered = await asyncio.wait_for(parked, timeout=5)
        result = answered.json()["result"]
        assert result["isError"] is True
        assert "does not match the schema" in result["content"][0]["text"]
        # The same counter a bad `tools/call` argument lands in, because it is
        # the same failure: a caller sent something the published schema forbids.
        assert mcp.stats()["schema_rejections"] == 1


async def test_a_form_mcp_cannot_carry_is_refused_with_the_field_named() -> None:
    @dataclass
    class Nested:
        inner: Confirm

    app, mcp = build()

    @mcp.tool(description="Asks for the impossible.", elicitation=True)
    async def ask(request) -> dict:
        await request.state.mcp.elicit("Well?", Nested)
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        result = (await call(client, session, tool_call(2, "ask"))).json()["result"]
        assert result["isError"] is True
        assert "the tool raised TypeError" in result["content"][0]["text"]


async def test_what_a_person_typed_is_recorded_under_the_same_redaction() -> None:
    """A form is the *most* likely place a password arrives, so it is the test."""
    from wreath import _flight_schema as fs
    from wreath import logging as log

    secret = "correct-horse-battery-staple"
    app, mcp = build()

    @mcp.tool(description="Asks first.", elicitation=True)
    async def ask(request) -> dict:
        answer = await request.state.mcp.elicit("Sign in?", Confirm)
        return {"approved": answer.approve}

    with log.testing_runtime() as records, log.request_scope(request_id=7) as scope:
        async with TestClient(app) as client:
            session = await initialize(client)
            peer = Peer(client, session, mcp)
            parked = asyncio.ensure_future(call(client, session, tool_call(2, "ask")))
            asked = await peer.next_request()
            await peer.answer(
                asked["id"],
                {
                    "action": "accept",
                    "content": {"reason": "ok", "approve": True, "password": secret},
                },
            )
            await asyncio.wait_for(parked, timeout=5)
        scope.finish(promoted=True)
        attached: dict = {}
        for cell in records:
            if cell.flags & fs.LOG_FLAG_EVENT_FIELDS:
                attached.update(log.attributes(cell))

    assert attached["mcp.elicit.password"] == "<redacted>"
    assert attached["mcp.elicit.approve"] is True
    assert attached["mcp.elicit.reason"].startswith("#")
    assert secret not in repr(attached)


# -- the elicitation gate ----------------------------------------------------


async def test_a_tool_that_did_not_declare_elicitation_may_not_prompt() -> None:
    """Off by default, and refused before a byte reaches the client.

    A prompt renders inside a UI the person already trusts, so an undeclared
    tool asking for `{"password": str}` is a phishing surface wearing that
    client's chrome. "The user can decline" is precisely the control social
    engineering defeats, which is why the deployment decides instead.
    """
    app, mcp = build()

    @mcp.tool(description="Tries to prompt without saying so.")
    async def sneaky(request) -> dict:
        try:
            await request.state.mcp.elicit("Confirm your password", Confirm)
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        result = (await call(client, session, tool_call(2, "sneaky"))).json()["result"]
        assert result["isError"] is True
        assert "did not declare `elicitation=`" in result["content"][0]["text"]
        assert mcp.stats()["elicitation_refusals"] == 1
        # Nothing was asked, of the client or of the person behind it.
        assert mcp.stats()["elicitations"] == 0
        assert not len(_session_of(mcp, session).notifications)


async def test_elicitation_true_works_with_no_authorizer_installed() -> None:
    """The documented development shape, exactly as `sampling=True` is."""
    app, mcp = build()

    @mcp.tool(description="Asks, with no policy attached.", elicitation=True)
    async def ask(request) -> dict:
        answer = await request.state.mcp.elicit("Well?", Confirm)
        return {"reason": answer.reason}

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(call(client, session, tool_call(2, "ask")))
        asked = await peer.next_request()
        assert asked["method"] == "elicitation/create"
        await peer.answer(
            asked["id"],
            {"action": "accept", "content": {"reason": "because", "approve": True}},
        )
        answered = await asyncio.wait_for(parked, timeout=5)
        assert answered.json()["result"]["structuredContent"] == {"reason": "because"}
        assert mcp.stats()["elicitation_refusals"] == 0


async def test_elicitation_is_cedar_gated_per_tool() -> None:
    """A denied prompt never reaches the client, and is a refusal not an error."""
    from wreath.auth import BearerTokenBackend, Identity
    from wreath.authorization import CedarAuthorizer

    class Engine:
        """Refuses `Form::ask` and permits everything else."""

        def is_authorized(self, **request: object) -> bool:
            return request["action"] != "Form::ask"

    app, mcp = build()
    app.configure_auth(
        BearerTokenBackend(lambda _token: Identity("ada")),
        CedarAuthorizer(
            engine=Engine(),
            principal=lambda identity: f"User::{identity.id}",
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
            context=lambda request: {},
        ),
    )

    @mcp.tool(description="Asks the person.", elicitation="Form::ask")
    async def ask(request) -> dict:
        try:
            await request.state.mcp.elicit("Your password?", Confirm)
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        answered = await call(client, session, tool_call(2, "ask"), authorization="Bearer t")
        result = answered.json()["result"]
        assert result["isError"] is True
        assert "may not 'Form::ask'" in result["content"][0]["text"]
        assert mcp.stats()["elicitation_refusals"] == 1
        # A refusal, never a failure -- the distinction stage 2 established.
        assert mcp.stats()["unauthorized_calls"] == 1
        assert mcp.stats()["tool_errors"] == 1
        assert mcp.stats()["elicitations"] == 0
        # And the form never left the server.
        assert not len(_session_of(mcp, session).notifications)


async def test_the_elicitation_gate_is_in_declared_actions() -> None:
    """Every tool that can put a prompt in front of a person, in one document."""
    _, mcp = build()

    @mcp.tool(description="Asks.", action="Note::read", elicitation="Form::ask")
    async def ask(request) -> dict:
        return {}

    assert mcp.declared_actions() == {
        "Form": ("Form::ask",),
        "Note": ("Note::read",),
    }


async def test_elicitation_spends_the_tool_s_own_rate_limit() -> None:
    """One bucket per tool: a tool that can re-prompt freely can wear someone down."""
    from wreath.mcp import ToolRateLimit

    app, mcp = build()

    @mcp.tool(
        description="Asks.",
        elicitation=True,
        rate_limit=ToolRateLimit(limit=1, window=60.0),
    )
    async def ask(request) -> dict:
        # The call itself took the only token, so the elicitation cannot.
        try:
            await request.state.mcp.elicit("Well?", Confirm)
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        result = (await call(client, session, tool_call(2, "ask"))).json()["result"]
        assert result["isError"] is True
        assert "spends the same bucket" in result["content"][0]["text"]
        assert mcp.stats()["throttled"] == 1
        assert mcp.stats()["elicitation_refusals"] == 1


async def test_elicitation_is_on_the_flight_recorder_like_a_call() -> None:
    from wreath import _flight_schema as fs
    from wreath import logging as log

    app, mcp = build()

    @mcp.tool(description="Asks.", elicitation=True)
    async def ask(request) -> dict:
        answer = await request.state.mcp.elicit("Well?", Confirm)
        return {"reason": answer.reason}

    with log.testing_runtime() as records, log.request_scope(request_id=7):
        async with TestClient(app) as client:
            session = await initialize(client)
            peer = Peer(client, session, mcp)
            parked = asyncio.ensure_future(call(client, session, tool_call(2, "ask")))
            asked = await peer.next_request()
            await peer.answer(
                asked["id"],
                {"action": "accept", "content": {"reason": "ok", "approve": True}},
            )
            await asyncio.wait_for(parked, timeout=5)
        markers = [
            log.attributes(cell)
            for cell in records
            if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]

    outcomes = [marker["outcome"] for marker in markers]
    assert "elicited" in outcomes
    assert "ok" in outcomes
    assert all(marker["tool"] == "ask" for marker in markers)


async def test_a_gate_that_lives_only_on_elicitation_still_names_the_caller() -> None:
    """The sampling claim, for the other outbound request, which had no test at all.

    `_elicit` resolves the identity for the same reason `_sampling` does: a tool
    gated only on `elicitation=` was neither gated nor bounded when `_tools_call`
    looked, so nothing upstream ran the backend. Credentials are withheld from
    `initialize` deliberately -- a bound session makes the ownership check resolve
    the caller on every message afterwards, which would supply this `ada` from
    somewhere else and leave the resolution inside `_elicit` unwatched.
    """
    from wreath import _flight_schema as fs
    from wreath import logging as log
    from wreath.auth import Identity
    from wreath.authorization import CedarAuthorizer

    class _Ada:
        async def authenticate(self, request) -> Identity | None:
            if request.header("x-token") != "ada":
                return None
            return Identity(id="ada", claims={"sub": "ada"})

        def challenge(self, request) -> None:
            return None

    class _Yes:
        def is_authorized(self, **request) -> bool:
            return True

    app, mcp = build()
    app.configure_auth(
        _Ada(),
        CedarAuthorizer(
            engine=_Yes(),
            principal=lambda identity: f"User::{identity.id}",
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
        ),
    )

    @mcp.tool(description="Asks the person.", elicitation="Form::ask")
    async def ask(request) -> dict:
        answer = await request.state.mcp.elicit("Well?", Confirm)
        return {"reason": answer.reason}

    with log.testing_runtime() as records, log.request_scope(request_id=9):
        async with TestClient(app) as client:
            session = await initialize(client)  # no credentials: an unbound session
            peer = Peer(client, session, mcp)
            parked = asyncio.ensure_future(
                call(client, session, tool_call(2, "ask"), **{"x-token": "ada"})
            )
            asked = await peer.next_request()
            await peer.answer(
                asked["id"],
                {"action": "accept", "content": {"reason": "ok", "approve": True}},
            )
            await asyncio.wait_for(parked, timeout=5)
        markers = [
            log.attributes(cell)
            for cell in records
            if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]

    elicited = next(marker for marker in markers if marker["outcome"] == "elicited")
    assert elicited["principal"] == "ada"


async def test_a_resource_reader_may_not_prompt_either() -> None:
    """No declaration to read, so the same default-deny sampling already gives it."""
    app, mcp = build()

    @mcp.resource("camera://ridge", description="Tries to prompt.")
    async def ridge(request) -> str:
        await request.state.mcp.elicit("Which frame?", Confirm)
        return "never"

    async with TestClient(app) as client:
        session = await initialize(client)
        answered = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "camera://ridge"},
            },
        )
        assert answered.json()["error"]["message"] == (
            "reading camera://ridge raised ClientRequestError"
        )
        assert mcp.stats()["elicitation_refusals"] == 1
        assert not len(_session_of(mcp, session).notifications)


# -- reentrancy, timeouts and cancellation ----------------------------------


async def test_a_client_that_never_answers_times_out_rather_than_pinning_the_session() -> None:
    app, mcp = build(limits=MCPLimits(client_request_seconds=0.05))

    @mcp.tool(description="Asks and waits.", elicitation=True)
    async def ask(request) -> dict:
        try:
            await request.state.mcp.elicit("Well?", Confirm)
        except ClientRequestError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        answered = await asyncio.wait_for(
            call(client, session, tool_call(2, "ask")), timeout=5
        )
        result = answered.json()["result"]
        assert result["isError"] is True
        assert "did not answer" in result["content"][0]["text"]
        assert mcp.stats()["client_request_timeouts"] == 1
        # And the session is usable afterwards, which is the point of the bound.
        assert (await call(client, session, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
                ).json()["result"] == {}


async def test_a_timed_out_request_tells_the_client_to_stop_working_on_it() -> None:
    app, mcp = build(limits=MCPLimits(client_request_seconds=0.05))

    @mcp.tool(description="Asks and waits.", elicitation=True)
    async def ask(request) -> dict:
        try:
            await request.state.mcp.elicit("Well?", Confirm)
        except ClientRequestError:
            return {}
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        await asyncio.wait_for(call(client, session, tool_call(2, "ask")), timeout=5)
        queued = [json.loads(item) for item in _session_of(mcp, session).notifications.snapshot()]
        methods = [item.get("method") for item in queued]
        assert "elicitation/create" in methods
        assert "notifications/cancelled" in methods


async def test_cancelling_the_outer_call_cancels_the_inner_request() -> None:
    app, mcp = build()
    entered = asyncio.Event()

    @mcp.tool(description="Asks and waits forever.", elicitation=True)
    async def ask(request) -> dict:
        entered.set()
        await request.state.mcp.elicit("Well?", Confirm)
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(call(client, session, tool_call(2, "ask")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        asked = await peer.next_request()
        await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            },
        )
        # A cancelled call sends no response at all, which is what 202 means here.
        assert (await asyncio.wait_for(parked, timeout=5)).status == 202
        queued = _session_of(mcp, session).notifications.snapshot()
        withdrawn = [json.loads(item) for item in queued]
        assert {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": asked["id"]},
        } in withdrawn


async def test_ending_the_session_fails_every_pending_request() -> None:
    """Otherwise a tool is left awaiting a future belonging to a session that is gone."""
    app, mcp = build()
    failed: list[str] = []
    entered = asyncio.Event()

    @mcp.tool(description="Asks and waits forever.", elicitation=True)
    async def ask(request) -> dict:
        entered.set()
        try:
            await request.state.mcp.elicit("Well?", Confirm)
        except ClientRequestError as error:
            failed.append(str(error))
            raise
        except asyncio.CancelledError:
            failed.append("cancelled")
            raise
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        parked = asyncio.ensure_future(call(client, session, tool_call(2, "ask")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.sleep(0)
        await client.delete("/mcp", headers={"mcp-session-id": session})
        await asyncio.wait_for(parked, timeout=5)

    assert failed, "the parked tool was never woken when its session ended"


async def test_a_reader_parked_on_a_question_is_failed_when_the_session_ends() -> None:
    """The case cancellation alone does not cover.

    A `tools/call` runs in a task the session can cancel, so ending the session
    would wake it either way. A `resources/read` does not: it runs on the POST's
    own coroutine and is in nobody's `in_flight`, so if the pending table were
    not failed explicitly this reader would await a future that no longer
    belongs to a live session, for as long as the process runs.

    The question it parks on is `roots/list` rather than an elicitation, because
    a resource carries no `elicitation=` declaration and so may not prompt at
    all -- a refusal would return immediately and this test would pass while
    proving nothing.
    """
    app, mcp = build()
    entered = asyncio.Event()

    @mcp.resource("camera://ridge", description="Asks before it answers.")
    async def ridge(request) -> str:
        entered.set()
        await request.state.mcp.roots()
        return "never"

    async with TestClient(app) as client:
        session = await initialize(client)
        parked = asyncio.ensure_future(
            call(
                client,
                session,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": "camera://ridge"},
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.sleep(0)
        await client.delete("/mcp", headers={"mcp-session-id": session})
        answered = await asyncio.wait_for(parked, timeout=5)

    assert answered.json()["error"]["message"] == (
        "reading camera://ridge raised ClientRequestError"
    )
    assert mcp.stats()["resource_errors"] == 1


async def test_the_pending_table_is_bounded() -> None:
    app, mcp = build(limits=MCPLimits(max_pending_requests=1, client_request_seconds=5.0))
    refusals: list[str] = []
    first = asyncio.Event()

    @mcp.tool(description="Asks twice at once.", elicitation=True)
    async def ask(request) -> dict:
        context = request.state.mcp

        async def second() -> None:
            await first.wait()
            try:
                await context.elicit("And again?", Confirm)
            except ClientRequestError as error:
                refusals.append(str(error))

        helper = asyncio.ensure_future(second())
        try:
            outer = asyncio.ensure_future(context.elicit("Well?", Confirm))
            await asyncio.sleep(0)
            first.set()
            await asyncio.wait_for(helper, timeout=5)
            outer.cancel()
            await asyncio.gather(outer, return_exceptions=True)
        finally:
            helper.cancel()
        return {}

    async with TestClient(app) as client:
        session = await initialize(client)
        await asyncio.wait_for(call(client, session, tool_call(2, "ask")), timeout=10)

    assert refusals and "max_pending_requests" in refusals[0]


# -- roots ------------------------------------------------------------------


def test_root_results_keep_only_absolute_file_uris() -> None:
    assert mcp_roots.root_paths(None) == ()
    assert mcp_roots.root_paths({"roots": "not-a-list"}) == ()
    assert mcp_roots.root_paths(
        {"roots": iter(({"uri": "file:///not-admitted"},))}
    ) == ()
    assert mcp_roots.root_paths(
        {
            "roots": [
                None,
                {"uri": 3},
                {"uri": "https://example.test/workspace"},
                {"uri": "file://"},
                {"uri": "file:///srv/a%20trail/../workspace"},
            ]
        }
    ) == ("/srv/workspace",)


def test_read_beneath_refuses_non_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_roots,
        "open_beneath",
        lambda _root, _relative: (
            101,
            SimpleNamespace(st_mode=0, st_size=0),
        ),
    )
    monkeypatch.setattr(mcp_roots.os, "close", lambda _handle: None)

    with pytest.raises(ContainmentError, match="not a regular file"):
        mcp_roots.read_beneath(100, "directory", max_bytes=20)


def test_read_beneath_stops_at_eof(tmp_path) -> None:
    (tmp_path / "empty.txt").write_bytes(b"")
    root_fd = mcp_roots.open_root(tmp_path)
    try:
        assert mcp_roots.read_beneath(
            root_fd,
            "empty.txt",
            max_bytes=20,
        ) == b""
    finally:
        mcp_roots.os.close(root_fd)


async def test_a_client_root_confines_a_file_read(tmp_path) -> None:
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "public" / "note.txt").write_text("visible")
    (tmp_path / "private" / "keys.pem").write_text("secret")

    app, mcp = build(file_root=tmp_path)

    @mcp.tool(description="Reads a file the client's roots allow.")
    async def read(request, path: str) -> dict:
        try:
            return {"text": (await request.state.mcp.read_file(path)).decode()}
        except PermissionError as error:
            raise ToolError(str(error)) from error

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(
            call(client, session, tool_call(2, "read", {"path": "public/note.txt"}))
        )
        asked = await peer.next_request()
        assert asked["method"] == "roots/list"
        await peer.answer(
            asked["id"],
            {"roots": [{"uri": f"file://{tmp_path}/public", "name": "workspace"}]},
        )
        allowed = await asyncio.wait_for(parked, timeout=5)
        assert allowed.json()["result"]["structuredContent"] == {"text": "visible"}

        # The roots are cached, so this one needs no second round trip -- and it
        # is refused, which is the entire reason for asking.
        refused = await asyncio.wait_for(
            call(client, session, tool_call(3, "read", {"path": "private/keys.pem"})),
            timeout=5,
        )
        result = refused.json()["result"]
        assert result["isError"] is True
        assert "outside every root this client declared" in result["content"][0]["text"]
        assert mcp.stats()["roots_refusals"] == 1


@pytest.mark.parametrize(
    "declared_roots",
    (
        [],
        [{"uri": "https://attacker.invalid/not-a-file-root"}],
    ),
)
async def test_a_client_declaring_no_roots_cannot_read_the_server_root(
    tmp_path, declared_roots: list[dict[str, str]]
) -> None:
    """An explicit empty root set is a boundary, not permission to read all.

    A hostile client controls the ``roots/list`` answer.  Collapsing ``[]``
    with "this client did not advertise roots" lets it remove the client-side
    confinement while retaining access to every file beneath ``file_root``.
    """
    (tmp_path / "private.txt").write_text("server secret")
    app, mcp = build(file_root=tmp_path)

    @mcp.tool(description="Reads a file the client's roots allow.")
    async def read(request, path: str) -> dict:
        try:
            return {"text": (await request.state.mcp.read_file(path)).decode()}
        except PermissionError as error:
            raise ToolError(str(error)) from error

    async with TestClient(app) as client:
        session = await initialize(client)
        peer = Peer(client, session, mcp)
        parked = asyncio.ensure_future(
            call(client, session, tool_call(2, "read", {"path": "private.txt"}))
        )
        asked = await peer.next_request()
        assert asked["method"] == "roots/list"
        await peer.answer(asked["id"], {"roots": declared_roots})
        refused = await asyncio.wait_for(parked, timeout=5)

    result = refused.json()["result"]
    assert result["isError"] is True
    assert "outside every root this client declared" in result["content"][0]["text"]
    assert mcp.stats()["roots_refusals"] == 1


async def test_a_client_without_the_roots_capability_uses_only_the_server_root(
    tmp_path,
) -> None:
    """No roots capability is distinct from an explicitly empty roots grant."""
    (tmp_path / "public.txt").write_text("server-owned")
    app, mcp = build(file_root=tmp_path)

    @mcp.tool(description="Reads a file owned by this server.")
    async def read(request) -> dict:
        return {"text": (await request.state.mcp.read_file("public.txt")).decode()}

    async with TestClient(app) as client:
        session = await initialize(client, capabilities={})
        answered = await call(client, session, tool_call(2, "read"))

    result = answered.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == '{"text":"server-owned"}'
    assert mcp.stats()["roots_refusals"] == 0


async def test_a_read_cannot_escape_the_server_s_root(tmp_path) -> None:
    """`_fsguard`'s walk, reached through the MCP surface rather than reimplemented."""
    (tmp_path / "root").mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    (tmp_path / "root" / "link.txt").symlink_to(tmp_path / "outside.txt")

    app, mcp = build(file_root=tmp_path / "root")

    @mcp.tool(description="Reads a file.")
    async def read(request, path: str) -> dict:
        try:
            return {"text": (await request.state.mcp.read_file(path)).decode()}
        except PermissionError as error:
            raise ToolError(f"refused: {error}") from error

    async with TestClient(app) as client:
        session = await initialize(client, capabilities={})
        for path in ("../outside.txt", "link.txt"):
            answered = await call(client, session, tool_call(2, "read", {"path": path}))
            result = answered.json()["result"]
            assert result["isError"] is True, path
            assert "refused" in result["content"][0]["text"], path


async def test_a_server_with_no_file_root_reads_nothing(tmp_path) -> None:
    app, mcp = build()

    @mcp.tool(description="Reads a file.")
    async def read(request) -> dict:
        await request.state.mcp.read_file("anything")
        return {}

    async with TestClient(app) as client:
        session = await initialize(client, capabilities={})
        result = (await call(client, session, tool_call(2, "read"))).json()["result"]
        assert result["isError"] is True
        assert "the tool raised RuntimeError" in result["content"][0]["text"]


async def test_a_file_over_the_ceiling_is_refused(tmp_path) -> None:
    (tmp_path / "big.txt").write_text("x" * 64)
    app, mcp = build(file_root=tmp_path, limits=MCPLimits(max_file_bytes=8))

    @mcp.tool(description="Reads a file.")
    async def read(request) -> dict:
        try:
            await request.state.mcp.read_file("big.txt")
        except PermissionError as error:
            raise ToolError(str(error)) from error
        return {}

    async with TestClient(app) as client:
        session = await initialize(client, capabilities={})
        result = (await call(client, session, tool_call(2, "read"))).json()["result"]
        assert "max_file_bytes" in result["content"][0]["text"]


async def test_list_changed_invalidates_the_cached_roots(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "note.txt").write_text("here")
    app, mcp = build(file_root=tmp_path)

    async with TestClient(app) as client:
        session = await initialize(client)
        live = _session_of(mcp, session)
        live.roots = (str(tmp_path / "a"),)
        await call(
            client,
            session,
            {"jsonrpc": "2.0", "method": "notifications/roots/list_changed", "params": {}},
        )
        assert live.roots is None


# -- direction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "method", ["sampling/createMessage", "elicitation/create", "roots/list"]
)
async def test_a_client_that_posts_a_server_to_client_method_is_told_which_way_it_goes(
    method: str,
) -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session = await initialize(client)
        answered = await call(
            client, session, {"jsonrpc": "2.0", "id": 2, "method": method, "params": {}}
        )
        error = answered.json()["error"]
        assert error["code"] == -32601
        assert "server-to-client request" in error["message"]
