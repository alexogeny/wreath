"""The JSON the read API returns.

A row and a response are not the same shape, and pretending they are is how a
column added for the ingest worker ends up in a public payload. These functions
are the one place the API's vocabulary is decided, which is also the one place
to change when it has to change.

**Coordinates are withheld for a sensitive station unless the caller may
locate it.** A station marked `sensitive` is a rhino midden or a raptor nest,
and publishing where it is assists poachers. Whether *this* caller may be told
is a policy question, and it is answered in `camera_trap.policies` by the same
Cedar engine that guards the routes — `station_json` takes the answer as a
parameter rather than working it out, so there is exactly one place the rule
lives and this file stays a serializer.

**The redaction is a missing key, not a null.** A `latitude: null` says "this
station has no coordinates", which is false and would be charted as the Gulf of
Guinea by a client that trusted it. An absent key says "you were not given
this", which is what happened. The `sensitive` flag stays on the wire either
way, so a client can tell the difference between a station it cannot locate and
one whose coordinates it forgot to ask for.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .models import Camera, Deployment, Reserve, Sighting, Species, Station


def _document(value: Any) -> Any:
    """A ``jsonb`` column as an object rather than a string.

    The ORM decodes ``jsonb`` on its Record hydration path and hands back the
    raw string on its native one, so the same column reaches a handler as a
    ``dict`` from a query with a joined include and as a ``str`` from a plain
    one. Normalising here keeps the API's shape from depending on which path an
    endpoint's query happened to take; it goes when the two paths agree.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


def _degrees(value: Decimal) -> float:
    """A latitude or longitude for the wire.

    ``Numeric`` reads back as an exact ``Decimal``, which JSON has no type for.
    A coordinate is a measurement rather than an amount of money, and six
    decimal places of degrees is about a tenth of a metre, so ``float`` loses
    nothing anybody was relying on. A column where rounding is *wrong* -- a
    balance -- would be rendered as a string instead.
    """
    return float(value)


def reserve_json(reserve: Reserve) -> dict[str, Any]:
    return {
        "id": reserve.id,
        "slug": reserve.slug,
        "name": reserve.name,
        # The timezone is on the wire because every timestamp below is an
        # instant, and a client that wants to say "last night" needs the zone
        # the question means.
        "timezone": reserve.timezone,
        "area_hectares": reserve.area_hectares,
    }


def station_json(
    station: Station,
    *,
    cameras: list[Camera] | None = None,
    locate: bool = False,
) -> dict[str, Any]:
    """One station, with its coordinates only if `locate` says so.

    `locate` defaults to `False` so that a call site which forgets the
    authorization question withholds the coordinates rather than publishing
    them. A default that leaks is a default that will eventually leak.
    """
    payload: dict[str, Any] = {
        "id": station.id,
        "reserve_id": station.reserve_id,
        "name": station.name,
        "habitat": station.habitat,
        "sensitive": station.sensitive,
    }
    if locate:
        payload["latitude"] = _degrees(station.latitude)
        payload["longitude"] = _degrees(station.longitude)
    if cameras is not None:
        payload["cameras"] = [camera_json(camera) for camera in cameras]
    return payload


def camera_json(camera: Camera) -> dict[str, Any]:
    return {
        "id": camera.id,
        "serial": camera.serial,
        "model": camera.model,
        "firmware": camera.firmware,
        "battery_pct": camera.battery_pct,
        "deployed_at": camera.deployed_at,
        # Null while the device is still in service, which is the difference
        # between "this camera" and "the camera that was there then".
        "retired_at": camera.retired_at,
    }


def species_json(species: Species) -> dict[str, Any]:
    return {
        "id": species.id,
        "code": species.code,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
        "protection": species.protection,
        "nocturnal": species.nocturnal,
    }


def deployment_json(deployment: Deployment) -> dict[str, Any]:
    return {
        "id": deployment.id,
        "station_id": deployment.station_id,
        "card_serial": deployment.card_serial,
        "collected_at": deployment.collected_at,
        "image_count": deployment.image_count,
        # Null means the card is collected but not yet ingested -- a real state
        # a client can act on, not a missing value.
        "ingested_at": deployment.ingested_at,
    }


def sighting_json(
    sighting: Sighting, *, related: bool = False, locate: bool = False
) -> dict[str, Any]:
    """One sighting.

    ``captured_at`` and ``uploaded_at`` are both here and are routinely weeks
    apart: the first is when the animal walked past, the second is when the card
    reached a laptop. A client that treats either as "when this happened" will be
    wrong about one of them.

    ``related=True`` reads the station, camera and species objects, which raises
    unless the query included them. That is the point of ``load="raise"``: the
    failure is at the one call site that forgot, not a query per row.
    """
    payload: dict[str, Any] = {
        "id": sighting.id,
        "station_id": sighting.station_id,
        "camera_id": sighting.camera_id,
        "species_id": sighting.species_id,
        "deployment_id": sighting.deployment_id,
        "captured_at": sighting.captured_at,
        "uploaded_at": sighting.uploaded_at,
        "confidence": sighting.confidence,
        "review_state": sighting.review_state,
        "image_key": sighting.image_key,
        "thumbnail_key": sighting.thumbnail_key,
        "tags": _document(sighting.tags),
        "notes": sighting.notes,
    }
    if related:
        payload["station"] = station_json(sighting.station, locate=locate)
        payload["camera"] = camera_json(sighting.camera)
        payload["species"] = species_json(sighting.species)
    return payload


def chart_json(result: Any) -> dict[str, Any]:
    """A series envelope with its averages made serialisable.

    **This conversion is forced, not a preference.** A `Series` measure declared
    with `avg()` over an integer column comes back as a `decimal.Decimal` —
    PostgreSQL's `avg(int)` is `numeric`, and the driver decodes it faithfully.
    `wreath._json` cannot serialise a `Decimal` and `wreath.temporal.jsonable`
    passes it through unchanged, so returning the envelope directly is a 500 on
    any bucket that has data. Rounding here is the application's own answer to a
    question the framework does not answer yet.

    Rounded to one place because the value is a percentage confidence, and the
    sixteen decimal places `numeric` carries are noise a chart cannot draw. A
    money column would want the `Decimal` kept and the encoder taught, which is
    the other half of why this belongs in an application's wire layer rather
    than being pushed into the framework as a global rule.

    The hazard this hides is worth naming: a bucket with no rows averages to
    `None`, so a fixture too small to fill any bucket serialises cleanly and the
    defect stays invisible. That is how it was found — a 2,000-row sample passed
    and a 4,000-row sample did not.
    """
    payload = result.as_dict()
    payload["series"] = [
        {**item, "values": [_plain(value) for value in item["values"]]}
        for item in payload["series"]
    ]
    return payload


def _plain(value: Any) -> Any:
    """`Decimal` to a rounded float; everything else untouched."""
    return round(float(value), 1) if isinstance(value, Decimal) else value
