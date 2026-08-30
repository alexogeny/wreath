from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import wreath._mcp.server as server_module
from wreath import Wreath
from wreath._auth.requirements import AuthRequirement, PolicyRequirement, SetRequirement
from wreath._mcp.auth import Unauthenticated
from wreath._mcp.outbound import ClientChannel
from wreath._mcp.protocol import INVALID_PARAMS, METHOD_NOT_FOUND, JsonRpcError, Message
from wreath._mcp.server import MCP, _Gate, _holds, _sampling_messages
from wreath._mcp.session import Session, ToolContext
from wreath.auth import Identity
from wreath.mcp import ClientRequestError, ToolRateLimit
from wreath.request import Request


async def _no_receive() -> dict[str, Any]:
    raise AssertionError("the receive channel must not be read")


async def test_failing_a_channel_wakes_pending_requests_and_preserves_answers() -> None:
    channel = ClientChannel(lambda _message: True, max_pending=2, timeout=1)
    loop = asyncio.get_running_loop()
    pending = loop.create_future()
    answered = loop.create_future()
    answered.set_result("kept")
    channel._pending.update({"pending": pending, "answered": answered})

    channel.fail_all("session ended")

    assert pending.done()
    with pytest.raises(ClientRequestError, match="session ended"):
        pending.result()
    assert answered.result() == "kept"
    assert len(channel) == 0


def _messages(*messages: object) -> Any:
    pending = iter(messages)

    async def receive() -> object:
        return next(pending)

    return receive


def _server(**kwargs: Any) -> MCP:
    return MCP(Wreath(), name="mutation-controls", version="1.0.0", **kwargs)


def _session() -> Session:
    return Session("session", "2025-06-18")


def _request(identity: Identity | None = None) -> Request:
    request = Request({"type": "http", "headers": []}, _no_receive)
    request._set_identity(identity)
    return request


def test_set_requirement_all_and_any_have_distinct_meanings() -> None:
    actual = ["reader"]
    all_check = SetRequirement(frozenset(("reader", "writer")), "all")
    any_check = SetRequirement(frozenset(("reader", "writer")), "any")

    assert _holds(actual, all_check) is False
    assert _holds(actual, any_check) is True


def test_sampling_bare_string_becomes_one_user_text_turn() -> None:
    assert _sampling_messages("summarise this") == [
        {
            "role": "user",
            "content": {"type": "text", "text": "summarise this"},
        }
    ]


def test_sampling_sequence_preserves_roles_and_promotes_text_content() -> None:
    messages = _sampling_messages(
        [
            {"role": "assistant", "content": "ready"},
            {"content": {"type": "image", "data": "abc"}},
        ]
    )

    assert messages == [
        {
            "role": "assistant",
            "content": {"type": "text", "text": "ready"},
        },
        {
            "role": "user",
            "content": {"type": "image", "data": "abc"},
        },
    ]


def test_sampling_refuses_a_non_mapping_message_with_its_actual_type() -> None:
    with pytest.raises(TypeError, match="got int"):
        _sampling_messages([7])


def test_sampling_refuses_non_text_non_mapping_content_with_its_actual_type() -> None:
    with pytest.raises(TypeError, match="content.*got list"):
        _sampling_messages([{"role": "user", "content": []}])


@pytest.mark.asyncio
async def test_unprotected_metadata_handler_fails_closed_when_called_directly() -> None:
    response = await _server()._metadata(_request())

    assert response.status == 404
    assert json.loads(response.body)["error"]["message"] == "this endpoint is not protected"


@pytest.mark.asyncio
async def test_late_client_response_without_an_outbound_channel_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()
    session = _session()

    async def authenticate(_self: MCP, _request: Request) -> None:
        return None

    async def session_for(
        _self: MCP,
        _request: Request,
        _identity: Any,
        *,
        identifier: Any,
    ) -> tuple[Session, None]:
        assert identifier == "late"
        return session, None

    monkeypatch.setattr(MCP, "_authenticate", authenticate)
    monkeypatch.setattr(MCP, "_session_for", session_for)
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"content-type", b"application/json"),
                (b"accept", b"application/json"),
            ],
        },
        _messages(
            {
                "type": "http.request",
                "body": b'{"jsonrpc":"2.0","id":"late","result":{}}',
                "more_body": False,
            }
        ),
    )

    response = await mcp._post(request)

    assert response.status == 202
    assert response.body == b""


