from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, cast

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._auth.cedar import CedarAuthorizer
from wreath._auth.cedar_engine import CedarPolicies
from wreath._auth.models import AuthorizationDecision, Identity
from wreath._auth.principal import Narrowing, human
from wreath._mcp.executor import ToolAuthorizationError, ToolRateLimitError
from wreath.binding import Body
from wreath.mcp import MCP, ToolError, ToolRateLimit


@dataclass
class Query:
    value: str


def configured_mcp(**kwargs: Any) -> MCP:
    mcp = MCP(name="agents", version="1", **kwargs)

    @mcp.tool(description="Echo one value.")
    async def echo(request: Any, query: Annotated[Query, Body()]) -> dict[str, Any]:
        invocation = request.state.agent_tool
        request.state.mcp.progress.update(25, "bound")
        return {
            "value": query.value,
            "tenant": invocation.tenant,
            "call_id": invocation.call_id,
            "effect_id": invocation.effect_id,
            "identity": request.identity.id,
            "metadata": invocation.metadata,
            "actor": request.identity.narrowing.actor,
            "progress": request.state.mcp.progress is not None,
        }

    @mcp.tool(description="Do not expose this tool to the agent.")
    async def hidden(_request: Any) -> str:
        return "hidden"

    return mcp


def test_selection_compiles_a_bounded_snapshot_of_registry_schemas() -> None:
    mcp = configured_mcp()

    executor = mcp.executor("echo", max_tools=1)

    assert [specification.name for specification in executor.specifications] == ["echo"]
    assert executor.specifications[0].input_schema is mcp.tools[0].input_schema
    assert executor.specifications[0].description == "Echo one value."
    assert executor.tool_names == frozenset({"echo"})


