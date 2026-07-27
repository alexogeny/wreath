"""Content negotiation: serialize a response in the format the client asked for.

Most endpoints return JSON, and JSON stays the default. But a client that sends
``Accept: application/msgpack`` (a mobile app minimizing bytes, a service-to-
service call) can be served MessagePack from the *same* handler — the handler
returns plain data, and the format is chosen from the ``Accept`` header.

    from wreath.negotiation import serialize

    @app.get("/report")
    async def report(request):
        return serialize(request, {"rows": rows})   # JSON or msgpack per Accept

`Accept` is parsed with q-values per RFC 9110 §12.5.1; an unsatisfiable `Accept`
yields ``406 Not Acceptable`` listing what is available. Responses carry
``Vary: Accept`` so a shared cache keys on the negotiated type.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ._json import dumps as _json_dumps
from ._native import _core
from .request import Request
from .response import ProblemResponse, Response

# The pure encoder stays the reference implementation and the parity contract
# (tests/test_msgpack_parity.py asserts the two are byte-for-byte), so
# WREATH_PURE=1 selects it exactly as it does for JSON.
if _core is not None and hasattr(_core, "msgpack_dumps"):
    _msgpack = _core.msgpack_dumps
else:
    from ._pure.msgpack import packb as _msgpack

__all__ = ["JSON", "MSGPACK", "Serializer", "negotiate", "parse_accept", "serialize"]


@dataclass(frozen=True, slots=True)
class Serializer:
    """A media type and the function that encodes data to its bytes."""

    media_type: str
    encode: Callable[[Any], bytes]


def _to_bytes(data: Any) -> bytes:
    encoded = _json_dumps(data)
    return encoded if isinstance(encoded, bytes) else encoded.encode("utf-8")


JSON = Serializer("application/json", _to_bytes)
MSGPACK = Serializer("application/msgpack", _msgpack)

#: JSON first, so it wins ties and is the default when Accept is absent/``*/*``.
DEFAULT_SERIALIZERS: tuple[Serializer, ...] = (JSON, MSGPACK)


def parse_accept(header: str | None) -> list[tuple[str, float]]:
    """Parse an ``Accept`` header into ``(media_range, q)`` pairs, best first.

    Ordered by q (desc) then specificity (exact > ``type/*`` > ``*/*``). A
    ``q=0`` range (explicitly not acceptable) is kept so matching can reject it.
    """
    if not header:
        return []
    ranges: list[tuple[str, float, int]] = []
    for index, part in enumerate(header.split(",")):
        token = part.strip()
        if not token:
            continue
        media, _, params = token.partition(";")
        media = media.strip().lower()
        if not media:
            continue
        q = 1.0
        for param in params.split(";"):
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0
        specificity = 2 if "*" not in media else (1 if media != "*/*" else 0)
        ranges.append((media, q, specificity * 1000 - index))
    ranges.sort(key=lambda r: (r[1], r[2]), reverse=True)
    return [(media, q) for media, q, _ in ranges]


def _matches(media_type: str, media_range: str) -> bool:
    if media_range in ("*/*", "*"):
        return True
    if media_range.endswith("/*"):
        return media_type.split("/", 1)[0] == media_range[:-2]
    return media_type == media_range


def negotiate(
    accept: str | None, serializers: Sequence[Serializer] = DEFAULT_SERIALIZERS
) -> Serializer | None:
    """Pick the best serializer for an ``Accept`` header, or None if unsatisfiable.

    A missing or ``*/*`` ``Accept`` yields the first (default) serializer.
    """
    parsed = parse_accept(accept)
    if not parsed:
        return serializers[0] if serializers else None
    # `q=0` means *not acceptable* (RFC 9110 §12.5.1), and it has to be applied
    # as an exclusion across the whole header rather than skipped in place:
    # `application/json;q=0, */*` ranks the wildcard first, so matching in order
    # served exactly the type the client had just refused.
    excluded = tuple(media_range for media_range, q in parsed if q <= 0.0)
    for media_range, q in parsed:
        if q <= 0.0:
            continue
        for serializer in serializers:
            if not _matches(serializer.media_type, media_range):
                continue
            if any(_matches(serializer.media_type, denied) for denied in excluded):
                continue
            return serializer
    return None


def serialize(
    request: Request,
    data: Any,
    *,
    serializers: Sequence[Serializer] = DEFAULT_SERIALIZERS,
    status: int = 200,
) -> Response:
    """Serialize ``data`` in the client's preferred format, or return ``406``."""
    chosen = negotiate(request.header("accept"), serializers)
    if chosen is None:
        available = ", ".join(s.media_type for s in serializers)
        return ProblemResponse(
            status=406,
            detail=f"none of the acceptable media types are available; offered: {available}",
        )
    response = Response(
        chosen.encode(data), status=status, media_type=chosen.media_type.encode("ascii")
    )
    response.headers.append((b"vary", b"Accept"))
    return response
