from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import wreath._mcp.server as server_module
from wreath import Router, Wreath
from wreath._auth.requirements import AuthRequirement, PolicyRequirement, SetRequirement
from wreath._mcp.auth import Unauthenticated
from wreath._mcp.outbound import ClientChannel
from wreath._mcp.protocol import INVALID_PARAMS, METHOD_NOT_FOUND, JsonRpcError, Message
from wreath._mcp.server import MCP, _Gate, _holds, _sampling_messages
from wreath._mcp.session import Session, ToolContext
from wreath.auth import Identity
from wreath.mcp import ClientRequestError, MCPLimits, ToolError, ToolRateLimit
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


@pytest.mark.parametrize("progress_interval", [0, -1, -0.25, float("nan"), float("inf")])
def test_mcp_refuses_each_nonpositive_progress_interval(progress_interval: float) -> None:
    with pytest.raises(ValueError, match="progress_interval must be positive"):
        _server(progress_interval=progress_interval)


def test_mcp_preserves_supplied_progress_registry() -> None:
    progress = object()

    assert _server(progress=progress)._progress is progress


def test_mcp_registers_file_cleanup_only_with_a_lifespan_owner(tmp_path: Path) -> None:
    app = Wreath()
    shutdown_handlers = len(app._shutdown_handlers)
    MCP(app, name="no-files", version="1.0.0")
    assert len(app._shutdown_handlers) == shutdown_handlers

    router = Router()
    MCP(router, name="files", version="1.0.0", file_root=tmp_path)


def test_mcp_refuses_an_empty_file_root() -> None:
    with pytest.raises(ValueError, match="file_root must name a directory"):
        _server(file_root="")


async def _initialize_direct(mcp: MCP, params: dict[str, object]) -> tuple[dict[str, Any], Any]:
    response = await mcp._initialize(
        _request(Identity("user")),
        Message("initialize", params, id=1, is_request=True),
        Identity("user"),
        stream=False,
    )
    body = json.loads(response.body)
    session_id = next(
        value.decode() for name, value in response.headers if name == b"mcp-session-id"
    )
    session = mcp._sessions.get(session_id)
    assert session is not None
    return body, session


@pytest.mark.asyncio
async def test_initialize_preserves_supported_protocol_and_client_facts() -> None:
    mcp = _server(instructions="Use the declared tools.")
    protocol = next(iter(server_module.SUPPORTED_PROTOCOL_VERSIONS))

    body, session = await _initialize_direct(
        mcp,
        {
            "protocolVersion": protocol,
            "clientInfo": {"name": "probe", "version": "1"},
            "capabilities": {"roots": {"listChanged": True}},
        },
    )

    assert body["result"]["protocolVersion"] == protocol
    assert body["result"]["instructions"] == "Use the declared tools."
    assert session.client_info == {"name": "probe", "version": "1"}
    assert session.client_capabilities == {"roots": {"listChanged": True}}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 1, "client", [], object()])
async def test_initialize_replaces_each_invalid_client_fact_shape(value: object) -> None:
    _body, session = await _initialize_direct(
        _server(),
        {
            "protocolVersion": "unsupported",
            "clientInfo": value,
            "capabilities": value,
        },
    )

    assert session.protocol_version == server_module.PROTOCOL_VERSION
    assert session.client_info == {}
    assert session.client_capabilities == {}


