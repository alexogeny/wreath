"""Content negotiation: serialize a response in the format the client asked for.

Most endpoints return JSON, and JSON stays the default. But a client that sends
`Accept: application/msgpack` (a mobile app minimizing bytes, a service-to-
service call) can be served MessagePack from the *same* handler — the handler
returns plain data, and the format is chosen from the `Accept` header.

```python
from wreath.negotiation import serialize

@app.get("/report")
async def report(request):
    return serialize(request, {"rows": rows})   # JSON or msgpack per Accept
```

`Accept` is parsed with q-values per RFC 9110 §12.5.1; an unsatisfiable `Accept`
yields `406 Not Acceptable` listing what is available. A negotiated response
carries `Vary: Accept` so a shared cache keys on the negotiated type; the 406
does not, because it varies with nothing a cache should reuse.

The msgpack encoder is C, held to the published msgpack specification vectors
by `tests/test_msgpack_parity.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ._json import dumps as _json_dumps
from ._native import _core
from .protobuf import encode as _protobuf_encode
from .protobuf import is_message as _is_message
from .request import Request
from .response import ProblemResponse, Response

#: `msgpack_dumps(obj)` -- MessagePack bytes for the JSON-shaped subset.
_msgpack: Callable[[object], bytes] = _core.msgpack_dumps

__all__ = [
    "JSON",
    "MSGPACK",
    "PROTOBUF",
    "PROTOBUF_MEDIA_TYPES",
    "Serializer",
    "negotiate",
    "parse_accept",
    "serialize",
]


@dataclass(frozen=True, slots=True)
class Serializer:
    """A media type and the function that encodes data to its bytes.

    `encode` takes the handler's data and returns the response body. It is
    called once per response, on the request's task, and whatever it raises
    propagates to the handler -- there is no fallback to another format.

    Args:
        media_type: Compared case-sensitively against a lowercased `Accept` range.
        encode: Any callable from data to `bytes`; `JSON` and `MSGPACK` are the built-ins.
    """

    media_type: str
    encode: Callable[[Any], bytes]


def _to_bytes(data: Any) -> bytes:
    encoded = _json_dumps(data)
    return encoded if isinstance(encoded, bytes) else encoded.encode("utf-8")


def _to_protobuf(data: Any) -> bytes:
    """Encode a declared message, refusing anything else by name.

    JSON and MessagePack are self-describing and encode any plain structure.
    Protobuf is schema-driven: it can only encode a class built by
    `@wreath.protobuf.message`, because the field numbers *are* the wire
    contract and there is nothing to infer them from. Reaching the codec with a
    plain dict raises `AttributeError` on a private attribute, which tells the
    caller nothing, so the precondition is guarded here instead.

    Asked of `wreath.protobuf` rather than read off the class: the private plan
    marker was spelled out here, which put a second notion of "is this a
    message?" in a second module, to drift the first time the marker moved.
    """
    if not _is_message(data):
        raise TypeError(
            "application/x-protobuf can only encode a class declared with "
            f"@message from wreath.protobuf; got {type(data).__name__}. "
            "Protobuf carries field numbers rather than names, so there is "
            "nothing to derive them from for an undeclared value."
        )
    return _protobuf_encode(data)


JSON = Serializer("application/json", _to_bytes)
MSGPACK = Serializer("application/msgpack", _msgpack)
PROTOBUF = Serializer("application/x-protobuf", _to_protobuf)

#: Content types `wreath.binding` reads a **request** body as protobuf under.
#:
#: Wreath emits exactly one of them -- `PROTOBUF.media_type`, which is what
#: OTLP/HTTP and every tool around it sends -- and reads both, because
#: `application/protobuf` is the IANA registration and the two name one format.
#: Being strict about a sender's spelling of an unambiguous type buys nothing
#: and costs a caller a body refused for a reason that is not about the body.
PROTOBUF_MEDIA_TYPES: frozenset[str] = frozenset(
    {PROTOBUF.media_type, "application/protobuf"}
)

#: JSON first, so it wins ties and is the default when Accept is absent/`*/*`.
#:
#: `PROTOBUF` is deliberately absent. JSON and MessagePack encode whatever a
#: handler returns, so offering them everywhere costs nothing; protobuf can only
#: encode a declared message, and a handler returning a dict is the common case.
#: Adding it here would turn every existing `serialize()` call site into a
#: runtime error for any client that sent `Accept: application/x-protobuf`. Pass
#: `serializers=(PROTOBUF, JSON)` at the call sites that return a message.
DEFAULT_SERIALIZERS: tuple[Serializer, ...] = (JSON, MSGPACK)


def parse_accept(header: str | None) -> list[tuple[str, float]]:
    """Parse an `Accept` header into `(media_range, q)` pairs, best first.

    Ordered by q (desc) then specificity (exact > `type/*` > `*/*`), with
    header order breaking a remaining tie. A `q=0` range (explicitly *not*
    acceptable) is kept rather than dropped, so `negotiate` can apply it as
    an exclusion. Media ranges are lowercased; parameters other than `q` are
    discarded, so `text/html;level=1` and `text/html` are one range.

    This never raises. A malformed `q` is read as `q=0` -- refusing the range
    rather than promoting it -- and an empty element is skipped.

    Args:
        header: A raw `Accept` header value, or None/empty for "no preference".

    Returns:
        `(media_range, q)` best first; empty when the header is absent or empty.
    """
    return _core.parse_accept(header)


def negotiate(
    accept: str | None, serializers: Sequence[Serializer] = DEFAULT_SERIALIZERS
) -> Serializer | None:
    """Pick the best serializer for an `Accept` header, or None if unsatisfiable.

    Ranges are tried best-first (see `parse_accept`) and the first
    `serializers` entry matching one wins, so the *offer* order decides between
    two types the client ranked equally. A missing or empty `Accept` yields
    `serializers[0]`; so does `*/*`, because it matches everything and the
    first offer is the first match.

    A `q=0` range excludes every type it matches, across the whole header, and
    that exclusion is applied even when a later wildcard would have matched:
    `application/json;q=0, */*` will not return JSON.

    Args:
        serializers: Offers in preference order; an empty sequence always yields None.

    Returns:
        The chosen serializer, or None when nothing offered is acceptable.
    """
    index = _core.negotiate_media(
        accept, tuple(serializer.media_type for serializer in serializers)
    )
    return None if index is None else serializers[index]


def serialize(
    request: Request,
    data: Any,
    *,
    serializers: Sequence[Serializer] = DEFAULT_SERIALIZERS,
    status: int = 200,
) -> Response:
    """Serialize `data` in the client's preferred format, or return `406`.

    The successful response carries the negotiated `Content-Type` and
    `Vary: Accept`, so a shared cache keys on the format rather than serving
    one client's msgpack to another client's JSON request. An unsatisfiable
    `Accept` produces a `ProblemResponse` listing what was offered -- returned,
    not raised, so it flows through the ordinary response path.

    `status` applies only to the successful case; the refusal is always 406.

    Args:
        data: Anything the chosen serializer's `encode` accepts.
        serializers: Offers in preference order; defaults to JSON then MessagePack.
        status: Status for the encoded response.

    Returns:
        A `Response` with the negotiated body, or a 406 `ProblemResponse`.
    """
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
