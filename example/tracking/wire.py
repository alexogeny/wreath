"""What crosses the wire, in both directions.

Two vocabularies live here and they are not the same one.

**Up** is protobuf, because the collar is on a metered satellite link and the
station relaying it is on a worse one. A position is six numbers; as JSON with
its keys it is about 140 bytes, and as the declaration below it is 31. Over a
season, for forty collars reporting every twenty minutes, that difference is the
airtime bill. The field *numbers* are the contract with every collar already in
the field, which is why they are written down rather than taken from declaration
order -- a collar potted in resin on a rhino's neck cannot be redeployed because
somebody sorted a dataclass alphabetically.

**Down** is JSON, because the reader is a map in a browser. The one thing worth
staring at in the JSON is what happens when a caller may not be told where an
animal is: the ``position`` key is **absent**, not null. ``"position": null``
says this fix has no coordinates, which is false -- it says the collar failed
when in fact the reader was not trusted. An absent key says "not for you", and a
client can tell the difference. That is the camera-trap example's argument about
a station's latitude, generalised from a boolean to a resolution: ``precision_m``
on the wire says how much of an answer this is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wreath.geospatial import Coordinate
from wreath.protobuf import field, message

from .models import Fix
from .place import Precision, degrade


@message
class Position:
    """One fix, as a collar reports it.

    ``accuracy_m`` and ``satellites`` are ``| None`` -- *explicit* presence --
    because a collar that could not estimate its accuracy and one that estimated
    it at zero metres are different claims, and proto3's implicit presence would
    make them the same byte sequence. Everything else is a plain type, where the
    proto3 default is the honest reading: a battery reported as 0% is a flat
    battery.

    ``recorded_at_ms`` is ``sfixed64`` rather than a varint. A millisecond
    timestamp has its high bits set, so a varint spends nine bytes encoding a
    number that fits in eight; ``sfixed64`` is a byte shorter *and* faster to
    both write and read. That is the whole reason `kind=` exists.

    ``lat``/``lon`` stay ``double``. ``float`` would halve them and cost about
    1.7 m of resolution at the equator, which is inside the GPS error and
    therefore tempting -- but the error and the quantisation are independent, so
    the rounding is a *bias* the receiver cannot recover and the noise is not.
    Eight bytes for a coordinate is the one place this declaration does not
    economise.
    """

    collar_id: int = field(1, kind="uint32")
    recorded_at_ms: int = field(2, kind="sfixed64")
    lat: float = field(3)
    lon: float = field(4)
    accuracy_m: float | None = field(5, kind="float")
    battery_pct: int = field(6, kind="uint32")
    satellites: int | None = field(7, kind="uint32")


@message
class PositionBatch:
    """What a field station POSTs: everything it has heard since last time.

    ``positions`` is a repeated *message*, so it is length-delimited per element
    and never packed -- packing applies to repeated scalars only. A batch of one
    and a batch of two hundred are the same request shape, which is what lets a
    station that has been offline for a week drain its spool in one call.
    """

    relay: str = field(1)
    positions: list[Position] = field(2)


@message
class BatchReceipt:
    """What the station gets back, so it knows what to delete from its spool.

    ``rejected`` is not an error count for the *batch* -- a batch that fails to
    parse never reaches this type, and answers 400. It counts positions inside a
    well-formed batch that could not mean anything: a latitude of 91 degrees, a
    collar id nobody has fitted. Those are dropped and reported rather than
    failing the whole batch, because one collar with a corrupted almanac must
    not stop thirty-nine working ones from being recorded.
    """

    accepted: int = field(1, kind="uint32")
    rejected: int = field(2, kind="uint32")
    #: The largest `recorded_at_ms` this application now holds for the relay.
    #: A station resuming after a restart sends everything newer than this,
    #: which is why the receipt carries it rather than making the station
    #: remember.
    watermark_ms: int = field(3, kind="sfixed64")


#: The media type a station sends and this application answers in. Wreath does
#: not map it to a codec: `wreath.protobuf` is a codec and not a content
#: negotiator, so the handler reads bytes and decodes them. See
#: `docs/cookbook/recipes/accept-a-protobuf-body.md`, which is the shape this
#: example follows rather than invents.
MEDIA_TYPE = "application/x-protobuf"

#: The same string as `bytes`, and the one the response constructor must be
#: given. **This is a framework defect the example works around.**
#:
#: `Response(body, media_type=...)` is annotated `bytes | None` and does not
#: check. A `str` is put into the header list unconverted, so the application
#: emits `(b"content-type", "application/x-protobuf")` -- a bytes name with a
#: `str` value, which is not a legal ASGI header pair. Nothing raises at the
#: call site; the response is simply invalid, and what surfaces is a `TypeError`
#: from `wreath.testing.TestResponse.header` or from whatever the deployed
#: server does with it.
#:
#: The reason it is worth a named constant rather than a `.encode()` at the one
#: call site is that `docs/cookbook/recipes/accept-a-protobuf-body.md` -- the
#: recipe this example deliberately follows -- shows the `str` form. Anyone
#: copying the recipe hits this, and the constant is where the answer lives.
#: Reported; when `Response` either encodes or refuses, this collapses back into
#: `MEDIA_TYPE`.
MEDIA_TYPE_HEADER = MEDIA_TYPE.encode("ascii")


def instant(milliseconds: int) -> datetime:
    """A collar's millisecond epoch as an aware UTC datetime.

    Aware, always. A collar reports UTC and there is no second candidate, but
    handing a naive datetime to the driver would store it as whatever the
    server's zone happens to be, and the failure -- a track that shifts by two
    hours when the application moves data centre -- looks like a GPS problem.
    """
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=UTC)


def milliseconds(moment: datetime) -> int:
    """The inverse of :func:`instant`, for the receipt's watermark."""
    return int(moment.timestamp() * 1000)


