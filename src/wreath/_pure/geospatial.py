"""The pure-Python reference for the geospatial maths.

`wreath.geospatial` prefers the native twin in `_native/geospatial.c` and falls
back to this; `WREATH_PURE=1` forces this one. The two agree to the tolerance
`wreath.geospatial` documents -- not byte-for-byte, because these are floating
point transcendentals and libm's `sin` is not required to be bit-identical to
CPython's. Where a codec twin can be held byte-for-byte (see
`tests/test_msgpack_parity.py`), a maths twin can only be held to a stated
error, and pretending otherwise would be a test that fails on someone else's
libm.
"""

from __future__ import annotations

from math import asin, cos, degrees, radians, sin, sqrt

#: Mean Earth radius in metres (IUGG). The sphere every distance here assumes.
#: An ellipsoidal (Vincenty/Karney) distance differs from this by up to ~0.5%;
#: `wreath.geospatial`'s guide states that rather than implying exactness.
EARTH_RADIUS_M = 6_371_008.8


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two degree coordinates.

    The haversine form is used rather than the spherical law of cosines
    because the latter loses all its significant figures at short distances --
    `acos` of something within an ulp of 1 -- and short distances are the
    common case for anything tracking vehicles, deliveries or animals.
    """
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lon2 - lon1)
    sin_half_phi = sin(d_phi * 0.5)
    sin_half_lambda = sin(d_lambda * 0.5)
    a = sin_half_phi * sin_half_phi + cos(phi1) * cos(phi2) * sin_half_lambda * sin_half_lambda
    # `a` can exceed 1 by an ulp for antipodal inputs, and `asin` would raise.
    if a > 1.0:
        a = 1.0
    return 2.0 * EARTH_RADIUS_M * asin(sqrt(a))


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
