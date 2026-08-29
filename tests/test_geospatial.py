from __future__ import annotations

import math

import pytest

from wreath.geospatial import Coordinate, GeospatialError, Polygon, distance


class TestCoordinateRefusesAmbiguity:
    def test_a_coordinate_is_built_with_keywords(self) -> None:
        here = Coordinate(lat=-27.4698, lon=153.0251)
        assert here.lat == pytest.approx(-27.4698)
        assert here.lon == pytest.approx(153.0251)

    def test_a_bare_pair_is_refused_by_name(self) -> None:
        with pytest.raises(TypeError) as caught:
            Coordinate(-27.4698, 153.0251)  # ty: ignore[missing-argument]
        message = str(caught.value)
        assert "lat=" in message and "lon=" in message

    def test_the_refusal_explains_the_geojson_trap(self) -> None:
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
        with pytest.raises(TypeError):
            Coordinate(1.0, 2.0, lat=-27.4698, lon=153.0251)  # ty: ignore[too-many-positional-arguments]


class TestPolygonRing:
    """The ring's construction refusals, which only the PostGIS suite reached.

    Those tests need a live database, so on a machine without one every refusal
    below was collected and skipped -- and `Polygon` is where the transposition
    trap this module exists to refuse is easiest to fall into, because WKT is a
    string a caller can paste.
    """

    CORNERS = (
        Coordinate(lat=-27.0, lon=153.0),
        Coordinate(lat=-27.0, lon=154.0),
        Coordinate(lat=-28.0, lon=153.5),
    )

    def test_wkt_text_is_refused_rather_than_parsed(self) -> None:
        with pytest.raises(TypeError) as caught:
            Polygon("POLYGON((153.0 -27.0, 154.0 -27.0, 153.5 -28.0, 153.0 -27.0))")  # ty: ignore[invalid-argument-type]
        message = str(caught.value)
        assert "WKT" in message
        assert "longitude" in message

    def test_a_bare_pair_among_the_vertices_is_refused_by_index(self) -> None:
        with pytest.raises(TypeError) as caught:
            Polygon([*self.CORNERS, (153.0, -27.0)])  # ty: ignore[invalid-argument-type]
        assert "vertex 3" in str(caught.value)

    def test_an_open_ring_is_closed_for_you(self) -> None:
        ring = Polygon(self.CORNERS)
        assert ring.vertices[0] == ring.vertices[-1]
        assert len(ring.vertices) == 4

    def test_a_hand_closed_ring_does_not_count_its_last_vertex_twice(self) -> None:
        by_hand = Polygon([*self.CORNERS, self.CORNERS[0]])
        assert by_hand.vertices == Polygon(self.CORNERS).vertices

    def test_three_distinct_vertices_are_enough(self) -> None:
        assert len(Polygon(self.CORNERS).vertices) == 4

    @pytest.mark.parametrize(
        ("ring", "distinct"),
        [
            (CORNERS[:2], 2),
            ((CORNERS[0], CORNERS[1], CORNERS[0]), 2),
            ((CORNERS[0],), 0),
            ((), 0),
        ],
        ids=["two-points", "a-duplicate-is-not-a-third", "one-point", "empty"],
    )
    def test_fewer_than_three_distinct_vertices_encloses_nothing(
        self, ring: tuple[Coordinate, ...], distinct: int
    ) -> None:
        with pytest.raises(ValueError) as caught:
            Polygon(ring)
        assert f"got {distinct}" in str(caught.value)


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
        a = Coordinate(lat=-27.4698, lon=153.0251)
        b = Coordinate(lat=-27.4698, lon=153.02511)
        metres = distance(a, b)
        assert 0.0 < metres < 2.0

    def test_longitude_convergence_toward_the_pole(self) -> None:
        at_equator = distance(Coordinate(lat=0.0, lon=0.0), Coordinate(lat=0.0, lon=1.0))
        at_sixty = distance(Coordinate(lat=60.0, lon=0.0), Coordinate(lat=60.0, lon=1.0))
        assert at_sixty == pytest.approx(at_equator * 0.5, rel=1e-3)

    def test_crossing_the_antimeridian_is_the_short_way(self) -> None:
        a = Coordinate(lat=0.0, lon=179.0)
        b = Coordinate(lat=0.0, lon=-179.0)
        two_degrees = 2.0 * 6_371_008.8 * math.pi / 180.0
        assert distance(a, b) == pytest.approx(two_degrees, rel=1e-9)
