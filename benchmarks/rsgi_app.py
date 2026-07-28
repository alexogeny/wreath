"""The no-framework floor: a raw RSGI application served by Granian.

`blacksheep-granian` answers "what does the Rust server buy this app?". This
answers the question underneath it: **what is left when the framework is gone
entirely?** It is the same pairing as the Rust arm but from the other side --
`axum` is the ceiling with no interpreter in the request path, this is the
ceiling with an interpreter and nothing else. A framework's cost is the distance
between this row and its own, and without this row that distance is unmeasurable.

RSGI is Granian's native protocol, so nothing here pays for an ASGI bridge: the
server calls `__rsgi__` with a scope and a protocol object, and the handler puts
bytes on the wire itself.

**This arm has no router, and is excluded from the routing scenarios for that
reason** (see `_ROUTED_FRAMEWORKS` in scenarios.py). Dispatch below is a dict
lookup on an exact path plus one prefix test, which is not what a router does --
it does no parameter extraction, no method-based resolution, and no ordering. Put
it in the routing comparison and it would post the best number in the table for
work no other arm is allowed to skip.
"""

from __future__ import annotations

import json

from .scenarios import LARGE_RESPONSE_BODY

_TEXT = [("content-type", "text/plain; charset=utf-8")]
_JSON = [("content-type", "application/json")]
_CACHED = [
    ("content-type", "text/plain; charset=utf-8"),
    ("cache-control", "public, max-age=60"),
]


async def _plaintext(scope, protocol) -> None:
    protocol.response_str(200, _TEXT, "hello, world")


async def _json(scope, protocol) -> None:
    protocol.response_str(200, _JSON, '{"message":"hello"}')


async def _parameter(scope, protocol) -> None:
    # The whole of this arm's "routing": everything after the prefix is the id.
    user_id = scope.path[len("/users/") :]
    protocol.response_str(200, _JSON, json.dumps({"user_id": user_id}))


async def _header_lookup(scope, protocol) -> None:
    protocol.response_str(200, _TEXT, scope.headers.get("x-benchmark") or "")


async def _body(scope, protocol) -> None:
    body = await protocol()
    protocol.response_str(200, _TEXT, str(len(body)))


async def _json_body(scope, protocol) -> None:
    body = await protocol()
    protocol.response_str(200, _JSON, json.dumps(json.loads(body)))


async def _large(scope, protocol) -> None:
    protocol.response_bytes(200, _TEXT, LARGE_RESPONSE_BODY)


async def _cached(scope, protocol) -> None:
    protocol.response_str(200, _CACHED, "cacheable")


_ROUTES = {
    "/": _plaintext,
    "/json": _json,
    "/headers": _header_lookup,
    "/body": _body,
    "/json-body": _json_body,
    "/response-64k": _large,
    "/cached": _cached,
}


async def app(scope, protocol) -> None:
    """Granian's RSGI entry point.

    A plain module-level function rather than an object with an `__rsgi__`
    method: Granian resolves the target as `getattr(target, '__rsgi__', target)`,
    so both work, and this one saves a bound-method call on every request. On an
    arm whose whole purpose is to measure what is left when the framework is
    gone, that is exactly the kind of cost that should not be in the number.
    """
    handler = _ROUTES.get(scope.path)
    if handler is not None:
        await handler(scope, protocol)
    elif scope.path.startswith("/users/"):
        await _parameter(scope, protocol)
    else:
        protocol.response_str(404, _TEXT, "not found")