def test_challenge_uses_default_only_when_refusal_has_no_description() -> None:
    mcp = _server()

    defaulted = json.loads(mcp._challenge(Unauthenticated(None)).body)
    described = json.loads(mcp._challenge(Unauthenticated("invalid_token", "token expired")).body)

    assert defaulted["error"]["message"] == "this MCP endpoint requires a bearer token"
    assert described["error"]["message"] == "token expired"


@pytest.mark.asyncio
async def test_cancel_notification_rejects_bool_and_unhashable_request_ids() -> None:
    mcp = _server()
    session = _session()
    task = asyncio.create_task(asyncio.Event().wait())
    session.in_flight[1] = task

    mcp._handle_notification(
        session,
        Message(
            "notifications/cancelled",
            {"requestId": True},
            is_notification=True,
        ),
    )
    mcp._handle_notification(
        session,
        Message(
            "notifications/cancelled",
            {"requestId": []},
            is_notification=True,
        ),
    )

    assert task.cancelling() == 0
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_notification_accepts_integer_and_string_request_ids() -> None:
    mcp = _server()
    session = _session()
    integer = asyncio.create_task(asyncio.Event().wait())
    text = asyncio.create_task(asyncio.Event().wait())
    session.in_flight[7] = integer
    session.in_flight["call"] = text

    mcp._handle_notification(
        session,
        Message("notifications/cancelled", {"requestId": 7}, is_notification=True),
    )
    mcp._handle_notification(
        session,
        Message(
            "notifications/cancelled",
            {"requestId": "call"},
            is_notification=True,
        ),
    )

    assert integer.cancelling() == 1
    assert text.cancelling() == 1
    await asyncio.gather(integer, text, return_exceptions=True)


def test_roots_changed_invalidates_only_the_roots_notification() -> None:
    mcp = _server()
    session = _session()
    session.roots = ("/workspace",)

    mcp._handle_notification(
        session,
        Message("notifications/unknown", {}, is_notification=True),
    )
    assert session.roots == ("/workspace",)

    mcp._handle_notification(
        session,
        Message("notifications/roots/list_changed", {}, is_notification=True),
    )
    assert session.roots is None


@pytest.mark.asyncio
async def test_dispatch_distinguishes_reserved_client_only_and_unknown_methods() -> None:
    mcp = _server()
    request = _request()
    session = _session()

    with pytest.raises(JsonRpcError) as reserved:
        await mcp._dispatch(
            request,
            session,
            Message("logging/setLevel", {}, id=1, is_request=True),
        )
    assert reserved.value.code == METHOD_NOT_FOUND
    assert "reserved" in reserved.value.message
    assert "logging" in reserved.value.message

    with pytest.raises(JsonRpcError) as client_only:
        await mcp._dispatch(
            request,
            session,
            Message("sampling/createMessage", {}, id=2, is_request=True),
        )
    assert client_only.value.code == METHOD_NOT_FOUND
    assert "server-to-client request" in client_only.value.message

    with pytest.raises(JsonRpcError) as unknown:
        await mcp._dispatch(
            request,
            session,
            Message("not/known", {}, id=3, is_request=True),
        )
    assert unknown.value.code == METHOD_NOT_FOUND
    assert "unknown method 'not/known'" == unknown.value.message


@pytest.mark.asyncio
async def test_prompt_get_refuses_non_string_name_before_registry_lookup() -> None:
    mcp = _server()

    with pytest.raises(JsonRpcError) as caught:
        await mcp._prompts_get(
            _request(),
            _session(),
            Message("prompts/get", {"name": 7}, id=1, is_request=True),
        )

    assert caught.value.code == INVALID_PARAMS
    assert caught.value.message == "`params.name` must name a prompt"


