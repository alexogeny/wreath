"""Expiring, optionally single-use authorization for exact HTTP paths."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .._http import _is_http_token
from ..request import Request
from ..response import ProblemResponse
from ..tokens import ActionTokens


def _tenant_scope(request: Request) -> str:
    tenant = request.state.get("tenant")
    if tenant is None:
        return ""
    return getattr(tenant, "key", tenant)


class SignedRoutePolicy:
    """Require an `ActionTokens` capability on configured exact paths.

    The signature covers the normalized method and exact raw request target.
    Re-encoding the path, or reordering or changing any ordinary query
    parameter, therefore invalidates it. The generated token is appended last,
    and duplicate token parameters are refused.

    `ActionTokens` supplies expiry, key rotation, and optional single-use
    replay refusal. This policy supplies the HTTP binding and fail-closed 403.
    By default, a bound tenant is part of that binding. `scope` can replace the
    request-side scope for another authority boundary; pass the matching value
    to `sign` when minting outside a tenant scope.

    Args:
        tokens: Application-owned action-token issuer/verifier.
        purpose: A purpose declared on `tokens`.
        paths: Exact protected absolute paths.
        methods: Protected methods. HEAD is normalized to GET so a signed GET
            URL retains ordinary HTTP HEAD semantics.
        scope: Request-side authority scope. Defaults to the bound tenant key.
        parameter: Query parameter carrying the capability token.
        detail: Generic 403 detail for every verification failure.
    """

    __slots__ = (
        "_methods",
        "_parameter",
        "_parameter_bytes",
        "_paths",
        "_purpose",
        "_scope",
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
        scope: Callable[[Request], str] = _tenant_scope,
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
        if not callable(scope):
            raise TypeError("SignedRoutePolicy scope must be callable")
        if not isinstance(detail, str) or not detail:
            raise ValueError("SignedRoutePolicy detail must not be empty")
        self._tokens = tokens
        self._purpose = purpose
        self._paths = protected
        self._methods = normalized_methods
        self._scope = scope
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

    @staticmethod
    def _request_target(request: Request, query: bytes) -> str:
        context = request._context
        scope = request._scope
        if context is not None:
            raw_path = context.raw_path
        elif scope is not None:
            raw_path = scope.get("raw_path")
        else:
            raise RuntimeError("request has neither an ASGI scope nor a native context")
        path = raw_path if isinstance(raw_path, bytes) else request.path.encode("utf-8")
        suffix = b"?" + query if query else b""
        return (path + suffix).decode("latin-1")

    @classmethod
    def _bound(cls, method: str, target: str, scope: str) -> str:
        base = cls._method(method) + "\x00" + target
        return base if not scope else base + "\x00" + scope

    def sign(
        self,
        path: str,
        *,
        method: str = "GET",
        scope: str | None = None,
        now: float | None = None,
    ) -> str:
        """Return `path` with an expiring capability appended to its query."""
        path_only, separator, query_text = path.partition("?")
        if path_only not in self._paths or "#" in path or path.count("?") > 1:
            raise ValueError("signed path must be one of this policy's exact protected paths")
        normalized_method = method.upper()
        if normalized_method not in self._methods:
            raise ValueError("signed method is not protected by this policy")
        query = query_text.encode("latin-1")
        if any(part.startswith(self._parameter_bytes) for part in query.split(b"&")):
            raise ValueError("signed path already contains the signature parameter")
        if scope is None:
            from ..tenancy import current_tenant_or_none

            tenant = current_tenant_or_none()
            scope = "" if tenant is None else tenant.key
        if not isinstance(scope, str):
            raise TypeError("signed route scope must be a string")
        target = self._target(path_only, query)
        bound = self._bound(normalized_method, target, scope)
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
        target = self._request_target(request, query)
        scope = self._scope(request)
        if not isinstance(scope, str):
            return self._refusal()
        bound = self._bound(request.method, target, scope)
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
