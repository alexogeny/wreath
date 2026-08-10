"""The sphere `wreath.geospatial` measures on, and the two span conversions.

**These two stay Python, deliberately.** The native module beside them,
`_native/geospatial.c`, implements exactly one function -- `geo_haversine` --
and that is by design rather than unfinished: a
distance is computed once per *row* in a proximity query, so it is the only
piece whose cost scales with the data. The two span conversions below turn a
radius in metres into degrees of latitude and longitude, and `wreath.geospatial`
calls them once per *query*, to size a bounding box before the scan rather than
inside it. There is nothing there for C to win.

`EARTH_RADIUS_M` is shared for a stronger reason than cost. The bounding box and
the distance have to measure on the same sphere or a row can fall inside the box
and outside the radius, so it is defined once here and mirrored by
`WREATH_EARTH_RADIUS_M` in `_native/geospatial.c` -- the one copy the C
preprocessor forces. `tests/test_geospatial_parity.py` holds the two in step.
"""

from __future__ import annotations

from math import asin, cos, degrees, radians, sin

#: Mean Earth radius in metres (IUGG). The sphere every distance here assumes.
#: An ellipsoidal (Vincenty/Karney) distance differs from this by up to ~0.5%;
#: `wreath.geospatial`'s guide states that rather than implying exactness.
EARTH_RADIUS_M = 6_371_008.8


def latitude_span(metres: float) -> float:
    """Degrees of latitude spanned by `metres` of arc. Constant everywhere."""
    return degrees(metres / EARTH_RADIUS_M)


def longitude_span(latitude: float, metres: float) -> float:
    """Degrees of longitude spanned by `metres` at `latitude`, or -1.0.

    Returns **-1.0** when the circle reaches a pole, where no finite longitude
    span bounds it and the caller must widen to the whole range. Signalling
    that with a sentinel rather than raising keeps this function total: a
    bounding box near a pole is a legitimate query, not an error.
    """
    angular = metres / EARTH_RADIUS_M
    denominator = cos(radians(latitude))
    if denominator <= 0.0:
        return -1.0
    ratio = sin(angular) / denominator
    if ratio >= 1.0:
        return -1.0
    return degrees(asin(ratio))
