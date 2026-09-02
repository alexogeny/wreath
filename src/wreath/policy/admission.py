"""First-class fail-fast concurrency admission policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._native import _core
from ..response import ProblemResponse


@dataclass(frozen=True, slots=True)
class AdmissionStats:
    """Current permits and cumulative refusals for one policy."""

    limit: int
    active: int
    refused: int


class ConcurrencyPolicy:
    """Bound simultaneously executing HTTP handlers and fail fast at capacity.

    This differs from rate limiting: a token bucket constrains arrivals over
    time, while this policy constrains work that is active *now*. The permit is
    held while the handler runs and released before response emission. A
    streaming response therefore protects handler construction, not the later
    lifetime of its producer; streams need their own domain-specific bound.

    The gate is a request-owned native atomic counter. There is deliberately no
    hidden queue: an unbounded wait merely moves overload into suspended tasks,
    while a bounded queue needs a latency/deadline policy of its own.

    Args:
        limit: Maximum handlers executing concurrently in this process.
        detail: Problem detail returned with a 503 refusal.
        retry_after: Optional whole seconds advertised through `Retry-After`.
    """

    __slots__ = ("_gate", "detail", "limit", "retry_after")

    def __init__(
        self,
        limit: int,
        *,
        detail: str = "Request concurrency limit reached",
        retry_after: int | None = None,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("ConcurrencyPolicy limit must be a positive integer")
        if retry_after is not None and (
            isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after < 0
        ):
            raise ValueError("ConcurrencyPolicy retry_after must be a non-negative integer")
        if not isinstance(detail, str) or not detail:
            raise ValueError("ConcurrencyPolicy detail must not be empty")
        self.limit = limit
        self.detail = detail
        self.retry_after = retry_after
        self._gate: Any = _core.AdmissionGate(limit)

    def _acquire(self) -> bool:
        return bool(self._gate.acquire())

    def _release(self) -> None:
        self._gate.release()

    def try_acquire(self) -> bool:
        """Acquire a permit for non-HTTP work without creating a wait queue."""
        return self._acquire()

    def release(self) -> None:
        """Release a permit acquired by `try_acquire`."""
        self._release()

    def refusal(self) -> ProblemResponse:
        """Build this policy's ordinary overload problem response."""
        return self._refusal()

    def _refusal(self) -> ProblemResponse:
        headers = (
            None
            if self.retry_after is None
            else ((b"retry-after", str(self.retry_after).encode("ascii")),)
        )
        return ProblemResponse(status=503, detail=self.detail, headers=headers)

    def stats(self) -> AdmissionStats:
        """Return the current permit count and cumulative refusals."""
        limit, active, refused = self._gate.snapshot()
        return AdmissionStats(limit, active, refused)

    def describe(self):
        """The 503 emitted when every handler permit is in use."""
        from ..openapi import ResponseSpec
        from .base import PolicyContract

        return PolicyContract(
            responses=(
                (
                    503,
                    ResponseSpec(
                        description="The application has no free handler-concurrency permit.",
                        media_type="application/problem+json",
                    ),
                ),
            ),
        )


__all__ = ["AdmissionStats", "ConcurrencyPolicy"]
