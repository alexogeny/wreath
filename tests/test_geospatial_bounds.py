from __future__ import annotations

import math

import pytest

from wreath.geospatial import (
    Coordinate,
    GeospatialError,
    bounding_boxes,
    distance,
)


class TestOrdinaryBoxes:
    def test_a_box_contains_its_centre(self) -> None:
        centre = Coordinate(lat=-27.4698, lon=153.0251)
        (box,) = bounding_boxes(centre, 5_000.0)
        assert box.contains(centre)

    @pytest.mark.parametrize(
        "centre",
        [
            Coordinate(lat=0.0, lon=0.0),
            Coordinate(lat=-27.4698, lon=153.0251),
            Coordinate(lat=60.0, lon=11.0),
            Coordinate(lat=-64.0, lon=-58.0),
        ],
        ids=["equator", "subtropical", "high-north", "high-south"],
    )
    def test_a_box_is_a_superset_of_the_circle(self, centre: Coordinate) -> None:
        radius = 10_000.0
        boxes = bounding_boxes(centre, radius)
        for bearing_deg in range(0, 360, 5):
            bearing = math.radians(bearing_deg)
            # Walk out along a great circle to just inside the radius.
            angular = (radius * 0.999) / 6_371_008.8
            phi1 = math.radians(centre.lat)
            lam1 = math.radians(centre.lon)
            phi2 = math.asin(
                math.sin(phi1) * math.cos(angular)
                + math.cos(phi1) * math.sin(angular) * math.cos(bearing)
            )
            lam2 = lam1 + math.atan2(
                math.sin(bearing) * math.sin(angular) * math.cos(phi1),
                math.cos(angular) - math.sin(phi1) * math.sin(phi2),
            )
            point = Coordinate(
                lat=math.degrees(phi2),
                lon=(math.degrees(lam2) + 540.0) % 360.0 - 180.0,
            )
            assert distance(centre, point) < radius
            assert any(box.contains(point) for box in boxes), (
                f"bearing {bearing_deg} escaped every box"
            )

    def test_a_bigger_radius_gives_a_bigger_box(self) -> None:
        centre = Coordinate(lat=0.0, lon=0.0)
        (small,) = bounding_boxes(centre, 1_000.0)
        (large,) = bounding_boxes(centre, 10_000.0)
        assert large.lat_max > small.lat_max
        assert large.lon_max > small.lon_max

    def test_latitude_span_does_not_depend_on_longitude(self) -> None:
        (equator,) = bounding_boxes(Coordinate(lat=0.0, lon=0.0), 10_000.0)
        (elsewhere,) = bounding_boxes(Coordinate(lat=0.0, lon=120.0), 10_000.0)
        assert (equator.lat_max - equator.lat_min) == pytest.approx(
            elsewhere.lat_max - elsewhere.lat_min
        )

    def test_longitude_span_widens_toward_the_pole(self) -> None:
        (equator,) = bounding_boxes(Coordinate(lat=0.0, lon=0.0), 10_000.0)
        (high,) = bounding_boxes(Coordinate(lat=60.0, lon=0.0), 10_000.0)
        assert (high.lon_max - high.lon_min) > (equator.lon_max - equator.lon_min)


class TestTheAntimeridian:
    """179E and 179W are two degrees apart. A box that does not know this
    returns nothing, and the bug reads as 'no vehicles near Fiji'."""

    def test_a_circle_crossing_the_date_line_yields_two_boxes(self) -> None:
        centre = Coordinate(lat=0.0, lon=179.95)
        boxes = bounding_boxes(centre, 20_000.0)
        assert len(boxes) == 2

    def test_neither_box_has_an_out_of_range_longitude(self) -> None:
        boxes = bounding_boxes(Coordinate(lat=0.0, lon=179.95), 20_000.0)
        for box in boxes:
            assert -180.0 <= box.lon_min <= 180.0
            assert -180.0 <= box.lon_max <= 180.0
            assert box.lon_min <= box.lon_max

    def test_a_point_just_over_the_line_is_covered(self) -> None:
        centre = Coordinate(lat=0.0, lon=179.95)
        just_over = Coordinate(lat=0.0, lon=-179.98)
        assert distance(centre, just_over) < 20_000.0
        boxes = bounding_boxes(centre, 20_000.0)
        assert any(box.contains(just_over) for box in boxes)

    def test_the_western_side_splits_too(self) -> None:
        centre = Coordinate(lat=0.0, lon=-179.95)
        just_over = Coordinate(lat=0.0, lon=179.98)
        assert distance(centre, just_over) < 20_000.0
        boxes = bounding_boxes(centre, 20_000.0)
        assert len(boxes) == 2
        assert any(box.contains(just_over) for box in boxes)

    def test_a_circle_not_crossing_the_line_stays_one_box(self) -> None:
        boxes = bounding_boxes(Coordinate(lat=0.0, lon=150.0), 20_000.0)
        assert len(boxes) == 1


