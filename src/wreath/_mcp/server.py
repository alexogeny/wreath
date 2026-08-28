"""The MCP endpoint: one route per HTTP method, and the dispatch behind it.

An MCP call is a route activation like any other. The endpoint is registered
with `app.post(...)`, so request limits, middleware, the exception boundary and
every observability hook apply to it unchanged, and nothing here reimplements
ingress, header parsing, or JSON. What is left -- classifying an envelope,
deciding whether the caller may make this call, looking a tool up, validating
its arguments, and running it -- is once-per-call orchestration in Python, which
is where this codebase puts once-per-call work.

Transport is **streamable HTTP**, the current MCP transport. The legacy
two-endpoint HTTP+SSE transport is deliberately not implemented; see the guide.

Four things can stop a `tools/call`, and they are four different facts about a
deployment, so they are four counters and four outcomes on the record:
`schema_rejections` (the caller's arguments were wrong), `throttled` (the caller
asked too often), `unauthorized_calls` (the caller was told no), and
`tool_errors` (the tool itself failed). Collapsing any pair of them would hide
which half of a system is broken -- the same reason `messaging.MessageBus` keeps
`doorbell_reconnects` apart from `handler_errors`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from itertools import chain
from typing import Any
from urllib.parse import urlsplit

from .._auth.requirements import AuthRequirement, second_factor_age
from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from ..binding import ValidationError
from ..progress import ProgressRegistry
from ..request import Request
from ..response import Response, ServerSentEvent, SSEResponse
from . import record as _record
from .auth import MCPAuth, Unauthenticated, metadata_path_for
from .completion import complete as _complete
from .elicit import form_schema
from .limits import DEFAULT_LIMITS, MCPLimits, ToolRateLimit
from .outbound import ClientRequestError
from .prompts import Prompt, PromptRegistry, build_prompt, render_messages
from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    RATE_LIMITED,
    RESOURCE_NOT_FOUND,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOO_MANY_CALLS,
    UNAUTHORIZED,
    JsonRpcError,
    Message,
    encode_failure,
    encode_success,
    parse_message,
)
from .registry import Tool, ToolRegistry, actions_by_type, bind_arguments, build_tool
from .resources import Resource, ResourceRegistry, build_resource, read_result
from .roots import ContainmentError, beneath_any, open_root, read_beneath, root_paths
from .session import CLOSE_STREAM, Session, SessionStore, ToolContext

#: Returned by dispatch when a call was cancelled. The specification says a
#: cancelled request's response must not be sent, so there is nothing to encode
#: and the POST answers 202 with no body.
_SUPPRESSED = object()

#: Methods this revision defines that Wreath has not implemented yet, and the
#: stage each is planned for. Answering `method not found` is correct either
#: way; naming the stage is the difference between a client author guessing and
#: knowing. See `docs/plans/native-mcp-server.md`.
_NOT_YET: dict[str, str] = {
    "logging/setLevel": "logging",
}

#: Methods that exist, and go the other way. A client that POSTs one of these
#: has confused the direction -- usually by pointing a server implementation at
#: a server -- and telling it so is far more useful than "unknown method", which
#: reads as "this build is too old".
_CLIENT_ONLY: frozenset[str] = frozenset(
    ("sampling/createMessage", "elicitation/create", "roots/list")
)

#: Client capabilities a server-to-client request needs, by method. A client
#: that did not advertise one is refused before anything is sent, because the
#: alternative is a request nothing will ever answer and a tool parked until the
#: timeout -- a hang whose cause is invisible from either end.
_REQUIRES_CAPABILITY: dict[str, str] = {
    "sampling/createMessage": "sampling",
    "elicitation/create": "elicitation",
    "roots/list": "roots",
}


@dataclasses.dataclass(frozen=True, slots=True)
class _Gate:
    """One `AuthRequirement` and the name a refusal should call it.

    `_authorize` decides over these rather than over tools, resources and
    prompts specifically, so a *second* requirement on a tool -- may this one
    ask the caller's model to generate? -- is decided by the same code and the
    same authorizer as the first, with nothing duplicated to keep in step.
    """

    name: str
    requirement: AuthRequirement


class ToolError(Exception):
    """A tool could not do its job, reported to the model rather than the wire.

    This is the difference that makes an MCP server usable. A JSON-RPC error
    tells the client that the *call* failed, which a model can only retry
    blindly; a `ToolError` comes back as an ordinary result carrying
    `isError: true` and your message, which a model can read and act on. Raise
    it for "no sighting matched that species", not for "the database is down".

        raise ToolError("no camera covers that trail; try 'ridge' or 'creek'")
    """

    __slots__ = ()


def _accepts(header: str | None) -> tuple[bool, bool]:
    """Whether the client will take JSON, and whether it will take an SSE stream."""
    if not header:
        # A client that names nothing gets JSON. The specification asks a client
        # to send both types; refusing the ones that forget would fail them at
        # the transport, where the error is hardest to read.
        return True, False
    wants_json = False
    wants_sse = False
    for part in header.split(","):
        media = part.split(";")[0].strip().lower()
        if media in ("application/json", "application/*", "*/*"):
            wants_json = True
        if media in ("text/event-stream", "text/*", "*/*"):
            wants_sse = True
    return wants_json, wants_sse


async def _single_event(body: bytes) -> AsyncIterator[ServerSentEvent]:
    """Frame one JSON-RPC reply as a single SSE `message` event and close.

    Compact JSON carries no newline, so this is one `data:` line, and the stream
    ends with the reply. A POST's stream carries that POST's answer and nothing
    else: everything the server sends unasked -- progress, a subscribed resource
    changing -- travels on the session's own `GET` stream, so a client is never
    left holding a request stream open waiting for something that is going
    somewhere else.
    """
    yield ServerSentEvent(body, event="message")


def _holds(actual: Any, check: Any) -> bool:
    """Whether an identity's roles or permissions satisfy one `SetRequirement`.

    `issubset`/`isdisjoint` rather than the operator forms, exactly as the
    application's own enforcement does: the operators demand a set on the right
    and raise for anything else, and a backend that passes a roles claim through
    unconverted hands over a list.
    """
    if check.mode == "all":
        return check.values.issubset(actual)
    return not check.values.isdisjoint(actual)


def _notification(method: str, params: dict[str, Any]) -> bytes:
    """One server-to-client JSON-RPC notification, framed once for every reader."""
    return _json_dumps({"jsonrpc": "2.0", "method": method, "params": params})


def _text_result(text: str, *, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _render_result(value: Any) -> dict[str, Any]:
    """Turn a tool's return value into an MCP `tools/call` result."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, str):
        return _text_result(value, is_error=False)
    result = _text_result(_json_dumps(value).decode("utf-8"), is_error=False)
    if isinstance(value, Mapping):
        # A mapping is the one return shape a client can consume without
        # re-parsing the text block, so it travels as both.
        result["structuredContent"] = value
    return result


