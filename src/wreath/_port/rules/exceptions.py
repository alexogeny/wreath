"""Exceptions and responses: `HTTPException`, exception handlers, and the
response classes a handler returns.
"""

from __future__ import annotations

from ..ir import NEEDS_REVIEW, TRANSLATED

EXCEPTIONS: dict[str, tuple[str, str, str, str]] = {
    "exc.http_literal": (
        "httpexception",
        "exceptions",
        TRANSLATED,
        "HTTPException(status_code=<int>) -> the matching wreath exception class, with the detail as its first positional argument. A 500 becomes `HTTPException(detail)` itself: wreath's base class declares `status = 500`",
    ),
    "exc.http_variable": (
        "httpexception",
        "exceptions",
        NEEDS_REVIEW,
        "The status here is computed, so the right wreath exception cannot be chosen for you. If the value has a small set of possibilities, raise the matching class (NotFound, Forbidden, Conflict, ...); otherwise raise HTTPException(detail) from wreath.exceptions and set status on a subclass.",
    ),
    "exc.http_unmapped": (
        "httpexception",
        "exceptions",
        NEEDS_REVIEW,
        "Two things can land here. Either wreath ships no exception class for this status -- subclass HTTPException and set status = <the number> -- or the call passes headers=, which wreath takes as a list of lowercase byte pairs ([(b'retry-after', b'30')]) rather than a dict of strings. Both matter: a 401 without its challenge header and a 429 without Retry-After are broken responses, so nothing was dropped for you.",
    ),
    "exc.handler": (
        "exception_handler",
        "exceptions",
        TRANSLATED,
        "@app.exception_handler(...) is unchanged.",
    ),
}

RESPONSES: dict[str, tuple[str, str, str, str]] = {
    "resp.class": (
        "response",
        "other",
        TRANSLATED,
        "The response class becomes the wreath one of the same name (PlainTextResponse is TextResponse). Two argument names differ: content= is the first argument, and status_code= is status=.",
    ),
    "resp.status_const": (
        "response",
        "other",
        TRANSLATED,
        "status.HTTP_404_NOT_FOUND is just 404. Where it is raised, the wreath exception class says it better: raise NotFound().",
    ),
    "resp.jsonable": (
        "response",
        "other",
        TRANSLATED,
        "Delete the jsonable_encoder() wrapper. wreath's JSON encoder already handles dataclasses, database rows, UUIDs and datetimes.",
    ),
    "route.response_class": (
        "route_option",
        "other",
        NEEDS_REVIEW,
        "Delete response_class= and return that response type from the handler instead. Wreath picks the response from what you return -- and it would not have picked this one, so deleting the keyword on its own changes the content type.",
    ),
    "route.response_class_default": (
        "route_option",
        "other",
        TRANSLATED,
        "response_class=JSONResponse names the response wreath already builds for what this handler returns, so the keyword goes and nothing else changes.",
    ),
}