class TestThePoles:
    """A circle containing a pole is bounded by no finite longitude span:
    every meridian passes through it."""

    def test_a_circle_over_the_pole_spans_every_longitude(self) -> None:
        (box,) = bounding_boxes(Coordinate(lat=89.99, lon=0.0), 50_000.0)
        assert box.lon_min == -180.0
        assert box.lon_max == 180.0

    def test_the_southern_pole_behaves_the_same(self) -> None:
        (box,) = bounding_boxes(Coordinate(lat=-89.99, lon=0.0), 50_000.0)
        assert box.lon_min == -180.0
        assert box.lon_max == 180.0

    def test_latitude_is_still_clamped_to_the_sphere(self) -> None:
        (box,) = bounding_boxes(Coordinate(lat=89.99, lon=0.0), 50_000.0)
        assert box.lat_max <= 90.0
        assert box.lat_min >= -90.0

    def test_a_point_on_the_other_side_of_the_pole_is_covered(self) -> None:
        centre = Coordinate(lat=89.9, lon=0.0)
        over_the_top = Coordinate(lat=89.9, lon=180.0)
        assert distance(centre, over_the_top) < 30_000.0
        boxes = bounding_boxes(centre, 30_000.0)
        assert any(box.contains(over_the_top) for box in boxes)

    def test_a_radius_larger_than_the_earth_covers_everything(self) -> None:
        (box,) = bounding_boxes(Coordinate(lat=0.0, lon=0.0), 40_000_000.0)
        assert box.lat_min == -90.0 and box.lat_max == 90.0
        assert box.lon_min == -180.0 and box.lon_max == 180.0


class TestRefusals:
    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_non_positive_or_infinite_radius_is_refused(self, bad: float) -> None:
        with pytest.raises(GeospatialError):
            bounding_boxes(Coordinate(lat=0.0, lon=0.0), bad)

    def test_a_bool_radius_is_refused(self) -> None:
        with pytest.raises(GeospatialError):
            bounding_boxes(Coordinate(lat=0.0, lon=0.0), True)  # ty: ignore[invalid-argument-type]

    def test_a_bare_pair_centre_is_refused(self) -> None:
        with pytest.raises(TypeError):
            bounding_boxes((0.0, 0.0), 100.0)  # ty: ignore[invalid-argument-type]

    def test_a_text_radius_is_refused_rather_than_parsed(self) -> None:
        with pytest.raises(GeospatialError):
            bounding_boxes(Coordinate(lat=0.0, lon=0.0), "100")  # ty: ignore[invalid-argument-type]

    def test_a_none_radius_is_refused(self) -> None:
        with pytest.raises(GeospatialError):
            bounding_boxes(Coordinate(lat=0.0, lon=0.0), None)  # ty: ignore[invalid-argument-type]


class TestBoundingBoxValue:
    """`BoundingBox`'s own behaviour. A mutation pass reported every one of
    these as `unreached` -- the type was exercised only through `contains`,
    so its equality and its immutability were asserted by nothing."""

    def test_a_box_is_immutable(self) -> None:
        (box,) = bounding_boxes(Coordinate(lat=0.0, lon=0.0), 1_000.0)
        with pytest.raises(AttributeError):
            box.lat_min = 5.0  # ty: ignore[invalid-assignment]

    def test_equal_boxes_compare_and_hash_equal(self) -> None:
        centre = Coordinate(lat=-27.4698, lon=153.0251)
        (a,) = bounding_boxes(centre, 5_000.0)
        (b,) = bounding_boxes(centre, 5_000.0)
        assert a == b
        assert hash(a) == hash(b)

    def test_boxes_differing_in_any_single_edge_are_unequal(self) -> None:
        from wreath.geospatial import BoundingBox

        base = BoundingBox(1.0, 2.0, 3.0, 4.0)
        assert base != BoundingBox(9.0, 2.0, 3.0, 4.0)
        assert base != BoundingBox(1.0, 9.0, 3.0, 4.0)
        assert base != BoundingBox(1.0, 2.0, 9.0, 4.0)
        assert base != BoundingBox(1.0, 2.0, 3.0, 9.0)

    def test_a_box_is_not_equal_to_a_foreign_type(self) -> None:
        from wreath.geospatial import BoundingBox

        assert BoundingBox(1.0, 2.0, 3.0, 4.0) != "not a box"
