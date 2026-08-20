/* Great-circle distance.
 *
 * Unlike the wire codecs -- msgpack and JSON, which tests hold byte-for-byte --
 * this is held to the haversine formula at a *stated tolerance* rather than to
 * bit equality, and that is a deliberate contract rather than a weaker test.
 * These are floating-point transcendentals: libm's
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

#include "simd.h"

#define _PY_DATETIME_IMPL
#include <datetime.h>
#undef _PY_DATETIME_IMPL

#include <math.h>

#include <limits.h>

/* Mean Earth radius in metres (IUGG). Must equal EARTH_RADIUS_M in
 * src/wreath/_geodesy.py, which is where both arms define the sphere; a
 * divergence here is a silent, uniform scaling error in every distance the
 * native build reports, which no unit test of *shape* would catch. */
#define WREATH_EARTH_RADIUS_M 6371008.8
/* `M_PI` is a libc extension rather than C11 and is absent under MSVC unless
 * callers opt into Microsoft's math definitions before including <math.h>.
 * Own the rounded binary64 input instead of making the build depend on that
 * platform switch. */
#define WREATH_PI 3.14159265358979323846264338327950288

#define WREATH_TRAJECTORY_CAPSULE "wreath.Trajectory"

typedef struct {
    int64_t when_us;
    double lat;
    double lon;
    double cumulative_metres;
    PyObject *when;
    Py_ssize_t order;
} WreathTrajectoryFix;

typedef struct {
    Py_ssize_t count;
    WreathTrajectoryFix *fixes;
} WreathTrajectory;

static void
wreath_trajectory_free(WreathTrajectory *trajectory)
{
    if (trajectory == NULL) return;
    for (Py_ssize_t index = 0; index < trajectory->count; index++)
        Py_XDECREF(trajectory->fixes[index].when);
    PyMem_Free(trajectory->fixes);
    PyMem_Free(trajectory);
}

static void
wreath_trajectory_capsule_destructor(PyObject *capsule)
{
    WreathTrajectory *trajectory = PyCapsule_GetPointer(
        capsule, WREATH_TRAJECTORY_CAPSULE);
    if (trajectory == NULL) PyErr_Clear();
    else wreath_trajectory_free(trajectory);
}

static WreathTrajectory *
wreath_trajectory_get(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, WREATH_TRAJECTORY_CAPSULE);
}

static PyDateTime_CAPI *
wreath_geo_datetime_api(PyObject *capsule)
{
    if (capsule != NULL && capsule != Py_None)
        return (PyDateTime_CAPI *)PyCapsule_GetPointer(
            capsule, PyDateTime_CAPSULE_NAME);
    return (PyDateTime_CAPI *)PyCapsule_Import(PyDateTime_CAPSULE_NAME, 0);
}

static int64_t
wreath_geo_days_before(int year, int month, int day)
{
    int64_t prior = year - 1;
    int64_t days = prior * 365 + prior / 4 - prior / 100 + prior / 400;
    static const unsigned short before_month[] = {
        0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334,
    };
    days += before_month[month] + day - 1;
    if (month > 2 && year % 4 == 0 && (year % 100 != 0 || year % 400 == 0))
        days++;
    return days;
}

