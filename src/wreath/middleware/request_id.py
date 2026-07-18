"""Request correlation identifiers.

Assigns every request a stable id, echoes it on the response, and records it on
request state so handlers and later observability layers can read it::

    app.add_middleware(RequestIDMiddleware(), priority=-5)

    @app.get("/")
    async def index(request):
        return {"trace": request_id(request)}

An inbound id is only reused when it survives validation, because it is echoed
into a response header and will later be written into access logs and trace
attributes. A rejected id is replaced rather than sanitized -- minting a fresh
one is cheaper than escaping, and a caller who sent junk has no expectation of
seeing it back.
"""

from __future__ import annotations

import os
from typing import Any

from .._headers import find_header
from .._native import _core
from ..request import Request

if _core is not None and hasattr(_core, "request_id_valid"):
    _request_id_valid: Any = _core.request_id_valid
else:  # pragma: no cover - exercised by the WREATH_PURE test matrix
    from .._pure.observability import request_id_valid as _request_id_valid

_STATE_KEY = "_wreath_request_id"


def request_id(request: Request) -> str:
    """Return the id :class:`RequestIDMiddleware` assigned to this request."""
    value = request.state.get(_STATE_KEY)
    if value is None:
        raise RuntimeError("RequestIDMiddleware has not assigned an id to this request")
    return value


class RequestIDMiddleware:
    """Accept a validated inbound request id, or mint one."""

    global_scope = True
    __slots__ = ("_echo", "_header", "_header_bytes", "_max_length", "_trust_inbound")

    def __init__(
        self,
        *,
        header: str = "x-request-id",
        trust_inbound: bool = True,
        echo: bool = True,
        max_length: int = 128,
    ) -> None:
        if not header:
            raise ValueError("header name must not be empty")
        if max_length < 1:
            raise ValueError("max_length must be at least 1")
        self._header = header
        self._header_bytes = header.encode("ascii").lower()
        self._trust_inbound = trust_inbound
        self._echo = echo
        self._max_length = max_length

    def _inbound(self, request: Request) -> str | None:
        value = find_header(request.headers, self._header_bytes)
        if value is None or not _request_id_valid(value, self._max_length):
            return None
        return value.decode("ascii")

    async def before(self, request: Request) -> None:
        value = self._inbound(request) if self._trust_inbound else None
        if value is None:
            # 16 bytes of stdlib randomness, hex-encoded: collision-free in
            # practice for correlation, and never crypto material.
            value = os.urandom(16).hex()
        request.state.__setattr__(_STATE_KEY, value)
        return None

    async def after(self, request: Request, response: Any) -> Any:
        if not self._echo:
            return response
        value = request.state.get(_STATE_KEY)
        if value is not None:
            response.headers.append((self._header_bytes, value.encode("ascii")))
        return response


__all__ = ["RequestIDMiddleware", "request_id"]