@pytest.mark.parametrize(
    ("names", "max_tools", "message"),
    [
        (("missing",), 1, "unknown MCP tool 'missing'"),
        (("echo", "echo"), 2, "selected more than once"),
        (("echo", "hidden"), 1, "at most 1"),
        (("echo",), 0, "max_tools must be at least 1"),
    ],
)
def test_invalid_agent_tool_selections_refuse_at_construction(
    names: tuple[Any, ...], max_tools: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        configured_mcp().executor(*names, max_tools=max_tools)


@pytest.mark.parametrize("max_tools", [True, 1.5, float("nan"), float("inf")])
def test_agent_tool_selection_refuses_non_integer_limits(max_tools: object) -> None:
    with pytest.raises(TypeError, match="max_tools must be an integer"):
        configured_mcp().executor("echo", max_tools=cast(Any, max_tools))


def test_agent_tool_selection_refuses_an_empty_name() -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        configured_mcp().executor("")


def test_agent_tool_selection_refuses_a_non_string_name() -> None:
    invalid_name: Any = 7
    with pytest.raises(ValueError, match="non-empty strings"):
        configured_mcp().executor(invalid_name)


async def test_direct_invocation_binds_without_json_and_publishes_durable_effect_identity() -> None:
    mcp = configured_mcp()
    executor = mcp.executor("echo")
    identity = Identity("user-7")
    delegation = Narrowing(
        actor="agent-3",
        scope=frozenset({"Record::read"}),
        on_behalf_of="user-7",
    )

    result = await executor.invoke(
        "echo",
        {"query": {"value": "direct"}},
        tenant="tenant-a",
        principal=identity,
        delegation=delegation,
        call_id="job-4:step-2",
        metadata={"source": "job"},
    )

    assert result.is_error is False
    assert result.structured_content == {
        "value": "direct",
        "tenant": "tenant-a",
        "call_id": "job-4:step-2",
        "effect_id": "mcp:8:tenant-a:6:user-7:12:job-4:step-2:echo",
        "identity": "user-7",
        "metadata": {"source": "job"},
        "actor": "agent-3",
        "progress": True,
    }
    assert result.effect_id == "mcp:8:tenant-a:6:user-7:12:job-4:step-2:echo"
    progress = mcp.progress.get("agent-tool:mcp:8:tenant-a:6:user-7:12:job-4:step-2:echo")
    assert progress is not None
    assert (progress.percent, progress.message) == (25, "bound")


async def test_direct_invocation_binds_a_composed_principal_once() -> None:
    executor = configured_mcp().executor("echo")
    principal = human(Identity("user-7")).narrow(actor="agent-3", scope={"Record::read"}, ttl=300)

    result = await executor.invoke(
        "echo",
        {"query": {"value": "direct"}},
        tenant="tenant-a",
        principal=principal,
        delegation=principal.narrowing,
        call_id="call-1",
    )

    assert result.structured_content["actor"] == "agent-3"
    assert result.structured_content["identity"] == "user-7"


class Authorizer:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.requests: list[Any] = []

    async def authorize(self, request: Any, requirement: Any) -> AuthorizationDecision:
        self.requests.append((request, requirement))
        narrowing = request.identity.narrowing
        if narrowing is not None and not narrowing.permits(requirement.action):
            return AuthorizationDecision(False, "delegation scope does not cover this action")
        return AuthorizationDecision(self.allowed, None if self.allowed else "outside tenant")


class PausingAuthorizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.seen: str | None = None

    async def authorize(self, request: Any, _requirement: Any) -> AuthorizationDecision:
        self.started.set()
        await self.proceed.wait()
        self.seen = request.state.mcp.arguments["query"]["value"]
        return AuthorizationDecision(True)


async def test_direct_invocation_snapshots_arguments_before_authorization() -> None:
    authorizer = PausingAuthorizer()
    mcp = MCP(name="agents", version="1", authorizer=authorizer)

    @mcp.tool(description="Read a record.", action="Record::read", resource='Record::"7"')
    async def read(_request: Any, query: Annotated[Query, Body()]) -> str:
        return query.value

    arguments = {"query": {"value": "allowed"}}
    task = asyncio.create_task(
        mcp.executor("read").invoke(
            "read",
            arguments,
            tenant="tenant-a",
            principal=Identity("user-7"),
            delegation=None,
            call_id="call-1",
        )
    )
    await authorizer.started.wait()
    arguments["query"]["value"] = "switched"
    authorizer.proceed.set()

    result = await task

    assert authorizer.seen == "allowed"
    assert result.text == "allowed"


async def test_direct_invocation_reuses_cedar_and_delegation_scope() -> None:
    authorizer = Authorizer(True)
    mcp = MCP(name="agents", version="1", authorizer=authorizer)

    @mcp.tool(description="Read a record.", action="Record::read", resource='Record::"7"')
    async def read(_request: Any) -> str:
        return "ok"

    executor = mcp.executor("read")
    identity = Identity("user-7")
    allowed = Narrowing(actor="agent", scope=frozenset({"Record::read"}))
    denied = Narrowing(actor="agent", scope=frozenset({"Record::write"}))

    result = await executor.invoke(
        "read", {}, tenant="a", principal=identity, delegation=allowed, call_id="1"
    )
    assert result.text == "ok"
    assert authorizer.requests[0][0].identity.narrowing is allowed

    with pytest.raises(ToolAuthorizationError, match="delegation scope"):
        await executor.invoke(
            "read", {}, tenant="a", principal=identity, delegation=denied, call_id="2"
        )


async def test_direct_invocation_runs_through_real_cedar_policies() -> None:
    authorizer = CedarAuthorizer(
        engine=CedarPolicies(
            'permit (principal == User::"user-7", action == Action::"Record::read", resource);'
        )
    )
    mcp = MCP(name="agents", version="1", authorizer=authorizer)

    @mcp.tool(description="Read a record.", action="Record::read", resource='Record::"7"')
    async def read(_request: Any) -> str:
        return "allowed"

    executor = mcp.executor("read")
    allowed = Narrowing(actor="agent", scope=frozenset({"Record::read"}))

    result = await executor.invoke(
        "read", {}, tenant="a", principal=Identity("user-7"), delegation=allowed, call_id="1"
    )
    assert result.text == "allowed"

    with pytest.raises(ToolAuthorizationError, match="no permit policy matched"):
        await executor.invoke(
            "read",
            {},
            tenant="a",
            principal=Identity("user-8"),
            delegation=allowed,
            call_id="2",
        )


async def test_direct_invocation_reuses_second_factor_requirement() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Delete a record.", second_factor=300)
    async def delete(_request: Any) -> str:
        return "deleted"

    executor = mcp.executor("delete")
    recent = Identity("user-7", claims={"second_factor_at": time.time() - 60})
    stale = Identity("user-7", claims={"second_factor_at": time.time() - 600})

    result = await executor.invoke(
        "delete", {}, tenant="a", principal=recent, delegation=None, call_id="1"
    )
    assert result.text == "deleted"

    with pytest.raises(ToolAuthorizationError, match="second factor"):
        await executor.invoke(
            "delete", {}, tenant="a", principal=stale, delegation=None, call_id="2"
        )


async def test_direct_invocation_reuses_tool_error_normalization() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Refuse visibly.")
    async def refuse(_request: Any) -> str:
        raise ToolError("choose a smaller range")

    @mcp.tool(description="Fail privately.")
    async def explode(_request: Any) -> str:
        raise RuntimeError("database password")

    executor = mcp.executor("refuse", "explode")

    refusal = await executor.invoke(
        "refuse", {}, tenant="a", principal=Identity("u"), delegation=None, call_id="1"
    )
    failure = await executor.invoke(
        "explode", {}, tenant="a", principal=Identity("u"), delegation=None, call_id="2"
    )

    assert (refusal.is_error, refusal.text) == (True, "choose a smaller range")
    assert (failure.is_error, failure.text) == (True, "the tool raised RuntimeError")


async def test_direct_invocation_keeps_mcp_recording_and_redaction_semantics() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Use a credential.")
    async def sign_in(_request: Any, username: str, password: str) -> str:
        return username

    with log.testing_runtime() as records, log.request_scope(request_id=7) as scope:
        await mcp.executor("sign_in").invoke(
            "sign_in",
            {"username": "ada", "password": "correct-horse-battery-staple"},
            tenant="tenant-a",
            principal=Identity("user-7"),
            delegation=None,
            call_id="job-1",
        )
        scope.finish(promoted=True)
        entries = [
            log.attributes(cell) for cell in records if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]
        fields: dict[str, Any] = {}
        for cell in records:
            if cell.flags & fs.LOG_FLAG_EVENT_FIELDS:
                fields.update(log.attributes(cell))

    assert entries[0]["tool"] == "sign_in"
    assert (entries[0]["outcome"], entries[0]["principal"]) == ("ok", "user-7")
    assert fields["mcp.arg.password"] == "<redacted>"
    assert "correct-horse-battery-staple" not in repr(entries) + repr(fields)


def test_direct_catalog_refuses_tools_that_require_mcp_client_round_trips() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Attempts sampling.", sampling=True)
    async def sampler(request: Any) -> str:
        await request.state.mcp.sample("hello")
        return "unreachable"

    @mcp.tool(description="Attempts elicitation.", elicitation=True)
    async def elicitor(request: Any) -> str:
        await request.state.mcp.elicit("hello", Query)
        return "unreachable"

    for name in ("sampler", "elicitor"):
        with pytest.raises(ValueError, match="client session.*cannot be selected"):
            mcp.executor(name)


@pytest.mark.parametrize("operation", ["sample", "elicit", "roots", "read_file"])
async def test_direct_context_refuses_operations_that_need_an_mcp_client(operation: str) -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Attempts a client operation.")
    async def outbound(request: Any) -> str:
        context = request.state.mcp
        if operation == "sample":
            await context.sample("hello")
        elif operation == "elicit":
            await context.elicit("hello", Query)
        elif operation == "roots":
            await context.roots()
        else:
            await context.read_file("file.txt")
        return "unexpected"

    result = await mcp.executor("outbound").invoke(
        "outbound", {}, tenant="a", principal=Identity("u"), delegation=None, call_id="1"
    )

    assert (result.is_error, result.text) == (True, "the tool raised ClientRequestError")


async def test_effect_identity_is_unambiguous_across_delimited_tenant_and_call_ids() -> None:
    executor = configured_mcp().executor("echo")
    identity = Identity("u")

    left = await executor.invoke(
        "echo",
        {"query": {"value": "x"}},
        tenant="a:b",
        principal=identity,
        delegation=Narrowing(actor="agent"),
        call_id="c",
    )
    right = await executor.invoke(
        "echo",
        {"query": {"value": "x"}},
        tenant="a",
        principal=identity,
        delegation=Narrowing(actor="agent"),
        call_id="b:c",
    )

    assert left.effect_id != right.effect_id


async def test_effect_identity_is_isolated_by_resolved_principal() -> None:
    mcp = configured_mcp()
    executor = mcp.executor("echo")

    alice = await executor.invoke(
        "echo",
        {"query": {"value": "x"}},
        tenant="tenant-a",
        principal=Identity("alice"),
        delegation=None,
        call_id="call-1",
    )
    bob = await executor.invoke(
        "echo",
        {"query": {"value": "x"}},
        tenant="tenant-a",
        principal=Identity("bob"),
        delegation=None,
        call_id="call-1",
    )

    assert alice.effect_id != bob.effect_id
    assert mcp.progress.get(f"agent-tool:{alice.effect_id}") is not None
    assert mcp.progress.get(f"agent-tool:{bob.effect_id}") is not None


async def test_anonymous_effect_identity_does_not_collide_with_the_text_none() -> None:
    executor = configured_mcp().executor("echo")
    common = {
        "name": "echo",
        "arguments": {"query": {"value": "x"}},
        "tenant": "tenant-a",
        "delegation": None,
        "call_id": "call-1",
    }

    anonymous = await executor.invoke(principal=None, **common)
    named = await executor.invoke(principal=Identity("None"), **common)

    assert anonymous.effect_id != named.effect_id


async def test_direct_rate_limits_are_isolated_by_tenant_and_principal() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="One per hour.", rate_limit=ToolRateLimit(1, window=3600))
    async def limited(_request: Any) -> str:
        return "ok"

    executor = mcp.executor("limited")
    identity = Identity("same-user")
    common = {"principal": identity, "delegation": None}

    assert (await executor.invoke("limited", {}, tenant="a", call_id="1", **common)).text == "ok"
    with pytest.raises(ToolRateLimitError) as caught:
        await executor.invoke("limited", {}, tenant="a", call_id="2", **common)
    assert caught.value.retry_after > 0
    assert (await executor.invoke("limited", {}, tenant="b", call_id="3", **common)).text == "ok"
    assert (
        await executor.invoke(
            "limited", {}, tenant="a", principal=None, delegation=None, call_id="4"
        )
    ).text == "ok"
    with pytest.raises(ToolRateLimitError):
        await executor.invoke(
            "limited", {}, tenant="a", principal=None, delegation=None, call_id="5"
        )
    assert (
        await executor.invoke(
            "limited",
            {},
            tenant="a",
            principal=Identity("None"),
            delegation=None,
            call_id="6",
        )
    ).text == "ok"


