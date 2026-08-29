"""Routing: the application and router objects, route decorators, route options,
and the dependency markers a route carries.
"""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

ROUTING: dict[str, tuple[str, str, str, str]] = {
    "route.app": ("app", "routing", TRANSLATED, "FastAPI() becomes Wreath()."),
    "route.router": (
        "router",
        "routing",
        TRANSLATED,
        "APIRouter(prefix=..., tags=...) becomes Router(prefix=..., tags=...).",
    ),
    "route.method": (
        "route",
        "routing",
        TRANSLATED,
        "The route decorator is unchanged. The handler takes request: Request as its first parameter.",
    ),
    "route.include_static": (
        "include_router",
        "routing",
        TRANSLATED,
        "include_router() is unchanged.",
    ),
    "route.include_dynamic": (
        "include_router",
        "routing",
        TRANSLATED,
        "A loop around include_router() is unchanged: each Router is still included when the loop executes.",
    ),
    "route.websocket": (
        "route",
        "routing",
        TRANSLATED,
        "The websocket decorator is unchanged on Wreath and Router. Import WebSocket and WebSocketDisconnect from wreath.websocket.",
    ),
    "ws.json_method": (
        "websocket",
        "other",
        TRANSLATED,
        "send_json() and receive_json() are unchanged and use Wreath's JSON codec.",
    ),
    "route.response_model": (
        "route_option",
        "other",
        TRANSLATED,
        "Drop response_model= and put the public model on the handler's return annotation. Wreath filters and validates plain return values through that annotation at runtime; an explicit Response keeps ownership of its wire body.",
    ),
    # `status_code=` is a route slot for coerced values. An explicitly returned
    # Response still owns its own status, so those shapes remain distinct.
    "route.status_code": (
        "route_option",
        "other",
        NEEDS_REVIEW,
        "Keep status_code= on the route. It applies to plain coerced values and the OpenAPI success response; an explicit Response still owns its own status, so confirm which return shape this handler uses.",
    ),
    "route.status_code_return": (
        "route_option",
        "other",
        TRANSLATED,
        "Return JSONResponse(<value>, status=<number>) and drop status_code= from the route. wreath already sends a dict, list or number as JSON, so only the status changes.",
    ),
    "route.status_code_text": (
        "route_option",
        "other",
        TRANSLATED,
        "Return TextResponse(<value>, status=<number>) and drop status_code= from the route. A string is text/plain in wreath, so JSONResponse would change the content type as well as the status.",
    ),
    "route.status_code_response": (
        "route_option",
        "other",
        TRANSLATED,
        "Drop status_code= from the route and pass status= to the response this handler already returns. The route-level value was doing nothing: the returned response's own status wins.",
    ),
    "route.status_code_empty": (
        "route_option",
        "other",
        TRANSLATED,
        "Return Response(status=<number>). A handler that returns nothing would otherwise answer 200 with a JSON null, and Response leaves out content-length for a status that carries no body.",
    ),
    "route.status_code_empty_body": (
        "route_option",
        "other",
        NEEDS_REVIEW,
        "This route says 204 or 304, which must have no body, but the handler returns one. FastAPI let that through. Decide which is right before porting: drop the return, or use a status that allows a body.",
    ),
    "route.include_in_schema": (
        "route_option",
        "other",
        TRANSLATED,
        "include_in_schema= is unchanged; false withholds the route from OpenAPI and generated clients.",
    ),
}

DEPENDENCIES: dict[str, tuple[str, str, str, str]] = {
    "depends.use": (
        "depends",
        "dependencies",
        TRANSLATED,
        "Depends(...) is unchanged. The function it points at takes request as its first parameter, like a handler.",
    ),
    "depends.router_call": (
        "depends_wiring",
        "other",
        NEEDS_REVIEW,
        "The router's dependencies= is a call rather than a plain list, so this tool cannot see what is in it. Write the Depends(...) entries out as a list.",
    ),
}
