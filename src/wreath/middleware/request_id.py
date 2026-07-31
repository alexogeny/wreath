"""Request correlation identifiers.

Assigns every request a stable id, echoes it on the response by default, and
records it on request state so handlers and later observability layers can
read it:

```python
app.add_middleware(RequestIDMiddleware(), priority=-5)

@app.get("/")
async def index(request):
    return {"trace": request_id(request)}
```
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
from .._webpolicy import replace_response_header
from ..request import Request

if _core is not None and hasattr(_core, "request_id_valid"):
    _request_id_valid: Any = _core.request_id_valid
else:  # pragma: no cover - exercised by the WREATH_PURE test matrix
    from .._pure.observability import request_id_valid as _request_id_valid

_STATE_KEY = "_wreath_request_id"


def request_id(request: Request) -> str:
    """Return the id `RequestIDMiddleware` assigned to this request.

    Reads request state, not a header, so it returns the same value whether the
    id was accepted from the caller or minted here, and whether or not `echo` is
    on.

    Returns:
        The correlation id for this request.

    Raises:
        RuntimeError: No id was assigned, so `RequestIDMiddleware` is not mounted.
    """
    value = request.state.get(_STATE_KEY)
    if value is None:
        raise RuntimeError("RequestIDMiddleware has not assigned an id to this request")
    return value


class RequestIDMiddleware:
    """Accept a validated inbound request id, or mint one.

    Global middleware, so every response is correlated, including route misses
    and errors. The id lands on request state for `request_id(request)` and, when
    `echo` is on, is appended to the response under the same header name it was
    read from.

    An inbound id is reused only when `trust_inbound` is on *and* the value
    passes validation, meaning it is between 1 and `max_length` bytes and
    contains only ASCII letters, digits, `-`, `_`, and `.`. Anything else is
    replaced, not sanitized: the id is echoed into a response header and later
    written into access logs and trace attributes, minting a fresh one is
    cheaper than escaping, and a caller who sent junk has no claim on seeing it
    back. A minted id is 16 bytes from `os.urandom` hex-encoded -- collision-free
    in practice for correlation, and never usable as crypto material.

    Note that a trusted inbound id is caller-controlled. Where log entries from
    different callers must not be conflatable, set `trust_inbound=False` and mint
    every id here.

    Args:
        header: Header the id is read from and echoed to. Compared lowercased.
        trust_inbound: Reuse a valid inbound id instead of always minting one.
        echo: Append the id to the response.
        max_length: Longest inbound id accepted, in bytes.

    Raises:
        ValueError: `header` is empty, or `max_length` is below 1.
    """

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

    def describe(self) -> Any:
        """The correlation header, in whichever direction this one is configured for.

        A middleware built with `echo=False` emits nothing, and saying so is
        the point: the document describes this instance, not the class.
        """
        from .base import HeaderSpec, MiddlewareContract

        request_headers = (
            (
                HeaderSpec(
                    self._header,
                    description="Correlation id; echoed back on the response.",
                ),
            )
            if self._trust_inbound
            else ()
        )
        response_headers = (
            ((None, HeaderSpec(self._header, description="Correlation id for this request.")),)
            if self._echo
            else ()
        )
        return MiddlewareContract(
            request_headers=request_headers,
            response_headers=response_headers,
        )

    def _inbound(self, request: Request) -> str | None:
        value = find_header(request.headers, self._header_bytes)
        if value is None or not _request_id_valid(value, self._max_length):
            return None
        return value.decode("ascii")

    def before_sync(self, request: Request) -> None:
        """Record the inbound id, or a freshly minted one, on request state."""
        value = self._inbound(request) if self._trust_inbound else None
        if value is None:
            # 16 bytes of stdlib randomness, hex-encoded: collision-free in
            # practice for correlation, and never crypto material.
            value = os.urandom(16).hex()
        request.state.__setattr__(_STATE_KEY, value)
        return None

    async def before(self, request: Request) -> None:
        """Compatibility wrapper; compiled middleware uses `before_sync`."""
        return self.before_sync(request)

    def after_inplace(self, request: Request, response: Any) -> None:
        """Echo the id on the response, unless `echo` is off or no id was assigned.

        No id is assigned when a `before` hook ahead of this one short-circuited
        the request, in which case the response goes out uncorrelated rather
        than carrying a guess.
        """
        if not self._echo:
            return
        value = request.state.get(_STATE_KEY)
        if value is not None:
            replace_response_header(
                response.headers, self._header_bytes, value.encode("ascii")
            )

    def after_sync(self, request: Request, response: Any) -> Any:
        """Compatibility transformer; compiled middleware mutates in place."""
        self.after_inplace(request, response)
        return response

    async def after(self, request: Request, response: Any) -> Any:
        """Compatibility wrapper; compiled middleware mutates in place."""
        return self.after_sync(request, response)


__all__ = ["RequestIDMiddleware", "request_id"]
