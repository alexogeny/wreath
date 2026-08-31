"""What a handler returns: status codes, `HTTPException`, and the response
classes wreath has an equivalent for."""

from __future__ import annotations

import ast

from ..._conditional import STATUS_WITHOUT_BODY as _STATUS_WITHOUT_BODY
from .imports import _Imports

STATUS_EXCEPTION: dict[int, str] = {
    400: "BadRequest",
    401: "Unauthorized",
    403: "Forbidden",
    404: "NotFound",
    405: "MethodNotAllowed",
    409: "Conflict",
    413: "PayloadTooLarge",
    422: "UnprocessableEntity",
    429: "TooManyRequests",
    431: "RequestHeaderFieldsTooLarge",
    500: "HTTPException",
}

# fastapi.responses / starlette.responses classes wreath ships an equivalent of.
_RESPONSE_CLASSES = frozenset(
    {
        "JSONResponse",
        "HTMLResponse",
        "PlainTextResponse",
        "RedirectResponse",
        "StreamingResponse",
        "FileResponse",
        "ORJSONResponse",
        "UJSONResponse",
    }
)

# Response classes wreath ships that are not in the fastapi.responses set above.
# A handler already returning one of these carries its own status, so a route
# `status_code=` in front of it was dead before the port.
_EXTRA_RESPONSE_CLASSES = frozenset({"Response", "TextResponse", "SSEResponse"})


def status_int(imports: _Imports, node: ast.AST | None) -> int | None:
    """The integer a status expression denotes, or `None` if it is not a literal.

    `status.HTTP_404_NOT_FOUND` is a literal wearing a name, and applications
    spell the status that way far more often than as a bare integer.
    """
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    if not isinstance(node, ast.Attribute) or not node.attr.startswith("HTTP_"):
        return None
    if imports.origin(node.value) not in ("fastapi.status", "starlette.status"):
        return None
    digits = node.attr.split("_")[1]
    return int(digits) if digits.isdigit() else None


def http_exception_status(node: ast.Call) -> ast.expr | None:
    """The `status_code` expression of an `HTTPException(...)`, however spelled."""
    for keyword in node.keywords:
        if keyword.arg == "status_code":
            return keyword.value
    return node.args[0] if node.args else None


def http_exception_rule(imports: _Imports, node: ast.Call) -> str:
    """The verdict one `HTTPException(...)` earns.

    Shared with the emitter so a status the emitter cannot rewrite is never
    reported as translated. Three outcomes:

    * `exc.http_literal` — the status resolves to an int `STATUS_EXCEPTION` has
      a class for, so the call becomes that class. `status.HTTP_404_NOT_FOUND`
      counts: it is a literal wearing a name, and applications spell the status
      that way far more often than as a bare integer.
    * `exc.http_unmapped` — the status is a literal wreath ships no class for
      (502/503/501/…), or the call carries `headers=`, whose wreath spelling is
      a sequence of lowercase byte pairs rather than fastapi's `dict[str, str]`.
    * `exc.http_variable` — the status is not readable here at all.
    """
    status = status_int(imports, http_exception_status(node))
    if status is None:
        return "exc.http_variable"
    if status not in STATUS_EXCEPTION:
        return "exc.http_unmapped"
    if any(keyword.arg == "headers" for keyword in node.keywords):
        return "exc.http_unmapped"
    return "exc.http_literal"


def status_code_rule(imports: _Imports, value: ast.expr, node) -> str:
    """Which verdict `status_code=` earns on this handler.

    Shared with the emitter for the reason `query_rule` is: the report and the
    `# TODO(wreath-port: …)` written into the source have to agree about one
    line, and the emitter must only perform the rewrite the report calls
    determined.

    Wreath has no `status_code` slot on the decorator — the status lives on the
    response the handler returns. So the question is which response class this
    return becomes, and for a *literal* return wreath's own coercion answers it
    (`app._to_response`: dict/list/tuple/number -> JSONResponse, str ->
    TextResponse). Wrapping such a return in the class wreath would have chosen
    anyway changes the status and nothing else.

    A `return some_name` is where that stops. The runtime type picks the class,
    and a dataclass is not JSON-serializable at all in wreath (`_json.dumps`
    raises; `dataclasses.asdict` is the documented step) — so wrapping an
    unknown value would emit code that fails on the first request, which is the
    silent conversion this tool exists to avoid.
    """
    status = status_int(imports, value)
    if status is None:
        return "route.status_code"
    returns = _returns_in(node)
    if status in _STATUS_WITHOUT_BODY:
        return "route.status_code_empty" if not returns else "route.status_code_empty_body"
    if len(returns) != 1 or returns[0].value is None:
        return "route.status_code"
    returned = returns[0].value
    if isinstance(returned, ast.Call) and _is_response_construction(imports, returned):
        return "route.status_code_response"
    if isinstance(returned, (ast.Dict, ast.List, ast.Tuple, ast.DictComp, ast.ListComp)):
        return "route.status_code_return"
    if isinstance(returned, ast.Constant):
        if isinstance(returned.value, str):
            return "route.status_code_text"
        if isinstance(returned.value, (bool, int, float)) or returned.value is None:
            return "route.status_code_return"
    return "route.status_code"


def response_class_rule(imports: _Imports, value: ast.expr, node) -> str:
    """Which verdict `response_class=` earns on this handler.

    Wreath has no `response_class` slot: the response comes from what the
    handler returns. So the keyword is deletable exactly when it names the class
    wreath would have picked anyway, and the same coercion `status_code_rule`
    reads answers that — dict/list/tuple/number/None becomes a JSON response, so
    `response_class=JSONResponse` in front of one of those is already what
    happens and the keyword is doing nothing.

    A `str` return is where it stops, and it is not a nicety: FastAPI's
    `JSONResponse` sends `"ok"` **with the quotes** as `application/json` and
    wreath sends `ok` as `text/plain`. Deleting the keyword there changes both
    the body and the content type, so it stays a decision.

    `HTMLResponse` and the rest are the same argument in the other direction:
    wreath would not have chosen them, so dropping the keyword silently changes
    the content type of every response this route sends.
    """
    if imports.origin(value).split(".")[-1] != "JSONResponse":
        return "route.response_class"
    for statement in _returns_in(node):
        returned = statement.value
        if returned is None:
            continue
        if isinstance(returned, ast.Call) and _is_response_construction(imports, returned):
            continue  # the handler builds its own response; the keyword was dead
        if isinstance(returned, (ast.Dict, ast.List, ast.Tuple, ast.DictComp, ast.ListComp)):
            continue
        if (
            isinstance(returned, ast.Constant)
            and (returned.value is None or isinstance(returned.value, (bool, int, float)))
            and not isinstance(returned.value, str)
        ):
            continue
        return "route.response_class"
    return "route.response_class_default"


def _is_response_construction(imports: _Imports, call: ast.Call) -> bool:
    """Whether this call builds a response object that carries its own status."""
    tail = imports.origin(call.func).split(".")[-1]
    return tail in _RESPONSE_CLASSES or tail in _EXTRA_RESPONSE_CLASSES


def _returns_in(node) -> list[ast.Return]:
    """Every `return` belonging to this function, not to one nested inside it.

    A nested `def`/`lambda` has its own returns (the streaming-generator
    pattern puts one right inside a handler), and counting those would make a
    one-return handler look like several.
    """
    found: list[ast.Return] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(current, ast.Return):
            found.append(current)
        stack.extend(ast.iter_child_nodes(current))
    return found
