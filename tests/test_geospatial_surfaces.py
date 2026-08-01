"""One `Coordinate` declaration, settled on every surface it crosses.

`wreath.temporal` is the precedent: an `Instant` is declared once and the ORM
column, binding coercion, REST JSON, the OpenAPI format, the typegen alias and
the GraphQL scalar all follow. Before this, those were five places that drifted.

The canonical wire shape is an **object**, `{"lat": ..., "lon": ...}`, never a
bare pair. GeoJSON orders `[lon, lat]` and humans say "lat, lon", so a
two-element array is the one shape that is ambiguous at exactly the moment it
matters -- which is why `Coordinate(...)` itself refuses positional arguments.
Accepting a bare pair here would reintroduce, at the wire, the trap the type
was built to close.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wreath import Wreath
from wreath.binding import ValidationError, validate
from wreath.geospatial import Coordinate


@dataclass
class Station:
    id: int
    at: Coordinate
    backup: Coordinate | None = None


# --- binding coercion --------------------------------------------------------


def test_an_object_coerces_to_a_coordinate() -> None:
    bound = validate(Station, {"id": 1, "at": {"lat": -33.8, "lon": 151.2}})
    assert isinstance(bound.at, Coordinate)
    assert bound.at.lat == pytest.approx(-33.8)
    assert bound.at.lon == pytest.approx(151.2)


def test_an_optional_coordinate_stays_optional() -> None:
    bound = validate(Station, {"id": 1, "at": {"lat": 0.0, "lon": 0.0}})
    assert bound.backup is None


def test_a_bare_pair_is_refused_for_being_the_wrong_shape() -> None:
    """The ordering trap, closed at the wire as well as at the constructor.

    Asserts *why* it was refused. A list also fails the key check below it, so
    a test that only asserts "raised" passes whichever branch fired -- which
    the mutation pass caught: removing the shape guard left this green.
    """
    with pytest.raises(ValidationError) as caught:
        validate(Station, {"id": 1, "at": [-33.8, 151.2]})
    messages = [error["msg"] for error in caught.value.errors]
    assert any("not a {lat, lon} object" in m for m in messages), messages


def test_an_out_of_range_latitude_is_refused() -> None:
    with pytest.raises(ValidationError):
        validate(Station, {"id": 1, "at": {"lat": 91.0, "lon": 0.0}})


def test_a_missing_component_is_refused() -> None:
    with pytest.raises(ValidationError):
        validate(Station, {"id": 1, "at": {"lat": 1.0}})


def test_a_non_numeric_component_is_refused() -> None:
    with pytest.raises(ValidationError):
        validate(Station, {"id": 1, "at": {"lat": "north", "lon": 0.0}})


# --- REST JSON ---------------------------------------------------------------
#
# Encoding a `Coordinate` needs a `__jsonable__` hook on the type itself --
# `wreath.temporal.jsonable` documents it as opt-in, precisely so that
# "serialize any dataclass" cannot put every field of every returned model on
# the wire past a sensitive-field guard. So the hook is declared on
# `Coordinate` in `wreath/geospatial.py`, next to the constructor whose refusal
# it carries outward, rather than inferred by the encoder.


def test_a_coordinate_encodes_as_a_named_object() -> None:
    """The way out, matching the way in.

    Asserts the *bytes*, not a round trip through `loads`: the ordering trap
    lives in the wire text, and a test that decodes first cannot see the
    difference between an object and a pair.
    """
    from wreath._json import dumps

    assert dumps(Coordinate(lat=-33.8, lon=151.2)) == b'{"lat":-33.8,"lon":151.2}'


def test_the_encoded_form_is_never_a_bare_pair() -> None:
    """The GeoJSON trap, closed on the way out too.

    A two-element array would round-trip through anything that agreed with it
    and silently transpose against everything else. This is the assertion that
    would fail if the hook were ever "simplified" to a tuple.
    """
    from wreath._json import dumps

    encoded = dumps(Coordinate(lat=1.0, lon=2.0))
    assert not encoded.startswith(b"["), encoded
    assert b"lat" in encoded and b"lon" in encoded, encoded


def test_a_coordinate_nested_in_a_payload_encodes() -> None:
    """What a handler actually returns: a model with a coordinate on it."""
    from wreath._json import dumps, loads

    payload = {"id": 7, "at": Coordinate(lat=0.5, lon=-0.5), "seen": [Coordinate(lat=1.0, lon=2.0)]}
    assert loads(dumps(payload)) == {
        "id": 7,
        "at": {"lat": 0.5, "lon": -0.5},
        "seen": [{"lat": 1.0, "lon": 2.0}],
    }


def test_a_handler_may_return_a_coordinate_directly() -> None:
    """End to end through the response layer, not just the encoder."""
    from wreath._json import loads
    from wreath.response import JSONResponse

    body = JSONResponse(Coordinate(lat=12.0, lon=34.0)).body
    assert loads(body) == {"lat": 12.0, "lon": 34.0}


def test_crud_serialization_defers_to_the_canonical_form() -> None:
    """One spelling, not two.

    `crud._jsonable` carried its own `Coordinate` branch while the canonical
    form did not exist. Two independent spellings of one wire contract is how
    they drift apart, so this asserts they are the *same* answer rather than
    two answers that happen to agree today.
    """
    from wreath.crud import _jsonable

    point = Coordinate(lat=-27.4698, lon=153.0251)
    assert _jsonable(point) == point.__jsonable__()


# --- the API contract --------------------------------------------------------


def _app() -> Wreath:
    app = Wreath()

    @app.get("/stations/{station_id}")
    async def get_station(request: Any, station_id: int) -> Station:
        return Station(id=station_id, at=Coordinate(lat=0.0, lon=0.0))

    return app


def test_openapi_describes_the_object_and_names_the_format() -> None:
    from wreath.openapi import generate_openapi

    schema = generate_openapi(_app())["components"]["schemas"]["Station"]
    at = schema["properties"]["at"]
    assert at.get("format") == "coordinate", at
    assert at["type"] == "object", at
    assert set(at["properties"]) == {"lat", "lon"}, at


def test_the_typescript_alias_keeps_the_pair_named() -> None:
    """A client must not be able to transpose the pair either.

    A tuple would type-check with the arguments the wrong way round, which is
    the same bug the constructor refuses -- so the emitted type is an object.
    """
    from wreath.typegen.inspect import build_api_model
    from wreath.typegen.targets.typescript import render_typescript

    source = "\n".join(render_typescript(build_api_model(_app())).values())
    assert "lat: number" in source, source
    assert "lon: number" in source, source


def test_the_python_target_annotates_the_real_coordinate() -> None:
    from wreath.openapi import generate_openapi
    from wreath.typegen.inspect import build_api_model
    from wreath.typegen.targets.python import render_python

    app = _app()
    rendered = render_python(
        build_api_model(app), document=generate_openapi(app), class_name="C"
    )
    models = rendered["models.py"]
    assert "at: Coordinate" in models, models
    assert "from wreath.geospatial import Coordinate" in models, models


def test_the_proto_target_emits_a_coordinate_message() -> None:
    from wreath.typegen.inspect import build_api_model
    from wreath.typegen.targets.proto import render_proto

    source = render_proto(build_api_model(_app()))["api.proto"]
    assert "message Coordinate {" in source, source
    assert "double lat = 1;" in source, source
    assert "double lon = 2;" in source, source