@pytest.mark.asyncio
async def test_prompt_get_refuses_non_object_arguments_before_binding() -> None:
    mcp = _server()

    @mcp.prompt(description="A prompt used to reach argument validation.")
    async def greeting(_request: Request, name: str = "world") -> str:
        return f"hello {name}"

    with pytest.raises(JsonRpcError) as caught:
        await mcp._prompts_get(
            _request(),
            _session(),
            Message(
                "prompts/get",
                {"name": "greeting", "arguments": []},
                id=1,
                is_request=True,
            ),
        )

    assert caught.value.code == INVALID_PARAMS
    assert caught.value.message == "`params.arguments` must be a JSON object"


@pytest.mark.asyncio
async def test_file_root_descriptor_is_opened_once_and_reused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opened: list[str] = []
    reads: list[tuple[int, str, int]] = []

    def open_root(path: str) -> int:
        opened.append(path)
        return 73

    def read_beneath(fd: int, path: str, *, max_bytes: int) -> bytes:
        reads.append((fd, path, max_bytes))
        return path.encode()

    monkeypatch.setattr(server_module, "_open_root", open_root)
    monkeypatch.setattr(server_module, "read_beneath", read_beneath)
    mcp = _server(file_root=tmp_path)
    session = _session()

    assert await mcp._read_file(session, "one.txt") == b"one.txt"
    assert await mcp._read_file(session, "two.txt") == b"two.txt"
    assert opened == [str(tmp_path)]
    assert [entry[:2] for entry in reads] == [(73, "one.txt"), (73, "two.txt")]


@pytest.mark.asyncio
async def test_permission_authorization_uses_all_and_any_modes() -> None:
    mcp = _server()
    identity = Identity("ada", permissions=frozenset(("read",)))

    all_entry = _Gate(
        "all-tool",
        AuthRequirement(permission_checks=(SetRequirement(frozenset(("read", "write")), "all"),)),
    )
    any_entry = _Gate(
        "any-tool",
        AuthRequirement(permission_checks=(SetRequirement(frozenset(("read", "write")), "any"),)),
    )

    assert "does not hold the permissions" in (
        await mcp._authorize(_request(identity), all_entry) or ""
    )
    assert await mcp._authorize(_request(identity), any_entry) is None


class _Authorizer:
    def __init__(self, reason: str | None) -> None:
        self.reason = reason

    async def authorize(self, _request: Request, _policy: PolicyRequirement) -> Any:
        return SimpleNamespace(allowed=False, reason=self.reason)


@pytest.mark.asyncio
async def test_policy_denial_preserves_reason_and_has_a_default() -> None:
    requirement = AuthRequirement(policies=(PolicyRequirement("record:read", 'Record::"one"'),))
    entry = _Gate("record", requirement)
    identity = Identity("ada")

    explained = await _server(authorizer=_Authorizer("outside tenant"))._authorize(
        _request(identity), entry
    )
    defaulted = await _server(authorizer=_Authorizer(None))._authorize(_request(identity), entry)

    assert explained == "the caller may not 'record:read': outside tenant"
    assert defaulted == "the caller may not 'record:read': denied"


@pytest.mark.asyncio
async def test_sampling_unknown_tool_is_a_declared_refusal_not_an_attribute_error() -> None:
    mcp = _server()
    context = ToolContext(
        "session",
        1,
        "missing",
        _server=mcp,
        _session=_session(),
        _request=_request(),
    )

    with pytest.raises(ClientRequestError, match="did not declare `sampling=`"):
        await context.sample("hello")


@pytest.mark.asyncio
async def test_public_sampling_skips_identity_and_preserves_optional_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()

    @mcp.tool(description="Samples without an identity gate.", sampling=True)
    async def sampler(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    async def identify(_self: MCP, _request: Request) -> Any:
        raise AssertionError("public unbounded sampling does not need an identity")

    asked: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []

    async def client_request(
        _self: MCP,
        _session: Session,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        assert method == "sampling/createMessage"
        asked.append(params)
        return "plain client result"

    monkeypatch.setattr(MCP, "_identify", identify)
    monkeypatch.setattr(MCP, "_client_request", client_request)
    monkeypatch.setattr(
        server_module._record,
        "record_call",
        lambda **values: markers.append(values),
    )
    context = ToolContext(
        "session",
        1,
        "sampler",
        identity=Identity("ada"),
        _server=mcp,
        _session=_session(),
        _request=_request(),
    )

    result = await context.sample(
        "hello",
        max_tokens=17,
        system_prompt="be concise",
        temperature=0.25,
        stop_sequences=("stop",),
        model_preferences={"hints": [{"name": "small"}]},
        include_context="thisServer",
        metadata={"trace": "one"},
    )

    assert result == {"content": "plain client result"}
    assert markers[-1]["principal"] == "ada"
    assert asked == [
        {
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": "hello"},
                }
            ],
            "maxTokens": 17,
            "systemPrompt": "be concise",
            "temperature": 0.25,
            "stopSequences": ("stop",),
            "modelPreferences": {"hints": [{"name": "small"}]},
            "includeContext": "thisServer",
            "metadata": {"trace": "one"},
        }
    ]


