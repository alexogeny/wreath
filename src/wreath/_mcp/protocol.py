"""JSON-RPC 2.0 envelopes, and the MCP revision Wreath implements.

The wire format is four keys deep, so there is no parser here beyond what the
shape needs: `wreath._json` already decodes and encodes it, and this module only
decides what a decoded value *is* — a request, a notification, or a response —
and refuses the shapes MCP does not allow.

The protocol revision is pinned rather than inferred. A client that asks for a
revision this build does not implement is told which ones it does, in the
`initialize` result, and a `MCP-Protocol-Version` header naming an unknown
revision on a later request is a JSON-RPC error with a readable message rather
than a bare 400. Both behaviours exist because "the spec moved" is the failure
mode this surface is most likely to meet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._json import dumps as _json_dumps

#: The revision Wreath speaks by default and advertises from `initialize`.
PROTOCOL_VERSION = "2025-06-18"

#: Every revision this build implements, newest first. Negotiation picks the
#: client's requested revision when it appears here and falls back to
#: `PROTOCOL_VERSION` otherwise, which is what the specification asks a server
#: to do: answer with something it supports and let the client decide.
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)

# JSON-RPC 2.0 reserved codes. MCP adds no codes of its own in this revision;
# a tool's own failure is a *result* with `isError`, not one of these.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# JSON-RPC reserves -32000..-32099 for an implementation's own server errors.
# The specification defines exactly one of them, and the rest are Wreath's. They
# exist because "the call was refused" and "the call failed" must not arrive as
# the same code -- a client that cannot tell them apart retries the one it
# should not.
#: The caller is authenticated but this tool's Cedar policy said no.
UNAUTHORIZED = -32001
#: MCP's specified code for a `resources/read` URI the server does not serve.
RESOURCE_NOT_FOUND = -32002
#: The caller is over this tool's rate limit. `data.retryAfter` is in seconds.
RATE_LIMITED = -32003
#: The session already has as many calls in flight as `MCPLimits` allows.
TOO_MANY_CALLS = -32004


class JsonRpcError(Exception):
    """A transport- or protocol-level failure, rendered as a JSON-RPC error.

    This is deliberately *not* how a tool reports that it could not do its job:
    a model needs to see that failure as a result it can reason about, which is
    what `wreath.mcp.ToolError` produces. Reserve this for envelopes the server
    could not honour at all.
    """

    __slots__ = ("code", "data", "message")

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class Message:
    """One decoded JSON-RPC message.

    Exactly one of `is_request`, `is_notification` and `is_response` is true.
    A request carries an `id` the reply must echo; a notification carries none
    and must never be replied to; and a **response is the answer to a request
    Wreath itself sent** -- `sampling/createMessage`, `elicitation/create`,
    `roots/list` all travel server-to-client, so `result` and `error` are
    carried here rather than discarded. `session.channel` is what an id is
    matched against; an id that matches nothing is dropped, because a client
    that answers twice, or answers something already timed out, is a race rather
    than a protocol violation.
    """

    method: str | None
    params: dict[str, Any]
    id: Any = None
    is_request: bool = False
    is_notification: bool = False
    is_response: bool = False
    result: Any = None
    error: dict[str, Any] | None = None


def parse_message(payload: Any) -> Message:
    """Classify one decoded JSON value as a JSON-RPC message.

    Raises:
        JsonRpcError: The value is not a single, well-formed JSON-RPC message.
    """
    if isinstance(payload, list):
        raise JsonRpcError(
            INVALID_REQUEST,
            "JSON-RPC batching is not supported. The MCP revision Wreath "
            f"implements ({PROTOCOL_VERSION}) removed it; send one message per "
            "request.",
        )
    if not isinstance(payload, dict):
        raise JsonRpcError(INVALID_REQUEST, "a JSON-RPC message must be a JSON object")
    if payload.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, 'a JSON-RPC message must carry `"jsonrpc": "2.0"`')

    has_id = "id" in payload
    identifier = payload.get("id")
    if has_id and identifier is None:
        # JSON-RPC allows a null id; MCP forbids it, because a null id cannot
        # be correlated with anything and reads as a notification to half the
        # implementations that meet it.
        raise JsonRpcError(INVALID_REQUEST, "a request id must not be null")
    if has_id and (isinstance(identifier, bool) or not isinstance(identifier, str | int)):
        raise JsonRpcError(INVALID_REQUEST, "a request id must be a string or an integer")

    method = payload.get("method")
    if method is None:
        if "method" in payload or "params" in payload:
            raise JsonRpcError(
                INVALID_REQUEST,
                "a JSON-RPC response must not carry request members `method` or `params`",
            )
        if not has_id:
            raise JsonRpcError(INVALID_REQUEST, "a JSON-RPC message must carry a method or an id")
        if "result" in payload and "error" in payload:
            raise JsonRpcError(
                INVALID_REQUEST,
                "a JSON-RPC response must carry either a `result` or an `error`, not both",
            )
        error = payload.get("error")
        if "error" in payload and not isinstance(error, dict):
            raise JsonRpcError(INVALID_REQUEST, "a JSON-RPC `error` must be a JSON object")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, bool) or not isinstance(code, int):
                raise JsonRpcError(
                    INVALID_REQUEST,
                    "a JSON-RPC `error` must carry an integer `code`",
                )
            if not isinstance(error.get("message"), str):
                raise JsonRpcError(
                    INVALID_REQUEST,
                    "a JSON-RPC `error` must carry a string `message`",
                )
        if "error" not in payload and "result" not in payload:
            raise JsonRpcError(
                INVALID_REQUEST,
                "a JSON-RPC response must carry either a `result` or an `error`",
            )
        return Message(
            method=None,
            params={},
            id=identifier,
            is_response=True,
            result=payload.get("result"),
            error=error,
        )
    if not isinstance(method, str):
        raise JsonRpcError(INVALID_REQUEST, "a JSON-RPC method must be a string")
    if "result" in payload or "error" in payload:
        raise JsonRpcError(
            INVALID_REQUEST,
            "a JSON-RPC request must not carry response members `result` or `error`",
        )

    params = payload.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        # JSON-RPC permits positional params; MCP defines every method with a
        # named-parameter object, so a list here is a client bug worth naming.
        raise JsonRpcError(INVALID_PARAMS, "`params` must be a JSON object")

    if has_id:
        return Message(method=method, params=params, id=identifier, is_request=True)
    return Message(method=method, params=params, is_notification=True)


def encode_success(identifier: Any, result: Any) -> bytes:
    """Encode a successful reply. `result` may already be serialized bytes.

    Accepting bytes is what lets `tools/list` serialize its payload once at
    registration time and pay only an envelope concatenation per call.
    """
    if isinstance(result, bytes):
        return b'{"jsonrpc":"2.0","id":%s,"result":%s}' % (_json_dumps(identifier), result)
    return _json_dumps({"jsonrpc": "2.0", "id": identifier, "result": result})


def encode_failure(identifier: Any, code: int, message: str, data: Any = None) -> bytes:
    """Encode an error reply. A null `identifier` is legal here and only here."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _json_dumps({"jsonrpc": "2.0", "id": identifier, "error": error})
