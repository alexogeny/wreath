"""Global response cache-control policy middleware."""

from __future__ import annotations

from .._webpolicy import find_response_header
from ..cache_control import PRIVATE_NO_STORE, CacheControl, CachePolicy
from ..request import Request


class CacheControlMiddleware:
    """Apply an explicit cache policy without overriding handler directives."""

    global_scope = True
    __slots__ = ("default", "policy")

    def __init__(
        self,
        default: CacheControl | None = None,
        policy: CachePolicy | None = None,
    ) -> None:
        self.default = default
        self.policy = policy

    async def after(self, request: Request, response):
        headers = response.headers
        if find_response_header(headers, b"cache-control") is not None:
            return response
        selected = self.policy(request, response) if self.policy is not None else None
        if selected is None:
            selected = self.default
        if selected is None:
            return response
        if selected.public and find_response_header(headers, b"set-cookie") is not None:
            selected = PRIVATE_NO_STORE
        headers.append((b"cache-control", selected.to_header()))
        return response


__all__ = ["CacheControlMiddleware"]
