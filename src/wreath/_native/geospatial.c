/* Great-circle distance, the faster twin of src/wreath/_pure/geospatial.py.
 *
 * The pure module remains the reference. Unlike the codec twins -- msgpack and
 * JSON, which tests hold byte-for-byte -- this pair is held to a *stated
 * tolerance* rather than to bit equality, and that is a deliberate contract
 * rather than a weaker test. These are floating-point transcendentals: libm's
 * sin/cos/asin are not required to be correctly rounded, and CPython's math
 * module calls the same libm but through its own error checking, so demanding
 * bit equality would produce a test that passes here and fails on a different
 * platform's libm for no defect. tests/test_geospatial_parity.py pins the
 * tolerance; wreath.geospatial's guide states it.
 *
 * The haversine form is used rather than the spherical law of cosines for the
 * reason it always is: acos() of a value within an ulp of 1 has lost every
 * significant figure, and sub-metre distances between consecutive GPS fixes are
 * the common case for anything this module exists to serve.
 */

#include "wreathcore.h"

#include <math.h>

/* Mean Earth radius in metres (IUGG). Must equal EARTH_RADIUS_M in the pure
 * twin; a divergence here is a silent, uniform scaling error in every distance
 * the native build reports, which no unit test of *shape* would catch. */
#define WREATH_EARTH_RADIUS_M 6371008.8

static double
wreath_haversine_metres(double lat1, double lon1, double lat2, double lon2)
{
    const double to_rad = M_PI / 180.0;
    double phi1 = lat1 * to_rad;
    double phi2 = lat2 * to_rad;
    double half_d_phi = (phi2 - phi1) * 0.5;
    double half_d_lambda = (lon2 - lon1) * to_rad * 0.5;
    double sin_phi = sin(half_d_phi);
    double sin_lambda = sin(half_d_lambda);
    double a = sin_phi * sin_phi + cos(phi1) * cos(phi2) * sin_lambda * sin_lambda;

    /* `a` can exceed 1 by an ulp for antipodal inputs, where asin() would
     * return NaN. The pure twin clamps identically. */
    if (a > 1.0) {
        a = 1.0;
    }
    return 2.0 * WREATH_EARTH_RADIUS_M * asin(sqrt(a));
}

PyObject *
wreath_geo_haversine(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    (void)self;
    if (nargs != 4) {
        PyErr_SetString(PyExc_TypeError,
                        "geo_haversine() takes exactly 4 arguments "
                        "(lat1, lon1, lat2, lon2)");
        return NULL;
    }

    double coords[4];
    for (Py_ssize_t i = 0; i < 4; i++) {
        coords[i] = PyFloat_AsDouble(args[i]);
        if (coords[i] == -1.0 && PyErr_Occurred()) {
            return NULL;
        }
    }

    double metres = wreath_haversine_metres(coords[0], coords[1], coords[2], coords[3]);
    return PyFloat_FromDouble(metres);
}
