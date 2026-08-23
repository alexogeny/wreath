"""Bounded transparent decoding for compressed HTTP request bodies."""

from __future__ import annotations

from typing import Any

from .._native import _core
from ..request import Request
from ..response import ProblemResponse

_CONTENT_ENCODING = b"content-encoding"
_CONTENT_LENGTH = b"content-length"


class RequestDecompressionPolicy:
    """Decode a gzip request representation before dependencies and handlers.

    The public `Request` API does not change: `body()`, `json()` and
    `form()` see decoded bytes and share their existing one-read cache.
    Compressed input remains bounded by `RequestLimits.max_body_bytes` while
    `max_output_bytes` independently bounds expansion.

    Unsupported, stacked, or duplicate content codings fail closed with 415.
    The native decoder accepts exactly one complete gzip member and rejects
    truncation and trailing bytes.

    Args:
        max_output_bytes: Decoded byte ceiling. `None` reuses the request's
            configured `RequestLimits.max_body_bytes`.
        format_aware: Give Content-Type to the format-aware native inflate
            kernel. Disable only for comparative measurement.
    """

    __slots__ = ("format_aware", "max_output_bytes")

    def __init__(
        self,
        *,
        max_output_bytes: int | None = None,
        format_aware: bool = True,
    ) -> None:
        if max_output_bytes is not None and (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes < 1
        ):
            raise ValueError(
                "RequestDecompressionPolicy max_output_bytes must be a positive integer or None"
            )
        if not isinstance(format_aware, bool):
            raise ValueError("RequestDecompressionPolicy format_aware must be a bool")
        self.max_output_bytes = max_output_bytes
        self.format_aware = format_aware

    async def _ingress(self, request: Request):
        try:
            raw_coding = request._single_header(_CONTENT_ENCODING)
        except ValueError:
            return ProblemResponse(
                status=415,
                detail="Content-Encoding must occur exactly once",
            )
        if raw_coding is None:
            return None
        try:
            coding = raw_coding.decode("ascii").strip().lower()
        except UnicodeDecodeError:
            coding = ""
        if coding == "identity":
            request._remove_headers(_CONTENT_ENCODING)
            return None
        if coding != "gzip":
            return ProblemResponse(
                status=415,
                detail="Only a single gzip Content-Encoding is supported",
            )

        encoded = await request.body()
        maximum = self.max_output_bytes or request._limits.max_body_bytes
        content_type = request.header(b"content-type", "unknown")
        format_hint = content_type if self.format_aware else "unknown"
        try:
            decoded = _core.gzip_decompress(encoded, maximum, format_hint)
        except ValueError as error:
            detail = str(error)
            if "expands past" in detail:
                return ProblemResponse(status=413, detail=detail)
            return ProblemResponse(status=400, detail=detail)
        request._body = decoded
        request._remove_headers(_CONTENT_ENCODING, _CONTENT_LENGTH)
        return None

    def describe(self) -> Any:
        """The accepted request coding and bounded-decoding failures."""
        from ..openapi import ResponseSpec
        from .base import HeaderSpec, PolicyContract

        return PolicyContract(
            request_headers=(
                HeaderSpec(
                    "content-encoding",
                    description="Optional request-body coding; gzip is decoded transparently.",
                    required=False,
                ),
            ),
            responses=(
                (400, ResponseSpec(description="Malformed gzip request body.")),
                (413, ResponseSpec(description="Decoded request body exceeds its limit.")),
                (415, ResponseSpec(description="Unsupported or ambiguous content coding.")),
            ),
        )


__all__ = ["RequestDecompressionPolicy"]
