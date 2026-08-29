from __future__ import annotations

import math

import pytest

from wreath._geodesy import EARTH_RADIUS_M
from wreath._native import _core as _native


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle metres, straight from the formula.

    `2R * asin(sqrt(sin^2(dlat/2) + cos(lat1) cos(lat2) sin^2(dlon/2)))`, on the
    sphere `EARTH_RADIUS_M` names.

    The `min(1.0, ...)` guards `asin` against an argument an ulp above 1, which
    is possible for points a half-turn apart and gives NaN. It does not fire for
    either antipodal case below on CPython's libm -- both land on exactly 1.0 --
    so it is here to keep the reference total rather than because it is
    load-bearing. `test_antipodal_points_are_half_a_circumference_not_nan` is
    what holds the C to the same behaviour.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


#: Relative agreement required between the twins. Far tighter than the ~0.5%
#: sphere-versus-ellipsoid modelling error the guide quotes: this bounds the
#: *implementation* difference, not the model's fidelity to the Earth. The two
#: numbers measure different things and the guide says so.
TWIN_TOLERANCE = 1e-9

_CASES = [
    ("identity", 0.0, 0.0, 0.0, 0.0),
    ("one degree of latitude", 0.0, 0.0, 1.0, 0.0),
    ("one degree of longitude", 0.0, 0.0, 0.0, 1.0),
    ("brisbane to sydney", -27.4698, 153.0251, -33.8688, 151.2093),
    ("london to new york", 51.5074, -0.1278, 40.7128, -74.0060),
    ("antipodal on the equator", 0.0, 0.0, 0.0, 180.0),
    ("pole to pole", 90.0, 0.0, -90.0, 0.0),
    ("across the antimeridian", 0.0, 179.0, 0.0, -179.0),
    ("sub-metre", -27.4698, 153.0251, -27.46980001, 153.02510001),
    ("high latitude", 78.2232, 15.6267, 78.2233, 15.6268),
    ("southern high latitude", -77.8463, 166.6683, -77.8464, 166.6684),
    ("negative meridian crossing", 0.0, -0.5, 0.0, 0.5),
]


@pytest.mark.parametrize("name,lat1,lon1,lat2,lon2", _CASES, ids=[c[0] for c in _CASES])
def test_the_c_matches_the_formula(
    name: str, lat1: float, lon1: float, lat2: float, lon2: float
) -> None:
    native = _native.geo_haversine(lat1, lon1, lat2, lon2)
    assert native == pytest.approx(haversine(lat1, lon1, lat2, lon2), rel=TWIN_TOLERANCE, abs=1e-6)


def test_the_c_measures_on_the_radius_python_sizes_boxes_with() -> None:
    native_degree = _native.geo_haversine(0.0, 0.0, 1.0, 0.0)
    implied_radius = native_degree * 180.0 / math.pi
    assert implied_radius == pytest.approx(EARTH_RADIUS_M, rel=1e-12)


def test_antipodal_points_are_half_a_circumference_not_nan() -> None:
    metres = _native.geo_haversine(0.0, 0.0, 0.0, 180.0)
    assert math.isfinite(metres)
    assert metres == pytest.approx(EARTH_RADIUS_M * math.pi, rel=1e-9)


def test_the_native_side_refuses_the_wrong_arity() -> None:
    with pytest.raises(TypeError):
        _native.geo_haversine(1.0, 2.0, 3.0)


def test_the_native_side_refuses_a_non_number() -> None:
    with pytest.raises(TypeError):
        _native.geo_haversine("0", 0.0, 0.0, 0.0)
