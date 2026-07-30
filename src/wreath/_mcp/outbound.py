"""Requests that go the other way, and the reentrancy they create.

Everything through stage 3 travelled client-to-server, or server-to-client as a
*notification* -- fire it at the queue and forget it. Sampling, elicitation and
`roots/list` are none of those: the server asks, and then waits for the client
to answer. That needs an id, a table of what is outstanding, a timeout, and a
way to give up; this module is all four and nothing else.

**The reentrancy is the whole problem.** A tool that elicits is awaiting the
client, on a POST the client is itself awaiting the answer to. It works here
because a `tools/call` already runs in its own task -- that is what made
`notifications/cancelled` implementable in stage 1 -- so the client's *next*
POST, carrying the response, is served by the endpoint while the first call is
still parked. The response is matched against this table and the parked task
wakes. A design that ran the call inline on the POST's own coroutine would
deadlock here and pass every test that never elicits.

Four ways out, and each has to be one of them rather than a hang:

- the client answers, and the future carries its result;
- the client answers with an error, which becomes a `ClientRequestError`;
- nobody answers within `MCPLimits.client_request_seconds`, and the wait ends
  with a timeout **and** a `notifications/cancelled` telling the client to stop
  working on a request nobody is waiting for any more;
- the session ends -- `DELETE`, idle expiry, a cancelled outer call -- and every
  outstanding future is failed rather than left for the garbage collector.

The table is bounded by `MCPLimits.max_pending_requests` because it is the one
structure here a client can grow for free: asking for work and never replying
costs it nothing and costs the server a future per attempt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .._json import dumps as _json_dumps

#: Prefix on every id Wreath mints for a request of its own. JSON-RPC gives each
#: direction its own id space, so a client is free to use the integer 1 for its
#: own request while this one is outstanding; the prefix means a reader of a
#: transcript never has to work out which side an id belongs to.
ID_PREFIX = "wreath-"


class ClientRequestError(Exception):
    """A server-to-client request could not be sent, or was not answered.

    Raised inside the tool that asked, so it is caught with an ordinary
    `try`/`except` and answered with a `ToolError` the model can read, or left
    to the boundary -- where it counts in `tool_errors` and the model is told
    only the type, like any other unplanned failure.

    The message always names which of the four endings happened: the client
    never advertised the capability, the session already has as many questions
    outstanding as it may, the client answered with an error, or nobody answered
    at all.
    """

    __slots__ = ()


class ClientChannel:
    """One session's outstanding server-to-client requests.

    Owned by the session rather than by the server, for the same reason
    `in_flight` is: a request belongs to one conversation, and matching a
    response across sessions would let one client answer another's question.
    """

    __slots__ = ("_max_pending", "_pending", "_publish", "_seq", "_timeout", "timeouts")

    def __init__(
        self,
        publish: Callable[[bytes], bool],
        *,
        max_pending: int,
        timeout: float,
    ) -> None:
        self._publish = publish
        self._max_pending = max_pending
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._seq = 0
        #: Requests this session gave up waiting for. Counted per session as
        #: well as per server: "this one client is not answering" and "the
        #: timeout is too short for everyone" look identical in a single total.
        self.timeouts = 0

    def __len__(self) -> int:
        return len(self._pending)

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Ask the client `method` and wait for its answer.

        Raises:
            ClientRequestError: The pending table is full, the notification
                queue would not take the request, the client answered with an
                error, the session ended, or nobody answered in time.
        """
        if len(self._pending) >= self._max_pending:
            raise ClientRequestError(
                f"this session already has {self._max_pending} unanswered "
                "server-to-client requests, its "
                "`MCPLimits(max_pending_requests=...)` ceiling. A client that "
                "is not answering must not be able to make the server hold a "
                "future per question."
            )
        self._seq += 1
        identifier = f"{ID_PREFIX}{self._seq}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        framed = _json_dumps(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        try:
            if not self._publish(framed):
                raise ClientRequestError(
                    f"the {method} request could not be queued: this session's "
                    "notification queue is full, which means nothing is reading "
                    "its `GET` stream. Open the stream before asking the client "
                    "for anything, or raise "
                    "`MCPLimits(max_pending_notifications=...)`."
                )
            try:
                return await asyncio.wait_for(future, self._timeout)
            except TimeoutError as error:
                self.timeouts += 1
                self._withdraw(identifier)
                raise ClientRequestError(
                    f"the client did not answer {method} within "
                    f"{self._timeout:.0f}s. It has been told to stop working on "
                    "it; raise "
                    "`MCPLimits(client_request_seconds=...)` if a person is "
                    "expected to be reading."
                ) from error
            except asyncio.CancelledError:
                # The outer call was cancelled -- `notifications/cancelled` on
                # the `tools/call`, a `DELETE`, a client hanging up. The inner
                # question is cancelled with it: leaving the client working on
                # an answer nobody will read is the leak this whole module is
                # about, one hop further out.
                self._withdraw(identifier)
                raise
        finally:
            self._pending.pop(identifier, None)

    def resolve(self, identifier: Any, result: Any, error: dict[str, Any] | None) -> bool:
        """Deliver one client response. False when nothing was waiting for it.

        A response that matches nothing is dropped rather than refused: a client
        that answers twice, or answers a request that has already timed out, is
        racing rather than misbehaving, and the timeout already told it so.
        """
        if not isinstance(identifier, str):
            return False
        future = self._pending.get(identifier)
        if future is None or future.done():
            return False
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            future.set_exception(
                ClientRequestError(
                    f"the client refused the request: {message or 'no reason given'}"
                )
            )
        else:
            future.set_result(result)
        return True

    def fail_all(self, reason: str) -> None:
        """End every outstanding request, so nothing is left awaiting a dead session.

        Called from the session's own teardown. Without it a `DELETE` arriving
        while a tool is eliciting leaves that tool parked on a future nobody
        will ever complete, which outlives the session it belonged to.
        """
        for future in list(self._pending.values()):
            if future.done():
                continue
            future.set_exception(ClientRequestError(reason))
            # Mark the exception retrieved here as well as by the awaiter: the
            # awaiter is usually being cancelled in the same breath, and a
            # future collected with an unretrieved exception logs a warning
            # about a failure that was handled.
            future.exception()
        self._pending.clear()

    def _withdraw(self, identifier: str) -> None:
        """Tell the client to stop working on a request nobody is waiting for."""
        self._publish(
            _json_dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": identifier},
                }
            )
        )


__all__ = ["ID_PREFIX", "ClientChannel", "ClientRequestError"]