@pytest.mark.asyncio
async def test_initialize_omits_absent_instructions() -> None:
    body, _session_value = await _initialize_direct(
        _server(),
        {"protocolVersion": server_module.PROTOCOL_VERSION},
    )

    assert "instructions" not in body["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [None, 1, b"tool", [], {}])
async def test_tools_call_refuses_each_non_text_name(name: object) -> None:
    with pytest.raises(JsonRpcError) as caught:
        await _server()._tools_call(
            _request(),
            _session(),
            Message("tools/call", {"name": name}, id=1, is_request=True),
        )

    assert caught.value.code == INVALID_PARAMS
    assert caught.value.message == "`params.name` must name a tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [1, "arguments", [], (), object()])
async def test_tools_call_refuses_each_non_object_arguments(arguments: object) -> None:
    mcp = _server()

    @mcp.tool(description="Probe invalid arguments.")
    async def probe(request: Request) -> dict[str, bool]:
        return {"ok": True}

    with pytest.raises(JsonRpcError) as caught:
        await mcp._tools_call(
            _request(),
            _session(),
            Message(
                "tools/call",
                {"name": "probe", "arguments": arguments},
                id=1,
                is_request=True,
            ),
        )

    assert caught.value.code == INVALID_PARAMS
    assert caught.value.message == "`params.arguments` must be a JSON object"


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
async def test_tool_errors_obey_the_serialized_result_limit() -> None:
    mcp = _server(limits=MCPLimits(max_result_bytes=64))

    @mcp.tool(description="Fails with a caller-controlled detail.")
    async def oversized(_request: Request) -> str:
        raise ToolError("x" * 128)

    tool = mcp._registry.get("oversized")
    assert tool is not None

    with pytest.raises(JsonRpcError, match="serialized result limit"):
        await mcp._invoke(tool, _request(), {})


@pytest.mark.parametrize(
    "content",
    (
        {},
        {"type": "resource", "uri": "file:///secret"},
        {"type": "resource", "data": "opaque"},
        {"type": "text", "text": 7},
        {"type": "image", "data": None},
    ),
)
def test_sampling_refuses_malformed_or_unsupported_content_blocks(
    content: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="content.*text.*image.*audio"):
        _sampling_messages([{"role": "user", "content": content}])


@pytest.mark.parametrize("role", ["system", "", True])
def test_sampling_refuses_roles_outside_user_and_assistant(role: object) -> None:
    with pytest.raises(ValueError, match="role.*user.*assistant"):
        _sampling_messages([{"role": role, "content": "hello"}])


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
        _session_id: str | None,
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


@pytest.mark.asyncio
async def test_concurrent_tool_calls_refuse_a_duplicate_request_id() -> None:
    mcp = _server()
    entered = asyncio.Event()
    release = asyncio.Event()

    @mcp.tool(description="Waits until the test releases it.")
    async def waiting(_request: Request) -> dict[str, bool]:
        entered.set()
        await release.wait()
        return {"released": True}

    session = _session()
    message = Message(
        "tools/call",
        {"name": "waiting"},
        id="same-id",
        is_request=True,
    )
    first = asyncio.create_task(mcp._tools_call(_request(), session, message))
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        with pytest.raises(JsonRpcError, match="already has a call in flight"):
            await asyncio.wait_for(
                mcp._tools_call(_request(), session, message),
                timeout=0.1,
            )
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_tool_call_refuses_an_id_used_by_another_request() -> None:
    mcp = _server()

    @mcp.tool(description="Does not run.")
    async def waiting(_request: Request) -> dict[str, bool]:
        return {"ran": True}

    session = _session()
    occupied = asyncio.create_task(asyncio.Event().wait())
    session.requests_in_flight["same-id"] = occupied
    try:
        with pytest.raises(JsonRpcError, match="already has a call in flight"):
            await mcp._tools_call(
                _request(),
                session,
                Message(
                    "tools/call",
                    {"name": "waiting"},
                    id="same-id",
                    is_request=True,
                ),
            )
    finally:
        occupied.cancel()
        await asyncio.gather(occupied, return_exceptions=True)


@pytest.mark.asyncio
async def test_integer_and_text_request_ids_do_not_share_progress_state() -> None:
    mcp = _server()
    task_ids: list[str] = []

    @mcp.tool(description="Records its progress identity.")
    async def identify_progress(request: Request) -> dict[str, bool]:
        task_ids.append(request.state.mcp.progress._task_id)
        return {"recorded": True}

    session = _session()
    for identifier in (1, "1"):
        await mcp._tools_call(
            _request(),
            session,
            Message(
                "tools/call",
                {"name": "identify_progress"},
                id=identifier,
                is_request=True,
            ),
        )

    assert len(set(task_ids)) == 2


@pytest.mark.asyncio
async def test_wrong_session_owner_does_not_refresh_the_idle_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()
    session = mcp._sessions.create(
        protocol_version="2025-06-18",
        client_info={},
    )
    previous = session.last_seen

    async def refuses_owner(
        _self: MCP,
        _request_value: Request,
        _session_value: Session,
        _identity: object,
    ) -> bool:
        return False

    monkeypatch.setattr(MCP, "_owns", refuses_owner)
    found, refusal = await mcp._session_for(_request(), None, session.id)

    assert found is None
    assert refusal is not None
    assert refusal.status == 401
    assert session.last_seen == previous


@pytest.mark.asyncio
async def test_session_disappearing_after_owner_check_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()
    session = mcp._sessions.create(
        protocol_version="2025-06-18",
        client_info={},
    )

    async def accepts_owner(
        _self: MCP,
        _request_value: Request,
        _session_value: Session,
        _identity: object,
    ) -> bool:
        return True

    monkeypatch.setattr(MCP, "_owns", accepts_owner)
    monkeypatch.setattr(type(mcp._sessions), "get", lambda _self, _identifier: None)
    found, refusal = await mcp._session_for(_request(), None, session.id)

    assert found is None
    assert refusal is not None
    assert refusal.status == 404


@pytest.mark.asyncio
async def test_session_replaced_after_owner_check_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()
    session = mcp._sessions.create(
        protocol_version="2025-06-18",
        client_info={},
    )
    replacement = Session(session.id, "2025-06-18")

    async def accepts_owner(
        _self: MCP,
        _request_value: Request,
        _session_value: Session,
        _identity: object,
    ) -> bool:
        return True

    monkeypatch.setattr(MCP, "_owns", accepts_owner)
    monkeypatch.setattr(
        type(mcp._sessions),
        "get",
        lambda _self, _identifier: replacement,
    )
    found, refusal = await mcp._session_for(_request(), None, session.id)

    assert found is None
    assert refusal is not None
    assert refusal.status == 404


@pytest.mark.asyncio
async def test_delete_does_not_discard_a_session_replaced_during_owner_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()
    session = mcp._sessions.create(
        protocol_version="2025-06-18",
        client_info={},
    )
    replacement = Session(session.id, "2025-06-18")
    seen = iter((session, replacement))
    discarded: list[str] = []

    async def accepts_owner(
        _self: MCP,
        _request_value: Request,
        _session_value: Session,
        _identity: object,
    ) -> bool:
        return True

    monkeypatch.setattr(MCP, "_owns", accepts_owner)
    monkeypatch.setattr(type(mcp._sessions), "peek", lambda _self, _identifier: next(seen))
    monkeypatch.setattr(
        type(mcp._sessions),
        "discard",
        lambda _self, identifier: discarded.append(identifier) or True,
    )
    request = Request(
        {"type": "http", "headers": [(b"mcp-session-id", session.id.encode())]},
        _no_receive,
    )

    response = await mcp._delete(request)

    assert response.status == 404
    assert discarded == []


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
async def test_roots_changed_during_a_list_request_discards_the_stale_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()
    session = _session()
    session.client_capabilities = {"roots": {"listChanged": True}}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def client_request(
        _self: MCP,
        _session_value: Session,
        _method: str,
        _params: dict[str, Any],
    ) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"roots": [{"uri": "file:///stale"}]}

    monkeypatch.setattr(MCP, "_client_request", client_request)
    pending = asyncio.create_task(mcp._roots(session))
    await asyncio.wait_for(entered.wait(), timeout=1)
    mcp._handle_notification(
        session,
        Message("notifications/roots/list_changed", {}, is_notification=True),
    )
    release.set()

    with pytest.raises(ClientRequestError, match="changed while roots/list was in flight"):
        await pending
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
    lock_enters = 0

    class CountingLock:
        async def __aenter__(self) -> None:
            nonlocal lock_enters
            lock_enters += 1

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(mcp, "_file_root_lock", CountingLock())
    session = _session()

    assert await mcp._read_file(session, "one.txt") == b"one.txt"
    assert await mcp._read_file(session, "two.txt") == b"two.txt"
    assert opened == [str(tmp_path)]
    assert [entry[:2] for entry in reads] == [(73, "one.txt"), (73, "two.txt")]
    assert lock_enters == 1


