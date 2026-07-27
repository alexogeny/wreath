"""Structured HTTP exceptions."""

from __future__ import annotations

from collections.abc import Iterable


class HTTPException(Exception):
    status = 500

    def __init__(
        self,
        detail: str = "",
        *,
        headers: Iterable[tuple[bytes, bytes]] = (),
    ) -> None:
        self.detail = detail or type(self).__name__
        self.headers = tuple(headers)
        super().__init__(self.detail)


class BadRequest(HTTPException):
    status = 400


class ClientDisconnect(BadRequest):
    """The peer went away before the request body finished arriving.

    A 4xx because the request never completed; distinct from `BadRequest` so an
    application can tell "malformed" from "never finished" in a handler or a
    log, and so the response -- which usually has nowhere to go -- is not
    counted as a server fault.
    """


class Unauthorized(HTTPException):
    status = 401

    def __init__(self, detail: str = "Unauthorized", *, challenge: str | None = "Bearer") -> None:
        # RFC 9110 15.5.2: a 401 MUST carry a WWW-Authenticate challenge, so the
        # default is a conformant one; pass a scheme-specific challenge to refine
        # it, or challenge=None only if a later layer supplies the header.
        headers = () if challenge is None else ((b"www-authenticate", challenge.encode("latin-1")),)
        super().__init__(detail, headers=headers)


class Forbidden(HTTPException):
    status = 403


class NotFound(HTTPException):
    status = 404


class MethodNotAllowed(HTTPException):
    status = 405

    def __init__(self, detail: str = "Method Not Allowed", *, allow: Iterable[str] = ()) -> None:
        # RFC 9110 15.5.6: a 405 response MUST carry an Allow header listing the
        # methods the target resource does support.
        methods = tuple(allow)
        headers = (
            ((b"allow", ", ".join(methods).encode("latin-1")),) if methods else ()
        )
        super().__init__(detail, headers=headers)


class Conflict(HTTPException):
    status = 409


class PayloadTooLarge(HTTPException):
    status = 413


class RequestHeaderFieldsTooLarge(HTTPException):
    """Too many header fields, or one too large to be worth parsing.

    RFC 6585 §5. Distinct from `PayloadTooLarge` because the limit that was hit
    is on the *request line and headers*, which a client fixes differently: a
    413 says "send a smaller body", a 431 says "send fewer cookies".
    """

    status = 431


class UnprocessableEntity(HTTPException):
    status = 422


class TooManyRequests(HTTPException):
    status = 429

    def __init__(
        self, detail: str = "Too Many Requests", *, retry_after: int | None = None
    ) -> None:
        # RFC 9110 10.2.3 / RFC 6585 4: a 429 MAY tell the client how long to
        # wait via Retry-After (delta-seconds).
        headers = (
            () if retry_after is None
            else ((b"retry-after", str(retry_after).encode("latin-1")),)
        )
        super().__init__(detail, headers=headers)