static int
wreath_geo_instant_us(PyObject *value, PyDateTime_CAPI *api, int64_t *out)
{
    if (!PyObject_TypeCheck(value, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(value)) {
        PyErr_SetString(PyExc_TypeError, "trajectory timestamps must be offset-aware datetimes");
        return -1;
    }
    int64_t offset_us = 0;
    if (PyDateTime_DATE_GET_TZINFO(value) != api->TimeZone_UTC) {
        PyObject *offset = PyObject_CallMethod(value, "utcoffset", NULL);
        if (offset == NULL) return -1;
        if (!PyObject_TypeCheck(offset, api->DeltaType)) {
            Py_DECREF(offset);
            PyErr_SetString(
                PyExc_TypeError,
                "trajectory timestamp utcoffset() must return timedelta");
            return -1;
        }
        offset_us =
            ((int64_t)PyDateTime_DELTA_GET_DAYS(offset) * INT64_C(86400) +
             PyDateTime_DELTA_GET_SECONDS(offset)) * INT64_C(1000000) +
            PyDateTime_DELTA_GET_MICROSECONDS(offset);
        Py_DECREF(offset);
    }
    int64_t local_us =
        (wreath_geo_days_before(
             PyDateTime_GET_YEAR(value), PyDateTime_GET_MONTH(value),
             PyDateTime_GET_DAY(value)) * INT64_C(86400) +
         PyDateTime_DATE_GET_HOUR(value) * INT64_C(3600) +
         PyDateTime_DATE_GET_MINUTE(value) * INT64_C(60) +
         PyDateTime_DATE_GET_SECOND(value)) * INT64_C(1000000) +
        PyDateTime_DATE_GET_MICROSECOND(value);
    *out = local_us - offset_us;
    return 0;
}

static int
wreath_trajectory_fix_compare(const void *left_pointer, const void *right_pointer)
{
    const WreathTrajectoryFix *left = left_pointer;
    const WreathTrajectoryFix *right = right_pointer;
    if (left->when_us < right->when_us) return -1;
    if (left->when_us > right->when_us) return 1;
    return left->order < right->order ? -1 : left->order > right->order;
}

static PyObject *
wreath_trajectory_capsule(WreathTrajectory *trajectory)
{
    PyObject *capsule = PyCapsule_New(
        trajectory, WREATH_TRAJECTORY_CAPSULE,
        wreath_trajectory_capsule_destructor);
    if (capsule == NULL) wreath_trajectory_free(trajectory);
    return capsule;
}

static double
wreath_haversine_metres(double lat1, double lon1, double lat2, double lon2)
{
    const double to_rad = WREATH_PI / 180.0;
    double phi1 = lat1 * to_rad;
    double phi2 = lat2 * to_rad;
    double half_d_phi = (phi2 - phi1) * 0.5;
    double half_d_lambda = (lon2 - lon1) * to_rad * 0.5;
    double sin_phi = sin(half_d_phi);
    double sin_lambda = sin(half_d_lambda);
    double a = sin_phi * sin_phi + cos(phi1) * cos(phi2) * sin_lambda * sin_lambda;

    /* `a` can exceed 1 by an ulp for antipodal inputs, where asin() would
     * return NaN. `tests/test_geospatial_parity.py` pins the clamp. */
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

PyObject *
wreath_geo_trajectory_compile(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *source, *error_type, *coordinate_type, *datetime_capi;
    if (!PyArg_ParseTuple(args, "OOOO:geo_trajectory_compile", &source,
                          &error_type, &coordinate_type, &datetime_capi)) return NULL;
    if (!PyExceptionClass_Check(error_type) || !PyType_Check(coordinate_type)) {
        PyErr_SetString(PyExc_TypeError,
                        "trajectory compiler needs exception and Coordinate types");
        return NULL;
    }
    PyDateTime_CAPI *api = wreath_geo_datetime_api(datetime_capi);
    if (api == NULL) return NULL;
    PyObject *sequence = PySequence_Fast(
        source, "trajectory fixes must be an ordered iterable of (timestamp, coordinate) pairs");
    if (sequence == NULL) return NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    if ((size_t)count > SIZE_MAX / sizeof(WreathTrajectoryFix)) {
        Py_DECREF(sequence);
        return PyErr_NoMemory();
    }
    WreathTrajectory *trajectory = PyMem_Calloc(1, sizeof(*trajectory));
    WreathTrajectoryFix *fixes = count != 0
        ? PyMem_Calloc((size_t)count, sizeof(*fixes)) : NULL;
    if (trajectory == NULL || (count != 0 && fixes == NULL)) {
        PyMem_Free(trajectory);
        PyMem_Free(fixes);
        Py_DECREF(sequence);
        return PyErr_NoMemory();
    }
    trajectory->count = count;
    trajectory->fixes = fixes;
    PyObject **items = PySequence_Fast_ITEMS(sequence);
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *fix = items[index];
        if (!PyTuple_Check(fix) || PyTuple_GET_SIZE(fix) != 2) {
            PyErr_Format(error_type,
                         "fix %zd must be a (timestamp, Coordinate) pair", index);
            goto error;
        }
        PyObject *when = PyTuple_GET_ITEM(fix, 0);
        PyObject *coordinate = PyTuple_GET_ITEM(fix, 1);
        if (!PyObject_TypeCheck(coordinate, (PyTypeObject *)coordinate_type)) {
            PyErr_Format(error_type,
                         "fix %zd must carry a Coordinate, got %.200s",
                         index, Py_TYPE(coordinate)->tp_name);
            goto error;
        }
        if (!PyObject_TypeCheck(when, api->DateTimeType) ||
            !_PyDateTime_HAS_TZINFO(when)) {
            PyErr_Format(error_type,
                         "fix %zd has a timestamp with no timezone; a trajectory "
                         "derives durations and speeds from these, and a naive "
                         "timestamp makes both wrong across a DST boundary. Use "
                         "wreath.temporal.Instant.", index);
            goto error;
        }
        PyObject *lat_object = PyObject_GetAttrString(coordinate, "lat");
        PyObject *lon_object = PyObject_GetAttrString(coordinate, "lon");
        if (lat_object == NULL || lon_object == NULL) {
            Py_XDECREF(lat_object);
            Py_XDECREF(lon_object);
            goto error;
        }
        fixes[index].lat = PyFloat_AsDouble(lat_object);
        fixes[index].lon = PyFloat_AsDouble(lon_object);
        Py_DECREF(lat_object);
        Py_DECREF(lon_object);
        if (PyErr_Occurred() ||
            wreath_geo_instant_us(when, api, &fixes[index].when_us) < 0)
            goto error;
        fixes[index].when = Py_NewRef(when);
        fixes[index].order = index;
    }
    Py_DECREF(sequence);
    if (count > 1) {
        qsort(fixes, (size_t)count, sizeof(*fixes),
              wreath_trajectory_fix_compare);
        for (Py_ssize_t index = 1; index < count; index++) {
            fixes[index].cumulative_metres =
                fixes[index - 1].cumulative_metres + wreath_haversine_metres(
                    fixes[index - 1].lat, fixes[index - 1].lon,
                    fixes[index].lat, fixes[index].lon);
        }
    }
    return wreath_trajectory_capsule(trajectory);

error:
    Py_DECREF(sequence);
    wreath_trajectory_free(trajectory);
    return NULL;
}

PyObject *
wreath_geo_trajectory_fixes(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathTrajectory *trajectory = wreath_trajectory_get(capsule);
    if (trajectory == NULL) return NULL;
    PyObject *result = PyTuple_New(trajectory->count);
    if (result == NULL) return NULL;
    for (Py_ssize_t index = 0; index < trajectory->count; index++) {
        WreathTrajectoryFix *fix = &trajectory->fixes[index];
        PyObject *row = Py_BuildValue("(Odd)", fix->when, fix->lat, fix->lon);
        if (row == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, index, row);
    }
    return result;
}

PyObject *
wreath_geo_trajectory_info(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    WreathTrajectory *trajectory = wreath_trajectory_get(capsule);
    if (trajectory == NULL) return NULL;
    double total = trajectory->count < 2 ? 0.0 :
        trajectory->fixes[trajectory->count - 1].cumulative_metres -
        trajectory->fixes[0].cumulative_metres;
    double duration = trajectory->count < 2 ? 0.0 :
        (double)(trajectory->fixes[trajectory->count - 1].when_us -
                 trajectory->fixes[0].when_us) / 1000000.0;
    PyObject *speed = duration > 0.0
        ? PyFloat_FromDouble(total / duration) : Py_NewRef(Py_None);
    if (speed == NULL) return NULL;
    PyObject *result = Py_BuildValue("nddN", trajectory->count, total, duration, speed);
    return result;
}

PyObject *
wreath_geo_trajectory_between(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *start, *end, *datetime_capi = NULL;
    if (!PyArg_ParseTuple(args, "OOO|O:geo_trajectory_between", &capsule,
                          &start, &end, &datetime_capi))
        return NULL;
    WreathTrajectory *source = wreath_trajectory_get(capsule);
    if (source == NULL) return NULL;
    PyDateTime_CAPI *api = wreath_geo_datetime_api(datetime_capi);
    if (api == NULL) return NULL;
    int64_t start_us, end_us;
    if (wreath_geo_instant_us(start, api, &start_us) < 0 ||
        wreath_geo_instant_us(end, api, &end_us) < 0) return NULL;
    Py_ssize_t anchor = -1, first_inside = -1, last_inside = -1;
    for (Py_ssize_t index = 0; index < source->count; index++) {
        int64_t when = source->fixes[index].when_us;
        if (when < start_us) {
            anchor = index;
            continue;
        }
        if (when >= end_us) break;
        if (first_inside < 0) first_inside = index;
        last_inside = index;
    }
    Py_ssize_t first = first_inside >= 0 ?
        (anchor >= 0 ? anchor : first_inside) : anchor;
    Py_ssize_t last = last_inside >= 0 ? last_inside : anchor;
    Py_ssize_t count = first >= 0 ? last - first + 1 : 0;
    WreathTrajectory *result = PyMem_Calloc(1, sizeof(*result));
    WreathTrajectoryFix *fixes = count != 0
        ? PyMem_Calloc((size_t)count, sizeof(*fixes)) : NULL;
    if (result == NULL || (count != 0 && fixes == NULL)) {
        PyMem_Free(result);
        PyMem_Free(fixes);
        return PyErr_NoMemory();
    }
    result->count = count;
    result->fixes = fixes;
    for (Py_ssize_t index = 0; index < count; index++) {
        fixes[index] = source->fixes[first + index];
        fixes[index].when = Py_NewRef(fixes[index].when);
        fixes[index].order = index;
    }
    return wreath_trajectory_capsule(result);
}

#if defined(WREATH_HAVE_AVX2)
WREATH_TARGET_AVX2 static int
wreath_trajectory_grid_avx2(
    const WreathTrajectoryFix *fixes, Py_ssize_t first, Py_ssize_t last,
    double lat_min, double lat_max, double lon_min, double lon_max,
    double lat_step, double lon_step, Py_ssize_t rows, Py_ssize_t columns,
    unsigned char *occupied, Py_ssize_t *occupied_count)
{
    const __m256d v_lat_min = _mm256_set1_pd(lat_min);
    const __m256d v_lat_max = _mm256_set1_pd(lat_max);
    const __m256d v_lon_min = _mm256_set1_pd(lon_min);
    const __m256d v_lon_max = _mm256_set1_pd(lon_max);
    const __m256d v_lat_step = _mm256_set1_pd(lat_step);
    const __m256d v_lon_step = _mm256_set1_pd(lon_step);
    const __m256i offsets = _mm256_setr_epi64x(0, 6, 12, 18);
    Py_ssize_t index = first;
    for (; index + 3 <= last; index += 4) {
        const double *lat_base = &fixes[index].lat;
        const double *lon_base = &fixes[index].lon;
        __m256d lat = _mm256_i64gather_pd(lat_base, offsets, 8);
        __m256d lon = _mm256_i64gather_pd(lon_base, offsets, 8);
        __m256d inside = _mm256_and_pd(
            _mm256_and_pd(_mm256_cmp_pd(lat, v_lat_min, _CMP_GE_OQ),
                          _mm256_cmp_pd(lat, v_lat_max, _CMP_LE_OQ)),
            _mm256_and_pd(_mm256_cmp_pd(lon, v_lon_min, _CMP_GE_OQ),
                          _mm256_cmp_pd(lon, v_lon_max, _CMP_LE_OQ)));
        int mask = _mm256_movemask_pd(inside);
        if (mask != 0) {
            __m256d row_values = _mm256_div_pd(
                _mm256_sub_pd(lat, v_lat_min), v_lat_step);
            __m256d column_values = _mm256_div_pd(
                _mm256_sub_pd(lon, v_lon_min), v_lon_step);
            int row4[4];
            int column4[4];
            _mm_storeu_si128((__m128i *)row4, _mm256_cvttpd_epi32(row_values));
            _mm_storeu_si128(
                (__m128i *)column4, _mm256_cvttpd_epi32(column_values));
            for (int lane = 0; lane < 4; lane++) {
                if ((mask & (1 << lane)) == 0) continue;
                Py_ssize_t row = row4[lane];
                Py_ssize_t column = column4[lane];
                if (row >= rows) row = rows - 1;
                if (column >= columns) column = columns - 1;
                Py_ssize_t cell = row * columns + column;
                unsigned char bit =
                    (unsigned char)(1u << ((unsigned int)cell & 7u));
                unsigned char *slot = &occupied[(size_t)cell >> 3];
                if ((*slot & bit) == 0) {
                    *slot |= bit;
                    (*occupied_count)++;
                }
            }
        }
        if ((index & 1023) == 1023 && PyErr_CheckSignals() < 0) return -1;
    }
    for (; index <= last; index++) {
        double lat = fixes[index].lat;
        double lon = fixes[index].lon;
        if (lat >= lat_min && lat <= lat_max && lon >= lon_min && lon <= lon_max) {
            Py_ssize_t row = (Py_ssize_t)floor((lat - lat_min) / lat_step);
            Py_ssize_t column = (Py_ssize_t)floor((lon - lon_min) / lon_step);
            if (row >= rows) row = rows - 1;
            if (column >= columns) column = columns - 1;
            Py_ssize_t cell = row * columns + column;
            unsigned char bit =
                (unsigned char)(1u << ((unsigned int)cell & 7u));
            unsigned char *slot = &occupied[(size_t)cell >> 3];
            if ((*slot & bit) == 0) {
                *slot |= bit;
                (*occupied_count)++;
            }
        }
    }
    return 0;
}
#endif

PyObject *
wreath_geo_trajectory_grid_summary(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *start, *end, *datetime_capi = NULL;
    double lat_min, lat_max, lon_min, lon_max, lat_step, lon_step;
    Py_ssize_t rows, columns;
    if (!PyArg_ParseTuple(
            args, "OOOddddddnn|O:geo_trajectory_grid_summary",
            &capsule, &start, &end,
            &lat_min, &lat_max, &lon_min, &lon_max, &lat_step, &lon_step,
            &rows, &columns, &datetime_capi)) return NULL;
    if (rows <= 0 || columns <= 0 || lat_step <= 0.0 || lon_step <= 0.0) {
        PyErr_SetString(PyExc_ValueError, "trajectory grid dimensions and steps must be positive");
        return NULL;
    }
    if (columns > PY_SSIZE_T_MAX / rows) return PyErr_NoMemory();
    Py_ssize_t cell_count = rows * columns;
    size_t bit_count = (size_t)cell_count;
    if (bit_count > SIZE_MAX - 7) return PyErr_NoMemory();
    size_t byte_count = (bit_count + 7) / 8;
    unsigned char *occupied = byte_count != 0 ? PyMem_Calloc(byte_count, 1) : NULL;
    if (byte_count != 0 && occupied == NULL) return PyErr_NoMemory();
    WreathTrajectory *trajectory = wreath_trajectory_get(capsule);
    if (trajectory == NULL) {
        PyMem_Free(occupied);
        return NULL;
    }
    PyDateTime_CAPI *api = wreath_geo_datetime_api(datetime_capi);
    int64_t start_us, end_us;
    if (api == NULL || wreath_geo_instant_us(start, api, &start_us) < 0 ||
        wreath_geo_instant_us(end, api, &end_us) < 0) {
        PyMem_Free(occupied);
        return NULL;
    }
    Py_ssize_t count = trajectory->count;
    Py_ssize_t anchor = -1, first_inside = -1, last_inside = -1;
    for (Py_ssize_t index = 0; index < count; index++) {
        int64_t when = trajectory->fixes[index].when_us;
        if (when < start_us) {
            anchor = index;
            continue;
        }
        if (when >= end_us) break;
        if (first_inside < 0) first_inside = index;
        last_inside = index;
    }
    Py_ssize_t first = first_inside >= 0 ? (anchor >= 0 ? anchor : first_inside) : anchor;
    Py_ssize_t last = last_inside >= 0 ? last_inside : anchor;
    double total_distance = first >= 0 && last > first
        ? trajectory->fixes[last].cumulative_metres -
          trajectory->fixes[first].cumulative_metres
        : 0.0;
    Py_ssize_t occupied_count = 0;
    if (first >= 0) {
#if defined(WREATH_HAVE_AVX2)
        if (last - first >= 15 && rows <= INT_MAX && columns <= INT_MAX &&
            wreath_simd_has_avx2()) {
            if (wreath_trajectory_grid_avx2(
                    trajectory->fixes, first, last, lat_min, lat_max,
                    lon_min, lon_max, lat_step, lon_step, rows, columns,
                    occupied, &occupied_count) < 0) goto error;
        }
        else
#endif
        for (Py_ssize_t index = first; index <= last; index++) {
            double lat = trajectory->fixes[index].lat;
            double lon = trajectory->fixes[index].lon;
            if (lat >= lat_min && lat <= lat_max && lon >= lon_min && lon <= lon_max) {
                Py_ssize_t row = (Py_ssize_t)floor((lat - lat_min) / lat_step);
                Py_ssize_t column = (Py_ssize_t)floor((lon - lon_min) / lon_step);
                if (row >= rows) row = rows - 1;
                if (column >= columns) column = columns - 1;
                Py_ssize_t cell = row * columns + column;
                unsigned char mask = (unsigned char)(1u << ((unsigned int)cell & 7u));
                unsigned char *slot = &occupied[(size_t)cell >> 3];
                if ((*slot & mask) == 0) {
                    *slot |= mask;
                    occupied_count++;
                }
            }
            if ((index & 1023) == 1023 && PyErr_CheckSignals() < 0) goto error;
        }
    }
    PyObject *cells = PyTuple_New(occupied_count);
    if (cells == NULL) goto error;
    Py_ssize_t output = 0;
    for (Py_ssize_t cell = 0; cell < cell_count; cell++) {
        unsigned char mask = (unsigned char)(1u << ((unsigned int)cell & 7u));
        if ((occupied[(size_t)cell >> 3] & mask) == 0) continue;
        PyObject *position = Py_BuildValue("(nn)", cell / columns, cell % columns);
        if (position == NULL) {
            Py_DECREF(cells);
            goto error;
        }
        PyTuple_SET_ITEM(cells, output++, position);
    }
    PyObject *speed = Py_NewRef(Py_None);
    if (first >= 0 && last > first) {
        double seconds = (double)(trajectory->fixes[last].when_us -
                                  trajectory->fixes[first].when_us) / 1000000.0;
        if (seconds > 0.0) {
            Py_SETREF(speed, PyFloat_FromDouble(total_distance / seconds));
            if (speed == NULL) {
                Py_DECREF(cells);
                goto error;
            }
        }
    }
    PyObject *result = wreath_tuple2_from_owned(cells, speed);
    PyMem_Free(occupied);
    return result;

error:
    PyMem_Free(occupied);
    return NULL;
}