@pytest.mark.asyncio
async def test_concurrent_first_file_reads_open_one_root_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opened: list[str] = []

    def open_root(path: str) -> int:
        opened.append(path)
        time.sleep(0.05)
        return 73

    monkeypatch.setattr(server_module, "_open_root", open_root)
    monkeypatch.setattr(
        server_module,
        "read_beneath",
        lambda _fd, path, *, max_bytes: path.encode(),
    )
    mcp = _server(file_root=tmp_path)
    session = _session()

    assert await asyncio.gather(
        mcp._read_file(session, "one.txt"),
        mcp._read_file(session, "two.txt"),
    ) == [b"one.txt", b"two.txt"]
    assert opened == [str(tmp_path)]


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
async def test_policy_authorization_requires_exact_true() -> None:
    requirement = AuthRequirement(policies=(PolicyRequirement("record:read", 'Record::"one"'),))
    entry = _Gate("record", requirement)
    identity = Identity("ada")

    class TruthyAuthorizer:
        async def authorize(self, _request: Request, _policy: PolicyRequirement) -> Any:
            return SimpleNamespace(allowed="yes", reason="not an exact authorization")

    denied = await _server(authorizer=TruthyAuthorizer())._authorize(
        _request(identity), entry
    )

    assert denied == "the caller may not 'record:read': not an exact authorization"


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


