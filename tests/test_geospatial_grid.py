from __future__ import annotations

import math

import pytest

from wreath.geospatial import (
    BoundingBox,
    Coordinate,
    GeospatialError,
    Grid,
    distance,
    grid,
)


def box(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> BoundingBox:
    return BoundingBox(lat_min, lat_max, lon_min, lon_max)


class TestGridArithmetic:
    def test_a_grid_covers_its_extent(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        assert made.rows >= 1
        assert made.columns >= 1
        # The lattice starts at the extent's corner and reaches past its far
        # edge -- a partial cell at the edge is still a cell, because dropping
        # it would silently narrow the region the reader asked for.
        assert made.cell(0, 0).lat_min == pytest.approx(-30.0)
        assert made.cell(made.rows - 1, made.columns - 1).lat_max >= -29.0
        assert made.cell(made.rows - 1, made.columns - 1).lon_max >= 151.0

    def test_cells_are_approximately_square_in_metres(self):
        # The whole point of sizing in metres rather than degrees: a 10 km cell
        # should be about 10 km on both sides, which a fixed degree step is not.
        made = grid(box(-30.0, -29.5, 150.0, 150.5), metres=10_000)
        cell = made.cell(0, 0)
        tall = distance(
            Coordinate(lat=cell.lat_min, lon=cell.lon_min),
            Coordinate(lat=cell.lat_max, lon=cell.lon_min),
        )
        wide = distance(
            Coordinate(lat=cell.lat_min, lon=cell.lon_min),
            Coordinate(lat=cell.lat_min, lon=cell.lon_max),
        )
        assert tall == pytest.approx(10_000, rel=0.02)
        assert wide == pytest.approx(10_000, rel=0.05)

    def test_a_cell_contains_its_own_centre(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=20_000)
        for row in range(made.rows):
            for column in range(made.columns):
                assert made.cell(row, column).contains(made.centre(row, column))

    def test_cells_tile_without_gap_or_overlap(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=25_000)
        for row in range(made.rows - 1):
            assert made.cell(row, 0).lat_max == pytest.approx(made.cell(row + 1, 0).lat_min)
        for column in range(made.columns - 1):
            assert made.cell(0, column).lon_max == pytest.approx(made.cell(0, column + 1).lon_min)

    def test_index_of_places_a_point_in_the_cell_that_contains_it(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        point = Coordinate(lat=-29.4, lon=150.6)
        row, column = made.index_of(point)
        assert made.cell(row, column).contains(point)

    def test_a_point_outside_the_extent_has_no_cell(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        assert made.index_of(Coordinate(lat=0.0, lon=0.0)) is None


class TestGridRefusals:
    def test_a_non_boundingbox_extent_is_refused(self):
        for bad in (None, (-30.0, -29.0, 150.0, 151.0), "the reserve"):
            with pytest.raises(TypeError, match="BoundingBox"):
                grid(bad, metres=10_000)

    @pytest.mark.parametrize("bad", [None, "10km", b"10", object()])
    def test_a_non_numeric_cell_size_is_refused(self, bad):
        with pytest.raises(GeospatialError, match="must be a number"):
            grid(box(-30.0, -29.0, 150.0, 151.0), metres=bad)

    def test_a_boolean_cell_size_is_refused_rather_than_read_as_one_metre(self):
        # `True` is an int in Python, and a 1 m grid over a degree would be a
        # hundred million cells rather than an error.
        with pytest.raises(GeospatialError, match="must be a number"):
            grid(box(-30.0, -29.0, 150.0, 151.0), metres=True)

    def test_a_cell_reaching_a_pole_is_refused_naming_it(self):
        # Distinct from the distortion refusal: here no *finite* longitude step
        # tiles the extent at all, because the cell's own width reaches the
        # pole. Caught before the distortion arithmetic, which would divide by
        # a cosine that has collapsed.
        with pytest.raises(GeospatialError, match="pole"):
            grid(box(89.0, 90.0, 150.0, 151.0), metres=200_000)

    def test_a_non_positive_cell_size_is_refused(self):
        for bad in (0, -1, -10_000.0):
            with pytest.raises(GeospatialError, match="positive"):
                grid(box(-30.0, -29.0, 150.0, 151.0), metres=bad)

    def test_a_non_finite_cell_size_is_refused(self):
        with pytest.raises(GeospatialError, match="finite"):
            grid(box(-30.0, -29.0, 150.0, 151.0), metres=float("inf"))

    def test_an_inverted_extent_is_refused(self):
        with pytest.raises(GeospatialError, match="lat_min"):
            grid(box(-29.0, -30.0, 150.0, 151.0), metres=10_000)

    def test_an_extent_crossing_the_antimeridian_is_refused_naming_the_remedy(self):
        # Not a silent wrap: a lattice generated over a wrapped longitude range
        # runs backwards. `bounding_boxes` already returns two boxes for this
        # case, so the caller has the tool -- the refusal names it.
        with pytest.raises(GeospatialError, match="antimeridian"):
            grid(box(-30.0, -29.0, 179.0, -179.0), metres=10_000)

    def test_an_extent_too_tall_to_tile_squarely_is_refused(self):
        # A single longitude step cannot stay square across a tall extent: the
        # metres-per-degree of longitude changes with latitude. Refusing beats
        # drawing cells that are 10 km wide at one edge and 5 km at the other
        # while the legend claims one number.
        with pytest.raises(GeospatialError, match="distort"):
            grid(box(0.0, 70.0, 150.0, 151.0), metres=10_000)

    def test_the_distortion_refusal_names_the_measured_variation(self):
        with pytest.raises(GeospatialError) as raised:
            grid(box(0.0, 70.0, 150.0, 151.0), metres=10_000)
        assert "%" in str(raised.value)

    def test_a_modest_extent_at_high_latitude_is_allowed(self):
        # High latitude is fine; it is *variation across* the extent that is
        # the problem. A library that refused Tromsø would be unusable there.
        made = grid(box(69.0, 69.5, 18.0, 19.0), metres=5_000)
        assert made.rows >= 1


class TestAnExtentStraddlingTheEquator:
    def test_the_widest_cell_is_found_inside_the_extent_not_at_an_edge(self):
        straddling = grid(box(-5.0, 5.0, 150.0, 151.0), metres=10_000)
        # Symmetric about the equator: both edges are equally far, and the
        # widest point is the equator between them.
        assert straddling.distortion > 0.0
        assert straddling.distortion < 0.10

    def test_a_straddling_extent_reports_more_distortion_than_a_shifted_one(self):
        # Same height, but one contains the equator and one does not. The
        # straddling one spans a wider range of cosines, so it distorts more.
        straddling = grid(box(-5.0, 5.0, 150.0, 151.0), metres=10_000)
        shifted = grid(box(0.0, 10.0, 150.0, 151.0), metres=10_000)
        assert straddling.distortion < shifted.distortion

    def test_an_extent_touching_the_equator_exactly_is_allowed(self):
        made = grid(box(0.0, 1.0, 150.0, 151.0), metres=10_000)
        assert made.rows >= 1


class TestGridIsHonestAboutItself:
    def test_the_grid_reports_its_own_distortion(self):
        made = grid(box(-31.0, -29.0, 150.0, 151.0), metres=10_000)
        assert 0.0 <= made.distortion < 0.10

    def test_a_grid_is_immutable(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        with pytest.raises(AttributeError):
            made.rows = 5

    def test_cell_count_is_rows_times_columns(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        assert made.count == made.rows * made.columns

    def test_an_out_of_range_cell_is_refused_rather_than_wrapped(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        with pytest.raises(GeospatialError, match="row"):
            made.cell(made.rows, 0)
        with pytest.raises(GeospatialError, match="column"):
            made.cell(0, made.columns)
        # Negative indices must not silently address the far edge the way a
        # Python list would.
        with pytest.raises(GeospatialError, match="row"):
            made.cell(-1, 0)


class TestGridIsATypeNotADict:
    def test_grid_is_the_declared_type(self):
        assert isinstance(grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000), Grid)

    def test_two_grids_over_the_same_extent_are_equal(self):
        a = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        b = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        assert a == b
        assert hash(a) == hash(b)

    def test_a_different_cell_size_is_a_different_grid(self):
        a = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        b = grid(box(-30.0, -29.0, 150.0, 151.0), metres=20_000)
        assert a != b


class TestTheLatitudeStepIsUniform:
    def test_every_row_has_the_same_height_in_degrees(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        heights = {
            round(made.cell(row, 0).lat_max - made.cell(row, 0).lat_min, 9)
            for row in range(made.rows)
        }
        assert len(heights) == 1

    def test_the_latitude_step_matches_the_declared_metres(self):
        made = grid(box(-30.0, -29.0, 150.0, 151.0), metres=10_000)
        cell = made.cell(0, 0)
        span = cell.lat_max - cell.lat_min
        assert span == pytest.approx(math.degrees(10_000 / 6_371_008.8), rel=1e-12)
