"""The bounds an MCP endpoint runs inside, in one object.

Every number here answers the same question: what does a caller who is not
acting in good faith get to consume? An MCP endpoint is unusual in that its
callers are *models*, so "a client would never do that" is not an argument --
a model will call the same tool in a loop, open a session per turn, and pass an
argument nobody wrote a test for.

**Payload size is deliberately absent.** A `tools/call` body is a POST body, and
Wreath already refuses an oversized one at `RequestLimits.max_body_bytes` before
the endpoint is entered. A second ceiling here would be a second place to
configure, a second place to forget, and a check that runs after the bytes have
already been buffered. Set it where it was always set:

    app = Wreath(limits=RequestLimits(max_body_bytes=256 * 1024))
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class MCPLimits:
    """Bounds for one `MCP` server.

    Attributes:
        max_tools: Tools one server may declare. Registration past it raises,
            at import time rather than at call time, because a `tools/list` a
            model cannot hold in its context is a design problem and not a
            runtime one.
        max_sessions: Concurrently live sessions. `initialize` is the one method
            that can be reached without a session, so before authentication is
            configured every POST that lands can mint one; a dictionary with no
            ceiling is then a memory leak with a network interface.
        max_concurrent_calls: Calls one session may have in flight at once. The
            bound is per session rather than per server so that one busy client
            cannot starve another, and it is small because a model that fans out
            is usually looping rather than working.
        session_idle_seconds: How long a session may go unused before it is
            collected. A client that never sends `DELETE` is the normal case,
            not the exception -- a process crashes, a laptop sleeps, a model
            stops mid-conversation -- so without this every abandoned session is
            held until restart and `max_sessions` is reached by attrition. Set
            it to `None` to keep sessions until they are ended explicitly.
        max_resources: Resources one server may declare, for the same reason
            `max_tools` exists: `resources/list` is text a model reads.
        max_prompts: Prompts one server may declare.
        max_subscriptions: URIs one session may hold a `resources/subscribe` on.
            A subscription costs a set entry and a fan-out visit per change, and
            a client is free to subscribe to every resource it can list, so the
            ceiling is what keeps that from being unbounded per session.
        max_pending_notifications: Notifications one session may hold while its
            server-to-client stream is closed or slow. The queue is bounded
            because a client that opens no stream must not be able to make the
            server buffer for it forever; past the ceiling the *newest*
            notification is dropped and counted in `MCP.stats()`, because a
            dropped notification that nobody counts is the silent degradation
            this codebase keeps finding.
        stream_keepalive_seconds: How long the notification stream may stay
            silent before it emits an SSE comment. Nothing in `SSEResponse` is
            emitted on a timer, deliberately, so an idle stream is invisible to
            an intermediary that reaps idle connections unless the application
            says something -- this is the application saying it.
        max_pending_requests: Server-to-client requests one session may have
            outstanding at once -- a `sampling/createMessage`, an
            `elicitation/create`, a `roots/list`. Each one holds a future and an
            entry in a table until the client answers it, so an unbounded table
            is a memory leak a client can drive simply by asking for work and
            never replying. Small on purpose: a tool that has three questions
            outstanding for one user is not waiting on a person any more.
        client_request_seconds: How long to wait for the client's answer to one
            of those requests. A client that never answers must not be able to
            pin a session's call slot for the session's whole idle life, which
            is the deadlock this number exists to make impossible.
        max_file_bytes: The largest file `ToolContext.read_file` will return.
            A resource read is a JSON-RPC result held whole in memory and then
            base64-encoded, so the ceiling is on what one answer may cost rather
            than on what the filesystem happens to hold.
        max_result_bytes: Largest serialized tool or resource result.

    Raises:
        ValueError: A bound is not positive.
    """

    max_tools: int = 256
    max_sessions: int = 1024
    max_concurrent_calls: int = 8
    session_idle_seconds: float | None = 900.0
    max_resources: int = 256
    max_prompts: int = 128
    max_subscriptions: int = 256
    max_pending_notifications: int = 64
    stream_keepalive_seconds: float = 15.0
    max_pending_requests: int = 4
    client_request_seconds: float = 30.0
    max_file_bytes: int = 1 << 20
    max_result_bytes: int = 1 << 20

    def __post_init__(self) -> None:
        for field_name in (
            "max_tools",
            "max_sessions",
            "max_concurrent_calls",
            "max_resources",
            "max_prompts",
            "max_subscriptions",
            "max_pending_notifications",
            "max_pending_requests",
            "max_file_bytes",
            "max_result_bytes",
        ):
            value = getattr(self, field_name)
            if type(value) is not int:
                raise TypeError(f"MCPLimits.{field_name} must be an integer")
            if value < 1:
                raise ValueError(f"MCPLimits.{field_name} must be at least 1")
        for field_name in ("stream_keepalive_seconds", "client_request_seconds"):
            value = getattr(self, field_name)
            if type(value) not in (int, float):
                raise TypeError(f"MCPLimits.{field_name} must be an int or float")
        if self.stream_keepalive_seconds <= 0 or not isfinite(self.stream_keepalive_seconds):
            raise ValueError("MCPLimits.stream_keepalive_seconds must be positive and finite")
        if self.client_request_seconds <= 0 or not isfinite(self.client_request_seconds):
            raise ValueError("MCPLimits.client_request_seconds must be positive and finite")
        idle = self.session_idle_seconds
        if idle is not None and type(idle) not in (int, float):
            raise TypeError("MCPLimits.session_idle_seconds must be an int or float, or None")
        if idle is not None and (idle <= 0 or not isfinite(idle)):
            raise ValueError(
                "MCPLimits.session_idle_seconds must be positive and finite, or None to "
                "keep sessions until they are ended explicitly"
            )


@dataclass(frozen=True, slots=True)
class ToolRateLimit:
    """How often one caller may invoke one tool.

    Declared per tool rather than per endpoint, because the tools on one server
    are not alike: listing yesterday's sightings is cheap and idempotent, and
    the one that sends an email is neither. A single ceiling across the endpoint
    would have to be set for the expensive tool and would then make the cheap
    one useless.

    The bucket is keyed on the caller -- so one client cannot spend another's
    allowance. Declaring a ceiling is itself enough to resolve who the caller is:
    a bounded tool authenticates the request before it charges the bucket,
    through `MCPAuth` when the endpoint carries one and through the application's
    own `app.configure_auth(...)` backend when it does not. Only when *neither*
    identifies anybody does the key fall back to the session -- and a session is
    free, so on an endpoint with no authentication at all a per-session ceiling
    is a ceiling per `initialize`, which is to say none.

    Attributes:
        limit: Calls allowed per `window`.
        window: The window, in seconds.
        burst: Calls allowed back to back before the sustained rate binds.
            Defaults to `limit`. A model that fans out a plan into five parallel
            calls is not abusing anything, and a burst of one would refuse it.

    Raises:
        ValueError: A bound is not positive. A `burst` below `limit` is allowed
            and simply means the window's full allowance is never reachable in
            one go, which is a legitimate shape rather than a mistake.
    """

    limit: int
    window: float = 60.0
    burst: int | None = None

    def __post_init__(self) -> None:
        if type(self.limit) is not int:
            raise TypeError("ToolRateLimit.limit must be an integer")
        if self.limit < 1:
            raise ValueError("ToolRateLimit.limit must be at least 1")
        if type(self.window) not in (int, float):
            raise TypeError("ToolRateLimit.window must be an int or float")
        if self.window <= 0 or not isfinite(self.window):
            raise ValueError("ToolRateLimit.window must be positive and finite")
        if self.burst is not None and type(self.burst) is not int:
            raise TypeError("ToolRateLimit.burst must be an integer or None")
        if self.burst is not None and self.burst < 1:
            raise ValueError("ToolRateLimit.burst must be at least 1")

    @property
    def capacity(self) -> float:
        return float(self.limit if self.burst is None else self.burst)

    @property
    def rate(self) -> float:
        """Tokens per second, the shape `RateLimitStore.configure` wants."""
        return self.limit / self.window


#: What an `MCP` uses when the caller says nothing.
DEFAULT_LIMITS = MCPLimits()

__all__ = ["DEFAULT_LIMITS", "MCPLimits", "ToolRateLimit"]
