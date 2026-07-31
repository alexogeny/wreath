"""The `wreath.geospatial` value type, its refusals, and the maths.

The contract this file exists to pin is the *ordering* one: GeoJSON writes
`[lon, lat]`, humans say "lat, lon", and a library that accepts a bare pair
picks a side silently. `wreath.temporal` already refuses a naive datetime
rather than assuming UTC; this is the same refusal for place.
"""

from __future__ import annotations

import math

import pytest

from wreath.geospatial import Coordinate, GeospatialError, distance


class TestCoordinateRefusesAmbiguity:
    def test_a_coordinate_is_built_with_keywords(self) -> None:
        here = Coordinate(lat=-27.4698, lon=153.0251)
        assert here.lat == pytest.approx(-27.4698)
        assert here.lon == pytest.approx(153.0251)

    def test_a_bare_pair_is_refused_by_name(self) -> None:
        """The whole point. A positional pair has no self-evident order."""
        with pytest.raises(TypeError) as caught:
            Coordinate(-27.4698, 153.0251)  # ty: ignore[missing-argument]
        message = str(caught.value)
        assert "lat=" in message and "lon=" in message

    def test_the_refusal_explains_the_geojson_trap(self) -> None:
        """A reader who hit this needs to know *why*, or they will work around it."""
        with pytest.raises(TypeError) as caught:
            Coordinate(1.0, 2.0)  # ty: ignore[missing-argument]
        message = str(caught.value).lower()
        assert "geojson" in message
        assert "lon" in message and "lat" in message

    def test_a_sequence_is_not_silently_unpacked(self) -> None:
        with pytest.raises(TypeError):
            Coordinate([153.0251, -27.4698])  # ty: ignore[missing-argument]

    @pytest.mark.parametrize("bad", [90.001, -90.001, 1e9, float("nan")])
    def test_latitude_out_of_range_is_refused(self, bad: float) -> None:
        with pytest.raises(GeospatialError) as caught:
            Coordinate(lat=bad, lon=0.0)
        assert "lat" in str(caught.value)

    @pytest.mark.parametrize("bad", [180.001, -180.001, 1e9, float("nan")])
    def test_longitude_out_of_range_is_refused(self, bad: float) -> None:
        with pytest.raises(GeospatialError) as caught:
            Coordinate(lat=0.0, lon=bad)
        assert "lon" in str(caught.value)

    def test_the_poles_and_the_antimeridian_are_valid_coordinates(self) -> None:
        """The bounds are inclusive; it is beyond them that is nonsense."""
        for coordinate in (
            Coordinate(lat=90.0, lon=0.0),
            Coordinate(lat=-90.0, lon=0.0),
            Coordinate(lat=0.0, lon=180.0),
            Coordinate(lat=0.0, lon=-180.0),
        ):
            assert isinstance(coordinate, Coordinate)

    def test_a_coordinate_is_frozen(self) -> None:
        here = Coordinate(lat=0.0, lon=0.0)
        with pytest.raises(AttributeError):
            here.lat = 1.0  # ty: ignore[invalid-assignment]

    def test_equal_coordinates_hash_equal(self) -> None:
        a = Coordinate(lat=-27.4698, lon=153.0251)
        b = Coordinate(lat=-27.4698, lon=153.0251)
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_integers_are_accepted_and_normalised_to_float(self) -> None:
        here = Coordinate(lat=0, lon=0)
        assert isinstance(here.lat, float)
        assert isinstance(here.lon, float)

    def test_a_bool_is_not_a_latitude(self) -> None:
        """`True` is an int in Python, and that is never a deliberate latitude."""
        with pytest.raises(GeospatialError):
            Coordinate(lat=True, lon=0.0)  # ty: ignore[invalid-argument-type]

    def test_a_string_is_refused_rather_than_parsed(self) -> None:
        with pytest.raises(GeospatialError):
            Coordinate(lat="-27.4698", lon=153.0251)  # ty: ignore[invalid-argument-type]

    # The three below were added after a mutation pass: dropping either half of
    # `lat is None or lon is None` survived, and the raise behind it was
    # reported `unreached`. Half a coordinate is the likeliest real mistake
    # here -- a caller who knows the keyword rule and typos one of them -- and
    # nothing covered it.

    def test_a_latitude_with_no_longitude_is_refused(self) -> None:
        with pytest.raises(TypeError):
            Coordinate(lat=-27.4698)  # ty: ignore[missing-argument]

    def test_a_longitude_with_no_latitude_is_refused(self) -> None:
        with pytest.raises(TypeError):
            Coordinate(lon=153.0251)  # ty: ignore[missing-argument]

    def test_no_arguments_at_all_is_refused(self) -> None:
        with pytest.raises(TypeError):
            Coordinate()  # ty: ignore[missing-argument]

    def test_positional_arguments_are_refused_even_alongside_keywords(self) -> None:
        """The `if args` guard's real job, and the only case that distinguishes
        it from the missing-keyword refusal below it.

        A mutation pass caught this: with the guard removed, every other test
        still passed, because a bare positional pair falls through to the
        missing-keyword check and raises the same error. Only a call carrying
        *both* tells the two apart -- and without the guard this one silently
        ignores the positional values and builds a coordinate from the
        keywords, which is precisely the silent wrong answer the whole type
        exists to prevent.
        """
        with pytest.raises(TypeError):
            Coordinate(1.0, 2.0, lat=-27.4698, lon=153.0251)  # ty: ignore[too-many-positional-arguments]


