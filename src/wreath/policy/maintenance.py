"""First-class operator-controlled request admission policy."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .._native import _core
from ..request import Request
from ..response import ProblemResponse


class MaintenancePolicy:
    """Refuse ordinary traffic while allowing explicit operational paths.

    The active flag and refusal counter live in a native atomic switch, so a
    request pays no Python lock and the same instance remains correct under the
    free-threaded build. Exempt paths are exact, not prefixes: exempting
    `/health` must not silently exempt `/health/delete-everything`.

    Args:
        active: Begin in maintenance mode.
        exempt_paths: Exact paths that remain available, typically readiness.
        detail: Problem detail returned with status 503.
        retry_after: Optional whole seconds advertised through `Retry-After`.
    """

    __slots__ = ("_exempt_paths", "_switch", "detail", "retry_after")

    def __init__(
        self,
        *,
        active: bool = False,
        exempt_paths: Iterable[str] = (),
        detail: str = "Application is in maintenance mode",
        retry_after: int | None = None,
    ) -> None:
        if not isinstance(active, bool):
            raise ValueError("MaintenancePolicy active must be a bool")
        paths = frozenset(exempt_paths)
        for path in paths:
            if not isinstance(path, str) or not path.startswith("/"):
                raise ValueError(
                    "MaintenancePolicy exempt paths must be absolute paths beginning with '/'"
                )
        if retry_after is not None and (
            isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after < 0
        ):
            raise ValueError("MaintenancePolicy retry_after must be a non-negative integer")
        if not isinstance(detail, str) or not detail:
            raise ValueError("MaintenancePolicy detail must not be empty")
        self._exempt_paths = paths
        self._switch: Any = _core.PolicySwitch(active)
        self.detail = detail
        self.retry_after = retry_after

    @property
    def active(self) -> bool:
        """Whether maintenance refusal is currently active."""
        active, _refused = self._switch.snapshot()
        return bool(active)

    @property
    def refused(self) -> int:
        """Requests refused since construction."""
        _active, refused = self._switch.snapshot()
        return int(refused)

    def enable(self) -> None:
        """Enter maintenance mode for subsequent requests."""
        self._switch.set(True)

    def disable(self) -> None:
        """Leave maintenance mode for subsequent requests."""
        self._switch.set(False)

    def _ingress_sync(self, request: Request):
        if request.path in self._exempt_paths or self._switch.allows():
            return None
        return self._refusal_response()

    def _native(self) -> tuple[Any, ...]:
        """Freeze the native admission check and its complete refusal response."""
        response = self._refusal_response()
        return (
            self._exempt_paths,
            self._switch.allows,
            tuple(response.headers),
            response.body,
        )

    def _refusal_response(self) -> ProblemResponse:
        headers = (
            None
            if self.retry_after is None
            else ((b"retry-after", str(self.retry_after).encode("ascii")),)
        )
        return ProblemResponse(status=503, detail=self.detail, headers=headers)

    def describe(self):
        """The 503 emitted while maintenance mode is active."""
        from ..openapi import ResponseSpec
        from .base import PolicyContract

        return PolicyContract(
            responses=(
                (
                    503,
                    ResponseSpec(
                        description="The application is temporarily refusing ordinary traffic.",
                        media_type="application/problem+json",
                    ),
                ),
            ),
        )


__all__ = ["MaintenancePolicy"]
