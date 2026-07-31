"""The native and pure geospatial twins agree.

Structurally this is `tests/test_msgpack_parity.py`, with one deliberate
difference: the codec twins are held **byte-for-byte**, and these are held to a
**stated tolerance**. That is not a weaker test, it is the only correct one.
`sin`, `cos` and `asin` are not required by IEEE-754 to be correctly rounded,
so two implementations calling different libm paths may differ in the last
ulp. A bit-equality assertion here would pass on this machine and fail on
someone else's for no defect, which is the shape of test that gets deleted
rather than fixed.

The tolerance is what `wreath.geospatial`'s guide promises, so this file is
what stops the guide drifting from the code.
"""

from __future__ import annotations

import math

import pytest

from wreath._pure import geospatial as pure

_native = pytest.importorskip(
    "wreath._native._core",
    reason="native extension not built; the pure twin is the reference and is "
    "covered by tests/test_geospatial.py",
)

if not hasattr(_native, "geo_haversine"):  # pragma: no cover - stale build
    pytest.skip("native build predates geo_haversine", allow_module_level=True)


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
def test_the_twins_agree(
    name: str, lat1: float, lon1: float, lat2: float, lon2: float
) -> None:
    native = _native.geo_haversine(lat1, lon1, lat2, lon2)
    reference = pure.haversine(lat1, lon1, lat2, lon2)
    assert native == pytest.approx(reference, rel=TWIN_TOLERANCE, abs=1e-6)


def test_the_twins_share_one_earth_radius() -> None:
    """A divergent radius is a uniform scaling error no shape test would catch.

    One degree of latitude is exactly `R * pi / 180`, so this reads the native
    side's constant back out rather than trusting the `#define` matches.
    """
    native_degree = _native.geo_haversine(0.0, 0.0, 1.0, 0.0)
    implied_radius = native_degree * 180.0 / math.pi
    assert implied_radius == pytest.approx(pure.EARTH_RADIUS_M, rel=1e-12)


def test_the_native_side_clamps_antipodal_rounding() -> None:
    """`asin(sqrt(a))` with `a` an ulp over 1 is NaN unless both sides clamp."""
    metres = _native.geo_haversine(0.0, 0.0, 0.0, 180.0)
    assert math.isfinite(metres)
    assert metres == pytest.approx(pure.EARTH_RADIUS_M * math.pi, rel=1e-9)


def test_the_native_side_refuses_the_wrong_arity() -> None:
    with pytest.raises(TypeError):
        _native.geo_haversine(1.0, 2.0, 3.0)


def test_the_native_side_refuses_a_non_number() -> None:
    with pytest.raises(TypeError):
        _native.geo_haversine("0", 0.0, 0.0, 0.0)
