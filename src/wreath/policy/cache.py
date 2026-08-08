"""First-class response cache-control policy."""

from __future__ import annotations

from .._webpolicy import find_response_header
from ..cache_control import PRIVATE_NO_STORE, CacheControl
from ..cache_control import CachePolicy as CacheSelector
from ..request import Request


class CachePolicy:
    """Set a default `Cache-Control` on responses that do not already carry one.

    Global policy, so it covers route misses, static files, and error
    responses as well as routed ones. It never overrides: a response that
    already has a `Cache-Control` header, from the handler or from a static
    mount, is returned untouched. This is the floor for everything that did not
    state a policy, not a way to impose one.

    `policy` is consulted first and may return `None` to decline, in which case
    `default` applies. With no policy and no default, nothing is added.

    A policy that resolves to a `public` directive on a response carrying
    `Set-Cookie` is downgraded to `private, no-store`. A cookie is per-caller by
    definition, and a shared cache that stored one would hand it to the next
    caller through the same URL.

    Args:
        default: Applied when `policy` is absent or declines. None adds nothing.
        policy: Called with the request and response to choose per response.
    """

    __slots__ = ("default", "policy")

    def __init__(
        self,
        default: CacheControl | None = None,
        policy: CacheSelector | None = None,
    ) -> None:
        self.default = default
        self.policy = policy

    def describe(self):
        """The `Cache-Control` floor, with its value when configuration fixes it.

        A `policy` chooses per response, so the value is only a constant when
        `default` alone is in play. Documenting a const that a policy can
        override would be a claim this policy cannot keep.
        """
        from .base import HeaderSpec, PolicyContract

        fixed = (
            self.default.to_header().decode("latin-1")
            if self.default is not None and self.policy is None
            else None
        )
        if self.default is None and self.policy is None:
            return PolicyContract()
        return PolicyContract(
            response_headers=(
                (
                    None,
                    HeaderSpec(
                        "Cache-Control",
                        description=(
                            "Default cache policy, applied only when the response "
                            "does not already carry one."
                        ),
                        const=fixed,
                    ),
                ),
            ),
        )

    async def after(self, request: Request, response):
        """Append the selected `Cache-Control` header, or return the response as is."""
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


__all__ = ["CachePolicy"]
