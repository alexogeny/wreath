"""First-class application handler deadline policy."""

from __future__ import annotations

from math import isfinite

from ..response import ProblemResponse


class DeadlinePolicy:
    """Bound asynchronous handler execution independently of request ingestion.

    Wreath's server `request_timeout` protects the socket-facing request
    phase. This policy starts after routing, authentication, and dependencies
    have activated and bounds the handler await itself. Synchronous Python
    cannot be pre-empted safely, so a synchronous handler is measured but can
    only observe the deadline after it returns; CPU work that needs hard
    pre-emption belongs in an isolated worker.

    Args:
        seconds: Positive handler deadline in seconds.
        detail: Problem detail returned with status 504.
    """

    __slots__ = ("_nanoseconds", "detail", "seconds")

    def __init__(
        self,
        seconds: float,
        *,
        detail: str = "Request handler exceeded its deadline",
    ) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("DeadlinePolicy seconds must be positive")
        try:
            resolved = float(seconds)
        except OverflowError as error:
            raise ValueError("DeadlinePolicy seconds must be positive and finite") from error
        if resolved <= 0 or not isfinite(resolved):
            raise ValueError("DeadlinePolicy seconds must be positive and finite")
        if not isinstance(detail, str) or not detail:
            raise ValueError("DeadlinePolicy detail must not be empty")
        self.seconds = resolved
        numerator, denominator = resolved.as_integer_ratio()
        self._nanoseconds = max(1, numerator * 1_000_000_000 // denominator)
        self.detail = detail

    def _refusal(self) -> ProblemResponse:
        return ProblemResponse(status=504, detail=self.detail)

    def describe(self):
        """The 504 emitted when asynchronous handler execution expires."""
        from ..openapi import ResponseSpec
        from .base import PolicyContract

        return PolicyContract(
            responses=((504, ResponseSpec(
                description="The request handler exceeded its configured deadline.",
                media_type="application/problem+json",
            )),),
        )


__all__ = ["DeadlinePolicy"]
