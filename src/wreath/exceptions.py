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


class Unauthorized(HTTPException):
    status = 401

    def __init__(self, detail: str = "Unauthorized", *, challenge: str | None = None) -> None:
        headers = () if challenge is None else ((b"www-authenticate", challenge.encode("latin-1")),)
        super().__init__(detail, headers=headers)


class Forbidden(HTTPException):
    status = 403


class NotFound(HTTPException):
    status = 404


class MethodNotAllowed(HTTPException):
    status = 405


class Conflict(HTTPException):
    status = 409


class PayloadTooLarge(HTTPException):
    status = 413


class UnprocessableEntity(HTTPException):
    status = 422


class TooManyRequests(HTTPException):
    status = 429