class TestDistance:
    """Distances are pinned against the same mean-Earth-radius sphere this
    module documents (R = 6_371_008.8 m), so they check *this* model rather
    than an ellipsoidal one -- see the tolerance note in the guide."""

    def test_a_point_is_zero_from_itself(self) -> None:
        here = Coordinate(lat=-27.4698, lon=153.0251)
        assert distance(here, here) == pytest.approx(0.0, abs=1e-6)

    def test_distance_is_symmetric(self) -> None:
        a = Coordinate(lat=-27.4698, lon=153.0251)
        b = Coordinate(lat=-33.8688, lon=151.2093)
        assert distance(a, b) == pytest.approx(distance(b, a), rel=1e-12)

    def test_one_degree_of_latitude_at_the_equator(self) -> None:
        """A degree of latitude is a fixed arc: R * pi / 180."""
        a = Coordinate(lat=0.0, lon=0.0)
        b = Coordinate(lat=1.0, lon=0.0)
        expected = 6_371_008.8 * math.pi / 180.0
        assert distance(a, b) == pytest.approx(expected, rel=1e-9)

    def test_antipodal_points_are_half_the_circumference(self) -> None:
        a = Coordinate(lat=0.0, lon=0.0)
        b = Coordinate(lat=0.0, lon=180.0)
        expected = 6_371_008.8 * math.pi
        assert distance(a, b) == pytest.approx(expected, rel=1e-9)

    def test_pole_to_pole(self) -> None:
        a = Coordinate(lat=90.0, lon=0.0)
        b = Coordinate(lat=-90.0, lon=0.0)
        expected = 6_371_008.8 * math.pi
        assert distance(a, b) == pytest.approx(expected, rel=1e-9)

    def test_a_very_short_distance_does_not_lose_precision(self) -> None:
        """The haversine exists because the law of cosines collapses here."""
        a = Coordinate(lat=-27.4698, lon=153.0251)
        b = Coordinate(lat=-27.4698, lon=153.02511)
        metres = distance(a, b)
        assert 0.0 < metres < 2.0

    def test_longitude_convergence_toward_the_pole(self) -> None:
        """A degree of longitude shrinks with the cosine of the latitude."""
        at_equator = distance(
            Coordinate(lat=0.0, lon=0.0), Coordinate(lat=0.0, lon=1.0)
        )
        at_sixty = distance(
            Coordinate(lat=60.0, lon=0.0), Coordinate(lat=60.0, lon=1.0)
        )
        assert at_sixty == pytest.approx(at_equator * 0.5, rel=1e-3)

    def test_crossing_the_antimeridian_is_the_short_way(self) -> None:
        """179E to 179W is two degrees apart, not 358."""
        a = Coordinate(lat=0.0, lon=179.0)
        b = Coordinate(lat=0.0, lon=-179.0)
        two_degrees = 2.0 * 6_371_008.8 * math.pi / 180.0
        assert distance(a, b) == pytest.approx(two_degrees, rel=1e-9)
