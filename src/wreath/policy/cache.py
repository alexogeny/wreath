"""First-class response cache-control policy."""

from __future__ import annotations

from .._webpolicy import find_response_header
from ..cache_control import PRIVATE_NO_STORE, CacheControl
from ..cache_control import CachePolicy as CacheSelector
from ..request import Request


class CachePolicy:
    """Set default browser and CDN cache policy on responses missing one.

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
        cdn_default: Applied as RFC 9213 `CDN-Cache-Control` when `cdn_policy`
            is absent or declines.
        cdn_policy: Called with the request and response to choose a targeted
            CDN policy per response.
    """

    __slots__ = ("cdn_default", "cdn_policy", "default", "policy")

    def __init__(
        self,
        default: CacheControl | None = None,
        policy: CacheSelector | None = None,
        *,
        cdn_default: CacheControl | None = None,
        cdn_policy: CacheSelector | None = None,
    ) -> None:
        if cdn_default is not None and not cdn_default.to_targeted_header():
            raise ValueError("cdn_default needs at least one cache directive")
        self.default = default
        self.policy = policy
        self.cdn_default = cdn_default
        self.cdn_policy = cdn_policy

    def describe(self):
        """The `Cache-Control` floor, with its value when configuration fixes it.

        A `policy` chooses per response, so the value is only a constant when
        `default` alone is in play. Documenting a const that a policy can
        override would be a claim this policy cannot keep.
        """
        from .base import HeaderSpec, PolicyContract

        fixed_cache = (
            self.default.to_header().decode("latin-1")
            if self.default is not None and self.policy is None
            else None
        )
        fixed_cdn = (
            self.cdn_default.to_targeted_header().decode("latin-1")
            if self.cdn_default is not None and self.cdn_policy is None
            else None
        )
        if (
            self.default is None
            and self.policy is None
            and self.cdn_default is None
            and self.cdn_policy is None
        ):
            return PolicyContract()
        response_headers = []
        if self.default is not None or self.policy is not None:
            response_headers.append(
                (
                    None,
                    HeaderSpec(
                        "Cache-Control",
                        description=(
                            "Default cache policy, applied only when the response "
                            "does not already carry one."
                        ),
                        const=fixed_cache,
                    ),
                )
            )
        if self.cdn_default is not None or self.cdn_policy is not None:
            response_headers.append(
                (
                    None,
                    HeaderSpec(
                        "CDN-Cache-Control",
                        description=(
                            "RFC 9213 CDN cache policy, applied only when the "
                            "response does not already carry one."
                        ),
                        const=fixed_cdn,
                    ),
                )
            )
        return PolicyContract(
            response_headers=tuple(response_headers),
        )

    async def after(self, request: Request, response):
        """Append the selected `Cache-Control` header, or return the response as is."""
        headers = response.headers
        has_cookie: bool | None = None
        if find_response_header(headers, b"cache-control") is None:
            selected = self.policy(request, response) if self.policy is not None else None
            if selected is None:
                selected = self.default
            if selected is not None:
                if selected.public:
                    has_cookie = find_response_header(headers, b"set-cookie") is not None
                    if has_cookie:
                        selected = PRIVATE_NO_STORE
                headers.append((b"cache-control", selected.to_header()))

        has_cdn_policy = self.cdn_default is not None or self.cdn_policy is not None
        if has_cdn_policy and find_response_header(headers, b"cdn-cache-control") is None:
            selected_cdn = (
                self.cdn_policy(request, response) if self.cdn_policy is not None else None
            )
            if selected_cdn is None:
                selected_cdn = self.cdn_default
            if selected_cdn is not None:
                if selected_cdn.public:
                    if has_cookie is None:
                        has_cookie = find_response_header(headers, b"set-cookie") is not None
                    if has_cookie:
                        selected_cdn = PRIVATE_NO_STORE
                value = selected_cdn.to_targeted_header()
                if not value:
                    raise ValueError("cdn_policy returned a policy with no cache directives")
                headers.append((b"cdn-cache-control", value))
        return response


__all__ = ["CachePolicy"]