def fix_json(
    row: Fix,
    *,
    precision: Precision | None,
    animal: str | None = None,
) -> dict[str, Any]:
    """One fix as JSON, at whatever resolution this reader has earned.

    ``precision`` is *taken*, not worked out. The rule about who may be told
    where an animal is lives in `tracking.policies` and is answered by Cedar;
    this function is a serializer and stays one. Passing the answer in rather
    than reaching for the identity is what keeps a second, drifting copy of the
    rule from growing here -- the camera trap's `station_json` takes its
    `locate` flag for exactly the same reason.

    ``None`` means withheld: the ``position`` and ``precision_m`` keys are both
    absent, and everything that is not a location stays. A reader still learns
    that this animal was heard from, when, and that its collar has 61% battery,
    which is most of what a welfare dashboard wants and none of what a poacher
    does.
    """
    body: dict[str, Any] = {
        "animal_id": row.animal_id,
        "collar_id": row.collar_id,
        "recorded_at": _isoformat(row.recorded_at),
        "received_at": _isoformat(row.received_at),
        "battery_pct": row.battery_pct,
    }
    if animal is not None:
        body["animal"] = animal
    if precision is None:
        return body
    shown = degrade(Coordinate(lat=row.latitude, lon=row.longitude), precision)
    body["position"] = {"lat": shown.lat, "lon": shown.lon}
    body["precision_m"] = precision.metres
    #: Only at full resolution. A 10 km cell centre with "accurate to 18 m"
    #: beside it is a contradiction a client would resolve in favour of the
    #: smaller number, and drawing an 18 m circle round a 10 km answer is
    #: exactly the map a degraded coordinate exists to prevent.
    if precision.metres == 0.0 and row.accuracy_m is not None:
        body["accuracy_m"] = row.accuracy_m
    return body


def landmark_json(name: str, kind: str, distance_m: float) -> dict[str, Any]:
    """A landmark and how far away it is. **Only ever attached to an exact fix.**

    This is the sharpest trap in the example and it is worth reading twice. A
    waterhole's coordinates are published -- they are on the visitor map. So
    "1.2 km from Ndovu Waterhole" is not a fact *about* a position, it very
    nearly *is* the position: it puts the animal on a circle of known centre and
    known radius. Give a reader two of those and the circles intersect at two
    points, one of which is usually in the air.

    Which means a degraded coordinate with a precise landmark distance beside it
    is not degraded at all. The 10 km cell centre is decoration on an answer
    accurate to a metre, and every individual piece looks harmless in review:
    the coordinate was coarsened by policy, and the distance is just a
    convenience for the map legend.

    So the rule is not "round the distance too" -- a distance rounded to 10 km
    is useless, and a distance rounded to 1 km still narrows a 10 km cell by two
    orders of magnitude. The rule is that this key exists only for a reader who
    was already given the exact position, and `tracking.routers` is where that
    is enforced.
    """
    return {"landmark": name, "kind": kind, "distance_m": round(distance_m, 1)}


def _isoformat(value: Any) -> str:
    """A timestamp for the wire, in the shape the ORM hands back.

    The driver returns an aware `datetime` for a `timestamptz`, so this is a
    method call and not a conversion. It is a named function because a naive
    value reaching here would silently serialise without an offset, and a
    reader parsing it would place a rhino in the wrong hour.
    """
    return value.isoformat()
