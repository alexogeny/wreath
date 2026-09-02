"""A first-party Model Context Protocol server, over the routes you already have.

An MCP server is a small protocol wrapped around several hard problems: a
schema for every callable a model may invoke, a session that survives across
calls, cancellation that actually stops work, an audit trail of what was called
with what, and an authorization boundary in front of all of it. Wreath owns
every one of those already -- the binding layer computes the schemas, the
Flight Recorder keeps the record, Cedar decides -- so serving MCP here is
wiring rather than a new stack.

Declare a server, decorate the callables a model should be able to reach, and
the endpoint is an ordinary route:

```python
from dataclasses import dataclass
from typing import Annotated

from wreath import Wreath
from wreath.binding import Body
from wreath.mcp import MCP, ToolError

app = Wreath()
mcp = MCP(app, name="camera-trap", version="1.0.0", path="/mcp")


@dataclass
class SightingQuery:
    species: str
    since: str | None = None


@mcp.tool(description="Find recent sightings of a species.")
async def find_sightings(request, query: Annotated[SightingQuery, Body()]) -> dict:
    if query.species not in KNOWN_SPECIES:
        raise ToolError(f"no camera is trained on {query.species!r}")
    return {"sightings": [...]}
```

The annotations that would bind an HTTP request body are the tool's
`inputSchema`, rendered by the same code that renders the OpenAPI document.
There is no second declaration syntax, no second validator, and a test asserts
that the two renderings agree so they cannot drift apart later.

**Point it at real work and it needs a boundary.** Pass `auth=MCPAuth(...)` and
the endpoint becomes an OAuth 2.1 resource server: it publishes
`/.well-known/oauth-protected-resource`, answers a request with no token with a
`401` naming that document, and refuses a token minted for a *different*
resource -- the confused-deputy failure the specification exists to prevent.
Gate individual tools with `@mcp.tool(action=...)`, which is the same Cedar
decision `@authorize` makes on a route, and bound them with
`rate_limit=ToolRateLimit(...)`. Every call leaves one Flight Recorder marker
carrying the tool, the caller, the duration and the outcome, with the arguments
beside it under the recording policy that already hides a password.

**More than tools.** `@mcp.resource(uri, ...)` declares something a model
*reads* rather than does, and a client may `resources/subscribe` to it: open the
`GET` stream and `mcp.notify_resource_updated(uri)` tells every subscriber it
changed. `@mcp.prompt(...)` declares text a *person* chooses -- a slash command,
a menu entry -- whose arguments are a flat map of strings, refused at
registration if they are annotated as anything else. A long-running tool reports
through `request.state.mcp.progress`, which is an ordinary
`wreath.progress.ProgressReporter`: the server relays what it writes as
`notifications/progress`, so there is no MCP-specific progress mechanism to
learn, and a `ProgressRegistry` given the message bus carries a durable job's
progress across workers to whichever one holds the stream.

**Routes you already have.** `expose_routes(mcp, app, tags=("public",))` turns
selected routes into tools. It takes an explicit selector and has no `all=True`:
converting an application's whole HTTP surface into model-callable actions in
one line includes the destructive half, and nobody reviews a line that short. A
route with no docstring is refused, because the description is the entire basis
on which a model decides to call it, and an exposed route keeps whatever
`@authorize`, `@roles`, `@permissions` or `@second_factor` it was behind --
exposing a route is never a way around what was put in front of it. The last of
those is the interesting one: a route behind `@second_factor(max_age=300)` is a
tool the model may call only while the *person* has re-proved a factor recently,
which is how "the model may read, and the human must step up before the model may
delete" is written down. A tool that was never a route says the same thing with
`@mcp.tool(second_factor=300)`, so writing it does not require inventing a route
to hang it on.

**Asking the client something.** Three MCP methods travel the other way, and a
tool reaches all three through `request.state.mcp`. `sample(...)` asks the
client's model to generate -- declared per tool with `@mcp.tool(sampling=...)`,
because whether a tool may spend the caller's model is a policy question, and
gated, throttled and recorded exactly as the call itself is. `elicit(message,
Form)` asks the person to fill a dataclass in: its fields are the requested
schema, through the same derivation a tool's `inputSchema` comes from, and the
answer is validated by the same binding layer and recorded under the same
redaction. It is declared and gated the same way, with
`@mcp.tool(elicitation=...)`, because a form renders inside a client UI the
person already trusts -- a tool asking for an API key is a phishing surface
wearing that chrome, and "they can decline" is the control social engineering is
built to walk through. `read_file(path)` reads beneath `MCP(file_root=...)` through
`wreath._fsguard`, and beneath whatever `roots` the client declared -- a
declared root is a boundary here, not a decoration. Each is a request the client
must answer; a client that never does gets a bounded wait and then a
`ClientRequestError`, never a session parked forever.

**Locally, over stdio.** `wreath mcp stdio app:app` puts the same ASGI
application behind a stdin/stdout pipe for an editor that speaks only that
transport. It is a byte relay over the in-process transport, not a second
server: the routing, the authentication, the limits and the record are the ones
the HTTP endpoint has.

**What is not here.** `logging/setLevel`, stream resumption by `Last-Event-ID`,
templated resources and `listChanged` notifications. Each answers `method not
found` and says which stage it is waiting for, rather than failing in a way a
client author has to reverse-engineer.

Reference: [MCP](../reference/mcp.md). Guide: [Serving MCP tools](../guides/mcp.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._mcp.auth import MCPAuth
    from ._mcp.limits import MCPLimits, ToolRateLimit
    from ._mcp.outbound import ClientRequestError
    from ._mcp.prompts import Prompt
    from ._mcp.protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, JsonRpcError
    from ._mcp.registry import Tool, ToolSignatureError
    from ._mcp.resources import Resource
    from ._mcp.routes import expose_routes
    from ._mcp.server import MCP, ToolError
    from ._mcp.session import ToolContext

__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "MCP",
    "ClientRequestError",
    "JsonRpcError",
    "MCPAuth",
    "MCPLimits",
    "Prompt",
    "Resource",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRateLimit",
    "ToolSignatureError",
    "expose_routes",
]

_EXPORTS = {
    "PROTOCOL_VERSION": "protocol",
    "SUPPORTED_PROTOCOL_VERSIONS": "protocol",
    "MCP": "server",
    "ClientRequestError": "outbound",
    "JsonRpcError": "protocol",
    "MCPAuth": "auth",
    "MCPLimits": "limits",
    "Prompt": "prompts",
    "Resource": "resources",
    "Tool": "registry",
    "ToolContext": "session",
    "ToolError": "server",
    "ToolRateLimit": "limits",
    "ToolSignatureError": "registry",
    "expose_routes": "routes",
}

_MODULE_EXPORTS = {
    "auth": ("MCPAuth",),
    "limits": ("MCPLimits", "ToolRateLimit"),
    "outbound": ("ClientRequestError",),
    "prompts": ("Prompt",),
    "protocol": ("PROTOCOL_VERSION", "SUPPORTED_PROTOCOL_VERSIONS", "JsonRpcError"),
    "registry": ("Tool", "ToolSignatureError"),
    "resources": ("Resource",),
    "routes": ("expose_routes",),
    "server": ("MCP", "ToolError"),
    "session": ("ToolContext",),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    loaded = import_module(f"._mcp.{module}", __package__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
