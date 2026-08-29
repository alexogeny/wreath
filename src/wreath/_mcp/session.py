"""Sessions: their identity, their idle life, and the calls they may cancel.

Sessions are kept in memory, which is the right default for a single-process
deployment and the wrong one for a fleet: a client whose `Mcp-Session-Id` lands
on a worker that never saw `initialize` is told 404 and re-initializes, which is
correct but wasteful. Moving this behind `wreath.session_store` is a later
stage's job and is why the store is an object rather than a dictionary on the
server.

A session also owns its in-flight requests, because that is what makes
`notifications/cancelled` implementable: the notification names a request id,
and the only place that id maps to a running task is the session it arrived on.
Cancelling across sessions would let one client stop another's work.

It owns its **server-to-client notifications** for the same reason. A resource
subscription and a progress report are both addressed to one conversation, and
the queue they land in is bounded: a client that subscribes and then never opens
the `GET` stream must not be able to make the server buffer on its behalf
indefinitely. Past the ceiling the newest notification is dropped and counted,
never silently discarded -- the close sentinel is the one item that always gets
in, evicting the oldest if it must, because a stream that is never told to end
is a connection that never closes.


**A session ends when it is abandoned, not only when it is closed.** `DELETE` is
the polite path and it is not the common one -- a process is killed, a laptop
sleeps, a model stops mid-conversation -- so without an idle bound every
abandoned session is held until the process restarts, and `MCPLimits.max_sessions`
is reached by attrition rather than by load. Expiry is checked when the store is
touched rather than on a timer: there is no background task to supervise, no
task to leak, and a session nobody looks at costs nothing by existing.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from types import MappingProxyType
from typing import Any

from ..progress import ProgressReporter
from ..queue import Queue, QueueEmpty
from .outbound import ClientChannel

#: Bytes of entropy behind a session id. The identifier is a bearer credential
#: for the session's in-flight calls, so it is minted from `secrets`, not from a
#: counter or a uuid4 hex.
_SESSION_ID_BYTES = 32

#: Enqueued on a session's notification queue to end its stream. A distinct
#: object rather than `None`, so it can never collide with a framed payload.
CLOSE_STREAM = object()


@dataclass(slots=True)
class Session:
    """One initialized MCP session."""

    id: str
    protocol_version: str
    client_info: dict[str, Any] = field(default_factory=dict)
    #: Request id -> the task running it. Populated only while a call is in
    #: flight, so cancelling an id that already returned is a no-op rather than
    #: an error -- the race is normal, not exceptional.
    in_flight: dict[Any, asyncio.Task[Any]] = field(default_factory=dict)
    #: Framed JSON-RPC notifications waiting for this session's `GET` stream,
    #: bounded by `MCPLimits.max_pending_notifications`.
    notifications: Queue = field(default_factory=Queue)
    #: Resource URIs this session asked to be told about.
    subscriptions: set[str] = field(default_factory=set)
    #: Whether a server-to-client stream is open. The specification allows one
    #: per session: two would split the notifications between them at random.
    stream_open: bool = False
    #: Notifications this session lost to a full queue. Counted per session as
    #: well as per server, because "one client is not reading" and "the server
    #: is over-producing" look identical in a single total.
    dropped: int = 0
    #: `time.monotonic()` at the last message on this session. Monotonic rather
    #: than wall time: a session must not outlive its idle bound because someone
    #: stepped the clock back, nor die early because NTP stepped it forward.
    last_seen: float = 0.0
    #: The subject of the verified token that opened it, when the endpoint is
    #: protected. A later message on this session must come from the same
    #: subject; otherwise a leaked session id would be a credential in its own
    #: right, which is exactly what it must not be.
    principal: str | None = None
    #: What the client said it could do in `initialize`. A server-to-client
    #: request for a capability that is not in here is refused immediately and
    #: with a message, rather than sent to a client that will never answer it --
    #: which would be a hang the client author has no way to diagnose.
    client_capabilities: dict[str, Any] = field(default_factory=dict)
    #: This session's outstanding server-to-client requests, or None until
    #: `SessionStore.create` builds one.
    channel: ClientChannel | None = None
    #: Filesystem roots the client declared, or None when they have not been
    #: asked for yet. Cached because `roots/list` is a round trip to a client
    #: that may be a person's laptop, and invalidated by
    #: `notifications/roots/list_changed` rather than by a clock.
    roots: tuple[str, ...] | None = None

    def touch(self, now: float) -> None:
        self.last_seen = now

    def publish(self, payload: bytes) -> bool:
        """Queue one framed notification. False when the queue was full.

        Dropping rather than blocking is the only answer available: the caller
        is a tool reporting progress or a handler saying a row changed, and
        neither may be made to wait on a client that is not reading.
        """
        # `offer` *is* this policy: keep it if there is room, otherwise refuse
        # and count. The try/except around `put_nowait` said the same thing in
        # four more lines, and the queue counts its own drops now -- `dropped`
        # here stays because per-session and per-server totals answer different
        # questions, as the field's own comment says.
        if self.notifications.offer(payload):
            return True
        self.dropped += 1
        return False

    def close_stream(self) -> None:
        """Tell this session's stream, if any, to end.

        The sentinel evicts the oldest notification when the queue is full,
        which is the one place dropping the *oldest* is right: a stream that
        never learns it should close is a connection nobody closes.
        """
        while not self.notifications.offer(CLOSE_STREAM):
            try:
                self.notifications.get_nowait()
            except QueueEmpty:  # pragma: no cover - full, then emptied by the reader
                return
            self.dropped += 1


class SessionStore:
    """In-memory sessions, bounded in count and in idle life.

    The count bound matters more than it looks. `initialize` is the one method
    that needs no session, so on an endpoint with no `MCPAuth` in front of it,
    every POST that arrives can mint one. A dictionary with no ceiling is then a
    memory leak with a network interface. Refusing at the ceiling is not a rate
    limit and is not pretending to be one; it is the floor under the worst case.
    """

    __slots__ = (
        "_idle_seconds",
        "_max_pending",
        "_max_pending_requests",
        "_max_sessions",
        "_next_sweep",
        "_publish",
        "_request_seconds",
        "_sessions",
        "_subscribers",
        "expired",
    )

    def __init__(
        self,
        *,
        max_sessions: int,
        idle_seconds: float | None,
        max_pending_notifications: int = 64,
        max_pending_requests: int = 4,
        client_request_seconds: float = 30.0,
        publish: Callable[[Session, bytes], bool] | None = None,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        if idle_seconds is not None and idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive, or None for no expiry")
        if max_pending_notifications < 1:
            raise ValueError("max_pending_notifications must be at least 1")
        self._max_sessions = max_sessions
        self._idle_seconds = idle_seconds
        self._max_pending = max_pending_notifications
        self._max_pending_requests = max_pending_requests
        self._request_seconds = client_request_seconds
        self._next_sweep = float("inf")
        # One funnel for everything the server puts on a session's queue, so a
        # dropped server-to-client *request* is counted in exactly the same
        # place as a dropped notification. Defaults to the session's own
        # `publish` when nothing is watching, which is what a bare store in a
        # test wants.
        self._publish: Callable[[Session, bytes], bool] = (
            (lambda session, payload: session.publish(payload)) if publish is None else publish
        )
        self._sessions: dict[str, Session] = {}
        self._subscribers: dict[str, dict[str, Session]] = {}
        #: Sessions collected for going idle. Counted because "the client says
        #: it re-initializes constantly" and "the idle bound is too short" look
        #: identical from the outside until this number is read.
        self.expired = 0

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def at_capacity(self) -> bool:
        return len(self._sessions) >= self._max_sessions

    def create(
        self,
        *,
        protocol_version: str,
        client_info: dict[str, Any],
        client_capabilities: dict[str, Any] | None = None,
        principal: str | None = None,
        now: float | None = None,
    ) -> Session:
        """Mint a session, first collecting any that have gone idle.

        Sweeping here rather than on a timer is what keeps the ceiling honest:
        the one operation that can hit the ceiling is the one that first frees
        whatever no longer deserves to hold it.

        Raises:
            RuntimeError: The store is still at capacity after the sweep.
        """
        moment = time.monotonic() if now is None else now
        self.sweep(moment, force=self.at_capacity)
        if self.at_capacity:
            raise RuntimeError(
                f"the MCP session store is at its ceiling of {self._max_sessions} "
                "sessions. Raise `MCPLimits(max_sessions=...)` if this is a "
                "legitimate load, lower `session_idle_seconds` if the sessions "
                "are abandoned rather than busy, and put `MCPAuth` in front of "
                "the endpoint if the load is not legitimate at all."
            )
        session = Session(
            id=secrets.token_urlsafe(_SESSION_ID_BYTES),
            protocol_version=protocol_version,
            client_info=client_info,
            client_capabilities=client_capabilities or {},
            last_seen=moment,
            principal=principal,
            notifications=Queue(capacity=self._max_pending),
        )
        session.channel = ClientChannel(
            partial(self._publish, session),
            max_pending=self._max_pending_requests,
            timeout=self._request_seconds,
        )
        self._sessions[session.id] = session
        if self._idle_seconds is not None:
            self._next_sweep = min(self._next_sweep, moment + self._idle_seconds)
        return session

    def get(self, identifier: str, *, now: float | None = None) -> Session | None:
        """The named session, or None when it is unknown or has gone idle.

        A found session is touched, so "idle" means *no traffic*, not "old". A
        conversation that runs for a day never expires under it, and one that
        stops for an hour does.
        """
        session = self._sessions.get(identifier)
        if session is None:
            return None
        moment = time.monotonic() if now is None else now
        if self._expired(session, moment):
            self._collect(session)
            return None
        session.touch(moment)
        return session

    def subscribers(self, uri: str) -> list[Session]:
        return list(self._subscribers.get(uri, {}).values())

    def subscribe(self, session: Session, uri: str) -> None:
        if uri in session.subscriptions:
            return
        session.subscriptions.add(uri)
        self._subscribers.setdefault(uri, {})[session.id] = session

    def unsubscribe(self, session: Session, uri: str) -> None:
        if uri not in session.subscriptions:
            return
        session.subscriptions.remove(uri)
        subscribers = self._subscribers[uri]
        subscribers.pop(session.id, None)
        if not subscribers:
            del self._subscribers[uri]

    def sweep(self, now: float | None = None, *, force: bool = False) -> int:
        """Collect every session past its idle bound. Returns how many."""
        idle_seconds = self._idle_seconds
        if idle_seconds is None:
            return 0
        if not self._sessions:
            self._next_sweep = float("inf")
            return 0
        moment = time.monotonic() if now is None else now
        if not force and moment < self._next_sweep:
            return 0
        stale = [session for session in self._sessions.values() if self._expired(session, moment)]
        for session in stale:
            self._collect(session)
        self._next_sweep = min(
            (session.last_seen + idle_seconds for session in self._sessions.values()),
            default=float("inf"),
        )
        return len(stale)

    def discard(self, identifier: str) -> bool:
        """End a session, cancelling anything it still has in flight."""
        session = self._sessions.pop(identifier, None)
        if session is None:
            return False
        self._drop_subscriptions(session)
        _cancel_all(session)
        if not self._sessions:
            self._next_sweep = float("inf")
        return True

    def _expired(self, session: Session, now: float) -> bool:
        idle = self._idle_seconds
        return idle is not None and now - session.last_seen >= idle

    def _collect(self, session: Session) -> None:
        if self._sessions.pop(session.id, None) is None:
            return
        self._drop_subscriptions(session)
        self.expired += 1
        _cancel_all(session)
        if not self._sessions:
            self._next_sweep = float("inf")

    def _drop_subscriptions(self, session: Session) -> None:
        for uri in session.subscriptions:
            subscribers = self._subscribers.get(uri)
            if subscribers is None:
                continue
            subscribers.pop(session.id, None)
            if not subscribers:
                del self._subscribers[uri]


def _cancel_all(session: Session) -> None:
    # Outstanding questions first, then the tasks that asked them. A tool parked
    # on `elicitation/create` is woken with a failure it can act on rather than
    # a bare cancellation, and anything the failure does not reach is cancelled
    # a line later -- neither order alone covers both, because a request may be
    # outstanding from a resource read that is not in `in_flight` at all.
    if session.channel is not None:
        session.channel.fail_all("the MCP session ended while this request was outstanding")
    for task in list(session.in_flight.values()):
        task.cancel()
    session.in_flight.clear()
    session.subscriptions.clear()
    session.roots = None
    # The stream goes with the session. A `GET` left running against a session
    # that has been deleted or collected would hold a connection open forever
    # and deliver nothing, which is worse than either half of that alone.
    session.close_stream()


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a running tool can learn about the call it is serving.

    Reachable from the handler as `request.state.mcp`, which keeps the ownership
    visible: it lives exactly as long as the request that carries it, and no
    global or context variable has to be consulted to find it.

    Cancellation arrives as `asyncio.CancelledError` inside the handler, because
    the call runs in its own task and `notifications/cancelled` cancels that
    task. A tool that holds a resource should therefore release it in a
    `finally:`, exactly as it would anywhere else; there is no cancellation flag
    to poll.

    It is also **the way out of this call and back to the client**: `sample`,
    `elicit`, `roots` and `read_file` are the three MCP methods that travel
    server-to-client, and they are methods here rather than free functions
    because each needs the session the call arrived on and the request it is
    being served under. The same task that makes cancellation work is what makes
    them possible: the client's answer arrives on a *different* POST, which the
    endpoint can only serve because this one is parked in a task of its own.

    Attributes:
        session_id: The `Mcp-Session-Id` this call arrived on.
        request_id: The JSON-RPC id, and what a cancellation would name.
        tool: The tool's registered name.
        progress_token: The client's `_meta.progressToken`, when it sent one.
            Present so a tool can tell whether anyone is listening; reporting
            progress does not depend on it.
        progress: A `wreath.progress.ProgressReporter` bound to this call.
            Always present, whether or not the client asked for progress:
            `reporter.update(42, "processing invoices")` writes to the server's
            `ProgressRegistry` exactly as it would from a durable job, and when
            the client *did* send a `progressToken` the server relays each
            report as a `notifications/progress` on the session's stream. There
            is no MCP-specific progress mechanism, deliberately -- the registry
            already models a task reporting a percentage and a message, already
            spans workers when it is given the message bus, and already has a
            status endpoint and an SSE stream of its own.
        identity: The verified caller, whenever the server had to resolve one --
            through `MCPAuth` on the endpoint, or through the application's own
            `app.configure_auth(...)` backend for a tool that is gated or rate
            limited. `None` for a tool that is neither, on an endpoint with no
            `MCPAuth`: nothing about that call depended on who was asking, so
            the backend was never run for it. Also published as
            `request.identity`, which is where the authorizer and every other
            Wreath component reads it; it is repeated here so a tool does not
            have to know which of the two is the real one.
        arguments: The call's raw `arguments` object, before binding. Present so
            a Cedar `resource=` resolver can name *which row* is being asked
            for: a route resolves its resource from the path, and a tool has no
            path to resolve it from. The bound, validated values arrive as the
            handler's own parameters; this is for the policy decision that has
            to happen first.
    """

    session_id: str
    request_id: Any
    tool: str
    progress_token: Any = None
    identity: Any = None
    arguments: Mapping[str, Any] = MappingProxyType({})
    progress: ProgressReporter | None = None
    #: The three things the outbound half needs, kept private because they are
    #: plumbing rather than facts about the call. A tool reaches them through
    #: `sample`, `elicit`, `roots` and `read_file`, never directly.
    _server: Any = None
    _session: Any = None
    _request: Any = None

    async def sample(
        self,
        messages: Any,
        *,
        max_tokens: int = 1024,
        system_prompt: str | None = None,
        temperature: float | None = None,
        stop_sequences: Any = None,
        model_preferences: Any = None,
        include_context: str | None = None,
        metadata: Any = None,
    ) -> dict[str, Any]:
        """Ask the client's model to generate, and wait for what it produced.

        `sampling/createMessage` is the one MCP method where the *server* is the
        one asking for work, and the tool doing the asking must have declared
        that it may: `@mcp.tool(sampling="Model::sample")` gates it on Cedar
        exactly as `action=` gates the call itself, and `sampling=True` declares
        it with no policy. A tool that declared neither cannot sample at all.
        The request also spends a token from the tool's *own* `rate_limit=`
        bucket, and leaves its own Flight Recorder marker beside the call's.

        `messages` is a string -- the common case, one `user` turn -- or a list
        of `{"role": ..., "content": ...}` mappings already in MCP's shape.

        Returns:
            The client's result: `role`, `content`, `model` and `stopReason`.

        Raises:
            ClientRequestError: The client never advertised the `sampling`
                capability, the tool did not declare `sampling=`, the caller was
                refused or throttled, or nobody answered.
        """
        return await self._server._sample(
            self,
            messages,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            temperature=temperature,
            stop_sequences=stop_sequences,
            model_preferences=model_preferences,
            include_context=include_context,
            metadata=metadata,
        )

    async def elicit(self, message: str, form: type[Any]) -> Any | None:
        """Ask the person at the other end to fill `form` in.

        `form` is a dataclass, and its fields *are* the requested schema: they go
        through `derive_input_schema`, the same path that renders a tool's
        `inputSchema`, and the answer comes back through the same binding
        validation. There is no second schema language and no hand-written
        check, which is why a field the client fills in wrongly is refused with
        the same error shape a bad `tools/call` argument gets.

        The tool doing the asking must have declared that it may:
        `@mcp.tool(elicitation="Form::ask")` gates it on Cedar exactly as
        `action=` gates the call itself, and `elicitation=True` declares it with
        no policy. **A tool that declared neither cannot elicit at all**, because
        a form renders inside a client UI the person already trusts and "they can
        decline" is the control social engineering exists to defeat. The request
        also spends a token from the tool's own `rate_limit=` bucket and leaves
        its own Flight Recorder marker beside the call's.

        Returns:
            An instance of `form`, or None when the person declined or cancelled.

        Raises:
            ClientRequestError: The client never advertised the `elicitation`
                capability, the tool did not declare `elicitation=`, the caller
                was refused or throttled, nobody answered, or the answer did not
                match the schema that was asked for.
            TypeError: `form` is not a dataclass, or has a field MCP cannot
                carry -- the specification allows primitives only.
        """
        return await self._server._elicit(self, message, form)

    async def roots(self) -> tuple[str, ...]:
        """The filesystem roots this client declared, as absolute paths.

        Asked of the client once per session with `roots/list` and cached until
        it sends `notifications/roots/list_changed`. Empty when the client did
        not advertise the capability, which is the case `read_file` treats as
        "the server's own root is the only bound".
        """
        return await self._server._roots(self._session)

    async def read_file(self, path: str) -> bytes:
        """Read a file beneath the server's `file_root`, and beneath the client's roots.

        Two confinements, both real. The bytes are opened through
        `wreath._fsguard` -- component by component beneath a trusted directory
        descriptor, refusing every symlink, the same walk static files and the
        template loader use -- so nothing outside `MCP(file_root=...)` is
        reachable however the path is spelled. And when the client declared
        roots, the file must also lie beneath one of them: a root a client
        declared is a boundary it expects the server to honour, not a hint.

        Raises:
            FileNotFoundError: No such file beneath the root.
            PermissionError: The path escapes the root, traverses a symlink, is
                not a regular file, is larger than `MCPLimits.max_file_bytes`,
                or lies outside every root the client declared.
            RuntimeError: This server was declared with no `file_root=`.
        """
        return await self._server._read_file(self._session, path)
