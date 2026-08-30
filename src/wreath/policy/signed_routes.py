"""Expiring, optionally single-use authorization for exact HTTP paths."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .._http import _is_http_token
from ..request import Request
from ..response import ProblemResponse
from ..tokens import ActionTokens


class SignedRoutePolicy:
    """Require an `ActionTokens` capability on configured exact paths.

    The signature covers the normalized method, exact decoded path, and raw
    query bytes in their original order. Reordering or changing any ordinary
    query parameter therefore invalidates it. The generated token is appended
    last, and duplicate token parameters are refused.

    `ActionTokens` supplies expiry, key rotation, and optional single-use
    replay refusal. This policy supplies the HTTP binding and fail-closed 403.

    Args:
        tokens: Application-owned action-token issuer/verifier.
        purpose: A purpose declared on `tokens`.
        paths: Exact protected absolute paths.
        methods: Protected methods. HEAD is normalized to GET so a signed GET
            URL retains ordinary HTTP HEAD semantics.
        parameter: Query parameter carrying the capability token.
        detail: Generic 403 detail for every verification failure.
    """

    __slots__ = (
        "_methods",
        "_parameter",
        "_parameter_bytes",
        "_paths",
        "_purpose",
        "_tokens",
        "detail",
    )

    def __init__(
        self,
        tokens: ActionTokens,
        purpose: str,
        paths: Iterable[str],
        *,
        methods: Iterable[str] = ("GET", "HEAD"),
        parameter: str = "signature",
        detail: str = "Signed URL is invalid or expired",
    ) -> None:
        if not isinstance(tokens, ActionTokens):
            raise TypeError("SignedRoutePolicy tokens must be ActionTokens")
        tokens._purpose(purpose)
        protected = frozenset(paths)
        if not protected or any(
            not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path
            for path in protected
        ):
            raise ValueError("SignedRoutePolicy paths must be non-empty exact absolute paths")
        raw_methods = tuple(methods)
        if any(not isinstance(method, str) for method in raw_methods):
            raise ValueError("SignedRoutePolicy methods must be valid HTTP methods")
        normalized_methods = frozenset(method.upper() for method in raw_methods)
        if not normalized_methods or any(
            not _is_http_token(method) for method in normalized_methods
        ):
            raise ValueError("SignedRoutePolicy methods must be valid HTTP methods")
        if not isinstance(parameter, str) or not _is_http_token(parameter):
            raise ValueError("SignedRoutePolicy parameter must be a valid HTTP token")
        if not isinstance(detail, str) or not detail:
            raise ValueError("SignedRoutePolicy detail must not be empty")
        self._tokens = tokens
        self._purpose = purpose
        self._paths = protected
        self._methods = normalized_methods
        self._parameter = parameter
        self._parameter_bytes = parameter.encode("ascii") + b"="
        self.detail = detail

    @staticmethod
    def _method(method: str) -> str:
        normalized = method.upper()
        return "GET" if normalized == "HEAD" else normalized

    @staticmethod
    def _target(path: str, query: bytes) -> str:
        suffix = b"?" + query if query else b""
        return (path.encode("utf-8") + suffix).decode("latin-1")

    def sign(
        self,
        path: str,
        *,
        method: str = "GET",
        now: float | None = None,
    ) -> str:
        """Return `path` with an expiring capability appended to its query."""
        path_only, separator, query_text = path.partition("?")
        if path_only not in self._paths or "#" in path or path.count("?") > 1:
            raise ValueError("signed path must be one of this policy's exact protected paths")
        normalized_method = method.upper()
        if normalized_method not in self._methods:
            raise ValueError("signed method is not protected by this policy")
        query = query_text.encode("latin-1") if separator else b""
        if any(part.startswith(self._parameter_bytes) for part in query.split(b"&") if part):
            raise ValueError("signed path already contains the signature parameter")
        target = self._target(path_only, query)
        bound = self._method(normalized_method) + "\x00" + target
        token = self._tokens.issue(self._purpose, target, bound=bound, now=now)
        joiner = "&" if query else "?"
        return f"{path}{joiner}{self._parameter}={token}"

    def _ingress_sync(self, request: Request):
        if request.path not in self._paths or request.method not in self._methods:
            return None
        token: str | None = None
        unsigned: list[bytes] = []
        parameter = self._parameter_bytes
        parameter_length = len(parameter)
        for part in request.query_string.split(b"&"):
            if part.startswith(parameter):
                if token is not None:
                    return self._refusal()
                try:
                    token = part[parameter_length:].decode("ascii")
                except UnicodeDecodeError:
                    return self._refusal()
            elif part:
                unsigned.append(part)
        if token is None:
            return self._refusal()
        query = b"&".join(unsigned)
        target = self._target(request.path, query)
        bound = self._method(request.method) + "\x00" + target
        claims = self._tokens.verify(self._purpose, token, bound=bound)
        if claims is None or claims.subject != target:
            return self._refusal()
        return None

    def _refusal(self) -> ProblemResponse:
        return ProblemResponse(status=403, detail=self.detail)

    def describe(self) -> Any:
        """The generic 403 emitted for an invalid signed capability."""
        from ..openapi import ResponseSpec
        from .base import PolicyContract

        return PolicyContract(
            responses=(
                (
                    403,
                    ResponseSpec(
                        description="The signed URL is missing, invalid, expired, or replayed.",
                        media_type="application/problem+json",
                    ),
                ),
            ),
            methods=self._methods,
        )


__all__ = ["SignedRoutePolicy"]