@pytest.mark.parametrize("max_tokens", [0, -1, True, 1.5])
@pytest.mark.asyncio
async def test_sampling_refuses_a_nonpositive_or_noninteger_token_limit(
    max_tokens: Any,
) -> None:
    mcp = _server()

    @mcp.tool(description="Samples with an invalid bound.", sampling=True)
    async def sampler(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    context = ToolContext(
        "session",
        1,
        "sampler",
        _server=mcp,
        _session=_session(),
        _request=_request(),
    )

    with pytest.raises(ValueError, match="max_tokens.*positive integer"):
        await context.sample("hello", max_tokens=max_tokens)


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
    partition_keys: list[str | None] = []

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

    def throttle(
        _self: MCP,
        _tool: Any,
        _session: Session,
        partition_key: str | None,
    ) -> float:
        partition_keys.append(partition_key)
        return 0.0

    monkeypatch.setattr(MCP, "_identify", identify)
    monkeypatch.setattr(MCP, "_authorize", authorize)
    monkeypatch.setattr(MCP, "_client_request", client_request)
    monkeypatch.setattr(MCP, "_throttle", throttle)

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
    assert partition_keys[0] is None
    assert partition_keys[1] == "4:User0:ada"


@pytest.mark.asyncio
async def test_transport_rate_limits_isolate_identity_types() -> None:
    mcp = _server()

    @mcp.tool(description="One per hour.", rate_limit=ToolRateLimit(1, window=3600))
    async def limited(_request: Request) -> str:
        return "ok"

    session = _session()
    for identifier, identity in enumerate(
        (Identity("same", type="User"), Identity("same", type="Service")),
        start=1,
    ):
        result = await mcp._tools_call(
            _request(identity),
            session,
            Message(
                "tools/call",
                {"name": "limited"},
                id=identifier,
                is_request=True,
            ),
        )
        assert result["content"][0]["text"] == "ok"


@dataclass
class _OptionalAnswer:
    note: str = "default"


@pytest.mark.asyncio
async def test_elicitation_partitions_only_a_declared_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _server()

    @mcp.tool(description="Elicits without a limit.", elicitation=True)
    async def unbounded(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    @mcp.tool(
        description="Elicits with a limit.",
        elicitation=True,
        rate_limit=ToolRateLimit(1000),
    )
    async def bounded(_request: Request) -> dict[str, str]:
        return {"unused": "handler"}

    partition_keys: list[str | None] = []

    def throttle(
        _self: MCP,
        _tool: Any,
        _session: Session,
        partition_key: str | None,
    ) -> float:
        partition_keys.append(partition_key)
        return 0.0

    async def client_request(
        _self: MCP,
        _session: Session,
        _method: str,
        _params: dict[str, Any],
    ) -> dict[str, str]:
        return {"action": "decline"}

    monkeypatch.setattr(MCP, "_throttle", throttle)
    monkeypatch.setattr(MCP, "_client_request", client_request)
    identity = Identity("ada", type="Service")
    for name in ("unbounded", "bounded"):
        context = ToolContext(
            "session",
            name,
            name,
            identity=identity,
            _server=mcp,
            _session=_session(),
            _request=_request(identity),
        )
        assert await context.elicit("Answer", _OptionalAnswer) is None

    assert partition_keys == [None, "7:Service0:ada"]


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