@pytest.mark.asyncio
async def test_sampling_omits_every_absent_optional_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()

    @mcp.tool(description="Samples with defaults.", sampling=True)
    async def sampler(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    asked: list[dict[str, Any]] = []

    async def client_request(
        _self: MCP,
        _session: Session,
        _method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        asked.append(params)
        return {"content": {"type": "text", "text": "answer"}}

    monkeypatch.setattr(MCP, "_client_request", client_request)
    context = ToolContext(
        "session",
        1,
        "sampler",
        _server=mcp,
        _session=_session(),
        _request=_request(),
    )

    result = await context.sample("hello", max_tokens=9)

    assert result == {"content": {"type": "text", "text": "answer"}}
    assert set(asked[0]) == {"messages", "maxTokens"}


@pytest.mark.asyncio
async def test_sampling_resolves_identity_for_policy_or_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()

    @mcp.tool(description="Policy-gated sample.", sampling="sample:policy")
    async def policy_tool(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    @mcp.tool(
        description="Rate-limited sample.",
        sampling=True,
        rate_limit=ToolRateLimit(limit=1000),
    )
    async def limited_tool(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    identified: list[str] = []

    async def identify(_self: MCP, _request: Request) -> Identity:
        identified.append("called")
        identity = Identity("ada")
        _request._set_identity(identity)
        return identity

    async def authorize(
        _self: MCP,
        _request: Request,
        _entry: Any,
        *,
        noun: str = "tool",
    ) -> None:
        assert noun == "sampling from tool"
        return None

    async def client_request(
        _self: MCP,
        _session: Session,
        _method: str,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        return {"content": {"type": "text", "text": "answer"}}

    monkeypatch.setattr(MCP, "_identify", identify)
    monkeypatch.setattr(MCP, "_authorize", authorize)
    monkeypatch.setattr(MCP, "_client_request", client_request)

    for name in ("policy_tool", "limited_tool"):
        context = ToolContext(
            "session",
            name,
            name,
            _server=mcp,
            _session=_session(),
            _request=_request(),
        )
        await context.sample("hello")

    assert identified == ["called", "called"]


@dataclass
class _OptionalAnswer:
    note: str = "default"


@pytest.mark.asyncio
async def test_elicitation_non_mapping_result_is_a_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()

    @mcp.tool(description="Elicits a form.", elicitation=True)
    async def asker(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    async def client_request(
        _self: MCP,
        _session: Session,
        _method: str,
        _params: dict[str, Any],
    ) -> list[str]:
        return ["not", "an", "envelope"]

    monkeypatch.setattr(MCP, "_client_request", client_request)
    context = ToolContext(
        "session",
        1,
        "asker",
        _server=mcp,
        _session=_session(),
        _request=_request(),
    )

    assert await context.elicit("Answer", _OptionalAnswer) is None
    assert mcp.elicitation_declines == 1


@pytest.mark.asyncio
async def test_elicitation_non_mapping_content_becomes_an_empty_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()

    @mcp.tool(description="Elicits a form.", elicitation=True)
    async def asker(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    async def client_request(
        _self: MCP,
        _session: Session,
        _method: str,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        return {"action": "accept", "content": ["not", "an", "object"]}

    monkeypatch.setattr(MCP, "_client_request", client_request)
    context = ToolContext(
        "session",
        1,
        "asker",
        _server=mcp,
        _session=_session(),
        _request=_request(),
    )

    answer = await context.elicit("Answer", _OptionalAnswer)

    assert answer == _OptionalAnswer()