async def test_direct_rate_limits_isolate_identity_types_and_namespaces() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="One per hour.", rate_limit=ToolRateLimit(1, window=3600))
    async def limited(_request: Any) -> str:
        return "ok"

    executor = mcp.executor("limited")
    identities = (
        Identity("same", type="User", namespace="issuer-a"),
        Identity("same", type="Service", namespace="issuer-a"),
        Identity("same", type="User", namespace="issuer-b"),
    )

    for index, identity in enumerate(identities):
        result = await executor.invoke(
            "limited",
            {},
            tenant="tenant-a",
            principal=identity,
            delegation=None,
            call_id=str(index),
        )
        assert result.text == "ok"


async def test_direct_invocation_refuses_ambiguous_or_invalid_identity_inputs() -> None:
    executor = configured_mcp().executor("echo")
    existing = Narrowing(actor="first")
    identity = Identity("u", narrowing=existing)

    with pytest.raises(ValueError, match="different delegation"):
        await executor.invoke(
            "echo",
            {"query": {"value": "x"}},
            tenant="a",
            principal=identity,
            delegation=Narrowing(actor="second"),
            call_id="1",
        )
    with pytest.raises(ValueError, match="tenant must be a non-empty string"):
        await executor.invoke(
            "echo",
            {"query": {"value": "x"}},
            tenant="",
            principal=Identity("u"),
            delegation=None,
            call_id="1",
        )
    with pytest.raises(ValueError, match="call_id must be a non-empty string"):
        await executor.invoke(
            "echo",
            {"query": {"value": "x"}},
            tenant="a",
            principal=Identity("u"),
            delegation=None,
            call_id="",
        )
    with pytest.raises(ValueError, match="tenant must be a non-empty string"):
        await executor.invoke(
            "echo",
            {"query": {"value": "x"}},
            tenant=7,
            principal=Identity("u"),
            delegation=None,
            call_id="1",
        )
    with pytest.raises(ValueError, match="call_id must be a non-empty string"):
        await executor.invoke(
            "echo",
            {"query": {"value": "x"}},
            tenant="a",
            principal=Identity("u"),
            delegation=None,
            call_id=7,
        )
    with pytest.raises(TypeError, match="arguments must be a mapping"):
        await executor.invoke(
            "echo", [], tenant="a", principal=Identity("u"), delegation=None, call_id="1"
        )
    with pytest.raises(ValueError, match="not in this agent's selected catalog"):
        await executor.invoke(
            "hidden", {}, tenant="a", principal=Identity("u"), delegation=None, call_id="1"
        )
    with pytest.raises(ValueError, match="delegation requires a principal"):
        await executor.invoke(
            "echo",
            {"query": {"value": "x"}},
            tenant="a",
            principal=None,
            delegation=Narrowing(actor="agent"),
            call_id="1",
        )
    with pytest.raises(ValueError, match="Wreath Identity or Principal"):
        await executor.invoke(
            "echo",
            {"query": {"value": "x"}},
            tenant="a",
            principal=SimpleNamespace(narrowing=None),
            delegation=Narrowing(actor="agent"),
            call_id="1",
        )


async def test_permission_denial_is_a_governance_refusal() -> None:
    mcp = MCP(name="agents", version="1")

    @mcp.tool(description="Needs permission.")
    async def plain(_request: Any) -> str:
        return "ok"

    tool = mcp.tools[0]
    object.__setattr__(
        tool,
        "requirement",
        SimpleNamespace(
            access_level=1,
            second_factor=None,
            role_checks=(),
            permission_checks=(SimpleNamespace(mode="all", values={"record:read"}),),
            policies=(),
        ),
    )
    executor = mcp.executor("plain")

    with pytest.raises(ToolAuthorizationError, match="permissions"):
        await executor.invoke(
            "plain", {}, tenant="a", principal=Identity("u"), delegation=None, call_id="1"
        )