def _origin(url: str) -> str:
    """Scheme and authority of `url`, with no trailing slash."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _sampling_messages(messages: Any) -> list[dict[str, Any]]:
    """What a tool passed to `sample()`, in the shape the specification carries.

    A bare string is one `user` turn, because that is what nine calls in ten
    are, and making every caller spell out the envelope would be a wire format
    leaking into application code.
    """
    if isinstance(messages, str):
        return [{"role": "user", "content": {"type": "text", "text": messages}}]
    rendered: list[dict[str, Any]] = []
    for entry in messages:
        if not isinstance(entry, Mapping):
            raise TypeError(
                "a sampling message must be a mapping with `role` and "
                f"`content`; got {type(entry).__name__}"
            )
        content = entry.get("content")
        if isinstance(content, str):
            content = {"type": "text", "text": content}
        elif not isinstance(content, Mapping):
            raise TypeError(
                "a sampling message's `content` must be text or a content "
                f"block; got {type(content).__name__}"
            )
        rendered.append({"role": entry.get("role", "user"), "content": dict(content)})
    return rendered


def _open_root(directory: str) -> int:
    """Open the confinement root, refusing with a message a deployment can act on."""
    try:
        return open_root(directory)
    except (ContainmentError, OSError) as error:
        raise RuntimeError(
            f"the MCP server's `file_root={directory!r}` could not be opened as "
            f"a confinement root: {error}"
        ) from error


class MCP:
    """A Model Context Protocol server mounted on a Wreath application.

    Declare it once, decorate the callables you want a model to be able to
    invoke, and the endpoint is a route:

        app = Wreath()
        mcp = MCP(app, name="camera-trap", version="1.0.0")

        @mcp.tool(description="Find recent sightings of a species.")
        async def find_sightings(request, query: Annotated[SightingQuery, Body()]) -> dict:
            ...

    There is no second declaration syntax and no second validator. A tool's
    `inputSchema` is derived from the same annotations that would bind an HTTP
    request body, by the same code that renders the OpenAPI document, so the
    schema a model reads and the validation a call meets cannot disagree.

    Pass `auth=MCPAuth(...)` to run it as an OAuth 2.1 resource server: the
    endpoint then publishes protected-resource metadata, refuses a request with
    no bearer token by pointing at that metadata, and refuses a token minted for
    a *different* resource -- which is the failure the specification exists to
    prevent. Without it the endpoint is exactly as protected as the route is,
    which for a bare `Wreath()` means not at all.

    Args:
        app: The application (or `Router`) to mount on. Pass None to mount later
            with `mount()`.
        name: The server name reported in `initialize`.
        version: The server version reported in `initialize`.
        path: The single endpoint path. POST carries every client message;
            DELETE ends a session.
        instructions: Optional guidance returned from `initialize`, describing
            how a model should use this server as a whole.
        auth: The OAuth 2.1 resource-server configuration, or None to install no
            authentication of this server's own.
        limits: The bounds this server runs inside. Payload size is deliberately
            not among them: it is `RequestLimits.max_body_bytes` on the app,
            where every other request's size is already decided.
        authorizer: The policy authorizer for Cedar-gated tools. Defaults to the
            one `app.configure_auth(...)` installed, which is what you want
            unless this server is mounted on a bare `Router`.
        progress: The registry a running tool reports progress to. A
            `ProgressRegistry(bus)` makes it fleet-wide.
        progress_interval: How often the relay polls that registry, in seconds.
        file_root: The one directory `request.state.mcp.read_file(...)` may read
            beneath, opened once as a trusted descriptor and walked component by
            component through `wreath._fsguard` -- the same confinement static
            files and the template loader use, refusing every symlink. Without
            it a tool cannot read a file at all, which is the right default for
            a surface a model drives. A client's declared `roots` narrow it
            further; they never widen it.
    """

    __slots__ = (
        "_app",
        "_auth",
        "_authorizer",
        "_file_root",
        "_file_root_fd",
        "_instructions",
        "_limits",
        "_metadata_path",
        "_metadata_url",
        "_name",
        "_path",
        "_progress",
        "_progress_interval",
        "_prompts",
        "_registry",
        "_resources",
        "_sessions",
        "_version",
        "client_request_timeouts",
        "elicitation_declines",
        "elicitation_refusals",
        "elicitations",
        "notifications_dropped",
        "prompt_errors",
        "prompt_renders",
        "resource_errors",
        "resource_reads",
        "roots_refusals",
        "sampling_refusals",
        "sampling_requests",
        "schema_rejections",
        "throttled",
        "tool_calls",
        "tool_errors",
        "unauthorized_calls",
    )

    def __init__(
        self,
        app: Any = None,
        *,
        name: str,
        version: str,
        path: str = "/mcp",
        instructions: str | None = None,
        auth: MCPAuth | None = None,
        limits: MCPLimits = DEFAULT_LIMITS,
        authorizer: Any = None,
        progress: ProgressRegistry | None = None,
        progress_interval: float = 0.25,
        file_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if progress_interval <= 0:
            raise ValueError("progress_interval must be positive")
        self._file_root = None if file_root is None else os.fspath(file_root)
        self._file_root_fd: int | None = None
        self._name = name
        self._version = version
        self._path = path
        self._instructions = instructions
        self._auth = auth
        self._limits = limits
        self._authorizer = authorizer
        self._app: Any = None
        self._registry = ToolRegistry(max_tools=limits.max_tools)
        self._resources = ResourceRegistry(max_resources=limits.max_resources)
        self._prompts = PromptRegistry(max_prompts=limits.max_prompts)
        # A registry of Wreath's own, unless the deployment gave one -- and
        # giving one is how progress crosses workers, because a
        # `ProgressRegistry(bus)` is fleet-wide and a durable job reporting from
        # worker 3 then reaches the client connected to worker 1.
        self._progress = ProgressRegistry() if progress is None else progress
        self._progress_interval = progress_interval
        self._sessions = SessionStore(
            max_sessions=limits.max_sessions,
            idle_seconds=limits.session_idle_seconds,
            max_pending_notifications=limits.max_pending_notifications,
            max_pending_requests=limits.max_pending_requests,
            client_request_seconds=limits.client_request_seconds,
            # One funnel, so a server-to-client *request* that could not be
            # queued is counted exactly where a dropped notification is.
            publish=self._publish,
        )
        self._metadata_path = metadata_path_for(path)
        self._metadata_url = (
            "" if auth is None else _origin(auth.resource) + self._metadata_path
        )
        #: Calls attempted, including the ones that failed.
        self.tool_calls = 0
        #: Calls whose tool raised. A rejected call is *not* counted here --
        #: an argument that failed validation never reached the tool, and
        #: conflating the two hides which half of a deployment is broken.
        self.tool_errors = 0
        #: Calls refused before invocation because the arguments did not match
        #: the published `inputSchema`.
        self.schema_rejections = 0
        #: Calls refused by a tool's Cedar policy, or made against a gated tool
        #: with no authorizer configured. A refusal, never a failure.
        self.unauthorized_calls = 0
        #: Calls refused by a tool's rate limit, or by the per-session ceiling
        #: on concurrent calls. Named as `middleware.ratelimit` names it.
        self.throttled = 0
        #: `resources/read` calls that reached a reader, including failed ones.
        self.resource_reads = 0
        #: Reads whose reader raised. A denied read is *not* counted here; it is
        #: a refusal, and it lands in `unauthorized_calls` with every other one.
        self.resource_errors = 0
        #: `prompts/get` calls that reached a handler, including failed ones.
        self.prompt_renders = 0
        #: Renders whose handler raised, or which returned something that is not
        #: a message.
        self.prompt_errors = 0
        #: Notifications lost because a session's queue was full -- nobody was
        #: reading its stream, or not fast enough. Counted rather than swallowed:
        #: a client whose progress bar never moves and a server that is dropping
        #: what it sends look identical until this number is read.
        self.notifications_dropped = 0
        #: `sampling/createMessage` requests that reached the client.
        self.sampling_requests = 0
        #: Sampling a tool asked for and did not get: it declared no
        #: `sampling=`, its policy refused, its rate limit refused, or the
        #: client never advertised the capability. A refusal, never a failure --
        #: the same separation `tool_errors` keeps from `unauthorized_calls`.
        self.sampling_refusals = 0
        #: `elicitation/create` requests that reached the client.
        self.elicitations = 0
        #: Elicitations the person declined or cancelled. Not an error at any
        #: level: a form a human said no to is the mechanism working.
        self.elicitation_declines = 0
        #: Prompts a tool wanted to put in front of a person and did not get to:
        #: it declared no `elicitation=`, its policy refused, its rate limit
        #: refused, or the client never advertised the capability. Read this
        #: one: an application probing for a form it is not allowed to show is
        #: the signal that something is trying to phish through your client's
        #: own chrome, and it is a refusal rather than a failure.
        self.elicitation_refusals = 0
        #: Server-to-client requests nobody answered in time. Counted apart from
        #: everything else because it is the one failure whose cause is entirely
        #: on the other side of the connection.
        self.client_request_timeouts = 0
        #: Filesystem reads refused for lying outside the roots the client
        #: declared -- as distinct from outside the server's own `file_root=`,
        #: which is a `PermissionError` from the containment walk. Counted so a
        #: deployment can tell "the client's workspace does not cover this" from
        #: "this server was never allowed to read it".
        self.roots_refusals = 0
        if app is not None:
            self.mount(app)

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def path(self) -> str:
        return self._path

    @property
    def limits(self) -> MCPLimits:
        return self._limits

    @property
    def auth(self) -> MCPAuth | None:
        return self._auth

    @property
    def metadata_path(self) -> str:
        """Where this server's protected-resource metadata is served."""
        return self._metadata_path

    @property
    def metadata_url(self) -> str:
        """The absolute metadata URL a `401` challenge names, or `""`."""
        return self._metadata_url

    @property
    def sessions(self) -> int:
        """Live sessions right now, expired ones already collected."""
        self._sessions.sweep()
        return len(self._sessions)

    @property
    def expired_sessions(self) -> int:
        """Sessions collected for going idle since this server started."""
        return self._sessions.expired

    def stats(self) -> dict[str, int]:
        """Every counter this server keeps, by name.

        The same shape `messaging.MessageBus.stats()` returns, and for the same
        reason: read one attribute at a time, an exporter has to know each name
        and gains nothing when one is added. `counters()` layers this mapping
        onto the canonical metrics protocol; mounting on a `Wreath` application
        registers it for Prometheus, OpenMetrics, StatsD and CloudWatch.
        """
        return {
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "schema_rejections": self.schema_rejections,
            "unauthorized_calls": self.unauthorized_calls,
            "throttled": self.throttled,
            "expired_sessions": self.expired_sessions,
            "resource_reads": self.resource_reads,
            "resource_errors": self.resource_errors,
            "prompt_renders": self.prompt_renders,
            "prompt_errors": self.prompt_errors,
            "notifications_dropped": self.notifications_dropped,
            "sampling_requests": self.sampling_requests,
            "sampling_refusals": self.sampling_refusals,
            "elicitations": self.elicitations,
            "elicitation_declines": self.elicitation_declines,
            "elicitation_refusals": self.elicitation_refusals,
            "client_request_timeouts": self.client_request_timeouts,
            "roots_refusals": self.roots_refusals,
        }

    def counters(self) -> Any:
        """This MCP server's counters, for `wreath.metrics.collect`."""
        from ..metrics import Counters

        return Counters(subsystem="mcp", instance=self._name, values=self.stats())

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Every declared tool, in the order `tools/list` renders them."""
        return tuple(self._registry.sorted_entries())

    @property
    def resources(self) -> tuple[Resource, ...]:
        """Every declared resource, in the order `resources/list` renders them."""
        return tuple(self._resources.sorted_entries())

    @property
    def prompts(self) -> tuple[Prompt, ...]:
        """Every declared prompt, in the order `prompts/list` renders them."""
        return tuple(self._prompts.sorted_entries())

    @property
    def progress(self) -> ProgressRegistry:
        """The registry a running tool reports progress to."""
        return self._progress

    def declared_actions(self) -> dict[str, tuple[str, ...]]:
        """Resource type -> the Cedar actions this server is gated on.

        The same shape `wreath.authorization.declared_actions` returns for an
        application's routes, and derived the same way -- from what is enforced,
        so there is no second list to drift. Read it to see, in one place, every
        action a model can reach through this server: tools, resources, prompts,
        and whatever `expose_routes` carried in from a route's own `@authorize`.
        """
        return actions_by_type(
            chain(
                self._registry.entries.values(),
                self._resources.entries.values(),
                self._prompts.entries.values(),
            )
        )

    #: The name `wreath._auth.permissions` looks for on a route's endpoint (or
    #: on the object it is bound to) when it asks what a surface declares. An
    #: MCP endpoint is one route in front of every tool, resource and prompt, so
    #: without this the application's vocabulary would be silently missing every
    #: action a model can reach -- and the two lists would agree only by
    #: somebody remembering to read both.
    __wreath_declared_actions__ = declared_actions

    def mount(self, app: Any) -> None:
        """Register this server's routes on `app`, which may be a `Router`."""
        self._app = app
        app.post(self._path, tags=("mcp",), summary=f"MCP endpoint for {self._name}")(
            self._post
        )
        app.get(self._path, tags=("mcp",), summary=f"MCP endpoint for {self._name}")(self._get)
        app.delete(self._path, tags=("mcp",), summary="End an MCP session")(self._delete)
        if self._auth is not None:
            # Unauthenticated by construction: a client that cannot read this
            # document cannot discover where to get the token the rest of the
            # endpoint demands.
            app.get(
                self._metadata_path,
                tags=("mcp",),
                summary=f"OAuth 2.0 protected-resource metadata for {self._name}",
            )(self._metadata)
        register_counters = getattr(app, "_register_counter_source", None)
        if callable(register_counters):
            register_counters(self)

    def tool(
        self,
        handler: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        action: str | None = None,
        resource: object | Callable[[Any], object] = None,
        rate_limit: ToolRateLimit | None = None,
        second_factor: float | None = None,
        sampling: str | bool | None = None,
        elicitation: str | bool | None = None,
    ) -> Any:
        """Declare a callable as a tool. Usable bare or with arguments.

        The handler's first parameter is the `Request`, exactly as a route
        handler's is; every later parameter is bound from the call's `arguments`
        object using its annotation. Reach for `Annotated[Model, Body()]` when a
        tool takes a structured argument, and plain annotated parameters for
        scalars.

        Args:
            name: The tool name. Defaults to the function's name.
            title: A human-facing display name.
            description: What the tool does. Defaults to the docstring, and one
                of the two must be present.
            action: The Cedar action this tool is gated on, conventionally
                `Type::verb`. Every call is then a policy decision made by the
                same authorizer, against the same entity shapes, as a route
                carrying `@authorize(action=...)` -- and a caller the policy
                refuses counts in `unauthorized_calls`, never `tool_errors`.
            resource: The Cedar resource, or a callable over the `Request` that
                produces one. Only meaningful with `action=`.
            rate_limit: A `ToolRateLimit` bounding how often one caller may
                invoke this tool. Keyed on the verified subject when the
                endpoint carries `MCPAuth`, and on the session otherwise.
            second_factor: Seconds within which the caller must have proved a
                second factor for this tool to run -- step-up, declared the way
                `@second_factor(max_age=...)` declares it on a route, and
                enforced by the same check against the same
                `second_factor_at` stamp `wreath.users` writes. Implies
                authentication. This is the natural way to say "this tool
                deletes things, so ask for the code again"; before it existed,
                step-up reached a tool only by exposing a route that already
                carried it. An identity with no stamp -- a bearer token, an
                OIDC login -- never satisfies it, so the default is closed.
            sampling: Whether this tool may call
                `request.state.mcp.sample(...)`, which asks the *client's* model
                to generate. Pass the Cedar action a caller must be allowed for
                it -- which is where this belongs, because "may this tool spend
                the caller's model on text of its own choosing" is a policy
                question and not a code one -- or `True` to declare it with no
                policy. A tool that declares neither may not sample at all, and
                a sampling request also spends a token from this tool's own
                `rate_limit=` bucket.
            elicitation: Whether this tool may call
                `request.state.mcp.elicit(...)`, which puts a form in front of
                the person at the other end. Declared, gated, throttled and
                recorded exactly as `sampling=` is, and off by default for a
                sharper reason: an elicitation renders **inside a client UI the
                user already trusts**, so a tool that asks for
                `{"api_key": str}` is a phishing surface wearing the client's
                chrome. "The user is asked and can decline" is not the control
                it looks like -- a consent dialog is the defence social
                engineering is built to defeat -- so which tools may prompt a
                human is a deployment's decision. Pass the Cedar action, or
                `True` to declare it with no policy.

        Raises:
            TypeError: The handler is not an async function.
            ValueError: The tool has no description, the name is taken, the
                server is at its `max_tools` ceiling, `resource=` was given
                without `action=`, or `second_factor=` is not positive.
            ToolSignatureError: The signature binds from a source that an MCP
                call cannot supply -- a header, a cookie, a form field, an
                upload, a path placeholder, or a dependency.
        """

        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            self._registry.add(
                build_tool(
                    function,
                    name=name,
                    title=title,
                    description=description,
                    action=action,
                    resource=resource,
                    rate_limit=rate_limit,
                    second_factor=second_factor,
                    sampling=sampling,
                    elicitation=elicitation,
                )
            )
            return function

        if handler is not None:
            return register(handler)
        return register

    def resource(
        self,
        uri: str,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        mime_type: str | None = None,
        action: str | None = None,
    ) -> Any:
        """Declare a reader as a resource, addressed by `uri`.

        A resource is what a model *reads* rather than what it does, so the
        reader takes the request and nothing else -- a `resources/read` carries
        a URI and no arguments. Return text, bytes, or a value to render as
        JSON; bytes travel as a base64 `blob` and everything else as `text`.

            @mcp.resource(
                "camera://ridge/latest",
                description="The most recent frame from the ridge camera.",
                mime_type="image/jpeg",
            )
            async def ridge_latest(request) -> bytes:
                ...

        Args:
            uri: The identifier a client reads and subscribes to.
            name: A short programmatic name. Defaults to the function's.
            title: A human-facing display name.
            description: What this holds. Defaults to the docstring, and one of
                the two must be present.
            mime_type: The media type reads are served as. Inferred from the
                reader's return value when omitted.
            action: The Cedar action a read is gated on. The **URI is the Cedar
                resource**: a resource has a stable identity, unlike a tool, so
                there is no second identifier to pass and nothing for the two to
                disagree about.

        Raises:
            TypeError: The reader is not an async function, or takes arguments.
            ValueError: The URI is empty or taken, there is no description, or
                the server is at its `max_resources` ceiling.
        """

        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            self._resources.add(
                build_resource(
                    function,
                    uri=uri,
                    name=name,
                    title=title,
                    description=description,
                    mime_type=mime_type,
                    action=action,
                )
            )
            return function

        return register

    def prompt(
        self,
        handler: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        action: str | None = None,
    ) -> Any:
        """Declare a callable as a prompt. Usable bare or with arguments.

        A prompt is chosen by a *person* -- a slash command, a menu entry --
        rather than by the model, which is why it has arguments instead of a
        schema and why every one of them is a string. Return the text of one
        message, or a sequence of `{"role": ..., "content": ...}` mappings.

            @mcp.prompt(description="Draft a report on a species' sightings.")
            async def sighting_report(request, species: str) -> str:
                return f"Summarise this month's {species} sightings."

        Args:
            name: The prompt name. Defaults to the function's name.
            title: A human-facing display name.
            description: What this prompt is for. Defaults to the docstring.
            action: The Cedar action rendering it is gated on, with the prompt's
                own name as the resource.

        Raises:
            TypeError: The handler is not an async function.
            ValueError: There is no description, the name is taken, or the
                server is at its `max_prompts` ceiling.
            ToolSignatureError: A parameter is annotated as something other than
                a string, or binds from a source `prompts/get` cannot fill.
        """

        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            self._prompts.add(
                build_prompt(
                    function,
                    name=name,
                    title=title,
                    description=description,
                    action=action,
                )
            )
            return function

        if handler is not None:
            return register(handler)
        return register

    # -- server-to-client notifications ----------------------------------

    def notify_resource_updated(self, uri: str) -> int:
        """Tell every subscriber that `uri` changed. Returns how many were told.

        Synchronous and non-blocking on purpose: the caller is the code that
        just wrote the row, and a fan-out that could block would put a client's
        reading speed on the write path. A session with no open stream keeps the
        notification queued up to `MCPLimits.max_pending_notifications`; past
        that it is dropped and counted in `notifications_dropped`.
        """
        payload = _notification("notifications/resources/updated", {"uri": uri})
        told = 0
        for session in self._sessions.subscribers(uri):
            if session.publish(payload):
                told += 1
            else:
                self.notifications_dropped += 1
        return told

    def _notify(self, session: Session, method: str, params: dict[str, Any]) -> None:
        self._publish(session, _notification(method, params))

    def _publish(self, session: Session, payload: bytes) -> bool:
        """Put one framed message on a session's queue, counting what is lost.

        Every server-to-client byte goes through here -- a notification, a
        progress report, a `roots/list` request -- so there is one place a drop
        is counted and one place to look when a client says it heard nothing.
        """
        if session.publish(payload):
            return True
        self.notifications_dropped += 1
        return False

    # -- HTTP surface ----------------------------------------------------

    async def _metadata(self, request: Request) -> Any:
        """RFC 9728 protected-resource metadata. Served without a token."""
        auth = self._auth
        if auth is None:  # pragma: no cover - the route is not registered without one
            return self._transport_error(404, INVALID_REQUEST, "this endpoint is not protected")
        return Response(
            _json_dumps(auth.document()),
            media_type=b"application/json",
            # A cache is welcome to hold this: it changes when a deployment's
            # authorization servers change, which is a deploy, not a request.
            headers=[(b"cache-control", b"public, max-age=3600")],
        )

    async def _post(self, request: Request) -> Any:
        """Accept one JSON-RPC message and answer it."""
        try:
            identity = await self._authenticate(request)
        except Unauthenticated as refusal:
            return self._challenge(refusal)

        media = (request.header("content-type") or "").split(";")[0].strip().lower()
        if media != "application/json":
            return self._transport_error(
                415,
                INVALID_REQUEST,
                "an MCP message must be sent as application/json",
            )
        wants_json, wants_sse = _accepts(request.header("accept"))
        if not wants_json and not wants_sse:
            return self._transport_error(
                406,
                INVALID_REQUEST,
                "this endpoint answers application/json or text/event-stream",
            )
        stream = wants_sse and not wants_json

        try:
            payload = _json_loads(await request.body())
        except ValueError:
            return self._transport_error(400, PARSE_ERROR, "request body is not valid JSON")
        try:
            message = parse_message(payload)
        except JsonRpcError as error:
            return self._transport_error(400, error.code, error.message, error.data)

        if message.is_request and message.method == "initialize":
            return await self._initialize(request, message, identity, stream=stream)

        session, refusal_response = await self._session_for(
            request, identity, identifier=message.id
        )
        if refusal_response is not None:
            return refusal_response

        declared = request.header("mcp-protocol-version")
        if declared is not None and declared not in SUPPORTED_PROTOCOL_VERSIONS:
            return self._transport_error(
                400,
                INVALID_REQUEST,
                f"MCP-Protocol-Version {declared!r} is not implemented here; "
                f"this server speaks {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
                identifier=message.id,
            )

        if message.is_notification:
            self._handle_notification(session, message)
            return Response(b"", status=202)
        if message.is_response:
            # The answer to something *this server* asked: a sampling result, a
            # filled-in form, a list of roots. It arrives on its own POST while
            # the tool that asked is parked on another one, which is exactly the
            # reentrancy that makes this work -- see `_mcp/outbound.py`. A
            # response matching nothing is accepted and dropped rather than
            # refused: answering twice, or answering after a timeout, is a race.
            channel = session.channel
            if channel is not None:
                channel.resolve(message.id, message.result, message.error)
            return Response(b"", status=202)

        try:
            result = await self._dispatch(request, session, message)
        except JsonRpcError as error:
            body = encode_failure(message.id, error.code, error.message, error.data)
        else:
            if result is _SUPPRESSED:
                return Response(b"", status=202)
            body = encode_success(message.id, result)
        return self._reply(body, stream=stream)

    async def _get(self, request: Request) -> Any:
        """Open this session's server-to-client notification stream.

        One stream per session, which the specification asks for and which is
        also the only arrangement that works: two would split a conversation's
        notifications between them at random, and a client would see half of
        each. Everything the server sends unasked travels here -- a subscribed
        resource changing, a running tool's progress -- while every *reply*
        still travels on the POST that asked for it.
        """
        try:
            identity = await self._authenticate(request)
        except Unauthenticated as refusal:
            return self._challenge(refusal)
        _wants_json, wants_sse = _accepts(request.header("accept"))
        if not wants_sse:
            return self._transport_error(
                406,
                INVALID_REQUEST,
                "the notification stream is Server-Sent Events; send "
                "`Accept: text/event-stream` to open it",
            )
        session, refusal_response = await self._session_for(request, identity)
        if refusal_response is not None:
            return refusal_response
        if session.stream_open:
            return self._transport_error(
                409,
                INVALID_REQUEST,
                "this session already has a notification stream open. The "
                "specification allows one per session, and a second would split "
                "this conversation's notifications between the two at random.",
            )
        session.stream_open = True
        return SSEResponse(self._notifications(session))

    async def _notifications(self, session: Session) -> AsyncIterator[ServerSentEvent]:
        """Frame this session's notifications until the session ends.

        Nothing in `SSEResponse` is emitted on a timer, deliberately, so the
        silence of an idle stream is this generator's problem: a comment every
        `stream_keepalive_seconds` is what keeps an intermediary from reaping a
        connection that is working exactly as intended.
        """
        keepalive = self._limits.stream_keepalive_seconds
        try:
            while True:
                try:
                    item = await asyncio.wait_for(session.notifications.get(), keepalive)
                except TimeoutError:
                    yield ServerSentEvent(comment="keep-alive")
                    continue
                if item is CLOSE_STREAM:
                    return
                yield ServerSentEvent(item, event="message")
        finally:
            # Reached on a clean close and on the client hanging up, which
            # arrives here as `GeneratorExit`. Either way the session may open
            # another stream, which is what a reconnecting client does.
            session.stream_open = False

    async def _session_for(
        self, request: Request, identity: Any, *, identifier: Any = None
    ) -> tuple[Any, Response | None]:
        """The session this request names, or the refusal that says why not."""
        session_id = request.header("mcp-session-id")
        if session_id is None:
            return None, self._transport_error(
                400,
                INVALID_REQUEST,
                "an Mcp-Session-Id header is required on every message after "
                "initialize",
                identifier=identifier,
            )
        session = self._sessions.get(session_id)
        if session is None:
            return None, self._transport_error(
                404,
                INVALID_REQUEST,
                "unknown, ended, or idle-expired MCP session; send initialize again",
                identifier=identifier,
            )
        if not await self._owns(request, session, identity):
            # A session id is a bearer credential for that session's in-flight
            # calls. Binding it to the subject that opened it means a leaked one
            # is not, on its own, worth anything to a different caller.
            return None, self._challenge(
                Unauthenticated("invalid_token", "this token did not open this MCP session")
            )
        return session, None

    async def _delete(self, request: Request) -> Any:
        """End the session named by `Mcp-Session-Id`."""
        try:
            identity = await self._authenticate(request)
        except Unauthenticated as refusal:
            return self._challenge(refusal)
        session_id = request.header("mcp-session-id")
        if session_id is None:
            return self._transport_error(
                400, INVALID_REQUEST, "an Mcp-Session-Id header is required to end a session"
            )
        session = self._sessions.get(session_id)
        if session is None:
            return self._transport_error(404, INVALID_REQUEST, "unknown or already ended session")
        if not await self._owns(request, session, identity):
            return self._challenge(
                Unauthenticated("invalid_token", "this token did not open this MCP session")
            )
        self._sessions.discard(session_id)
        return Response(b"", status=204)

    # -- authentication --------------------------------------------------

    async def _authenticate(self, request: Request) -> Any:
        """Verify the caller and publish the identity, or None when unprotected.

        The identity goes on the `Request` rather than only into a local, so
        every component that already knows how to find a caller -- the
        authorizer above all -- finds this one in the usual place.
        """
        auth = self._auth
        if auth is None:
            return None
        identity = await auth.authenticate(request)
        request._set_identity(identity)
        return identity

    async def _owns(self, request: Request, session: Session, identity: Any) -> bool:
        """Whether this request's caller is the subject that opened `session`.

        An `Mcp-Session-Id` is a bearer credential for that session's in-flight
        calls, its notification stream, and the answers it may give to a tool's
        `elicitation/create` -- so a session names a subject and a message from
        anybody else is refused. **Both ways in have to resolve that subject or
        neither does.** `MCPAuth` publishes an identity for the whole endpoint
        and this used to read only that, so an application authenticating with
        its own `app.configure_auth(...)` backend -- the supported second way,
        and the one `expose_routes` exists for -- opened every session with
        `principal=None` and bound nothing. A control that holds on one of two
        supported configurations is not a control.

        Resolving here rather than unconditionally keeps `_identify`'s laziness:
        an endpoint whose sessions are unbound, because there was nobody to bind
        them to, never runs a backend for this.
        """
        principal = session.principal
        if principal is None:
            return True
        if identity is None:
            identity = await self._identify(request)
        return identity is not None and identity.id == principal

    def _challenge(self, refusal: Unauthenticated) -> Response:
        auth = self._auth
        headers: list[tuple[bytes, bytes]] = []
        if auth is not None:
            headers.append(
                (
                    b"www-authenticate",
                    auth.challenge(
                        self._metadata_url,
                        error=refusal.error,
                        description=refusal.description,
                    ),
                )
            )
        return self._transport_error(
            401,
            INVALID_REQUEST,
            refusal.description or "this MCP endpoint requires a bearer token",
            headers=headers,
        )

    # -- dispatch --------------------------------------------------------

    async def _initialize(
        self, request: Request, message: Message, identity: Any, *, stream: bool
    ) -> Any:
        params = message.params
        if identity is None:
            # The subject this session will be bound to. `MCPAuth` has already
            # published one; without it the application's own backend is what
            # identifies the caller, and a session opened without asking it is a
            # session bound to nobody -- which is what made a leaked session id a
            # credential in its own right on exactly the deployments that
            # authenticate the other supported way. Once per session, not per
            # message, and `_identify` still answers `None` immediately for an
            # application that installed no backend at all.
            identity = await self._identify(request)
        requested = params.get("protocolVersion")
        # Answer with a revision this build implements. The specification puts
        # the decision with the client: it sees what came back and disconnects
        # if it cannot speak it, which is a clearer failure than a server
        # guessing at a revision it has never seen.
        negotiated = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        )
        client_info = params.get("clientInfo")
        # What the client says it can do is the only thing that decides whether
        # a server-to-client request is even attempted. Kept on the session
        # because it is a fact about this conversation, and because a refusal
        # naming the missing capability is the difference between a client
        # author reading one message and reverse-engineering a hang.
        client_capabilities = params.get("capabilities")
        try:
            session = self._sessions.create(
                protocol_version=negotiated,
                client_info=client_info if isinstance(client_info, dict) else {},
                client_capabilities=(
                    client_capabilities if isinstance(client_capabilities, dict) else {}
                ),
                principal=None if identity is None else identity.id,
            )
        except RuntimeError as error:
            return self._transport_error(
                503, INTERNAL_ERROR, str(error), identifier=message.id
            )
        # Advertised from what is declared, not from what the build could do. A
        # server with no resources that claims the capability invites a client
        # to open a stream and subscribe to nothing.
        capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
        if self._resources:
            capabilities["resources"] = {"subscribe": True, "listChanged": False}
        if self._prompts:
            capabilities["prompts"] = {"listChanged": False}
            # Advertised with the prompts rather than on its own: what
            # `completion/complete` answers *is* a prompt argument's declared
            # values, so a server with no prompts has nothing to complete and
            # says so by not claiming it.
            capabilities["completions"] = {}
        result: dict[str, Any] = {
            "protocolVersion": negotiated,
            "capabilities": capabilities,
            "serverInfo": {"name": self._name, "version": self._version},
        }
        if self._instructions is not None:
            result["instructions"] = self._instructions
        return self._reply(
            encode_success(message.id, result), stream=stream, session_id=session.id
        )

    def _handle_notification(self, session: Session, message: Message) -> None:
        if message.method == "notifications/cancelled":
            requested = message.params.get("requestId")
            if not isinstance(requested, str | int) or isinstance(requested, bool):
                return
            task = session.in_flight.get(requested)
            if task is not None:
                # Cancelling the task is also what stops anything that call had
                # outstanding *at the client*: the inner `wait_for` takes the
                # `CancelledError` and withdraws its own request on the way out.
                task.cancel()
            return
        if message.method == "notifications/roots/list_changed":
            # Invalidated rather than re-fetched. Asking now would be a round
            # trip to a client that may have nothing to read the answer with;
            # the next `read_file` asks, and pays for it only if anyone cares.
            session.roots = None
        # `notifications/initialized` needs nothing done, and JSON-RPC says an
        # unknown notification is dropped rather than answered.

    async def _dispatch(self, request: Request, session: Session, message: Message) -> Any:
        method = message.method
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._registry.listing()
        if method == "tools/call":
            return await self._tools_call(request, session, message)
        if method == "resources/list":
            return self._resources.listing()
        if method == "resources/read":
            return await self._resources_read(request, session, message)
        if method == "resources/templates/list":
            # Answered rather than refused. A server that declares the
            # `resources` capability and then rejects a method the capability
            # implies reads as broken; a server with no templated resources
            # correctly has none to list.
            return {"resourceTemplates": []}
        if method == "resources/subscribe":
            return self._subscribe(session, message, subscribe=True)
        if method == "resources/unsubscribe":
            return self._subscribe(session, message, subscribe=False)
        if method == "prompts/list":
            return self._prompts.listing()
        if method == "prompts/get":
            return await self._prompts_get(request, session, message)
        if method == "completion/complete":
            return _complete(self._prompts, message.params)
        stage = _NOT_YET.get(method)
        if stage is not None:
            raise JsonRpcError(
                METHOD_NOT_FOUND,
                f"{method} is reserved: Wreath's MCP surface does not implement "
                f"{stage} yet",
            )
        if method in _CLIENT_ONLY:
            raise JsonRpcError(
                METHOD_NOT_FOUND,
                f"{method} is a server-to-client request: Wreath issues it, on "
                "this session's `GET` stream, and answers it nowhere. Your "
                "client replies to it with a JSON-RPC response POSTed here.",
            )
        raise JsonRpcError(METHOD_NOT_FOUND, f"unknown method {method!r}")

    async def _tools_call(self, request: Request, session: Session, message: Message) -> Any:
        params = message.params
        name = params.get("name")
        if not isinstance(name, str):
            raise JsonRpcError(INVALID_PARAMS, "`params.name` must name a tool")
        tool = self._registry.get(name)
        if tool is None:
            raise JsonRpcError(INVALID_PARAMS, f"unknown tool {name!r}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "`params.arguments` must be a JSON object")

        started = time.perf_counter()
        # Before the throttle and before the marker, both of which name the
        # caller. `MCPAuth` has already published an identity; without it the
        # application's own backend has not run yet, and `_authorize` would
        # otherwise resolve it *after* the bucket was charged on the session and
        # after the marker said "anonymous" -- a per-caller ceiling that a free
        # `initialize` resets, and an audit trail that names nobody. Still lazy:
        # a tool that is neither gated nor bounded never runs the backend, which
        # is the property `_identify` exists to protect.
        if tool.limiter is not None or tool.requirement.access_level > 0:
            await self._identify(request)
        identity = request.identity
        principal = None if identity is None else identity.id
        # What was asked for, before anything decides whether it was allowed:
        # a refused call is exactly the one an audit needs the arguments of.
        _record.record_arguments(arguments)

        def marker(outcome: str) -> None:
            _record.record_call(
                tool=name,
                outcome=outcome,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                principal=principal,
                session=session.id,
            )

        # Abuse control first: it is the cheapest check, and a caller that is
        # over its ceiling should not get a policy decision out of the server as
        # a side effect of hammering it.
        retry_after = self._throttle(tool, session, principal)
        if retry_after > 0.0:
            self.throttled += 1
            marker(_record.OUTCOME_THROTTLED)
            raise JsonRpcError(
                RATE_LIMITED,
                f"tool {name!r} is rate limited for this caller; retry in "
                f"{retry_after:.1f}s",
                {"retryAfter": retry_after},
            )

        try:
            kwargs = tool.bind(arguments)
        except ValidationError as error:
            self.schema_rejections += 1
            marker(_record.OUTCOME_REJECTED)
            raise JsonRpcError(
                INVALID_PARAMS,
                f"arguments for tool {name!r} do not match its inputSchema",
                {"errors": error.errors},
            ) from error

        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        # Scoped to the session and the request, so two calls in flight on one
        # session report separately and a task id can never collide with a
        # durable job's in a registry the deployment shares between the two.
        task_id = f"mcp:{session.id}:{message.id}"
        # Published before the policy decision, not after, because a Cedar
        # resource generally depends on *which row* is being asked for and the
        # only place that lives is the arguments. A route resolves its resource
        # from the path; a tool resolves it from here.
        request.state.mcp = ToolContext(
            session_id=session.id,
            request_id=message.id,
            tool=name,
            progress_token=token,
            identity=identity,
            arguments=arguments,
            progress=self._progress.reporter(task_id),
            _server=self,
            _session=session,
            _request=request,
        )

        denial = await self._authorize(request, tool)
        if denial is not None:
            self.unauthorized_calls += 1
            marker(_record.OUTCOME_DENIED)
            raise JsonRpcError(UNAUTHORIZED, denial)

        if len(session.in_flight) >= self._limits.max_concurrent_calls:
            self.throttled += 1
            marker(_record.OUTCOME_THROTTLED)
            raise JsonRpcError(
                TOO_MANY_CALLS,
                f"this session already has {self._limits.max_concurrent_calls} "
                "calls in flight, its `MCPLimits(max_concurrent_calls=...)` "
                "ceiling. Await one, or cancel it.",
            )

        watcher = None
        if token is not None:
            # Seeded before the watcher starts, so the registry has an entry the
            # moment it looks: `ProgressRegistry.stream` gives up on a task id
            # that never appears, which is the right behaviour for an endpoint
            # anyone can watch and the wrong one for a call we know just began.
            self._progress.report(task_id, 0.0, "")
            watcher = asyncio.ensure_future(
                self._relay_progress(session, task_id, token)
            )

        # The call runs in its own task so that `notifications/cancelled`, which
        # necessarily arrives on a *different* POST, has something to cancel.
        task = asyncio.ensure_future(self._invoke(tool, request, kwargs))
        session.in_flight[message.id] = task
        try:
            # `wait` rather than `await task`: awaiting a task we may cancel
            # ourselves would raise CancelledError here and be indistinguishable
            # from this request being cancelled, which must still propagate.
            await asyncio.wait({task})
        finally:
            session.in_flight.pop(message.id, None)
            if not task.done():
                task.cancel()
            if watcher is not None:
                watcher.cancel()
        if task.cancelled():
            marker(_record.OUTCOME_CANCELLED)
            return _SUPPRESSED
        result, outcome = task.result()
        marker(outcome)
        return result

    async def _relay_progress(self, session: Session, task_id: str, token: Any) -> None:
        """Turn one call's progress reports into `notifications/progress`.

        There is no second progress mechanism here, deliberately.
        `wreath.progress` already models a running task reporting a percentage
        and a message; already spans workers when it is given the message bus,
        which is what makes a durable job's progress reach the client whichever
        worker holds the stream; and already has a status route and an SSE
        stream of its own. This relays what it holds onto the MCP session, and
        owns nothing.
        """
        async for snapshot in self._progress.stream(
            task_id, interval=self._progress_interval
        ):
            params: dict[str, Any] = {
                "progressToken": token,
                "progress": snapshot.percent,
                "total": 100.0,
            }
            if snapshot.message:
                params["message"] = snapshot.message
            self._notify(session, "notifications/progress", params)

    # -- resources -------------------------------------------------------

    async def _resources_read(
        self, request: Request, session: Session, message: Message
    ) -> Any:
        resource = self._resource_named(message.params)
        request.state.mcp = ToolContext(
            session_id=session.id,
            request_id=message.id,
            tool=resource.uri,
            identity=request.identity,
            _server=self,
            _session=session,
            _request=request,
        )
        denial = await self._authorize(request, resource, noun="resource")
        if denial is not None:
            self.unauthorized_calls += 1
            raise JsonRpcError(UNAUTHORIZED, denial)
        self.resource_reads += 1
        try:
            value = await resource.handler(request)
        except ToolError as error:
            # A reader saying "this is gone" is not a server fault, and a client
            # that is told so can stop asking. `resources/read` has no `isError`
            # result to carry it, so it is the specification's own code.
            self.resource_errors += 1
            raise JsonRpcError(RESOURCE_NOT_FOUND, str(error)) from error
        except Exception as error:
            # The same boundary a tool gets, for the same reason: a reader that
            # raises must not take the session down, and only the exception's
            # *type* travels, because its message was written for an operator.
            # The whole failure is on the request's own record.
            self.resource_errors += 1
            raise JsonRpcError(
                INTERNAL_ERROR,
                f"reading {resource.uri} raised {type(error).__name__}",
            ) from error
        return read_result(resource, value)

    def _resource_named(self, params: Mapping[str, Any]) -> Resource:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise JsonRpcError(INVALID_PARAMS, "`params.uri` must name a resource")
        resource = self._resources.get(uri)
        if resource is None:
            raise JsonRpcError(RESOURCE_NOT_FOUND, f"no resource at {uri!r}")
        return resource

    def _subscribe(self, session: Session, message: Message, *, subscribe: bool) -> Any:
        """Start or stop telling one session about a resource's changes."""
        resource = self._resource_named(message.params)
        if not subscribe:
            self._sessions.unsubscribe(session, resource.uri)
            return {}
        if (
            resource.uri not in session.subscriptions
            and len(session.subscriptions) >= self._limits.max_subscriptions
        ):
            raise JsonRpcError(
                TOO_MANY_CALLS,
                f"this session already holds {self._limits.max_subscriptions} "
                "subscriptions, its `MCPLimits(max_subscriptions=...)` ceiling. "
                "Unsubscribe from something, or raise it.",
            )
        self._sessions.subscribe(session, resource.uri)
        return {}

    # -- prompts ---------------------------------------------------------

    async def _prompts_get(
        self, request: Request, session: Session, message: Message
    ) -> Any:
        params = message.params
        name = params.get("name")
        if not isinstance(name, str):
            raise JsonRpcError(INVALID_PARAMS, "`params.name` must name a prompt")
        prompt = self._prompts.get(name)
        if prompt is None:
            raise JsonRpcError(INVALID_PARAMS, f"unknown prompt {name!r}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "`params.arguments` must be a JSON object")
        _record.record_arguments(arguments)
        try:
            kwargs = prompt.bind(arguments)
        except ValidationError as error:
            self.schema_rejections += 1
            raise JsonRpcError(
                INVALID_PARAMS,
                f"arguments for prompt {name!r} do not match the ones it declares",
                {"errors": error.errors},
            ) from error
        request.state.mcp = ToolContext(
            session_id=session.id,
            request_id=message.id,
            tool=name,
            identity=request.identity,
            arguments=arguments,
            _server=self,
            _session=session,
            _request=request,
        )
        denial = await self._authorize(request, prompt, noun="prompt")
        if denial is not None:
            self.unauthorized_calls += 1
            raise JsonRpcError(UNAUTHORIZED, denial)
        self.prompt_renders += 1
        try:
            return render_messages(prompt, await prompt.handler(request, **kwargs))
        except Exception as error:
            # As for a tool and a resource: the type travels, the message does
            # not, and the failure is counted rather than swallowed.
            self.prompt_errors += 1
            raise JsonRpcError(
                INTERNAL_ERROR, f"prompt {name!r} raised {type(error).__name__}"
            ) from error

    # -- server-to-client requests ---------------------------------------

    async def _client_request(
        self, session: Session, method: str, params: dict[str, Any]
    ) -> Any:
        """Ask the client one question, or say why it could not be asked.

        Raises:
            ClientRequestError: The client never advertised the capability, the
                session's pending table is full, the queue would not take it,
                the client answered with an error, or nobody answered.
        """
        capability = _REQUIRES_CAPABILITY[method]
        if capability not in session.client_capabilities:
            raise ClientRequestError(
                f"this client did not advertise the {capability!r} capability in "
                f"`initialize`, so {method} would be a request nothing will ever "
                "answer. A refusal now is the only alternative to a hang whose "
                "cause is invisible from either end."
            )
        channel = session.channel
        if channel is None:  # pragma: no cover - every live session has one
            raise ClientRequestError("this session cannot issue requests")
        before = channel.timeouts
        try:
            return await channel.request(method, params)
        finally:
            if channel.timeouts != before:
                self.client_request_timeouts += 1

    async def _sample(
        self,
        context: ToolContext,
        messages: Any,
        *,
        max_tokens: int,
        system_prompt: str | None,
        temperature: float | None,
        stop_sequences: Any,
        model_preferences: Any,
        include_context: str | None,
        metadata: Any,
    ) -> dict[str, Any]:
        """`sampling/createMessage`, gated, throttled and recorded like a call.

        Three reuses and no new machinery: the gate is one more
        `AuthRequirement` through `_authorize`, the ceiling is the tool's own
        rate-limit bucket, and the record is the `tools/call` marker with an
        outcome of its own. A tool that samples on every invocation therefore
        spends its allowance twice per call, which is the honest accounting --
        the second half of the work is the expensive half.
        """
        session = context._session
        request = context._request
        started = time.perf_counter()
        identity = context.identity
        principal = None if identity is None else identity.id

        def marker(outcome: str) -> None:
            _record.record_call(
                tool=context.tool,
                outcome=outcome,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                principal=principal,
                session=session.id,
            )

        tool = self._registry.get(context.tool)
        requirement = getattr(tool, "sampling_requirement", None)
        if requirement is None:
            self.sampling_refusals += 1
            marker(_record.OUTCOME_SAMPLE_DENIED)
            raise ClientRequestError(
                f"{context.tool!r} did not declare `sampling=`, so it may not "
                "ask the client's model to generate. Declare the Cedar action a "
                "caller must be allowed for it -- `@mcp.tool(sampling=\"...\")` "
                "-- or `sampling=True` for no policy at all. Sampling is off by "
                "default because a tool that can put words in the caller's model "
                "is a different thing from a tool that reads a row."
            )

        # As in `_tools_call`, and *not* for free: a tool that is itself neither
        # gated nor bounded left the identity unresolved there, so a gate that
        # lives only on `sampling=` would charge the bucket and name the caller
        # before its own `_authorize` resolved them. `_identify` memoizes onto
        # the request, so this is at most one backend call per request.
        if tool.limiter is not None or requirement.access_level > 0:
            identity = await self._identify(request)
            principal = None if identity is None else identity.id

        retry_after = self._throttle(tool, session, principal)
        if retry_after > 0.0:
            self.sampling_refusals += 1
            self.throttled += 1
            marker(_record.OUTCOME_SAMPLE_THROTTLED)
            raise ClientRequestError(
                f"tool {tool.name!r} is over its rate limit, and a sampling "
                f"request spends the same bucket a call does; retry in "
                f"{retry_after:.1f}s."
            )

        denial = await self._authorize(
            request, _Gate(tool.name, requirement), noun="sampling from tool"
        )
        if denial is not None:
            self.sampling_refusals += 1
            self.unauthorized_calls += 1
            marker(_record.OUTCOME_SAMPLE_DENIED)
            raise ClientRequestError(denial)

        params: dict[str, Any] = {
            "messages": _sampling_messages(messages),
            "maxTokens": max_tokens,
        }
        for key, value in (
            ("systemPrompt", system_prompt),
            ("temperature", temperature),
            ("stopSequences", stop_sequences),
            ("modelPreferences", model_preferences),
            ("includeContext", include_context),
            ("metadata", metadata),
        ):
            if value is not None:
                params[key] = value

        self.sampling_requests += 1
        try:
            result = await self._client_request(session, "sampling/createMessage", params)
        except ClientRequestError:
            marker(_record.OUTCOME_SAMPLE_FAILED)
            raise
        marker(_record.OUTCOME_SAMPLED)
        return result if isinstance(result, dict) else {"content": result}

    async def _elicit(self, context: ToolContext, message: str, form: type[Any]) -> Any | None:
        """`elicitation/create`, gated like sampling and validated like a call.

        The schema is `derive_input_schema` over the form's fields -- the same
        derivation a tool's `inputSchema` comes from -- and the answer goes
        through `bind_arguments`, the same validator a `tools/call`'s arguments
        meet. What the person typed is recorded first, under the same
        `crud.SENSITIVE_FIELD` rule that already hides a password argument,
        because a form is the *most* likely place for one to arrive.

        **The gate in front of all that is the same one sampling has**, and it
        exists because "the user is asked and can decline" is not a control. An
        elicitation renders inside a client UI the person already trusts, so a
        tool asking for `{"password": str}` is a phishing surface wearing that
        client's chrome, and a consent dialog is exactly what social engineering
        is built to walk through. A framework that ships an authorizer should let
        a deployment name the tools that may put a prompt in front of a person,
        so a tool that declared no `elicitation=` may not, the decision is
        `_authorize` over one more `AuthRequirement`, and the request spends the
        tool's own rate-limit bucket -- a tool that can re-prompt without limit
        can wear an answer out of someone.
        """
        session = context._session
        request = context._request
        started = time.perf_counter()
        identity = context.identity
        principal = None if identity is None else identity.id

        def marker(outcome: str) -> None:
            _record.record_call(
                tool=context.tool,
                outcome=outcome,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                principal=principal,
                session=session.id,
            )

        # A resource reader and a prompt handler are looked up here too and find
        # nothing, which is the same answer sampling gives them: neither carries
        # a declaration, so neither may prompt.
        tool = self._registry.get(context.tool)
        requirement = None if tool is None else tool.elicitation_requirement
        if tool is None or requirement is None:
            self.elicitation_refusals += 1
            marker(_record.OUTCOME_ELICIT_DENIED)
            raise ClientRequestError(
                f"{context.tool!r} did not declare `elicitation=`, so it may not "
                "put a form in front of the person at the other end. Declare the "
                "Cedar action a caller must be allowed for it -- "
                '`@mcp.tool(elicitation="...")` -- or `elicitation=True` for no '
                "policy at all. Prompting is off by default because an "
                "elicitation renders inside a client UI the user already trusts, "
                "which makes a tool that asks for a password or an API key a "
                "phishing surface wearing that client's chrome; being able to "
                "decline is the defence social engineering is built to defeat."
            )

        # The same resolution `_sampling` does, and for the same reason: the
        # gate may live only on `elicitation=`, in which case `_tools_call`
        # resolved nobody and the marker would name nobody.
        if tool.limiter is not None or requirement.access_level > 0:
            identity = await self._identify(request)
            principal = None if identity is None else identity.id

        retry_after = self._throttle(tool, session, principal)
        if retry_after > 0.0:
            self.elicitation_refusals += 1
            self.throttled += 1
            marker(_record.OUTCOME_ELICIT_THROTTLED)
            raise ClientRequestError(
                f"tool {tool.name!r} is over its rate limit, and an elicitation "
                f"spends the same bucket a call does; retry in {retry_after:.1f}s."
            )

        denial = await self._authorize(
            request, _Gate(tool.name, requirement), noun="elicitation from tool"
        )
        if denial is not None:
            self.elicitation_refusals += 1
            self.unauthorized_calls += 1
            marker(_record.OUTCOME_ELICIT_DENIED)
            raise ClientRequestError(denial)

        schema, spec = form_schema(form)
        self.elicitations += 1
        try:
            result = await self._client_request(
                session,
                "elicitation/create",
                {"message": message, "requestedSchema": schema},
            )
        except ClientRequestError:
            marker(_record.OUTCOME_ELICIT_FAILED)
            raise
        action = result.get("action") if isinstance(result, Mapping) else None
        if action != "accept":
            self.elicitation_declines += 1
            marker(_record.OUTCOME_ELICIT_DECLINED)
            return None
        content = result.get("content")
        if not isinstance(content, Mapping):
            content = {}
        # Before validation, exactly as a call's arguments are recorded before
        # anything decides whether they were acceptable: a refused answer is one
        # an audit still needs to know arrived.
        _record.record_arguments(content, prefix=_record.ELICIT_PREFIX)
        try:
            kwargs = bind_arguments(spec, content, label="content")
        except ValidationError as error:
            self.schema_rejections += 1
            marker(_record.OUTCOME_ELICIT_FAILED)
            raise ClientRequestError(
                f"the client's answer does not match the schema {form.__name__!r} "
                f"asked for: {error.errors}"
            ) from error
        marker(_record.OUTCOME_ELICITED)
        return form(**kwargs)

    async def _roots(self, session: Session) -> tuple[str, ...]:
        """The client's declared roots, asked for once and cached until it says otherwise."""
        cached = session.roots
        if cached is not None:
            return cached
        if "roots" not in session.client_capabilities:
            session.roots = ()
            return ()
        declared = root_paths(await self._client_request(session, "roots/list", {}))
        session.roots = declared
        return declared

    async def _read_file(self, session: Session, path: str) -> bytes:
        """Read one file, confined by the server's root *and* the client's.

        Raises:
            RuntimeError: No `file_root=` was declared for this server.
            PermissionError: The path escapes a root, traverses a symlink, is
                not a regular file, or is over `MCPLimits.max_file_bytes`.
            FileNotFoundError: There is no such file beneath the root.
        """
        root = self._file_root
        if root is None:
            raise RuntimeError(
                "this MCP server has no `file_root=`, so it reads no files. "
                "Declare the directory reads are confined to: "
                "`MCP(app, ..., file_root=\"/srv/data\")`. There is deliberately "
                "no way to read a path that is not beneath one."
            )
        declared = await self._roots(session)
        # An empty answer from a client that advertised `roots` means it grants
        # access beneath no client root.  It is not the same state as a client
        # with no roots capability, where the server's `file_root` remains the
        # only boundary.  Treating both as the falsey tuple used to let a
        # hostile client answer `{"roots": []}` and then read anywhere beneath
        # the server root.
        client_roots_apply = "roots" in session.client_capabilities
        outside_client_roots = client_roots_apply and not beneath_any(
            declared, os.path.join(root, path)
        )
        if outside_client_roots:
            # The client told us where its workspace is. A root that is not
            # consulted is a comment, so this is the point of asking at all.
            self.roots_refusals += 1
            raise PermissionError(
                f"{path!r} is outside every root this client declared "
                f"({', '.join(declared)}). A client's roots bound what this "
                "server may read on its behalf, not merely what it prefers."
            )
        if self._file_root_fd is None:
            self._file_root_fd = await asyncio.to_thread(_open_root, root)
        try:
            return await asyncio.to_thread(
                read_beneath,
                self._file_root_fd,
                path,
                max_bytes=self._limits.max_file_bytes,
            )
        except ContainmentError as error:
            raise PermissionError(str(error)) from error

    def _throttle(self, tool: Tool, session: Session, principal: str | None) -> float:
        """Seconds this caller must wait before calling `tool`, or 0.0."""
        limiter = tool.limiter
        if limiter is None:
            return 0.0
        # The verified subject when there is one, the session otherwise. Never
        # the client address: an MCP client is usually a gateway, so addresses
        # collapse every caller onto one bucket.
        key = principal if principal is not None else session.id
        return limiter.try_acquire(key, 1.0, time.monotonic())

    async def _authorize(
        self, request: Request, entry: Any, *, noun: str = "tool"
    ) -> str | None:
        """None when the call may proceed, or the reason it may not.

        One decision for tools, resources, prompts and route-derived tools
        alike, over the `AuthRequirement` each of them carries. That is what
        makes `expose_routes` safe to have: a route behind `@authorize`,
        `@roles` or `@permissions` arrives here carrying exactly those, and is
        held to them, rather than being translated into some weaker thing on the
        way in.
        """
        requirement: AuthRequirement = entry.requirement
        if requirement.access_level == 0:
            return None
        name = getattr(entry, "name", None) or getattr(entry, "uri", "")
        identity = await self._identify(request)
        if identity is None:
            return (
                f"{noun} {name!r} requires an authenticated caller and this "
                "request carried none. Put `MCPAuth(...)` in front of this "
                "endpoint, or install an authentication backend the request "
                "can satisfy with `app.configure_auth(...)`."
            )
        if requirement.second_factor is not None:
            age = second_factor_age(identity, time.time())
            if age is None or age > requirement.second_factor:
                return (
                    f"{noun} {name!r} requires a second factor proved within "
                    f"{requirement.second_factor:.0f}s, and this caller has not."
                )
        for check in requirement.role_checks:
            if not _holds(identity.roles, check):
                return f"the caller does not hold the roles {noun} {name!r} requires"
        for check in requirement.permission_checks:
            if not _holds(identity.permissions, check):
                return f"the caller does not hold the permissions {noun} {name!r} requires"
        if not requirement.policies:
            return None
        authorizer = self._authorizer
        if authorizer is None:
            authorizer = getattr(self._app, "_authorizer", None)
        if authorizer is None:
            # Fail closed and say why. A declaration that meets no authorizer is
            # a deployment mistake, and admitting the call "because nothing is
            # configured" would make the declaration a comment.
            return (
                f"{noun} {name!r} is gated on the Cedar action "
                f"{requirement.policies[0].action!r}, but this application has "
                "no authorizer. Install one with "
                "`app.configure_auth(backend, authorizer)`."
            )
        for policy in requirement.policies:
            decision = await authorizer.authorize(request, policy)
            if not decision.allowed:
                reason = decision.reason or "denied"
                return f"the caller may not {policy.action!r}: {reason}"
        return None

    async def _identify(self, request: Request) -> Any:
        """The caller, resolving them through the app's backend if need be.

        `MCPAuth` publishes an identity for the whole endpoint, and when it is
        absent this is the fallback that makes `expose_routes` work at all: a
        route protected by the application's own session or bearer backend is
        exposed as a tool, and the identity that route would have seen has to be
        resolved from somewhere. Done lazily, on the first declaration that
        needs it, so an endpoint of ungated tools never runs the backend.
        """
        identity = request.identity
        if identity is not None:
            return identity
        backend = getattr(self._app, "_auth_backend", None)
        if backend is None:
            return None
        identity = await backend.authenticate(request)
        request._set_identity(identity)
        return identity

    async def _invoke(
        self, tool: Tool, request: Request, kwargs: dict[str, Any]
    ) -> tuple[Any, str]:
        self.tool_calls += 1
        try:
            return _render_result(await tool.handler(request, **kwargs)), _record.OUTCOME_OK
        except ToolError as error:
            self.tool_errors += 1
            return _text_result(str(error), is_error=True), _record.OUTCOME_TOOL_ERROR
        except Exception as error:  # noqa: BLE001 - see below; counted, never silent
            # A tool is application code at a protocol boundary, and one that
            # raises must not take the session down with it -- the model is
            # entitled to learn that this call failed and try another. The
            # failure is counted in `tool_errors`, distinct from
            # `schema_rejections`, `unauthorized_calls` and `throttled`, so a
            # deployment can tell a broken tool from a confused caller, a
            # refused one, and a busy one. `CancelledError` is a `BaseException`
            # and passes straight through, which is what makes cancellation work.
            #
            # Only the exception's type travels. Its message is application
            # detail written for an operator, not for whoever is driving the
            # model; a tool that wants to say something to the caller raises
            # `ToolError`. The whole failure is on the Flight Recorder marker.
            self.tool_errors += 1
            return (
                _text_result(f"the tool raised {type(error).__name__}", is_error=True),
                _record.OUTCOME_RAISED,
            )

    # -- responses -------------------------------------------------------

    def _reply(self, body: bytes, *, stream: bool, session_id: str | None = None) -> Any:
        headers: list[tuple[bytes, bytes]] = []
        if session_id is not None:
            headers.append((b"mcp-session-id", session_id.encode("ascii")))
        if stream:
            return SSEResponse(_single_event(body), headers=headers)
        return Response(body, media_type=b"application/json", headers=headers)

    def _transport_error(
        self,
        status: int,
        code: int,
        message: str,
        data: Any = None,
        *,
        identifier: Any = None,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> Response:
        """An HTTP-level refusal that still carries a readable JSON-RPC error.

        An empty 400 is the single most expensive failure an MCP client can
        meet, because nothing on the wire says which of a dozen preconditions
        was not met. Every refusal here names one.
        """
        return Response(
            encode_failure(identifier, code, message, data),
            status=status,
            media_type=b"application/json",
            headers=headers or [],
        )
