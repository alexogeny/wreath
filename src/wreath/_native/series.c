#include "wreathcore.h"

/* Fetch the datetime C API per operation.  The implementation guard prevents
 * datetime.h from declaring its usual translation-unit static API pointer, so
 * this file introduces no process-global mutable state. */
#define _PY_DATETIME_IMPL
#include <datetime.h>
#undef _PY_DATETIME_IMPL

#include <math.h>
#include <stdio.h>

static PyObject *series_name_astimezone;
static PyObject *series_name_utcoffset;

int
wreath_series_ready(void)
{
    if (series_name_astimezone == NULL &&
        (series_name_astimezone = PyUnicode_InternFromString("astimezone")) == NULL)
        return -1;
    if (series_name_utcoffset == NULL &&
        (series_name_utcoffset = PyUnicode_InternFromString("utcoffset")) == NULL)
        return -1;
    return 0;
}

static inline PyObject *
series_astimezone(PyObject *value, PyObject *tz)
{
    return PyObject_CallMethodOneArg(value, series_name_astimezone, tz);
}

static const char series_digit_pairs[] =
    "00010203040506070809"
    "10111213141516171819"
    "20212223242526272829"
    "30313233343536373839"
    "40414243444546474849"
    "50515253545556575859"
    "60616263646566676869"
    "70717273747576777879"
    "80818283848586878889"
    "90919293949596979899";

typedef struct {
    PyObject *name;
    PyObject *empty;
} SeriesMeasure;

static inline double
series_as_double(PyObject *value)
{
    return PyFloat_Check(value) ? PyFloat_AS_DOUBLE(value)
                                : PyFloat_AsDouble(value);
}

typedef struct {
    int year, month, day, hour, minute, second, microsecond;
} SeriesWallClock;

static int64_t series_wall_ordinal(const SeriesWallClock *wall);

static int
series_days_in_month(int year, int month)
{
    static const unsigned char days[] = {
        31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    };
    int result = days[month - 1];
    if (month == 2 && (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)))
        result++;
    return result;
}

static void
series_wall_add_day(SeriesWallClock *wall)
{
    if (++wall->day <= series_days_in_month(wall->year, wall->month)) return;
    wall->day = 1;
    if (++wall->month <= 12) return;
    wall->month = 1;
    wall->year++;
}

static int
series_weekday(int year, int month, int day)
{
    /* Sakamoto's Gregorian weekday, rotated from Sunday=0 to Monday=0. */
    static const unsigned char offset[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    if (month < 3) year--;
    int sunday = (year + year / 4 - year / 100 + year / 400 +
                  offset[month - 1] + day) % 7;
    return (sunday + 6) % 7;
}

static void
series_wall_truncate(SeriesWallClock *wall, int unit)
{
    wall->microsecond = 0;
    wall->second = 0;
    if (unit == 0) return;
    wall->minute = 0;
    if (unit == 1) return;
    wall->hour = 0;
    if (unit == 2) return;
    if (unit == 3) {
        int weekday = series_weekday(wall->year, wall->month, wall->day);
        while (weekday-- != 0) {
            if (--wall->day != 0) continue;
            if (--wall->month == 0) {
                wall->month = 12;
                wall->year--;
            }
            wall->day = series_days_in_month(wall->year, wall->month);
        }
        return;
    }
    wall->day = 1;
    if (unit == 4) return;
    if (unit == 5) wall->month = 1 + 3 * ((wall->month - 1) / 3);
    else wall->month = 1;
}

static int
series_wall_advance(SeriesWallClock *wall, int unit)
{
    if (unit == 0) {
        if (++wall->minute < 60) return 0;
        wall->minute = 0;
        if (++wall->hour < 24) return 0;
        wall->hour = 0;
        series_wall_add_day(wall);
    }
    else if (unit == 1) {
        if (++wall->hour < 24) return 0;
        wall->hour = 0;
        series_wall_add_day(wall);
    }
    else if (unit == 2) series_wall_add_day(wall);
    else if (unit == 3) {
        for (int index = 0; index < 7; index++) series_wall_add_day(wall);
    }
    else {
        int months = unit == 4 ? 1 : unit == 5 ? 3 : 12;
        int total = wall->year * 12 + wall->month - 1 + months;
        wall->year = total / 12;
        wall->month = total % 12 + 1;
    }
    if (wall->year <= 9999) return 0;
    PyErr_SetString(PyExc_OverflowError, "series spine exceeds datetime year 9999");
    return -1;
}

static int
series_wall_retreat(SeriesWallClock *wall, int unit)
{
    int days = 0;
    if (unit == 0) {
        if (--wall->minute >= 0) return 0;
        wall->minute = 59;
        if (--wall->hour >= 0) return 0;
        wall->hour = 23;
        days = 1;
    }
    else if (unit == 1) {
        if (--wall->hour >= 0) return 0;
        wall->hour = 23;
        days = 1;
    }
    else if (unit == 2) days = 1;
    else if (unit == 3) days = 7;
    else {
        int months = unit == 4 ? 1 : unit == 5 ? 3 : 12;
        int total = wall->year * 12 + wall->month - 1 - months;
        wall->year = total / 12;
        wall->month = total % 12 + 1;
        if (wall->year >= 1) return 0;
        PyErr_SetString(PyExc_OverflowError, "series spine precedes datetime year 1");
        return -1;
    }
    while (days-- != 0) {
        if (--wall->day != 0) continue;
        if (--wall->month == 0) {
            wall->month = 12;
            wall->year--;
        }
        if (wall->year < 1) {
            PyErr_SetString(
                PyExc_OverflowError, "series spine precedes datetime year 1");
            return -1;
        }
        wall->day = series_days_in_month(wall->year, wall->month);
    }
    return 0;
}

static int
series_wall_compare(const SeriesWallClock *left, const SeriesWallClock *right)
{
#define SERIES_COMPARE_PART(name) \
    do { \
        if (left->name < right->name) return -1; \
        if (left->name > right->name) return 1; \
    } while (0)
    SERIES_COMPARE_PART(year);
    SERIES_COMPARE_PART(month);
    SERIES_COMPARE_PART(day);
    SERIES_COMPARE_PART(hour);
    SERIES_COMPARE_PART(minute);
    SERIES_COMPARE_PART(second);
    SERIES_COMPARE_PART(microsecond);
#undef SERIES_COMPARE_PART
    return 0;
}

static int
series_wall_definitely_before(const SeriesWallClock *left,
                              const SeriesWallClock *right)
{
    /* Python requires every UTC offset to lie strictly inside +/-24 hours.
     * Two local walls at least three calendar days apart therefore cannot be
     * reversed in instant order by even the largest legal offset change. This
     * lets coarse week/month/year cardinalities avoid constructing two folded
     * aware datetimes and calling tz.utcoffset() twice for their final bucket. */
    return series_wall_ordinal(right) - series_wall_ordinal(left) >= 3;
}

static int64_t
series_offset_microseconds(PyObject *delta, PyDateTime_CAPI *api, int *valid)
{
    if (!PyObject_TypeCheck(delta, api->DeltaType)) {
        PyErr_Format(
            PyExc_TypeError, "time zone utcoffset() must return timedelta, got %.200s",
            Py_TYPE(delta)->tp_name);
        *valid = 0;
        return 0;
    }
    *valid = 1;
    return ((int64_t)PyDateTime_DELTA_GET_DAYS(delta) * INT64_C(86400) +
            PyDateTime_DELTA_GET_SECONDS(delta)) * INT64_C(1000000) +
           PyDateTime_DELTA_GET_MICROSECONDS(delta);
}

static PyObject *
series_wall_in_zone(const SeriesWallClock *wall, PyObject *tz,
                    PyObject *offset_method, PyTypeObject *result_type,
                    PyDateTime_CAPI *api)
{
    PyObject *first = api->DateTime_FromDateAndTimeAndFold(
        wall->year, wall->month, wall->day, wall->hour, wall->minute,
        wall->second, wall->microsecond, tz, 0, result_type);
    PyObject *second = api->DateTime_FromDateAndTimeAndFold(
        wall->year, wall->month, wall->day, wall->hour, wall->minute,
        wall->second, wall->microsecond, tz, 1, result_type);
    if (first == NULL || second == NULL) {
        Py_XDECREF(first);
        Py_XDECREF(second);
        return NULL;
    }
    PyObject *first_delta = PyObject_CallOneArg(offset_method, first);
    PyObject *second_delta = PyObject_CallOneArg(offset_method, second);
    if (first_delta == NULL || second_delta == NULL) {
        Py_XDECREF(first_delta);
        Py_XDECREF(second_delta);
        Py_DECREF(first);
        Py_DECREF(second);
        return NULL;
    }
    int first_valid, second_valid;
    int64_t first_offset = series_offset_microseconds(
        first_delta, api, &first_valid);
    int64_t second_offset = series_offset_microseconds(
        second_delta, api, &second_valid);
    Py_DECREF(first_delta);
    Py_DECREF(second_delta);
    if (!first_valid || !second_valid) {
        Py_DECREF(first);
        Py_DECREF(second);
        return NULL;
    }
    if (second_offset < first_offset) {
        Py_DECREF(first);
        return second;
    }
    Py_DECREF(second);
    return first;
}

static PyObject *
series_base_datetime(PyObject *value, PyDateTime_CAPI *api)
{
    return api->DateTime_FromDateAndTimeAndFold(
        PyDateTime_GET_YEAR(value), PyDateTime_GET_MONTH(value),
        PyDateTime_GET_DAY(value), PyDateTime_DATE_GET_HOUR(value),
        PyDateTime_DATE_GET_MINUTE(value), PyDateTime_DATE_GET_SECOND(value),
        PyDateTime_DATE_GET_MICROSECOND(value),
        PyDateTime_DATE_GET_TZINFO(value), PyDateTime_DATE_GET_FOLD(value),
        api->DateTimeType);
}

static PyDateTime_CAPI *
series_datetime_api(PyObject *capsule)
{
    return capsule == NULL
        ? (PyDateTime_CAPI *)PyCapsule_Import(PyDateTime_CAPSULE_NAME, 0)
        : (PyDateTime_CAPI *)PyCapsule_GetPointer(capsule, PyDateTime_CAPSULE_NAME);
}

PyObject *
wreath_series_spine(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *start, *end, *tz, *capsule = NULL;
    int unit;
    if (!PyArg_ParseTuple(
            args, "OOiO|O:series_spine", &start, &end, &unit, &tz, &capsule))
        return NULL;
    PyDateTime_CAPI *api = series_datetime_api(capsule);
    if (api == NULL) return NULL;
    if (!PyObject_TypeCheck(start, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(start)) {
        PyErr_SetString(
            PyExc_TypeError, "series spine start must be an offset-aware datetime");
        return NULL;
    }
    if (!PyObject_TypeCheck(end, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(end)) {
        PyErr_SetString(
            PyExc_TypeError, "series spine end must be an offset-aware datetime");
        return NULL;
    }
    if (unit < 0 || unit > 6) {
        PyErr_SetString(PyExc_ValueError, "series spine unit must be in range 0..6");
        return NULL;
    }
    PyObject *local = series_astimezone(start, tz);
    if (local == NULL) return NULL;
    PyObject *offset_method = PyObject_GetAttr(tz, series_name_utcoffset);
    if (offset_method == NULL) {
        Py_DECREF(local);
        return NULL;
    }
    SeriesWallClock wall = {
        PyDateTime_GET_YEAR(local), PyDateTime_GET_MONTH(local),
        PyDateTime_GET_DAY(local), PyDateTime_DATE_GET_HOUR(local),
        PyDateTime_DATE_GET_MINUTE(local), PyDateTime_DATE_GET_SECOND(local),
        PyDateTime_DATE_GET_MICROSECOND(local),
    };
    Py_DECREF(local);
    series_wall_truncate(&wall, unit);
    PyObject *out = PyList_New(0);
    if (out == NULL) {
        Py_DECREF(offset_method);
        return NULL;
    }
    Py_ssize_t count = 0;
    for (;;) {
        PyObject *current = series_wall_in_zone(
            &wall, tz, offset_method, Py_TYPE(start), api);
        if (current == NULL) goto error;
        int before_end = PyObject_RichCompareBool(current, end, Py_LT);
        if (before_end <= 0) {
            Py_DECREF(current);
            if (before_end < 0) goto error;
            break;
        }
        if (PyList_Append(out, current) < 0) {
            Py_DECREF(current);
            goto error;
        }
        Py_DECREF(current);
        if (series_wall_advance(&wall, unit) < 0) goto error;
        if ((++count & 255) == 0 && PyErr_CheckSignals() < 0) goto error;
    }
    PyObject *result = PyList_AsTuple(out);
    Py_DECREF(out);
    Py_DECREF(offset_method);
    return result;

error:
    Py_DECREF(out);
    Py_DECREF(offset_method);
    return NULL;
}

PyObject *
wreath_series_spine_length(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *start, *end, *tz, *capsule = NULL;
    int unit;
    if (!PyArg_ParseTuple(
            args, "OOiO|O:series_spine_length",
            &start, &end, &unit, &tz, &capsule))
        return NULL;
    PyDateTime_CAPI *api = series_datetime_api(capsule);
    if (api == NULL) return NULL;
    if (!PyObject_TypeCheck(start, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(start)) {
        PyErr_SetString(
            PyExc_TypeError, "series spine start must be an offset-aware datetime");
        return NULL;
    }
    if (!PyObject_TypeCheck(end, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(end)) {
        PyErr_SetString(
            PyExc_TypeError, "series spine end must be an offset-aware datetime");
        return NULL;
    }
    if (unit < 0 || unit > 6) {
        PyErr_SetString(PyExc_ValueError, "series spine unit must be in range 0..6");
        return NULL;
    }
    PyObject *offset_method = PyObject_GetAttr(tz, series_name_utcoffset);
    if (offset_method == NULL) return NULL;
    PyObject *base_start = series_base_datetime(start, api);
    PyObject *base_end = series_base_datetime(end, api);
    PyObject *local_start = base_start == NULL ? NULL
        : series_astimezone(base_start, tz);
    PyObject *local_end = base_end == NULL ? NULL
        : series_astimezone(base_end, tz);
    Py_XDECREF(base_start);
    Py_XDECREF(base_end);
    if (local_start == NULL || local_end == NULL) {
        Py_XDECREF(local_start);
        Py_XDECREF(local_end);
        Py_DECREF(offset_method);
        return NULL;
    }
    SeriesWallClock wall = {
        PyDateTime_GET_YEAR(local_start), PyDateTime_GET_MONTH(local_start),
        PyDateTime_GET_DAY(local_start), PyDateTime_DATE_GET_HOUR(local_start),
        PyDateTime_DATE_GET_MINUTE(local_start), PyDateTime_DATE_GET_SECOND(local_start),
        PyDateTime_DATE_GET_MICROSECOND(local_start),
    };
    SeriesWallClock end_wall = {
        PyDateTime_GET_YEAR(local_end), PyDateTime_GET_MONTH(local_end),
        PyDateTime_GET_DAY(local_end), PyDateTime_DATE_GET_HOUR(local_end),
        PyDateTime_DATE_GET_MINUTE(local_end), PyDateTime_DATE_GET_SECOND(local_end),
        PyDateTime_DATE_GET_MICROSECOND(local_end),
    };
    Py_DECREF(local_start);
    Py_DECREF(local_end);
    series_wall_truncate(&wall, unit);
    int64_t count_value = 0;
    if (series_wall_compare(&wall, &end_wall) < 0) {
        int64_t day_delta =
            series_wall_ordinal(&end_wall) - series_wall_ordinal(&wall);
        int partial_day = end_wall.hour != 0 || end_wall.minute != 0 ||
                          end_wall.second != 0 || end_wall.microsecond != 0;
        if (unit == 0) {
            count_value = day_delta * INT64_C(1440) +
                          (end_wall.hour - wall.hour) * 60 +
                          end_wall.minute - wall.minute;
            if (end_wall.second != 0 || end_wall.microsecond != 0) count_value++;
        }
        else if (unit == 1) {
            count_value = day_delta * INT64_C(24) +
                          end_wall.hour - wall.hour;
            if (end_wall.minute != 0 || end_wall.second != 0 ||
                end_wall.microsecond != 0) count_value++;
        }
        else if (unit == 2) count_value = day_delta + partial_day;
        else if (unit == 3) {
            count_value = day_delta / 7;
            if (day_delta % 7 != 0 || partial_day) count_value++;
        }
        else {
            int64_t months = (int64_t)(end_wall.year - wall.year) * 12 +
                             end_wall.month - wall.month;
            int64_t stride = unit == 4 ? 1 : unit == 5 ? 3 : 12;
            count_value = months / stride;
            if (months % stride != 0 || end_wall.day != 1 || partial_day)
                count_value++;
        }
    }
    if (count_value < 0 || count_value > PY_SSIZE_T_MAX) {
        Py_DECREF(offset_method);
        return PyErr_NoMemory();
    }
    Py_ssize_t count = (Py_ssize_t)count_value;
    SeriesWallClock last = end_wall;
    series_wall_truncate(&last, unit);
    if (count != 0 && series_wall_compare(&last, &end_wall) == 0 &&
        series_wall_retreat(&last, unit) < 0) {
        Py_DECREF(offset_method);
        return NULL;
    }
    /* Local wall order and instant order differ only inside a repeated or
     * skipped clock interval.  Only the final candidate can straddle `end`, so
     * resolve that one through the zone instead of allocating two aware
     * datetimes for every element merely to answer len(). */
    if (count != 0 && !series_wall_definitely_before(&last, &end_wall)) {
        PyObject *current = series_wall_in_zone(
            &last, tz, offset_method, api->DateTimeType, api);
        if (current == NULL) {
            Py_DECREF(offset_method);
            return NULL;
        }
        int before_end = PyObject_RichCompareBool(current, end, Py_LT);
        Py_DECREF(current);
        if (before_end < 0) {
            Py_DECREF(offset_method);
            return NULL;
        }
        if (!before_end) count--;
    }
    Py_DECREF(offset_method);
    return PyLong_FromSsize_t(count);
}

PyObject *
wreath_series_spine_lengths(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *start, *end, *unit_source, *tz, *capsule = NULL;
    if (!PyArg_ParseTuple(
            args, "OOOO|O:series_spine_lengths",
            &start, &end, &unit_source, &tz, &capsule))
        return NULL;
    PyDateTime_CAPI *api = series_datetime_api(capsule);
    if (api == NULL) return NULL;
    if (!PyObject_TypeCheck(start, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(start)) {
        PyErr_SetString(
            PyExc_TypeError,
            "series spine lengths start must be an offset-aware datetime");
        return NULL;
    }
    if (!PyObject_TypeCheck(end, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(end)) {
        PyErr_SetString(
            PyExc_TypeError,
            "series spine lengths end must be an offset-aware datetime");
        return NULL;
    }
    PyObject *units = PySequence_Fast(
        unit_source, "series spine units must be an iterable of unit indices");
    PyObject *offset_method = units == NULL ? NULL
        : PyObject_GetAttr(tz, series_name_utcoffset);
    PyObject *base_start = offset_method == NULL ? NULL
        : series_base_datetime(start, api);
    PyObject *base_end = base_start == NULL ? NULL
        : series_base_datetime(end, api);
    PyObject *local_start = base_start == NULL ? NULL
        : series_astimezone(base_start, tz);
    PyObject *local_end = base_end == NULL ? NULL
        : series_astimezone(base_end, tz);
    Py_XDECREF(base_start);
    Py_XDECREF(base_end);
    if (units == NULL || offset_method == NULL || local_start == NULL ||
        local_end == NULL) {
        Py_XDECREF(units);
        Py_XDECREF(offset_method);
        Py_XDECREF(local_start);
        Py_XDECREF(local_end);
        return NULL;
    }
    SeriesWallClock original_start = {
        PyDateTime_GET_YEAR(local_start), PyDateTime_GET_MONTH(local_start),
        PyDateTime_GET_DAY(local_start), PyDateTime_DATE_GET_HOUR(local_start),
        PyDateTime_DATE_GET_MINUTE(local_start),
        PyDateTime_DATE_GET_SECOND(local_start),
        PyDateTime_DATE_GET_MICROSECOND(local_start),
    };
    SeriesWallClock end_wall = {
        PyDateTime_GET_YEAR(local_end), PyDateTime_GET_MONTH(local_end),
        PyDateTime_GET_DAY(local_end), PyDateTime_DATE_GET_HOUR(local_end),
        PyDateTime_DATE_GET_MINUTE(local_end),
        PyDateTime_DATE_GET_SECOND(local_end),
        PyDateTime_DATE_GET_MICROSECOND(local_end),
    };
    Py_DECREF(local_start);
    Py_DECREF(local_end);
    Py_ssize_t unit_count = PySequence_Fast_GET_SIZE(units);
    PyObject *result = PyTuple_New(unit_count);
    if (result == NULL) goto error;
    PyObject **unit_items = PySequence_Fast_ITEMS(units);
    for (Py_ssize_t item = 0; item < unit_count; item++) {
        long unit = PyLong_AsLong(unit_items[item]);
        if ((unit == -1 && PyErr_Occurred()) || unit < 0 || unit > 6) {
            if (!PyErr_Occurred()) PyErr_Format(
                PyExc_ValueError,
                "series spine unit %ld must be in range 0..6", unit);
            Py_DECREF(result);
            goto error;
        }
        SeriesWallClock wall = original_start;
        series_wall_truncate(&wall, (int)unit);
        int64_t count_value = 0;
        if (series_wall_compare(&wall, &end_wall) < 0) {
            int64_t day_delta =
                series_wall_ordinal(&end_wall) - series_wall_ordinal(&wall);
            int partial_day = end_wall.hour != 0 || end_wall.minute != 0 ||
                              end_wall.second != 0 || end_wall.microsecond != 0;
            if (unit == 0) {
                count_value = day_delta * INT64_C(1440) +
                              (end_wall.hour - wall.hour) * 60 +
                              end_wall.minute - wall.minute;
                if (end_wall.second != 0 || end_wall.microsecond != 0)
                    count_value++;
            }
            else if (unit == 1) {
                count_value = day_delta * INT64_C(24) +
                              end_wall.hour - wall.hour;
                if (end_wall.minute != 0 || end_wall.second != 0 ||
                    end_wall.microsecond != 0) count_value++;
            }
            else if (unit == 2) count_value = day_delta + partial_day;
            else if (unit == 3) {
                count_value = day_delta / 7;
                if (day_delta % 7 != 0 || partial_day) count_value++;
            }
            else {
                int64_t months = (int64_t)(end_wall.year - wall.year) * 12 +
                                 end_wall.month - wall.month;
                int64_t stride = unit == 4 ? 1 : unit == 5 ? 3 : 12;
                count_value = months / stride;
                if (months % stride != 0 || end_wall.day != 1 || partial_day)
                    count_value++;
            }
        }
        if (count_value < 0 || count_value > PY_SSIZE_T_MAX) {
            Py_DECREF(result);
            PyErr_NoMemory();
            goto error;
        }
        Py_ssize_t count = (Py_ssize_t)count_value;
        SeriesWallClock last = end_wall;
        series_wall_truncate(&last, (int)unit);
        if (count != 0 && series_wall_compare(&last, &end_wall) == 0 &&
            series_wall_retreat(&last, (int)unit) < 0) {
            Py_DECREF(result);
            goto error;
        }
        if (count != 0 && !series_wall_definitely_before(&last, &end_wall)) {
            PyObject *current = series_wall_in_zone(
                &last, tz, offset_method, Py_TYPE(start), api);
            if (current == NULL) {
                Py_DECREF(result);
                goto error;
            }
            int before_end = PyObject_RichCompareBool(current, end, Py_LT);
            Py_DECREF(current);
            if (before_end < 0) {
                Py_DECREF(result);
                goto error;
            }
            if (!before_end) count--;
        }
        PyObject *count_object = PyLong_FromSsize_t(count);
        if (count_object == NULL) {
            Py_DECREF(result);
            goto error;
        }
        PyTuple_SET_ITEM(result, item, count_object);
    }
    Py_DECREF(units);
    Py_DECREF(offset_method);
    return result;

error:
    Py_DECREF(units);
    Py_DECREF(offset_method);
    return NULL;
}

static int
series_numeric_array(PyObject *source, const char *name,
                     PyObject **sequence_out, double **values_out)
{
    PyObject *sequence = PySequence_Fast(source, name);
    if (sequence == NULL) return -1;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    if ((size_t)count > SIZE_MAX / sizeof(double)) {
        Py_DECREF(sequence);
        PyErr_NoMemory();
        return -1;
    }
    double *values = count != 0
        ? PyMem_Malloc((size_t)count * sizeof(*values)) : NULL;
    if (count != 0 && values == NULL) {
        Py_DECREF(sequence);
        PyErr_NoMemory();
        return -1;
    }
    PyObject **items = PySequence_Fast_ITEMS(sequence);
    for (Py_ssize_t index = 0; index < count; index++) {
        values[index] = series_as_double(items[index]);
        if (PyErr_Occurred() || !isfinite(values[index])) {
            if (!PyErr_Occurred()) PyErr_Format(
                PyExc_ValueError, "%s item %zd must be finite", name, index);
            PyMem_Free(values);
            Py_DECREF(sequence);
            return -1;
        }
    }
    *sequence_out = sequence;
    *values_out = values;
    return 0;
}

PyObject *
wreath_series_lttb(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *x_source, *y_source, *x_sequence = NULL, *y_sequence = NULL;
    Py_ssize_t threshold;
    double *x = NULL, *y = NULL;
    if (!PyArg_ParseTuple(args, "OOn:series_lttb", &x_source, &y_source, &threshold))
        return NULL;
    if (series_numeric_array(
            x_source, "LTTB x values must be a finite numeric iterable",
            &x_sequence, &x) < 0 ||
        series_numeric_array(
            y_source, "LTTB y values must be a finite numeric iterable",
            &y_sequence, &y) < 0) goto error;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(x_sequence);
    if (PySequence_Fast_GET_SIZE(y_sequence) != count) {
        PyErr_Format(
            PyExc_ValueError, "LTTB needs one y value per x value; got %zd and %zd",
            count, PySequence_Fast_GET_SIZE(y_sequence));
        goto error;
    }
    if (threshold < 3 && threshold < count) {
        PyErr_Format(
            PyExc_ValueError,
            "LTTB threshold must be at least 3 when reducing %zd points, got %zd",
            count, threshold);
        goto error;
    }
    Py_ssize_t output_count = threshold < count ? threshold : count;
    PyObject *result = PyTuple_New(output_count);
    if (result == NULL) goto error;
    if (output_count == count) {
        for (Py_ssize_t index = 0; index < count; index++) {
            PyObject *value = PyLong_FromSsize_t(index);
            if (value == NULL) {
                Py_DECREF(result);
                goto error;
            }
            PyTuple_SET_ITEM(result, index, value);
        }
        goto done;
    }
    double every = (double)(count - 2) / (double)(threshold - 2);
    Py_ssize_t anchor = 0;
    PyObject *first = PyLong_FromLong(0);
    if (first == NULL) {
        Py_DECREF(result);
        goto error;
    }
    PyTuple_SET_ITEM(result, 0, first);
    for (Py_ssize_t bucket = 0; bucket < threshold - 2; bucket++) {
        Py_ssize_t average_start = (Py_ssize_t)floor((bucket + 1) * every) + 1;
        Py_ssize_t average_end = (Py_ssize_t)floor((bucket + 2) * every) + 1;
        if (average_end > count) average_end = count;
        double average_x = 0.0, average_y = 0.0;
        for (Py_ssize_t index = average_start; index < average_end; index++) {
            average_x += x[index];
            average_y += y[index];
        }
        Py_ssize_t average_count = average_end - average_start;
        if (average_count == 0) {
            average_x = x[count - 1];
            average_y = y[count - 1];
        }
        else {
            average_x /= (double)average_count;
            average_y /= (double)average_count;
        }
        Py_ssize_t range_start = (Py_ssize_t)floor(bucket * every) + 1;
        Py_ssize_t range_end = (Py_ssize_t)floor((bucket + 1) * every) + 1;
        if (range_end > count - 1) range_end = count - 1;
        Py_ssize_t best = range_start;
        double best_area = -1.0;
        for (Py_ssize_t index = range_start; index < range_end; index++) {
            double area = fabs(
                (x[anchor] - average_x) * (y[index] - y[anchor]) -
                (x[anchor] - x[index]) * (average_y - y[anchor]));
            if (area > best_area) {
                best_area = area;
                best = index;
            }
        }
        PyObject *selected = PyLong_FromSsize_t(best);
        if (selected == NULL) {
            Py_DECREF(result);
            goto error;
        }
        PyTuple_SET_ITEM(result, bucket + 1, selected);
        anchor = best;
        if ((bucket & 255) == 255 && PyErr_CheckSignals() < 0) {
            Py_DECREF(result);
            goto error;
        }
    }
    PyObject *last = PyLong_FromSsize_t(count - 1);
    if (last == NULL) {
        Py_DECREF(result);
        goto error;
    }
    PyTuple_SET_ITEM(result, threshold - 1, last);

done:
    PyMem_Free(x);
    PyMem_Free(y);
    Py_DECREF(x_sequence);
    Py_DECREF(y_sequence);
    return result;

error:
    PyMem_Free(x);
    PyMem_Free(y);
    Py_XDECREF(x_sequence);
    Py_XDECREF(y_sequence);
    return NULL;
}

static int
series_format_uint9(char *out, uint32_t value)
{
    int digits;
    if (value < 10000U) {
        if (value < 100U) digits = value < 10U ? 1 : 2;
        else digits = value < 1000U ? 3 : 4;
    }
    else if (value < 1000000U)
        digits = value < 100000U ? 5 : 6;
    else if (value < 100000000U)
        digits = value < 10000000U ? 7 : 8;
    else digits = 9;
    int position = digits;
    while (position >= 2) {
        unsigned int pair = value % 100U;
        value /= 100U;
        position -= 2;
        out[position] = series_digit_pairs[pair * 2];
        out[position + 1] = series_digit_pairs[pair * 2 + 1];
    }
    if (position != 0) out[0] = (char)('0' + value);
    return digits;
}

static Py_ssize_t
series_format_double(char out[32], double value)
{
    if (!isfinite(value)) {
        PyErr_SetString(PyExc_ValueError, "series path coordinates must be finite");
        return -1;
    }
    char *cursor = out;
    if (value == 0.0) {
        *cursor = '0';
        return 1;
    }
    if (value < 0.0) {
        *cursor++ = '-';
        value = -value;
    }
    if (value < 1000000000.0) {
        uint32_t integer = (uint32_t)value;
        if ((double)integer == value) {
            cursor += series_format_uint9(cursor, integer);
            return cursor - out;
        }
    }
    /* Nine significant digits are comfortably below a screen pixel for a
     * chart coordinate while keeping the SVG stable and compact.  Formatting
     * them as integer digits avoids allocating one temporary string per point
     * and is locale-independent, unlike snprintf(). */
    int exponent;
    double unit;
    if (value >= 0.0001 && value < 1000000000.0) {
        /* Chart coordinates overwhelmingly occupy this ordinary decimal
         * range.  A comparison tree and an exact power-of-ten table avoid a
         * log10() and pow() for every emitted point while preserving the same
         * nine-significant-digit representation. */
        if (value >= 1.0) {
            if (value < 10000.0) {
                if (value < 100.0)
                    exponent = value < 10.0 ? 0 : 1;
                else exponent = value < 1000.0 ? 2 : 3;
            }
            else if (value < 1000000.0)
                exponent = value < 100000.0 ? 4 : 5;
            else if (value < 100000000.0)
                exponent = value < 10000000.0 ? 6 : 7;
            else exponent = 8;
        }
        else if (value >= 0.01)
            exponent = value >= 0.1 ? -1 : -2;
        else exponent = value >= 0.001 ? -3 : -4;
        static const double units[] = {
            1e-12, 1e-11, 1e-10, 1e-9, 1e-8, 1e-7, 1e-6,
            1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0,
        };
        unit = units[exponent + 4];
    }
    else {
        exponent = (int)floor(log10(value));
        unit = pow(10.0, (double)(exponent - 8));
    }
    if (unit == 0.0 || !isfinite(unit)) {
        char *text = PyOS_double_to_string(value, 'g', 9, 0, NULL);
        if (text == NULL) return -1;
        size_t length = strlen(text);
        if (length > (size_t)(out + 32 - cursor)) {
            PyMem_Free(text);
            return PyErr_NoMemory(), -1;
        }
        memcpy(cursor, text, length);
        PyMem_Free(text);
        return (Py_ssize_t)(cursor - out) + (Py_ssize_t)length;
    }
    uint64_t rounded = (uint64_t)floor(value / unit + 0.5);
    if (rounded >= UINT64_C(1000000000)) {
        rounded /= 10;
        exponent++;
    }
    char digits[9];
    for (int index = 7; index >= 1; index -= 2) {
        unsigned int pair = (unsigned int)(rounded % 100);
        rounded /= 100;
        digits[index] = series_digit_pairs[pair * 2];
        digits[index + 1] = series_digit_pairs[pair * 2 + 1];
    }
    digits[0] = (char)('0' + rounded);
    int used = 9;
    while (used > 1 && digits[used - 1] == '0') used--;
    if (exponent < -4 || exponent >= 9) {
        *cursor++ = digits[0];
        if (used > 1) {
            *cursor++ = '.';
            memcpy(cursor, digits + 1, (size_t)(used - 1));
            cursor += used - 1;
        }
        *cursor++ = 'e';
        *cursor++ = exponent < 0 ? '-' : '+';
        unsigned int magnitude = (unsigned int)(exponent < 0 ? -exponent : exponent);
        char reversed[4];
        Py_ssize_t count = 0;
        do {
            reversed[count++] = (char)('0' + magnitude % 10U);
            magnitude /= 10U;
        } while (magnitude != 0);
        for (Py_ssize_t index = 0; index < count; index++)
            *cursor++ = reversed[count - index - 1];
        return cursor - out;
    }
    int decimal = exponent + 1;
    if (decimal <= 0) {
        *cursor++ = '0';
        *cursor++ = '.';
        for (int index = 0; index < -decimal; index++)
            *cursor++ = '0';
        memcpy(cursor, digits, (size_t)used);
        cursor += used;
        return cursor - out;
    }
    if (decimal >= used) {
        memcpy(cursor, digits, (size_t)used);
        cursor += used;
        for (int index = used; index < decimal; index++)
            *cursor++ = '0';
        return cursor - out;
    }
    memcpy(cursor, digits, (size_t)decimal);
    cursor += decimal;
    *cursor++ = '.';
    memcpy(cursor, digits + decimal, (size_t)(used - decimal));
    cursor += used - decimal;
    return cursor - out;
}

static Py_ssize_t
series_format_index(char out[32], Py_ssize_t value)
{
    char reversed[32];
    Py_ssize_t count = 0;
    do {
        reversed[count++] = (char)('0' + value % 10);
        value /= 10;
    } while (value != 0);
    for (Py_ssize_t index = 0; index < count; index++)
        out[index] = reversed[count - index - 1];
    return count;
}

PyObject *
wreath_format_duration_parts(PyObject *Py_UNUSED(self), PyObject *args)
{
    int days;
    int seconds;
    int microseconds;
    if (!PyArg_ParseTuple(
            args, "iii:format_duration_parts", &days, &seconds, &microseconds))
        return NULL;
    if (days < -999999999 || days > 999999999 || seconds < 0 ||
        seconds >= 86400 || microseconds < 0 || microseconds >= 1000000) {
        PyErr_SetString(PyExc_ValueError, "invalid normalized timedelta components");
        return NULL;
    }
    int negative = days < 0;
    Py_ssize_t magnitude_days;
    int magnitude_seconds = seconds;
    int magnitude_microseconds = microseconds;
    if (negative) {
        magnitude_days = -(Py_ssize_t)days;
        if (seconds != 0 || microseconds != 0) {
            magnitude_days--;
            int64_t remaining = INT64_C(86400000000) -
                ((int64_t)seconds * INT64_C(1000000) + microseconds);
            magnitude_seconds = (int)(remaining / INT64_C(1000000));
            magnitude_microseconds = (int)(remaining % INT64_C(1000000));
        }
    }
    else {
        magnitude_days = days;
    }
    int hours = magnitude_seconds / 3600;
    int rest = magnitude_seconds % 3600;
    int minutes = rest / 60;
    int secs = rest % 60;
    char text[64];
    Py_ssize_t length = 0;
    if (negative) text[length++] = '-';
    text[length++] = 'P';
    if (magnitude_days != 0) {
        length += series_format_index(text + length, magnitude_days);
        text[length++] = 'D';
    }
    if (hours != 0 || minutes != 0 || secs != 0 ||
        magnitude_microseconds != 0 || magnitude_days == 0) {
        text[length++] = 'T';
        if (hours != 0) {
            length += series_format_index(text + length, hours);
            text[length++] = 'H';
        }
        if (minutes != 0) {
            length += series_format_index(text + length, minutes);
            text[length++] = 'M';
        }
        if (secs != 0 || magnitude_microseconds != 0 ||
            (hours == 0 && minutes == 0)) {
            length += series_format_index(text + length, secs);
            if (magnitude_microseconds != 0) {
                text[length++] = '.';
                int divisor = 100000;
                for (int digit = 0; digit < 6; digit++) {
                    text[length++] = (char)('0' + magnitude_microseconds / divisor % 10);
                    divisor /= 10;
                }
                while (text[length - 1] == '0') length--;
            }
            text[length++] = 'S';
        }
    }
    return PyUnicode_DecodeASCII(text, length, NULL);
}

static inline void
series_iso_two(char **cursor, int value)
{
    *(*cursor)++ = (char)('0' + value / 10);
    *(*cursor)++ = (char)('0' + value % 10);
}

static inline void
series_iso_four(char **cursor, int value)
{
    series_iso_two(cursor, value / 100);
    series_iso_two(cursor, value % 100);
}

static inline void
series_iso_six(char **cursor, int value)
{
    series_iso_two(cursor, value / 10000);
    series_iso_two(cursor, value / 100 % 100);
    series_iso_two(cursor, value % 100);
}

static int
series_iso_offset(PyObject *value, PyObject *tz, PyDateTime_CAPI *api,
                  int64_t *offset, int *present)
{
    PyObject *delta;
    int valid;
    if (tz == Py_None) {
        *present = 0;
        return 0;
    }
    if (tz == api->TimeZone_UTC) {
        *offset = 0;
        *present = 1;
        return 0;
    }
    delta = PyObject_CallMethodNoArgs(value, series_name_utcoffset);
    if (delta == NULL) return -1;
    if (delta == Py_None) {
        Py_DECREF(delta);
        *present = 0;
        return 0;
    }
    *offset = series_offset_microseconds(delta, api, &valid);
    Py_DECREF(delta);
    if (!valid) return -1;
    if (*offset <= -INT64_C(86400000000)
        || *offset >= INT64_C(86400000000)) {
        PyErr_SetString(PyExc_ValueError,
                        "time zone offset must be strictly between -24h and +24h");
        return -1;
    }
    *present = 1;
    return 0;
}

PyObject *
wreath_format_iso_datetime(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *value, *capsule;
    PyDateTime_CAPI *api;
    PyObject *tz;
    int offset_present = 0;
    int64_t offset = 0;
    char text[64];
    char *cursor = text;

    if (!PyArg_ParseTuple(args, "OO:format_iso_datetime", &value, &capsule))
        return NULL;
    api = series_datetime_api(capsule);
    if (api == NULL) return NULL;
    if (!PyObject_TypeCheck(value, api->DateTimeType)) {
        PyErr_Format(PyExc_TypeError,
                     "native ISO formatter expected datetime, got %.200s",
                     Py_TYPE(value)->tp_name);
        return NULL;
    }
    tz = PyDateTime_DATE_GET_TZINFO(value);
    if (series_iso_offset(value, tz, api, &offset, &offset_present) < 0)
        return NULL;

    series_iso_four(&cursor, PyDateTime_GET_YEAR(value));
    *cursor++ = '-';
    series_iso_two(&cursor, PyDateTime_GET_MONTH(value));
    *cursor++ = '-';
    series_iso_two(&cursor, PyDateTime_GET_DAY(value));
    *cursor++ = 'T';
    series_iso_two(&cursor, PyDateTime_DATE_GET_HOUR(value));
    *cursor++ = ':';
    series_iso_two(&cursor, PyDateTime_DATE_GET_MINUTE(value));
    *cursor++ = ':';
    series_iso_two(&cursor, PyDateTime_DATE_GET_SECOND(value));
    int microsecond = PyDateTime_DATE_GET_MICROSECOND(value);
    if (microsecond != 0) {
        *cursor++ = '.';
        series_iso_six(&cursor, microsecond);
    }
    if (offset_present) {
        uint64_t magnitude;
        if (offset < 0) {
            *cursor++ = '-';
            magnitude = (uint64_t)-offset;
        }
        else {
            *cursor++ = '+';
            magnitude = (uint64_t)offset;
        }
        int hours = (int)(magnitude / UINT64_C(3600000000));
        magnitude %= UINT64_C(3600000000);
        int minutes = (int)(magnitude / UINT64_C(60000000));
        magnitude %= UINT64_C(60000000);
        int seconds = (int)(magnitude / UINT64_C(1000000));
        int micros = (int)(magnitude % UINT64_C(1000000));
        series_iso_two(&cursor, hours);
        *cursor++ = ':';
        series_iso_two(&cursor, minutes);
        if (seconds != 0 || micros != 0) {
            *cursor++ = ':';
            series_iso_two(&cursor, seconds);
            if (micros != 0) {
                *cursor++ = '.';
                series_iso_six(&cursor, micros);
            }
        }
    }
    return PyUnicode_FromStringAndSize(text, cursor - text);
}

static PyObject *
series_relative_english(double seconds, int future)
{
    if (seconds < 0.0) seconds = -seconds;
    if (seconds < 45.0) return PyUnicode_FromString("just now");
    if (seconds >= 79200.0 && seconds < 129600.0)
        return PyUnicode_FromString(future ? "tomorrow" : "yesterday");

    long long amount;
    const char *unit;
    if (seconds < 90.0) {
        amount = 1;
        unit = "minute";
    }
    else {
        double scaled = seconds / 60.0;
        amount = (long long)round(scaled);
        unit = "minute";
        if (amount >= 45) {
            scaled = seconds / 3600.0;
            amount = (long long)round(scaled);
            unit = "hour";
            if (amount >= 22) {
                scaled = seconds / 86400.0;
                amount = (long long)round(scaled);
                unit = "day";
                if (amount >= 26) {
                    scaled = seconds / 2629800.0;
                    amount = (long long)round(scaled);
                    unit = "month";
                    if (amount >= 11) {
                        amount = (long long)round(seconds / 31557600.0);
                        unit = "year";
                    }
                }
            }
        }
    }
    char text[96];
    int length = snprintf(
        text, sizeof(text), future ? "in %lld %s%s" : "%lld %s%s ago",
        amount, unit, amount == 1 ? "" : "s");
    if (length < 0 || (size_t)length >= sizeof(text)) {
        PyErr_SetString(PyExc_RuntimeError, "relative-time formatting overflowed");
        return NULL;
    }
    return PyUnicode_DecodeASCII(text, length, NULL);
}

PyObject *
wreath_relative_english(PyObject *Py_UNUSED(self), PyObject *args)
{
    double seconds;
    int future;
    if (!PyArg_ParseTuple(args, "dp:relative_english", &seconds, &future))
        return NULL;
    return series_relative_english(seconds, future);
}

PyObject *
wreath_relative_english_between(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *reference, *moment, *capsule;
    PyDateTime_CAPI *api;
    if (!PyArg_ParseTuple(
            args, "OOO:relative_english_between", &reference, &moment, &capsule))
        return NULL;
    api = series_datetime_api(capsule);
    if (api == NULL) return NULL;
    if (!PyObject_TypeCheck(reference, api->DateTimeType) ||
        !PyObject_TypeCheck(moment, api->DateTimeType)) {
        PyErr_SetString(
            PyExc_TypeError,
            "native relative formatter expected two datetime values");
        return NULL;
    }
    PyObject *delta = PyNumber_Subtract(reference, moment);
    if (delta == NULL) return NULL;
    if (!PyObject_TypeCheck(delta, api->DeltaType)) {
        PyErr_Format(PyExc_TypeError,
                     "datetime subtraction returned %.200s, not timedelta",
                     Py_TYPE(delta)->tp_name);
        Py_DECREF(delta);
        return NULL;
    }
    double seconds = (double)PyDateTime_DELTA_GET_DAYS(delta) * 86400.0 +
                     (double)PyDateTime_DELTA_GET_SECONDS(delta) +
                     (double)PyDateTime_DELTA_GET_MICROSECONDS(delta) / 1000000.0;
    Py_DECREF(delta);
    int future = seconds < 0.0;
    return series_relative_english(seconds, future);
}

PyObject *
wreath_series_path(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *x_source, *y_source;
    if (!PyArg_ParseTuple(args, "OO:series_path", &x_source, &y_source)) return NULL;
    PyObject *x = PySequence_Fast(
        x_source, "series path x values must be a numeric iterable");
    PyObject *y = PySequence_Fast(
        y_source, "series path y values must be a numeric-or-None iterable");
    if (x == NULL || y == NULL) {
        Py_XDECREF(x);
        Py_XDECREF(y);
        return NULL;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(x);
    if (PySequence_Fast_GET_SIZE(y) != count) {
        PyErr_Format(
            PyExc_ValueError,
            "series path needs one y value per x value; got %zd and %zd",
            count, PySequence_Fast_GET_SIZE(y));
        Py_DECREF(x);
        Py_DECREF(y);
        return NULL;
    }
    if (count > PY_SSIZE_T_MAX / 34) {
        Py_DECREF(x);
        Py_DECREF(y);
        return PyErr_NoMemory();
    }
    PyObject *result = PyUnicode_New(count * 34, 127);
    if (result == NULL) {
        Py_DECREF(x);
        Py_DECREF(y);
        return NULL;
    }
    char *buffer = (char *)PyUnicode_1BYTE_DATA(result);
    Py_ssize_t length = 0;
    int open = 0;
    PyObject **x_items = PySequence_Fast_ITEMS(x);
    PyObject **y_items = PySequence_Fast_ITEMS(y);
    for (Py_ssize_t index = 0; index < count; index++) {
        if (y_items[index] == Py_None) {
            open = 0;
            continue;
        }
        double x_value = series_as_double(x_items[index]);
        double y_value = series_as_double(y_items[index]);
        if (PyErr_Occurred()) goto path_error;
        char *point = buffer + length;
        point[0] = open ? 'L' : 'M';
        Py_ssize_t x_length = series_format_double(point + 1, x_value);
        if (x_length < 0) goto path_error;
        point[x_length + 1] = ',';
        Py_ssize_t y_length = series_format_double(
            point + x_length + 2, y_value);
        if (y_length < 0) goto path_error;
        length += 2 + x_length + y_length;
        open = 1;
        if ((index & 1023) == 1023 && PyErr_CheckSignals() < 0) goto path_error;
    }
    Py_DECREF(x);
    Py_DECREF(y);
    if (PyUnicode_Resize(&result, length) < 0) return NULL;
    return result;

path_error:
    Py_DECREF(x);
    Py_DECREF(y);
    Py_DECREF(result);
    return NULL;
}

PyObject *
wreath_series_nice_ticks(PyObject *Py_UNUSED(self), PyObject *args)
{
    double minimum, maximum;
    Py_ssize_t target;
    if (!PyArg_ParseTuple(
            args, "ddn:series_nice_ticks", &minimum, &maximum, &target)) return NULL;
    if (!isfinite(minimum) || !isfinite(maximum)) {
        PyErr_SetString(PyExc_ValueError, "tick bounds must be finite");
        return NULL;
    }
    if (maximum < minimum) {
        PyErr_SetString(PyExc_ValueError, "tick maximum is below minimum");
        return NULL;
    }
    if (target < 2) {
        PyErr_Format(PyExc_ValueError, "tick target must be at least 2, got %zd", target);
        return NULL;
    }
    if (minimum == maximum) return Py_BuildValue("(d)", minimum);
    double raw = (maximum - minimum) / (double)(target - 1);
    double exponent = floor(log10(raw));
    double magnitude = pow(10.0, exponent);
    double fraction = raw / magnitude;
    double nice = fraction <= 1.0 ? 1.0 : fraction <= 2.0 ? 2.0 :
                  fraction <= 5.0 ? 5.0 : 10.0;
    double step = nice * magnitude;
    double first = floor(minimum / step) * step;
    double last = ceil(maximum / step) * step;
    double count_value = floor((last - first) / step + 0.5) + 1.0;
    if (count_value > 10000.0) {
        PyErr_SetString(PyExc_ValueError, "tick selection exceeds 10000 values");
        return NULL;
    }
    Py_ssize_t count = (Py_ssize_t)count_value;
    PyObject *result = PyTuple_New(count);
    if (result == NULL) return NULL;
    for (Py_ssize_t index = 0; index < count; index++) {
        double value = first + (double)index * step;
        PyObject *tick = PyFloat_FromDouble(value == 0.0 ? 0.0 : value);
        if (tick == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, index, tick);
    }
    return result;
}

static PyObject *
series_chart_ticks(double minimum, double maximum, Py_ssize_t target)
{
    if (minimum == maximum) return Py_BuildValue("(d)", minimum);
    double raw = (maximum - minimum) / (double)(target - 1);
    double exponent = floor(log10(raw));
    double magnitude = pow(10.0, exponent);
    double fraction = raw / magnitude;
    double nice = fraction <= 1.0 ? 1.0 : fraction <= 2.0 ? 2.0 :
                  fraction <= 5.0 ? 5.0 : 10.0;
    double step = nice * magnitude;
    double first = floor(minimum / step) * step;
    double last = ceil(maximum / step) * step;
    Py_ssize_t count = (Py_ssize_t)(floor((last - first) / step + 0.5) + 1.0);
    PyObject *result = PyTuple_New(count);
    if (result == NULL) return NULL;
    for (Py_ssize_t index = 0; index < count; index++) {
        double value = first + (double)index * step;
        PyObject *tick = PyFloat_FromDouble(value == 0.0 ? 0.0 : value);
        if (tick == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, index, tick);
    }
    return result;
}

static int
series_chart_write_ticks(WreathBytesWriter *writer, double minimum,
                         double maximum, Py_ssize_t target, int separate,
                         Py_ssize_t *total)
{
    if (separate && wreath_writer_byte(writer, ';') < 0) return -1;
    double first = minimum;
    double step = 0.0;
    Py_ssize_t count = 1;
    if (minimum != maximum) {
        double raw = (maximum - minimum) / (double)(target - 1);
        double exponent = floor(log10(raw));
        double magnitude = pow(10.0, exponent);
        double fraction = raw / magnitude;
        double nice = fraction <= 1.0 ? 1.0 : fraction <= 2.0 ? 2.0 :
                      fraction <= 5.0 ? 5.0 : 10.0;
        step = nice * magnitude;
        first = floor(minimum / step) * step;
        double last = ceil(maximum / step) * step;
        count = (Py_ssize_t)(floor((last - first) / step + 0.5) + 1.0);
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        if (index != 0 && wreath_writer_byte(writer, ',') < 0) return -1;
        double value = first + (double)index * step;
        char *text = PyOS_double_to_string(
            value == 0.0 ? 0.0 : value, 'g', 6, 0, NULL);
        if (text == NULL) return -1;
        Py_ssize_t length = (Py_ssize_t)strlen(text);
        int written = wreath_writer_write(writer, text, length);
        PyMem_Free(text);
        if (written < 0) return -1;
    }
    *total += count;
    return 0;
}

static PyObject *
series_chart_path(const double *values, const unsigned char *present,
                  Py_ssize_t count, const Py_ssize_t *indices,
                  Py_ssize_t index_count, const unsigned char *index_plan,
                  const char *value_text, const size_t *value_offsets,
                  const unsigned char *value_lengths)
{
    Py_ssize_t points = indices != NULL ? index_count : count;
    if (points > PY_SSIZE_T_MAX / 37) return PyErr_NoMemory();
    PyObject *result = PyUnicode_New(points * 37, 127);
    if (result == NULL) return NULL;
    char *buffer = (char *)PyUnicode_1BYTE_DATA(result);
    Py_ssize_t length = 0;
    int open = 0;
    for (Py_ssize_t position = 0; position < points; position++) {
        Py_ssize_t index = indices != NULL ? indices[position] : position;
        if (!present[index]) {
            open = 0;
            continue;
        }
        char *point = buffer + length;
        point[0] = open ? 'L' : 'M';
        Py_ssize_t index_length;
        if (index_plan != NULL) {
            const unsigned char *planned = index_plan + (size_t)index * 21;
            index_length = planned[0];
            memcpy(point + 1, planned + 1, (size_t)index_length);
        }
        else index_length = series_format_index(point + 1, index);
        point[index_length + 1] = ',';
        Py_ssize_t value_length;
        if (value_lengths != NULL) {
            value_length = value_lengths[index];
            memcpy(point + index_length + 2,
                   value_text + value_offsets[index], (size_t)value_length);
        }
        else value_length = series_format_double(
            point + index_length + 2, values[index]);
        if (value_length < 0) {
            Py_DECREF(result);
            return NULL;
        }
        length += 2 + index_length + value_length;
        open = 1;
    }
    if (PyUnicode_Resize(&result, length) < 0) return NULL;
    return result;
}

static void
series_chart_minmax_scalar(const double *values, Py_ssize_t count,
                           double *minimum_out, double *maximum_out)
{
    double minimum = count != 0 ? values[0] : 0.0;
    double maximum = minimum;
    for (Py_ssize_t index = 1; index < count; index++) {
        if (values[index] < minimum) minimum = values[index];
        if (values[index] > maximum) maximum = values[index];
    }
    *minimum_out = minimum;
    *maximum_out = maximum;
}

#if defined(WREATH_HAVE_AVX2)
WREATH_TARGET_AVX2 static void
series_chart_minmax_avx2(const double *values, Py_ssize_t count,
                         double *minimum_out, double *maximum_out)
{
    if (count < 4) {
        series_chart_minmax_scalar(values, count, minimum_out, maximum_out);
        return;
    }
    __m256d minimums = _mm256_loadu_pd(values);
    __m256d maximums = minimums;
    Py_ssize_t index = 4;
    for (; index <= count - 4; index += 4) {
        __m256d values4 = _mm256_loadu_pd(values + index);
        minimums = _mm256_min_pd(minimums, values4);
        maximums = _mm256_max_pd(maximums, values4);
    }
    double minimum_lanes[4];
    double maximum_lanes[4];
    _mm256_storeu_pd(minimum_lanes, minimums);
    _mm256_storeu_pd(maximum_lanes, maximums);
    double minimum = minimum_lanes[0];
    double maximum = maximum_lanes[0];
    for (int lane = 1; lane < 4; lane++) {
        if (minimum_lanes[lane] < minimum) minimum = minimum_lanes[lane];
        if (maximum_lanes[lane] > maximum) maximum = maximum_lanes[lane];
    }
    for (; index < count; index++) {
        if (values[index] < minimum) minimum = values[index];
        if (values[index] > maximum) maximum = values[index];
    }
    *minimum_out = minimum;
    *maximum_out = maximum;
}
#endif

static void
series_chart_minmax(const double *values, Py_ssize_t count,
                    double *minimum_out, double *maximum_out)
{
#if defined(WREATH_HAVE_AVX2)
    if (count >= 16 && wreath_simd_has_avx2()) {
        series_chart_minmax_avx2(values, count, minimum_out, maximum_out);
        return;
    }
#endif
    series_chart_minmax_scalar(values, count, minimum_out, maximum_out);
}

static Py_ssize_t *
series_chart_lttb(const double *values, const double *prefix,
                  const double *bounds, Py_ssize_t count,
                  Py_ssize_t threshold, Py_ssize_t *workspace,
                  Py_ssize_t *selected_count,
                  double *minimum_out, double *maximum_out)
{
    Py_ssize_t output_count = threshold < count ? threshold : count;
    if ((size_t)output_count > SIZE_MAX / sizeof(Py_ssize_t)) {
        PyErr_NoMemory();
        return NULL;
    }
    Py_ssize_t *selected = workspace != NULL ? workspace : output_count != 0
        ? PyMem_Malloc((size_t)output_count * sizeof(*selected)) : NULL;
    if (output_count != 0 && selected == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    double minimum;
    double maximum;
    if (bounds != NULL) {
        minimum = bounds[0];
        maximum = bounds[1];
    }
    else series_chart_minmax(values, count, &minimum, &maximum);
    if (output_count == count) {
        for (Py_ssize_t index = 0; index < count; index++) {
            selected[index] = index;
        }
        *selected_count = output_count;
        *minimum_out = minimum;
        *maximum_out = maximum;
        return selected;
    }
    double every = (double)(count - 2) / (double)(threshold - 2);
    Py_ssize_t anchor = 0;
    selected[0] = 0;
    for (Py_ssize_t bucket = 0; bucket < threshold - 2; bucket++) {
        Py_ssize_t average_start = (Py_ssize_t)floor((bucket + 1) * every) + 1;
        Py_ssize_t average_end = (Py_ssize_t)floor((bucket + 2) * every) + 1;
        if (average_end > count) average_end = count;
        double average_y = prefix == NULL ? 0.0
                                          : prefix[average_end] - prefix[average_start];
        if (prefix == NULL) {
            for (Py_ssize_t index = average_start; index < average_end; index++)
                average_y += values[index];
        }
        Py_ssize_t average_count = average_end - average_start;
        double average_x;
        if (average_count == 0) {
            average_x = (double)(count - 1);
            average_y = values[count - 1];
        }
        else {
            average_x = ((double)average_start + (double)(average_end - 1)) * 0.5;
            average_y /= (double)average_count;
        }
        Py_ssize_t range_start = (Py_ssize_t)floor(bucket * every) + 1;
        Py_ssize_t range_end = (Py_ssize_t)floor((bucket + 1) * every) + 1;
        if (range_end > count - 1) range_end = count - 1;
        Py_ssize_t best = range_start;
        double best_area = -1.0;
        double anchor_x = (double)anchor - average_x;
        double average_y_delta = average_y - values[anchor];
        double area_constant = -anchor_x * values[anchor] -
                               (double)anchor * average_y_delta;
        /* The x contribution is an arithmetic progression.  Carrying it
         * forward removes one integer-to-double conversion and multiply per
         * candidate from the LTTB inner loop. */
        double linear_area = (double)range_start * average_y_delta + area_constant;
        for (Py_ssize_t index = range_start; index < range_end; index++) {
            double area = fabs(anchor_x * values[index] + linear_area);
            if (area > best_area) {
                best_area = area;
                best = index;
            }
            linear_area += average_y_delta;
        }
        selected[bucket + 1] = best;
        anchor = best;
    }
    selected[threshold - 1] = count - 1;
    *selected_count = output_count;
    *minimum_out = minimum;
    *maximum_out = maximum;
    return selected;
}

typedef struct {
    PyObject **items;
    Py_hash_t *hashes;
    Py_hash_t *measure_hashes;
} SeriesChartDense;

static int
series_chart_fill_row(PyObject *by_bucket, const SeriesMeasure *measure,
                      const SeriesChartDense *dense, Py_ssize_t bucket_count,
                      double *values, unsigned char *present)
{
    for (Py_ssize_t bucket_index = 0; bucket_index < bucket_count; bucket_index++) {
        /* Both dictionaries remain strongly owned by the caller throughout
         * this GIL-only native operation. Borrow their exact numeric cells so
         * 11 x 730 projection does not manufacture two owned references per
         * lookup. A non-exact numeric is retained across its potentially
         * re-entrant conversion below. */
        PyObject *row = _PyDict_GetItem_KnownHash(
            by_bucket, dense->items[bucket_index], dense->hashes[bucket_index]);
        if (row == NULL && PyErr_Occurred()) return -1;
        if (row != NULL && !PyDict_Check(row)) {
            PyErr_Format(
                PyExc_TypeError,
                "series bucket %R values must be a measure dict, got %.200s",
                dense->items[bucket_index], Py_TYPE(row)->tp_name);
            return -1;
        }
        PyObject *value = row == NULL ? NULL
            : PyDict_GetItemWithError(row, measure->name);
        if (value == NULL && PyErr_Occurred()) return -1;
        if (value == NULL || value == Py_None) value = measure->empty;
        if (value == Py_None) {
            present[bucket_index] = 0;
            values[bucket_index] = 0.0;
            continue;
        }
        if (PyFloat_Check(value)) values[bucket_index] = PyFloat_AS_DOUBLE(value);
        else if (PyLong_CheckExact(value)) {
            values[bucket_index] = PyLong_AsDouble(value);
            if (PyErr_Occurred()) return -1;
        }
        else {
            PyObject *owned = Py_NewRef(value);
            values[bucket_index] = PyFloat_AsDouble(owned);
            Py_DECREF(owned);
            if (PyErr_Occurred()) return -1;
        }
        if (!isfinite(values[bucket_index])) {
            PyErr_Format(
                PyExc_ValueError, "series chart value at bucket %zd must be finite",
                bucket_index);
            return -1;
        }
        present[bucket_index] = 1;
    }
    return 0;
}

typedef int (*SeriesChartFillRow)(
    void *context, PyObject *by_bucket, const SeriesMeasure *measure,
    Py_ssize_t bucket_count, double *values, unsigned char *present);

static int
series_chart_fill_dense(void *context, PyObject *by_bucket,
                        const SeriesMeasure *measure, Py_ssize_t bucket_count,
                        double *values, unsigned char *present)
{
    return series_chart_fill_row(
        by_bucket, measure, (const SeriesChartDense *)context,
        bucket_count, values, present);
}

static int
series_chart_fill_group(PyObject *by_bucket, const SeriesMeasure *measures,
                        const Py_ssize_t *rows, Py_ssize_t group_count,
                        Py_ssize_t measure_count, const SeriesChartDense *dense,
                        Py_ssize_t bucket_count, double *values,
                        unsigned char *present)
{
    for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
        PyObject *row = _PyDict_GetItem_KnownHash(
            by_bucket, dense->items[bucket], dense->hashes[bucket]);
        if (row == NULL && PyErr_Occurred()) return -1;
        if (row != NULL && !PyDict_Check(row)) {
            PyErr_Format(
                PyExc_TypeError,
                "series bucket %R values must be a measure dict, got %.200s",
                dense->items[bucket], Py_TYPE(row)->tp_name);
            return -1;
        }
        for (Py_ssize_t slot = 0; slot < group_count; slot++) {
            const SeriesMeasure *measure = &measures[rows[slot] % measure_count];
            PyObject *value = row == NULL ? NULL
                : _PyDict_GetItem_KnownHash(
                    row, measure->name,
                    dense->measure_hashes[rows[slot] % measure_count]);
            if (value == NULL && PyErr_Occurred()) return -1;
            if (value == NULL || value == Py_None) value = measure->empty;
            size_t cell = (size_t)slot * (size_t)bucket_count + (size_t)bucket;
            if (value == Py_None) {
                present[cell] = 0;
                values[cell] = 0.0;
                continue;
            }
            if (PyFloat_Check(value)) values[cell] = PyFloat_AS_DOUBLE(value);
            else if (PyLong_CheckExact(value)) {
                values[cell] = PyLong_AsDouble(value);
                if (PyErr_Occurred()) return -1;
            }
            else {
                PyObject *owned = Py_NewRef(value);
                values[cell] = PyFloat_AsDouble(owned);
                Py_DECREF(owned);
                if (PyErr_Occurred()) return -1;
            }
            if (!isfinite(values[cell])) {
                PyErr_Format(
                    PyExc_ValueError,
                    "series chart value at bucket %zd must be finite", bucket);
                return -1;
            }
            present[cell] = 1;
        }
    }
    return 0;
}

typedef struct {
    SeriesWallClock start;
    int64_t start_ordinal;
    int ordinal_year;
    int ordinal_leap;
    int64_t ordinal_year_start;
    int unit;
    PyObject *tz;
    PyObject *offset_method;
    PyTypeObject *result_type;
    PyDateTime_CAPI *api;
} SeriesChartSpine;

static int64_t
series_wall_ordinal(const SeriesWallClock *wall)
{
    int64_t year = wall->year - 1;
    int64_t days = year * 365 + year / 4 - year / 100 + year / 400;
    static const unsigned short before_month[] = {
        0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334,
    };
    days += before_month[wall->month] + wall->day - 1;
    if (wall->month > 2 && series_days_in_month(wall->year, 2) == 29) days++;
    return days;
}

static int64_t
series_chart_wall_ordinal(SeriesChartSpine *spine, const SeriesWallClock *wall)
{
    if (spine->ordinal_year != wall->year) {
        int64_t year = wall->year - 1;
        spine->ordinal_year = wall->year;
        spine->ordinal_year_start =
            year * 365 + year / 4 - year / 100 + year / 400;
        spine->ordinal_leap =
            wall->year % 4 == 0 &&
            (wall->year % 100 != 0 || wall->year % 400 == 0);
    }
    static const unsigned short before_month[] = {
        0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334,
    };
    return spine->ordinal_year_start + before_month[wall->month] + wall->day - 1 +
           (wall->month > 2 && spine->ordinal_leap);
}

static int
series_chart_spine_index(SeriesChartSpine *spine, PyObject *bucket,
                         Py_ssize_t bucket_count, Py_ssize_t *index_out)
{
    if ((!Py_IS_TYPE(bucket, spine->result_type) &&
         !PyObject_TypeCheck(bucket, spine->api->DateTimeType)) ||
        !_PyDateTime_HAS_TZINFO(bucket)) return 0;
    int same_zone = PyDateTime_DATE_GET_TZINFO(bucket) == spine->tz;
    PyObject *local = same_zone
        ? bucket : series_astimezone(bucket, spine->tz);
    if (local == NULL) return -1;
    SeriesWallClock wall = {
        PyDateTime_GET_YEAR(local), PyDateTime_GET_MONTH(local),
        PyDateTime_GET_DAY(local), PyDateTime_DATE_GET_HOUR(local),
        PyDateTime_DATE_GET_MINUTE(local), PyDateTime_DATE_GET_SECOND(local),
        PyDateTime_DATE_GET_MICROSECOND(local),
    };
    if (!same_zone) {
        PyObject *canonical = series_wall_in_zone(
            &wall, spine->tz, spine->offset_method,
            spine->result_type, spine->api);
        if (canonical == NULL) {
            if (!same_zone) Py_DECREF(local);
            return -1;
        }
        int equal = PyObject_RichCompareBool(canonical, bucket, Py_EQ);
        Py_DECREF(canonical);
        if (equal <= 0) {
            Py_DECREF(local);
            return equal;
        }
        Py_DECREF(local);
    }
    SeriesWallClock truncated = wall;
    series_wall_truncate(&truncated, spine->unit);
    if (series_wall_compare(&truncated, &wall) != 0) return 0;

    int64_t day_delta = series_chart_wall_ordinal(spine, &wall) -
                        spine->start_ordinal;
    int64_t index;
    if (spine->unit == 0) {
        index = day_delta * INT64_C(1440) +
                (wall.hour - spine->start.hour) * 60 +
                wall.minute - spine->start.minute;
    }
    else if (spine->unit == 1) {
        index = day_delta * INT64_C(24) + wall.hour - spine->start.hour;
    }
    else if (spine->unit == 2) index = day_delta;
    else if (spine->unit == 3) {
        if (day_delta % 7 != 0) return 0;
        index = day_delta / 7;
    }
    else {
        int64_t months = (int64_t)(wall.year - spine->start.year) * 12 +
                         wall.month - spine->start.month;
        int64_t stride = spine->unit == 4 ? 1 : spine->unit == 5 ? 3 : 12;
        if (months % stride != 0) return 0;
        index = months / stride;
    }
    if (index < 0 || index >= bucket_count) return 0;
    *index_out = (Py_ssize_t)index;
    return 1;
}

static PyObject *
series_chart_project(Py_ssize_t bucket_count, Py_ssize_t series_count,
                     Py_ssize_t measure_count, SeriesMeasure *measures,
                     PyObject **series_maps, PyObject *keys,
                     PyObject *downsample, PyObject *full,
                     Py_ssize_t threshold, Py_ssize_t tick_target,
                     SeriesChartFillRow fill_row, void *fill_context,
                     int tick_text)
{
    Py_ssize_t downsample_count = PySequence_Fast_GET_SIZE(downsample);
    Py_ssize_t full_count = PySequence_Fast_GET_SIZE(full);
    if (downsample_count > PY_SSIZE_T_MAX - full_count) {
        PyErr_NoMemory();
        return NULL;
    }
    Py_ssize_t output_count = downsample_count + full_count;
    Py_ssize_t row_count = series_count * measure_count;
    PyObject *paths = NULL;
    PyObject *ticks = NULL;
    WreathBytesWriter tick_writer = {0};
    double *values = NULL;
    unsigned char *present = NULL;
    Py_ssize_t *selected_workspace = NULL;
    Py_ssize_t *row_indices = output_count != 0 ? PyMem_Malloc(
        (size_t)output_count * sizeof(*row_indices)) : NULL;
    if (output_count != 0 && row_indices == NULL) return PyErr_NoMemory();
    PyObject **downsample_items = PySequence_Fast_ITEMS(downsample);
    PyObject **full_items = PySequence_Fast_ITEMS(full);
    for (Py_ssize_t output = 0; output < output_count; output++) {
        PyObject *item = output < downsample_count
            ? downsample_items[output]
            : full_items[output - downsample_count];
        Py_ssize_t row = PyLong_AsSsize_t(item);
        if (row == -1 && PyErr_Occurred()) goto error;
        if (row < 0 || row >= row_count) {
            PyErr_Format(PyExc_IndexError,
                         "series chart row %zd is outside 0..%zd",
                         row, row_count - 1);
            goto error;
        }
        row_indices[output] = row;
    }
    Py_ssize_t maximum_group = 0;
    for (int section = 0; section < 2; section++) {
        Py_ssize_t begin = section == 0 ? 0 : downsample_count;
        Py_ssize_t end = section == 0 ? downsample_count : output_count;
        for (Py_ssize_t base = begin; base < end;) {
            Py_ssize_t series = row_indices[base] / measure_count;
            Py_ssize_t next = base + 1;
            while (next < end && row_indices[next] / measure_count == series)
                next++;
            if (next - base > maximum_group) maximum_group = next - base;
            base = next;
        }
    }
    if ((size_t)bucket_count > SIZE_MAX / sizeof(double) ||
        (maximum_group != 0 &&
         (size_t)bucket_count > SIZE_MAX / (size_t)maximum_group) ||
        (size_t)bucket_count * (size_t)maximum_group >
            SIZE_MAX / sizeof(double)) {
        PyErr_NoMemory();
        goto error;
    }
    size_t cell_count = (size_t)bucket_count * (size_t)maximum_group;
    paths = PyTuple_New(output_count);
    ticks = tick_text ? NULL : PyTuple_New(downsample_count);
    Py_ssize_t tick_count = 0;
    values = cell_count != 0
        ? PyMem_Malloc(cell_count * sizeof(*values)) : NULL;
    present = cell_count != 0
        ? PyMem_Malloc(cell_count) : NULL;
    Py_ssize_t workspace_count = threshold < bucket_count ? threshold : bucket_count;
    selected_workspace = workspace_count != 0
        ? PyMem_Malloc((size_t)workspace_count * sizeof(*selected_workspace))
        : NULL;
    if (tick_text && wreath_writer_init(&tick_writer, 256) < 0) goto error;
    if (paths == NULL || (!tick_text && ticks == NULL) ||
        (workspace_count != 0 && selected_workspace == NULL) ||
        (cell_count != 0 && (values == NULL || present == NULL))) {
        PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t base = 0; base < downsample_count;) {
        Py_ssize_t series = row_indices[base] / measure_count;
        Py_ssize_t next = base + 1;
        while (next < downsample_count &&
               row_indices[next] / measure_count == series) next++;
        Py_ssize_t group_count = next - base;
        if (fill_row == series_chart_fill_dense) {
            if (series_chart_fill_group(
                    series_maps[series], measures, row_indices + base,
                    group_count, measure_count,
                    (const SeriesChartDense *)fill_context,
                    bucket_count, values, present) < 0) goto error;
        }
        else {
            for (Py_ssize_t slot = 0; slot < group_count; slot++) {
                Py_ssize_t row = row_indices[base + slot];
                if (fill_row(
                        fill_context, series_maps[series],
                        &measures[row % measure_count], bucket_count,
                        bucket_count != 0 ? values +
                            (size_t)slot * (size_t)bucket_count : NULL,
                        bucket_count != 0 ? present +
                            (size_t)slot * (size_t)bucket_count : NULL) < 0)
                    goto error;
            }
        }
        for (Py_ssize_t output = base; output < next; output++) {
            Py_ssize_t slot = output - base;
            double *row_values = bucket_count != 0 ? values +
                (size_t)slot * (size_t)bucket_count : NULL;
            unsigned char *row_present = bucket_count != 0 ? present +
                (size_t)slot * (size_t)bucket_count : NULL;
            double minimum, maximum;
            Py_ssize_t selected_count = 0;
            Py_ssize_t *selected = series_chart_lttb(
                row_values, NULL, NULL, bucket_count, threshold,
                selected_workspace, &selected_count, &minimum, &maximum);
            if (selected == NULL && bucket_count != 0) goto error;
            PyObject *path = series_chart_path(
                row_values, row_present, bucket_count, selected,
                selected_count, NULL, NULL, NULL, NULL);
            PyObject *axis = tick_text ? NULL : series_chart_ticks(
                minimum, maximum, tick_target);
            int tick_error = tick_text ? series_chart_write_ticks(
                &tick_writer, minimum, maximum, tick_target, output != 0,
                &tick_count) : 0;
            if (path == NULL || (!tick_text && axis == NULL) || tick_error < 0) {
                Py_XDECREF(path);
                Py_XDECREF(axis);
                goto error;
            }
            PyTuple_SET_ITEM(paths, output, path);
            if (!tick_text) PyTuple_SET_ITEM(ticks, output, axis);
        }
        base = next;
    }
    for (Py_ssize_t base = downsample_count; base < output_count;) {
        Py_ssize_t series = row_indices[base] / measure_count;
        Py_ssize_t next = base + 1;
        while (next < output_count &&
               row_indices[next] / measure_count == series) next++;
        Py_ssize_t group_count = next - base;
        if (fill_row == series_chart_fill_dense) {
            if (series_chart_fill_group(
                    series_maps[series], measures, row_indices + base,
                    group_count, measure_count,
                    (const SeriesChartDense *)fill_context,
                    bucket_count, values, present) < 0) goto error;
        }
        else {
            for (Py_ssize_t slot = 0; slot < group_count; slot++) {
                Py_ssize_t row = row_indices[base + slot];
                if (fill_row(
                        fill_context, series_maps[series],
                        &measures[row % measure_count], bucket_count,
                        bucket_count != 0 ? values +
                            (size_t)slot * (size_t)bucket_count : NULL,
                        bucket_count != 0 ? present +
                            (size_t)slot * (size_t)bucket_count : NULL) < 0)
                    goto error;
            }
        }
        for (Py_ssize_t output = base; output < next; output++) {
            Py_ssize_t slot = output - base;
            PyObject *path = series_chart_path(
                bucket_count != 0 ? values +
                    (size_t)slot * (size_t)bucket_count : NULL,
                bucket_count != 0 ? present +
                    (size_t)slot * (size_t)bucket_count : NULL,
                bucket_count, NULL, 0, NULL, NULL, NULL, NULL);
            if (path == NULL) goto error;
            PyTuple_SET_ITEM(paths, output, path);
        }
        base = next;
    }
    PyObject *tick_output = ticks;
    if (tick_text) {
        PyObject *bytes = wreath_writer_finish(&tick_writer);
        tick_writer.bytes = NULL;
        tick_output = bytes == NULL ? NULL : PyUnicode_DecodeASCII(
            PyBytes_AS_STRING(bytes), PyBytes_GET_SIZE(bytes), NULL);
        Py_XDECREF(bytes);
        if (tick_output == NULL) goto error;
    }
    PyObject *result = tick_text
        ? Py_BuildValue("nOOOn", row_count, keys, paths, tick_output, tick_count)
        : Py_BuildValue("nOOO", row_count, keys, paths, tick_output);
    Py_DECREF(paths);
    Py_DECREF(tick_output);
    PyMem_Free(values);
    PyMem_Free(present);
    PyMem_Free(selected_workspace);
    PyMem_Free(row_indices);
    return result;

error:
    Py_XDECREF(paths);
    Py_XDECREF(ticks);
    Py_XDECREF(tick_writer.bytes);
    PyMem_Free(values);
    PyMem_Free(present);
    PyMem_Free(selected_workspace);
    PyMem_Free(row_indices);
    return NULL;
}

typedef struct {
    Py_ssize_t row;
    Py_ssize_t slot;
} SeriesChartRowEntry;

typedef struct {
    PyObject *key;
    Py_ssize_t index;
} SeriesChartBucketEntry;

typedef struct {
    Py_ssize_t bucket_count;
    Py_ssize_t series_count;
    Py_ssize_t measure_count;
    double *values;
    unsigned char *present;
    unsigned char *index_plan;
    char *value_text;
    size_t *value_offsets;
    unsigned char *value_lengths;
    char *path_text;
    size_t *path_offsets;
    double *prefix;
    double *bounds;
    PyObject *keys;
} SeriesData;

#define SERIES_DATA_CAPSULE_NAME "wreath.series_data"
#define SERIES_CHART_PLAN_CAPSULE_NAME "wreath.series_chart_plan"

typedef struct {
    PyObject *data_capsule;
    SeriesData *data;
    Py_ssize_t downsample_count;
    Py_ssize_t full_count;
    Py_ssize_t tick_target;
    Py_ssize_t *rows;
    size_t *selected_offsets;
    Py_ssize_t *selected;
    double *bounds;
} SeriesChartPlan;

static void
series_data_free(SeriesData *data)
{
    if (data == NULL) return;
    PyMem_Free(data->values);
    PyMem_Free(data->present);
    PyMem_Free(data->index_plan);
    PyMem_Free(data->value_text);
    PyMem_Free(data->value_offsets);
    PyMem_Free(data->value_lengths);
    PyMem_Free(data->path_text);
    PyMem_Free(data->path_offsets);
    PyMem_Free(data->prefix);
    PyMem_Free(data->bounds);
    Py_XDECREF(data->keys);
    PyMem_Free(data);
}

static void
series_data_destroy(PyObject *capsule)
{
    SeriesData *data = PyCapsule_GetPointer(capsule, SERIES_DATA_CAPSULE_NAME);
    if (data == NULL) {
        PyErr_WriteUnraisable(capsule);
        return;
    }
    series_data_free(data);
}

static SeriesData *
series_data_get(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, SERIES_DATA_CAPSULE_NAME);
}

static void
series_chart_plan_free(SeriesChartPlan *plan)
{
    if (plan == NULL) return;
    Py_XDECREF(plan->data_capsule);
    PyMem_Free(plan->rows);
    PyMem_Free(plan->selected_offsets);
    PyMem_Free(plan->selected);
    PyMem_Free(plan->bounds);
    PyMem_Free(plan);
}

static void
series_chart_plan_destroy(PyObject *capsule)
{
    SeriesChartPlan *plan = PyCapsule_GetPointer(
        capsule, SERIES_CHART_PLAN_CAPSULE_NAME);
    if (plan == NULL) {
        PyErr_WriteUnraisable(capsule);
        return;
    }
    series_chart_plan_free(plan);
}

static SeriesChartPlan *
series_chart_plan_get(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, SERIES_CHART_PLAN_CAPSULE_NAME);
}

PyObject *
wreath_series_data(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *bucket_source, *sparse, *fills;
    if (!PyArg_ParseTuple(args, "OOO:series_data", &bucket_source, &sparse, &fills))
        return NULL;
    if (!PyDict_Check(sparse) || !PyDict_Check(fills)) {
        PyErr_SetString(
            PyExc_TypeError, "series data sparse values and fills must be dicts");
        return NULL;
    }
    PyObject *buckets = PySequence_Fast(
        bucket_source, "series data buckets must be an iterable dense run");
    if (buckets == NULL) return NULL;
    Py_ssize_t bucket_count = PySequence_Fast_GET_SIZE(buckets);
    Py_ssize_t series_count = PyDict_GET_SIZE(sparse);
    Py_ssize_t measure_count = PyDict_GET_SIZE(fills);
    if (measure_count == 0) {
        Py_DECREF(buckets);
        PyErr_SetString(PyExc_ValueError, "series data needs at least one measure");
        return NULL;
    }
    if (series_count > PY_SSIZE_T_MAX / measure_count) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    Py_ssize_t row_count = series_count * measure_count;
    if (bucket_count != 0 &&
        (size_t)row_count > SIZE_MAX / (size_t)bucket_count) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    size_t cell_count = (size_t)row_count * (size_t)bucket_count;
    if ((size_t)row_count != 0 &&
        (size_t)bucket_count + 1 > SIZE_MAX / (size_t)row_count) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    size_t prefix_count = (size_t)row_count * ((size_t)bucket_count + 1);
    if (cell_count > SIZE_MAX / sizeof(double) ||
        prefix_count > SIZE_MAX / sizeof(double) ||
        (size_t)row_count > SIZE_MAX / (2 * sizeof(double)) ||
        cell_count > SIZE_MAX / sizeof(size_t) ||
        (size_t)bucket_count > SIZE_MAX / 21 ||
        (size_t)measure_count > SIZE_MAX / sizeof(SeriesMeasure)) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }

    SeriesData *data = PyMem_Calloc(1, sizeof(*data));
    SeriesMeasure *measures = PyMem_Malloc(
        (size_t)measure_count * sizeof(*measures));
    if (data == NULL || measures == NULL) {
        PyMem_Free(data);
        PyMem_Free(measures);
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    data->bucket_count = bucket_count;
    data->series_count = series_count;
    data->measure_count = measure_count;
    data->values = cell_count != 0
        ? PyMem_Malloc(cell_count * sizeof(*data->values)) : NULL;
    data->present = cell_count != 0 ? PyMem_Malloc(cell_count) : NULL;
    data->index_plan = bucket_count != 0
        ? PyMem_Malloc((size_t)bucket_count * 21) : NULL;
    data->prefix = prefix_count != 0
        ? PyMem_Malloc(prefix_count * sizeof(*data->prefix)) : NULL;
    data->bounds = row_count != 0
        ? PyMem_Malloc((size_t)row_count * 2 * sizeof(*data->bounds)) : NULL;
    data->keys = PyTuple_New(series_count);
    if ((cell_count != 0 && (data->values == NULL || data->present == NULL)) ||
        (prefix_count != 0 && data->prefix == NULL) ||
        (row_count != 0 && data->bounds == NULL) ||
        (bucket_count != 0 && data->index_plan == NULL) ||
        data->keys == NULL) {
        PyMem_Free(measures);
        Py_DECREF(buckets);
        series_data_free(data);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
        unsigned char *planned = data->index_plan + (size_t)bucket * 21;
        planned[0] = (unsigned char)series_format_index(
            (char *)planned + 1, bucket);
    }

    Py_ssize_t position = 0, measure_index = 0;
    PyObject *name, *empty;
    while (PyDict_Next(fills, &position, &name, &empty)) {
        measures[measure_index] = (SeriesMeasure){name, empty};
        double number = 0.0;
        unsigned char has_value = empty != Py_None;
        if (has_value) {
            number = series_as_double(empty);
            if (PyErr_Occurred() || !isfinite(number)) {
                if (!PyErr_Occurred()) PyErr_Format(
                    PyExc_ValueError,
                    "series fill for measure %R must be finite", name);
                goto error;
            }
        }
        for (Py_ssize_t series = 0; series < series_count; series++) {
            size_t offset = ((size_t)series * (size_t)measure_count +
                             (size_t)measure_index) * (size_t)bucket_count;
            for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
                data->values[offset + (size_t)bucket] = number;
                data->present[offset + (size_t)bucket] = has_value;
            }
        }
        measure_index++;
    }

    PyObject **bucket_items = PySequence_Fast_ITEMS(buckets);
    position = 0;
    Py_ssize_t series_index = 0;
    PyObject *key, *by_bucket;
    while (PyDict_Next(sparse, &position, &key, &by_bucket)) {
        if (!PyDict_Check(by_bucket)) {
            PyErr_Format(
                PyExc_TypeError,
                "series %R values must be a bucket dict", key);
            goto error;
        }
        PyTuple_SET_ITEM(data->keys, series_index, Py_NewRef(key));
        for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
            PyObject *row = NULL;
            int found = PyDict_GetItemRef(by_bucket, bucket_items[bucket], &row);
            if (found < 0) goto error;
            if (found == 0) continue;
            if (!PyDict_Check(row)) {
                PyErr_Format(
                    PyExc_TypeError,
                    "series bucket %R values must be a measure dict, got %.200s",
                    bucket_items[bucket], Py_TYPE(row)->tp_name);
                Py_DECREF(row);
                goto error;
            }
            for (Py_ssize_t measure = 0; measure < measure_count; measure++) {
                PyObject *value = PyDict_GetItemWithError(
                    row, measures[measure].name);
                if (value == NULL) {
                    if (PyErr_Occurred()) {
                        Py_DECREF(row);
                        goto error;
                    }
                    continue;
                }
                size_t cell = ((size_t)series_index *
                               (size_t)measure_count + (size_t)measure) *
                              (size_t)bucket_count + (size_t)bucket;
                if (value == Py_None) {
                    data->values[cell] = 0.0;
                    data->present[cell] = 0;
                    continue;
                }
                double number = series_as_double(value);
                if (PyErr_Occurred() || !isfinite(number)) {
                    if (!PyErr_Occurred()) PyErr_Format(
                        PyExc_ValueError,
                        "series chart value at bucket %zd must be finite", bucket);
                    Py_DECREF(row);
                    goto error;
                }
                data->values[cell] = number;
                data->present[cell] = 1;
            }
            Py_DECREF(row);
        }
        series_index++;
    }
    for (Py_ssize_t row = 0; row < row_count; row++) {
        size_t cell_offset = (size_t)row * (size_t)bucket_count;
        double *row_prefix = data->prefix +
            (size_t)row * ((size_t)bucket_count + 1);
        row_prefix[0] = 0.0;
        for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++)
            row_prefix[bucket + 1] = row_prefix[bucket] +
                                     data->values[cell_offset + (size_t)bucket];
        series_chart_minmax(data->values + cell_offset, bucket_count,
                            &data->bounds[(size_t)row * 2],
                            &data->bounds[(size_t)row * 2 + 1]);
    }
    data->value_offsets = cell_count != 0
        ? PyMem_Malloc(cell_count * sizeof(*data->value_offsets)) : NULL;
    data->value_lengths = cell_count != 0 ? PyMem_Malloc(cell_count) : NULL;
    if (cell_count != 0 &&
        (data->value_offsets == NULL || data->value_lengths == NULL)) {
        PyErr_NoMemory();
        goto error;
    }
    size_t text_size = 0;
    char formatted[32];
    for (size_t cell = 0; cell < cell_count; cell++) {
        data->value_offsets[cell] = text_size;
        if (!data->present[cell]) {
            data->value_lengths[cell] = 0;
            continue;
        }
        Py_ssize_t length = series_format_double(formatted, data->values[cell]);
        if (length < 0) goto error;
        if ((size_t)length > SIZE_MAX - text_size) {
            PyErr_NoMemory();
            goto error;
        }
        if (length > UCHAR_MAX) {
            PyErr_SetString(PyExc_OverflowError,
                            "series coordinate text exceeds 255 bytes");
            goto error;
        }
        data->value_lengths[cell] = (unsigned char)length;
        text_size += (size_t)length;
    }
    data->value_text = text_size != 0 ? PyMem_Malloc(text_size) : NULL;
    if (text_size != 0 && data->value_text == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    for (size_t cell = 0; cell < cell_count; cell++) {
        if (!data->present[cell]) continue;
        Py_ssize_t length = series_format_double(
            data->value_text + data->value_offsets[cell], data->values[cell]);
        if (length < 0) goto error;
    }
    if ((size_t)row_count >
        SIZE_MAX / sizeof(*data->path_offsets) - 1) {
        PyErr_NoMemory();
        goto error;
    }
    data->path_offsets = PyMem_Malloc(
        ((size_t)row_count + 1) * sizeof(*data->path_offsets));
    if (data->path_offsets == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    size_t path_size = 0;
    for (Py_ssize_t row = 0; row < row_count; row++) {
        data->path_offsets[row] = path_size;
        size_t offset = (size_t)row * (size_t)bucket_count;
        for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
            size_t cell = offset + (size_t)bucket;
            if (!data->present[cell]) continue;
            size_t point_size = 2 + data->index_plan[(size_t)bucket * 21] +
                                data->value_lengths[cell];
            if (point_size > SIZE_MAX - path_size) {
                PyErr_NoMemory();
                goto error;
            }
            path_size += point_size;
        }
    }
    data->path_offsets[row_count] = path_size;
    data->path_text = path_size != 0 ? PyMem_Malloc(path_size) : NULL;
    if (path_size != 0 && data->path_text == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t row = 0; row < row_count; row++) {
        size_t written = data->path_offsets[row];
        size_t offset = (size_t)row * (size_t)bucket_count;
        int open = 0;
        for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
            size_t cell = offset + (size_t)bucket;
            if (!data->present[cell]) {
                open = 0;
                continue;
            }
            const unsigned char *planned =
                data->index_plan + (size_t)bucket * 21;
            data->path_text[written++] = open ? 'L' : 'M';
            memcpy(data->path_text + written, planned + 1, planned[0]);
            written += planned[0];
            data->path_text[written++] = ',';
            memcpy(data->path_text + written,
                   data->value_text + data->value_offsets[cell],
                   data->value_lengths[cell]);
            written += data->value_lengths[cell];
            open = 1;
        }
    }
    PyMem_Free(measures);
    Py_DECREF(buckets);
    return PyCapsule_New(data, SERIES_DATA_CAPSULE_NAME, series_data_destroy);

error:
    PyMem_Free(measures);
    Py_DECREF(buckets);
    series_data_free(data);
    return NULL;
}

static SeriesChartPlan *
series_chart_plan_compile(
    PyObject *data_capsule, PyObject *downsample_source, PyObject *full_source,
    Py_ssize_t threshold, Py_ssize_t tick_target)
{
    if (threshold < 3 || tick_target < 2 || tick_target > 10000) {
        PyErr_SetString(
            PyExc_ValueError,
            "series chart threshold must be >= 3 and tick target in 2..10000");
        return NULL;
    }
    SeriesData *data = series_data_get(data_capsule);
    if (data == NULL) return NULL;
    PyObject *downsample = PySequence_Fast(
        downsample_source,
        "series chart downsample rows must be an iterable of indices");
    PyObject *full = PySequence_Fast(
        full_source, "series chart full rows must be an iterable of indices");
    if (downsample == NULL || full == NULL) {
        Py_XDECREF(downsample);
        Py_XDECREF(full);
        return NULL;
    }
    Py_ssize_t downsample_count = PySequence_Fast_GET_SIZE(downsample);
    Py_ssize_t full_count = PySequence_Fast_GET_SIZE(full);
    if (downsample_count > PY_SSIZE_T_MAX - full_count) {
        Py_DECREF(downsample);
        Py_DECREF(full);
        PyErr_NoMemory();
        return NULL;
    }
    Py_ssize_t output_count = downsample_count + full_count;
    Py_ssize_t workspace_count = threshold < data->bucket_count
        ? threshold : data->bucket_count;
    if (downsample_count == PY_SSIZE_T_MAX ||
        (size_t)output_count > SIZE_MAX / sizeof(Py_ssize_t) ||
        (size_t)(downsample_count + 1) > SIZE_MAX / sizeof(size_t) ||
        (size_t)downsample_count > SIZE_MAX / (2 * sizeof(double)) ||
        (size_t)workspace_count > SIZE_MAX / sizeof(Py_ssize_t) ||
        (workspace_count != 0 && (size_t)downsample_count >
            SIZE_MAX / sizeof(Py_ssize_t) / (size_t)workspace_count)) {
        Py_DECREF(downsample);
        Py_DECREF(full);
        PyErr_NoMemory();
        return NULL;
    }
    SeriesChartPlan *plan = PyMem_Calloc(1, sizeof(*plan));
    if (plan == NULL) {
        Py_DECREF(downsample);
        Py_DECREF(full);
        PyErr_NoMemory();
        return NULL;
    }
    plan->rows = output_count != 0
        ? PyMem_Malloc((size_t)output_count * sizeof(*plan->rows)) : NULL;
    plan->selected_offsets = PyMem_Malloc(
        (size_t)(downsample_count + 1) * sizeof(*plan->selected_offsets));
    size_t selected_capacity =
        (size_t)downsample_count * (size_t)workspace_count;
    plan->selected = selected_capacity != 0
        ? PyMem_Malloc(selected_capacity * sizeof(*plan->selected)) : NULL;
    plan->bounds = downsample_count != 0
        ? PyMem_Malloc((size_t)downsample_count * 2 * sizeof(*plan->bounds))
        : NULL;
    if ((output_count != 0 && plan->rows == NULL) ||
        plan->selected_offsets == NULL ||
        (selected_capacity != 0 && plan->selected == NULL) ||
        (downsample_count != 0 && plan->bounds == NULL)) {
        PyErr_NoMemory();
        goto plan_error;
    }
    plan->data_capsule = Py_NewRef(data_capsule);
    plan->data = data;
    plan->downsample_count = downsample_count;
    plan->full_count = full_count;
    plan->tick_target = tick_target;
    PyObject **downsample_items = PySequence_Fast_ITEMS(downsample);
    PyObject **full_items = PySequence_Fast_ITEMS(full);
    Py_ssize_t row_count = data->series_count * data->measure_count;
    size_t selected_used = 0;
    for (Py_ssize_t output = 0; output < downsample_count; output++) {
        Py_ssize_t row = PyLong_AsSsize_t(downsample_items[output]);
        if (row == -1 && PyErr_Occurred()) goto plan_error;
        if (row < 0 || row >= row_count) {
            PyErr_Format(PyExc_IndexError,
                         "series chart row %zd is outside 0..%zd",
                         row, row_count - 1);
            goto plan_error;
        }
        plan->rows[output] = row;
        plan->selected_offsets[output] = selected_used;
        size_t offset = (size_t)row * (size_t)data->bucket_count;
        Py_ssize_t selected_count = 0;
        double minimum;
        double maximum;
        Py_ssize_t *selected = series_chart_lttb(
            data->bucket_count != 0 ? data->values + offset : NULL,
            data->prefix +
                (size_t)row * ((size_t)data->bucket_count + 1),
            data->bounds + (size_t)row * 2,
            data->bucket_count, threshold,
            selected_capacity != 0 ? plan->selected + selected_used : NULL,
            &selected_count, &minimum, &maximum);
        if (selected == NULL && data->bucket_count != 0) goto plan_error;
        selected_used += (size_t)selected_count;
        plan->bounds[(size_t)output * 2] = minimum;
        plan->bounds[(size_t)output * 2 + 1] = maximum;
    }
    plan->selected_offsets[downsample_count] = selected_used;
    for (Py_ssize_t output = 0; output < full_count; output++) {
        Py_ssize_t row = PyLong_AsSsize_t(full_items[output]);
        if (row == -1 && PyErr_Occurred()) goto plan_error;
        if (row < 0 || row >= row_count) {
            PyErr_Format(PyExc_IndexError,
                         "series chart row %zd is outside 0..%zd",
                         row, row_count - 1);
            goto plan_error;
        }
        plan->rows[downsample_count + output] = row;
    }
    Py_DECREF(downsample);
    Py_DECREF(full);
    return plan;

plan_error:
    Py_DECREF(downsample);
    Py_DECREF(full);
    series_chart_plan_free(plan);
    return NULL;
}

static PyObject *
series_chart_plan_render(SeriesChartPlan *plan, int tick_text)
{
    SeriesData *data = plan->data;
    Py_ssize_t output_count = plan->downsample_count + plan->full_count;
    PyObject *paths = PyTuple_New(output_count);
    PyObject *ticks = tick_text ? NULL : PyTuple_New(plan->downsample_count);
    WreathBytesWriter tick_writer = {0};
    Py_ssize_t tick_count = 0;
    if (paths == NULL || (!tick_text && ticks == NULL) ||
        (tick_text && wreath_writer_init(&tick_writer, 256) < 0)) {
        Py_XDECREF(paths);
        Py_XDECREF(ticks);
        Py_XDECREF(tick_writer.bytes);
        return NULL;
    }
    for (Py_ssize_t output = 0; output < plan->downsample_count; output++) {
        Py_ssize_t row = plan->rows[output];
        size_t offset = (size_t)row * (size_t)data->bucket_count;
        size_t selected_start = plan->selected_offsets[output];
        size_t selected_end = plan->selected_offsets[output + 1];
        PyObject *path = series_chart_path(
            data->bucket_count != 0 ? data->values + offset : NULL,
            data->bucket_count != 0 ? data->present + offset : NULL,
            data->bucket_count,
            selected_end != selected_start
                ? plan->selected + selected_start : NULL,
            (Py_ssize_t)(selected_end - selected_start), data->index_plan,
            data->value_text,
            data->bucket_count != 0 ? data->value_offsets + offset : NULL,
            data->bucket_count != 0 ? data->value_lengths + offset : NULL);
        double minimum = plan->bounds[(size_t)output * 2];
        double maximum = plan->bounds[(size_t)output * 2 + 1];
        PyObject *axis = tick_text ? NULL : series_chart_ticks(
            minimum, maximum, plan->tick_target);
        int tick_error = tick_text ? series_chart_write_ticks(
            &tick_writer, minimum, maximum, plan->tick_target, output != 0,
            &tick_count) : 0;
        if (path == NULL || (!tick_text && axis == NULL) || tick_error < 0) {
            Py_XDECREF(path);
            Py_XDECREF(axis);
            goto render_error;
        }
        PyTuple_SET_ITEM(paths, output, path);
        if (!tick_text) PyTuple_SET_ITEM(ticks, output, axis);
    }
    for (Py_ssize_t output = 0; output < plan->full_count; output++) {
        Py_ssize_t row = plan->rows[plan->downsample_count + output];
        size_t path_start = data->path_offsets[row];
        size_t path_size = data->path_offsets[row + 1] - path_start;
        if (path_size > (size_t)PY_SSIZE_T_MAX) {
            PyErr_NoMemory();
            goto render_error;
        }
        PyObject *path = PyUnicode_DecodeASCII(
            data->path_text == NULL ? "" : data->path_text + path_start,
            (Py_ssize_t)path_size, NULL);
        if (path == NULL) goto render_error;
        PyTuple_SET_ITEM(paths, plan->downsample_count + output, path);
    }
    PyObject *tick_output = ticks;
    if (tick_text) {
        PyObject *bytes = wreath_writer_finish(&tick_writer);
        tick_writer.bytes = NULL;
        tick_output = bytes == NULL ? NULL : PyUnicode_DecodeASCII(
            PyBytes_AS_STRING(bytes), PyBytes_GET_SIZE(bytes), NULL);
        Py_XDECREF(bytes);
        if (tick_output == NULL) goto render_error;
    }
    PyObject *result = tick_text
        ? Py_BuildValue(
            "nOOOn", data->series_count * data->measure_count, data->keys,
            paths, tick_output, tick_count)
        : Py_BuildValue(
            "nOOO", data->series_count * data->measure_count, data->keys,
            paths, tick_output);
    Py_DECREF(paths);
    Py_DECREF(tick_output);
    return result;

render_error:
    Py_XDECREF(paths);
    Py_XDECREF(ticks);
    Py_XDECREF(tick_writer.bytes);
    return NULL;
}

PyObject *
wreath_series_data_chart_plan(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *data;
    PyObject *downsample;
    PyObject *full;
    Py_ssize_t threshold;
    Py_ssize_t tick_target;
    if (!PyArg_ParseTuple(
            args, "OOOnn:series_data_chart_plan", &data, &downsample, &full,
            &threshold, &tick_target)) return NULL;
    SeriesChartPlan *plan = series_chart_plan_compile(
        data, downsample, full, threshold, tick_target);
    if (plan == NULL) return NULL;
    PyObject *capsule = PyCapsule_New(
        plan, SERIES_CHART_PLAN_CAPSULE_NAME, series_chart_plan_destroy);
    if (capsule == NULL) series_chart_plan_free(plan);
    return capsule;
}

PyObject *
wreath_series_chart_plan(PyObject *Py_UNUSED(self), PyObject *plan_capsule)
{
    SeriesChartPlan *plan = series_chart_plan_get(plan_capsule);
    return plan == NULL ? NULL : series_chart_plan_render(plan, 0);
}

PyObject *
wreath_series_chart_plan_text(PyObject *Py_UNUSED(self), PyObject *plan_capsule)
{
    SeriesChartPlan *plan = series_chart_plan_get(plan_capsule);
    return plan == NULL ? NULL : series_chart_plan_render(plan, 1);
}

static PyObject *
series_data_chart(PyObject *args, int tick_text)
{
    PyObject *capsule, *downsample_source, *full_source;
    Py_ssize_t threshold, tick_target;
    if (!PyArg_ParseTuple(
            args, "OOOnn:series_data_chart", &capsule, &downsample_source,
            &full_source, &threshold, &tick_target)) return NULL;
    if (threshold < 3 || tick_target < 2 || tick_target > 10000) {
        PyErr_SetString(
            PyExc_ValueError,
            "series chart threshold must be >= 3 and tick target in 2..10000");
        return NULL;
    }
    SeriesData *data = series_data_get(capsule);
    if (data == NULL) return NULL;
    PyObject *downsample = PySequence_Fast(
        downsample_source,
        "series chart downsample rows must be an iterable of indices");
    PyObject *full = PySequence_Fast(
        full_source, "series chart full rows must be an iterable of indices");
    if (downsample == NULL || full == NULL) {
        Py_XDECREF(downsample);
        Py_XDECREF(full);
        return NULL;
    }
    Py_ssize_t downsample_count = PySequence_Fast_GET_SIZE(downsample);
    Py_ssize_t full_count = PySequence_Fast_GET_SIZE(full);
    PyObject *paths = PyTuple_New(downsample_count + full_count);
    PyObject *ticks = tick_text ? NULL : PyTuple_New(downsample_count);
    WreathBytesWriter tick_writer = {0};
    Py_ssize_t tick_count = 0;
    if (tick_text && wreath_writer_init(&tick_writer, 256) < 0) {
        Py_XDECREF(paths);
        Py_XDECREF(tick_writer.bytes);
        Py_DECREF(downsample);
        Py_DECREF(full);
        return NULL;
    }
    if (paths == NULL || (!tick_text && ticks == NULL)) {
        Py_XDECREF(paths);
        Py_XDECREF(ticks);
        Py_XDECREF(tick_writer.bytes);
        Py_DECREF(downsample);
        Py_DECREF(full);
        return NULL;
    }
    Py_ssize_t row_count = data->series_count * data->measure_count;
    PyObject **downsample_items = PySequence_Fast_ITEMS(downsample);
    PyObject **full_items = PySequence_Fast_ITEMS(full);
    Py_ssize_t workspace_count = threshold < data->bucket_count
        ? threshold : data->bucket_count;
    Py_ssize_t *selected_workspace = workspace_count != 0
        ? PyMem_Malloc((size_t)workspace_count * sizeof(*selected_workspace))
        : NULL;
    if (workspace_count != 0 && selected_workspace == NULL) goto chart_error;
    for (Py_ssize_t output = 0; output < downsample_count; output++) {
        Py_ssize_t row = PyLong_AsSsize_t(downsample_items[output]);
        if (row == -1 && PyErr_Occurred()) goto chart_error;
        if (row < 0 || row >= row_count) {
            PyErr_Format(PyExc_IndexError,
                         "series chart row %zd is outside 0..%zd",
                         row, row_count - 1);
            goto chart_error;
        }
        size_t offset = (size_t)row * (size_t)data->bucket_count;
        const double *row_values = data->bucket_count != 0
            ? data->values + offset : NULL;
        const unsigned char *row_present = data->bucket_count != 0
            ? data->present + offset : NULL;
        double minimum, maximum;
        Py_ssize_t selected_count = 0;
        const double *row_prefix = data->prefix +
            (size_t)row * ((size_t)data->bucket_count + 1);
        const double *row_bounds = data->bounds + (size_t)row * 2;
        Py_ssize_t *selected = series_chart_lttb(
            row_values, row_prefix, row_bounds, data->bucket_count, threshold,
            selected_workspace, &selected_count, &minimum, &maximum);
        if (selected == NULL && data->bucket_count != 0) goto chart_error;
        PyObject *path = series_chart_path(
            row_values, row_present, data->bucket_count,
            selected, selected_count, data->index_plan,
            data->value_text,
            data->bucket_count != 0 ? data->value_offsets + offset : NULL,
            data->bucket_count != 0 ? data->value_lengths + offset : NULL);
        PyObject *axis = tick_text ? NULL : series_chart_ticks(
            minimum, maximum, tick_target);
        int tick_error = tick_text ? series_chart_write_ticks(
            &tick_writer, minimum, maximum, tick_target, output != 0,
            &tick_count) : 0;
        if (path == NULL || (!tick_text && axis == NULL) || tick_error < 0) {
            Py_XDECREF(path);
            Py_XDECREF(axis);
            goto chart_error;
        }
        PyTuple_SET_ITEM(paths, output, path);
        if (!tick_text) PyTuple_SET_ITEM(ticks, output, axis);
    }
    for (Py_ssize_t output = 0; output < full_count; output++) {
        Py_ssize_t row = PyLong_AsSsize_t(full_items[output]);
        if (row == -1 && PyErr_Occurred()) goto chart_error;
        if (row < 0 || row >= row_count) {
            PyErr_Format(PyExc_IndexError,
                         "series chart row %zd is outside 0..%zd",
                         row, row_count - 1);
            goto chart_error;
        }
        size_t path_start = data->path_offsets[row];
        size_t path_size = data->path_offsets[row + 1] - path_start;
        if (path_size > (size_t)PY_SSIZE_T_MAX) {
            PyErr_NoMemory();
            goto chart_error;
        }
        PyObject *path = PyUnicode_DecodeASCII(
            data->path_text == NULL ? "" : data->path_text + path_start,
            (Py_ssize_t)path_size, NULL);
        if (path == NULL) goto chart_error;
        PyTuple_SET_ITEM(paths, downsample_count + output, path);
    }
    PyObject *tick_output = ticks;
    if (tick_text) {
        PyObject *bytes = wreath_writer_finish(&tick_writer);
        tick_output = bytes == NULL ? NULL : PyUnicode_DecodeASCII(
            PyBytes_AS_STRING(bytes), PyBytes_GET_SIZE(bytes), NULL);
        Py_XDECREF(bytes);
        if (tick_output == NULL) goto chart_error;
    }
    PyObject *result = tick_text
        ? Py_BuildValue("nOOOn", row_count, data->keys, paths,
                        tick_output, tick_count)
        : Py_BuildValue("nOOO", row_count, data->keys, paths, tick_output);
    Py_DECREF(paths);
    Py_DECREF(tick_output);
    Py_DECREF(downsample);
    Py_DECREF(full);
    PyMem_Free(selected_workspace);
    return result;

chart_error:
    Py_XDECREF(paths);
    Py_XDECREF(ticks);
    Py_XDECREF(tick_writer.bytes);
    Py_DECREF(downsample);
    Py_DECREF(full);
    PyMem_Free(selected_workspace);
    return NULL;
}

PyObject *
wreath_series_data_chart(PyObject *Py_UNUSED(self), PyObject *args)
{
    return series_data_chart(args, 0);
}

PyObject *
wreath_series_data_chart_text(PyObject *Py_UNUSED(self), PyObject *args)
{
    return series_data_chart(args, 1);
}

static PyObject *
series_chart_project_spine(Py_ssize_t bucket_count, Py_ssize_t series_count,
                           Py_ssize_t measure_count, SeriesMeasure *measures,
                           PyObject **series_maps, PyObject *keys,
                           PyObject *downsample, PyObject *full,
                           Py_ssize_t threshold, Py_ssize_t tick_target,
                           SeriesChartSpine *spine)
{
    Py_ssize_t downsample_count = PySequence_Fast_GET_SIZE(downsample);
    Py_ssize_t full_count = PySequence_Fast_GET_SIZE(full);
    if (downsample_count > PY_SSIZE_T_MAX - full_count) return PyErr_NoMemory();
    Py_ssize_t output_count = downsample_count + full_count;
    Py_ssize_t row_count = series_count * measure_count;
    if (output_count > PY_SSIZE_T_MAX / 4 ||
        bucket_count > PY_SSIZE_T_MAX / 2 ||
        (size_t)output_count > SIZE_MAX / sizeof(Py_ssize_t) ||
        (size_t)series_count > SIZE_MAX / sizeof(Py_ssize_t))
        return PyErr_NoMemory();

    Py_ssize_t *rows = output_count != 0
        ? PyMem_Malloc((size_t)output_count * sizeof(*rows)) : NULL;
    Py_ssize_t *output_slots = output_count != 0
        ? PyMem_Malloc((size_t)output_count * sizeof(*output_slots)) : NULL;
    Py_ssize_t hash_capacity = 1;
    while (hash_capacity < output_count * 2)
        hash_capacity *= 2;
    if ((size_t)hash_capacity > SIZE_MAX / sizeof(SeriesChartRowEntry)) {
        PyMem_Free(rows);
        PyMem_Free(output_slots);
        return PyErr_NoMemory();
    }
    SeriesChartRowEntry *row_table = PyMem_Malloc(
        (size_t)hash_capacity * sizeof(*row_table));
    if ((output_count != 0 && (rows == NULL || output_slots == NULL)) ||
        row_table == NULL) {
        PyMem_Free(rows);
        PyMem_Free(output_slots);
        PyMem_Free(row_table);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t index = 0; index < hash_capacity; index++)
        row_table[index].row = -1;

    PyObject **downsample_items = PySequence_Fast_ITEMS(downsample);
    PyObject **full_items = PySequence_Fast_ITEMS(full);
    Py_ssize_t unique_count = 0;
    for (Py_ssize_t output = 0; output < output_count; output++) {
        PyObject *item = output < downsample_count
            ? downsample_items[output] : full_items[output - downsample_count];
        Py_ssize_t row = PyLong_AsSsize_t(item);
        if (row == -1 && PyErr_Occurred()) goto plan_error;
        if (row < 0 || row >= row_count) {
            PyErr_Format(PyExc_IndexError,
                         "series chart row %zd is outside 0..%zd",
                         row, row_count - 1);
            goto plan_error;
        }
        size_t hash = (size_t)row * (size_t)2654435761U;
        Py_ssize_t entry = (Py_ssize_t)(hash & (size_t)(hash_capacity - 1));
        while (row_table[entry].row != -1 && row_table[entry].row != row)
            entry = (entry + 1) & (hash_capacity - 1);
        if (row_table[entry].row == -1) {
            row_table[entry] = (SeriesChartRowEntry){row, unique_count};
            rows[unique_count++] = row;
        }
        output_slots[output] = row_table[entry].slot;
    }
    PyMem_Free(row_table);
    row_table = NULL;

    if (bucket_count != 0 &&
        (size_t)unique_count > SIZE_MAX / (size_t)bucket_count) {
        PyErr_NoMemory();
        goto plan_error;
    }
    size_t cell_count = (size_t)unique_count * (size_t)bucket_count;
    size_t bucket_hash_capacity = 1;
    size_t bucket_hash_target = (size_t)bucket_count * 2;
    while (bucket_hash_capacity < bucket_hash_target) {
        if (bucket_hash_capacity > SIZE_MAX / 2) {
            PyErr_NoMemory();
            goto plan_error;
        }
        bucket_hash_capacity *= 2;
    }
    if (cell_count > SIZE_MAX / sizeof(double) ||
        (size_t)bucket_count > SIZE_MAX / 21 ||
        bucket_hash_capacity > SIZE_MAX / sizeof(SeriesChartBucketEntry)) {
        PyErr_NoMemory();
        goto plan_error;
    }
    double *values = cell_count != 0
        ? PyMem_Malloc(cell_count * sizeof(*values)) : NULL;
    unsigned char *present = cell_count != 0
        ? PyMem_Malloc(cell_count) : NULL;
    Py_ssize_t *next_slot = unique_count != 0
        ? PyMem_Malloc((size_t)unique_count * sizeof(*next_slot)) : NULL;
    size_t *cell_offsets = unique_count != 0
        ? PyMem_Malloc((size_t)unique_count * sizeof(*cell_offsets)) : NULL;
    unsigned char *index_plan = bucket_count != 0
        ? PyMem_Malloc((size_t)bucket_count * 21) : NULL;
    SeriesChartBucketEntry *bucket_table = PyMem_Calloc(
        bucket_hash_capacity, sizeof(*bucket_table));
    Py_ssize_t *series_head = series_count != 0
        ? PyMem_Malloc((size_t)series_count * sizeof(*series_head)) : NULL;
    if ((cell_count != 0 && (values == NULL || present == NULL)) ||
        (unique_count != 0 && (next_slot == NULL || cell_offsets == NULL)) ||
        (bucket_count != 0 && index_plan == NULL) ||
        bucket_table == NULL ||
        (series_count != 0 && series_head == NULL)) {
        PyMem_Free(values);
        PyMem_Free(present);
        PyMem_Free(next_slot);
        PyMem_Free(cell_offsets);
        PyMem_Free(index_plan);
        PyMem_Free(bucket_table);
        PyMem_Free(series_head);
        PyErr_NoMemory();
        goto plan_error;
    }
    for (Py_ssize_t series = 0; series < series_count; series++)
        series_head[series] = -1;
    for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
        unsigned char *planned = index_plan + (size_t)bucket * 21;
        planned[0] = (unsigned char)series_format_index(
            (char *)planned + 1, bucket);
    }
    for (Py_ssize_t slot = 0; slot < unique_count; slot++) {
        Py_ssize_t series = rows[slot] / measure_count;
        next_slot[slot] = series_head[series];
        series_head[series] = slot;
        rows[slot] %= measure_count;
        cell_offsets[slot] = (size_t)slot * (size_t)bucket_count;
        SeriesMeasure *measure = &measures[rows[slot]];
        double empty = 0.0;
        unsigned char has_empty = measure->empty != Py_None;
        if (has_empty) {
            empty = series_as_double(measure->empty);
            if (PyErr_Occurred() || !isfinite(empty)) {
                if (!PyErr_Occurred()) PyErr_SetString(
                    PyExc_ValueError, "series chart fill value must be finite");
                goto data_error;
            }
        }
        double *slot_values = bucket_count != 0
            ? values + cell_offsets[slot] : NULL;
        unsigned char *slot_present = bucket_count != 0
            ? present + cell_offsets[slot] : NULL;
        for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
            slot_values[bucket] = empty;
            slot_present[bucket] = has_empty;
        }
    }

    size_t bucket_cache_count = 0;
    for (Py_ssize_t series = 0; series < series_count; series++) {
        if (series_head[series] < 0) continue;
        Py_ssize_t position = 0;
        PyObject *bucket, *bucket_values;
        while (PyDict_Next(
                series_maps[series], &position, &bucket, &bucket_values)) {
            Py_ssize_t bucket_index;
            size_t hash = (size_t)(uintptr_t)bucket;
            hash ^= hash >> 17;
            hash *= (size_t)UINT64_C(0x9e3779b97f4a7c15);
            size_t entry = hash & (bucket_hash_capacity - 1);
            while (bucket_table[entry].key != NULL &&
                   bucket_table[entry].key != bucket)
                entry = (entry + 1) & (bucket_hash_capacity - 1);
            int matched;
            if (bucket_table[entry].key == bucket) {
                bucket_index = bucket_table[entry].index;
                matched = bucket_index >= 0;
            }
            else {
                matched = series_chart_spine_index(
                    spine, bucket, bucket_count, &bucket_index);
                if (matched >= 0 &&
                    bucket_cache_count < bucket_hash_capacity / 2) {
                    bucket_table[entry].key = bucket;
                    bucket_table[entry].index = matched ? bucket_index : -1;
                    bucket_cache_count++;
                }
            }
            if (matched < 0) goto data_error;
            if (!matched) continue;
            if (!PyDict_Check(bucket_values)) {
                PyErr_Format(
                    PyExc_TypeError,
                    "series bucket %R values must be a measure dict, got %.200s",
                    bucket, Py_TYPE(bucket_values)->tp_name);
                goto data_error;
            }
            for (Py_ssize_t slot = series_head[series]; slot >= 0;
                 slot = next_slot[slot]) {
                SeriesMeasure *measure = &measures[rows[slot]];
                PyObject *value = NULL;
                int measured = PyDict_GetItemRef(
                    bucket_values, measure->name, &value);
                if (measured < 0) goto data_error;
                if (value == NULL || value == Py_None) {
                    Py_XDECREF(value);
                    continue;
                }
                double number = series_as_double(value);
                Py_DECREF(value);
                if (PyErr_Occurred() || !isfinite(number)) {
                    if (!PyErr_Occurred()) PyErr_Format(
                        PyExc_ValueError,
                        "series chart value at bucket %zd must be finite",
                        bucket_index);
                    goto data_error;
                }
                size_t cell = cell_offsets[slot] + (size_t)bucket_index;
                values[cell] = number;
                present[cell] = 1;
            }
        }
    }

    PyObject *paths = PyTuple_New(output_count);
    PyObject *ticks = PyTuple_New(downsample_count);
    if (paths == NULL || ticks == NULL) {
        Py_XDECREF(paths);
        Py_XDECREF(ticks);
        goto data_error;
    }
    for (Py_ssize_t output = 0; output < downsample_count; output++) {
        Py_ssize_t slot = output_slots[output];
        double *slot_values = bucket_count != 0
            ? values + cell_offsets[slot] : NULL;
        unsigned char *slot_present = bucket_count != 0
            ? present + cell_offsets[slot] : NULL;
        double minimum, maximum;
        Py_ssize_t selected_count = 0;
        Py_ssize_t *selected = series_chart_lttb(
            slot_values, NULL, NULL, bucket_count, threshold, NULL, &selected_count,
            &minimum, &maximum);
        if (selected == NULL && bucket_count != 0) goto output_error;
        PyObject *path = series_chart_path(
            slot_values, slot_present, bucket_count, selected, selected_count,
            index_plan, NULL, NULL, NULL);
        PyMem_Free(selected);
        PyObject *axis = series_chart_ticks(minimum, maximum, tick_target);
        if (path == NULL || axis == NULL) {
            Py_XDECREF(path);
            Py_XDECREF(axis);
            goto output_error;
        }
        PyTuple_SET_ITEM(paths, output, path);
        PyTuple_SET_ITEM(ticks, output, axis);
    }
    for (Py_ssize_t output = 0; output < full_count; output++) {
        Py_ssize_t slot = output_slots[downsample_count + output];
        double *slot_values = bucket_count != 0
            ? values + cell_offsets[slot] : NULL;
        unsigned char *slot_present = bucket_count != 0
            ? present + cell_offsets[slot] : NULL;
        PyObject *path = series_chart_path(
            slot_values, slot_present, bucket_count, NULL, 0, index_plan,
            NULL, NULL, NULL);
        if (path == NULL) goto output_error;
        PyTuple_SET_ITEM(paths, downsample_count + output, path);
    }
    PyObject *result = Py_BuildValue("nOOO", row_count, keys, paths, ticks);
    Py_DECREF(paths);
    Py_DECREF(ticks);
    PyMem_Free(values);
    PyMem_Free(present);
    PyMem_Free(next_slot);
    PyMem_Free(cell_offsets);
    PyMem_Free(index_plan);
    PyMem_Free(bucket_table);
    PyMem_Free(series_head);
    PyMem_Free(rows);
    PyMem_Free(output_slots);
    return result;

output_error:
    Py_DECREF(paths);
    Py_DECREF(ticks);
data_error:
    PyMem_Free(values);
    PyMem_Free(present);
    PyMem_Free(next_slot);
    PyMem_Free(cell_offsets);
    PyMem_Free(index_plan);
    PyMem_Free(bucket_table);
    PyMem_Free(series_head);
plan_error:
    PyMem_Free(row_table);
    PyMem_Free(rows);
    PyMem_Free(output_slots);
    return NULL;
}

static PyObject *
series_chart(PyObject *args, int tick_text)
{
    PyObject *bucket_source, *sparse, *fills, *downsample_source, *full_source;
    Py_ssize_t threshold, tick_target;
    if (!PyArg_ParseTuple(
            args, "OOOOOnn:series_chart", &bucket_source, &sparse, &fills,
            &downsample_source, &full_source, &threshold, &tick_target)) return NULL;
    if (!PyDict_Check(sparse) || !PyDict_Check(fills)) {
        PyErr_SetString(PyExc_TypeError, "series chart sparse values and fills must be dicts");
        return NULL;
    }
    if (threshold < 3 || tick_target < 2) {
        PyErr_SetString(PyExc_ValueError, "series chart threshold must be >= 3 and tick target >= 2");
        return NULL;
    }
    PyObject *buckets = PySequence_Fast(
        bucket_source, "series chart buckets must be an iterable dense run");
    PyObject *downsample = PySequence_Fast(
        downsample_source, "series chart downsample rows must be an iterable of indices");
    PyObject *full = PySequence_Fast(
        full_source, "series chart full rows must be an iterable of indices");
    if (buckets == NULL || downsample == NULL || full == NULL) goto error;
    Py_ssize_t bucket_count = PySequence_Fast_GET_SIZE(buckets);
    Py_ssize_t series_count = PyDict_GET_SIZE(sparse);
    Py_ssize_t measure_count = PyDict_GET_SIZE(fills);
    if (measure_count == 0 || series_count > PY_SSIZE_T_MAX / measure_count) {
        if (measure_count == 0)
            PyErr_SetString(PyExc_ValueError, "series chart needs at least one measure");
        else PyErr_NoMemory();
        goto error;
    }
    if ((size_t)measure_count > SIZE_MAX / sizeof(SeriesMeasure) ||
        (size_t)series_count > SIZE_MAX / sizeof(PyObject *) ||
        (size_t)bucket_count > SIZE_MAX / sizeof(Py_hash_t) ||
        (size_t)measure_count > SIZE_MAX / sizeof(Py_hash_t)) {
        PyErr_NoMemory();
        goto error;
    }
    SeriesMeasure *measures = PyMem_Malloc((size_t)measure_count * sizeof(*measures));
    PyObject **series_maps = PyMem_Malloc((size_t)series_count * sizeof(*series_maps));
    PyObject *keys = PyTuple_New(series_count);
    if (measures == NULL || series_maps == NULL || keys == NULL) {
        PyMem_Free(measures);
        PyMem_Free(series_maps);
        Py_XDECREF(keys);
        PyErr_NoMemory();
        goto error;
    }
    Py_ssize_t position = 0, index = 0;
    PyObject *name, *empty;
    while (PyDict_Next(fills, &position, &name, &empty))
        measures[index++] = (SeriesMeasure){name, empty};
    position = 0;
    PyObject *key, *mapping;
    index = 0;
    while (PyDict_Next(sparse, &position, &key, &mapping)) {
        if (!PyDict_Check(mapping)) {
            PyErr_Format(PyExc_TypeError, "series %R values must be a bucket dict", key);
            PyMem_Free(measures);
            PyMem_Free(series_maps);
            Py_DECREF(keys);
            goto error;
        }
        series_maps[index] = mapping;
        PyTuple_SET_ITEM(keys, index++, Py_NewRef(key));
    }
    PyObject **bucket_items = PySequence_Fast_ITEMS(buckets);
    Py_hash_t *bucket_hashes = bucket_count != 0
        ? PyMem_Malloc((size_t)bucket_count * sizeof(*bucket_hashes)) : NULL;
    Py_hash_t *measure_hashes = PyMem_Malloc(
        (size_t)measure_count * sizeof(*measure_hashes));
    if ((bucket_count != 0 && bucket_hashes == NULL) ||
        measure_hashes == NULL) {
        PyMem_Free(bucket_hashes);
        PyMem_Free(measure_hashes);
        PyMem_Free(measures);
        PyMem_Free(series_maps);
        Py_DECREF(keys);
        PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
        bucket_hashes[bucket] = PyObject_Hash(bucket_items[bucket]);
        if (bucket_hashes[bucket] == -1 && PyErr_Occurred()) {
            PyMem_Free(bucket_hashes);
            PyMem_Free(measure_hashes);
            PyMem_Free(measures);
            PyMem_Free(series_maps);
            Py_DECREF(keys);
            goto error;
        }
    }
    for (Py_ssize_t measure = 0; measure < measure_count; measure++) {
        measure_hashes[measure] = PyObject_Hash(measures[measure].name);
        if (measure_hashes[measure] == -1 && PyErr_Occurred()) {
            PyMem_Free(bucket_hashes);
            PyMem_Free(measure_hashes);
            PyMem_Free(measures);
            PyMem_Free(series_maps);
            Py_DECREF(keys);
            goto error;
        }
    }
    /* Each bucket and measure is looked up for several selected rows. Hash
     * each once for this projection and keep the compact plan request-owned. */
    SeriesChartDense dense = {bucket_items, bucket_hashes, measure_hashes};
    PyObject *result = series_chart_project(
        bucket_count, series_count, measure_count, measures, series_maps,
        keys, downsample, full, threshold, tick_target,
        series_chart_fill_dense, &dense, tick_text);
    PyMem_Free(bucket_hashes);
    PyMem_Free(measure_hashes);
    PyMem_Free(measures);
    PyMem_Free(series_maps);
    Py_DECREF(keys);
    Py_DECREF(buckets);
    Py_DECREF(downsample);
    Py_DECREF(full);
    return result;
error:
    Py_XDECREF(buckets);
    Py_XDECREF(downsample);
    Py_XDECREF(full);
    return NULL;
}

PyObject *
wreath_series_chart(PyObject *Py_UNUSED(self), PyObject *args)
{
    return series_chart(args, 0);
}

PyObject *
wreath_series_chart_text(PyObject *Py_UNUSED(self), PyObject *args)
{
    return series_chart(args, 1);
}

PyObject *
wreath_series_chart_spine(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *start, *end, *tz, *sparse, *fills, *capsule = NULL;
    PyObject *downsample_source, *full_source;
    int unit;
    Py_ssize_t threshold, tick_target;
    if (!PyArg_ParseTuple(
            args, "OOiOOOOOnn|O:series_chart_spine",
            &start, &end, &unit, &tz, &sparse, &fills,
            &downsample_source, &full_source, &threshold, &tick_target,
            &capsule))
        return NULL;
    PyDateTime_CAPI *api = series_datetime_api(capsule);
    if (api == NULL) return NULL;
    if (!PyObject_TypeCheck(start, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(start)) {
        PyErr_SetString(
            PyExc_TypeError,
            "series chart spine start must be an offset-aware datetime");
        return NULL;
    }
    if (!PyObject_TypeCheck(end, api->DateTimeType) ||
        !_PyDateTime_HAS_TZINFO(end)) {
        PyErr_SetString(
            PyExc_TypeError,
            "series chart spine end must be an offset-aware datetime");
        return NULL;
    }
    if (unit < 0 || unit > 6) {
        PyErr_SetString(
            PyExc_ValueError, "series chart spine unit must be in range 0..6");
        return NULL;
    }
    if (!PyDict_Check(sparse) || !PyDict_Check(fills)) {
        PyErr_SetString(
            PyExc_TypeError,
            "series chart sparse values and fills must be dicts");
        return NULL;
    }
    if (threshold < 3 || tick_target < 2 || tick_target > 10000) {
        PyErr_SetString(
            PyExc_ValueError,
            "series chart threshold must be >= 3 and tick target in 2..10000");
        return NULL;
    }

    PyObject *offset_method = PyObject_GetAttr(tz, series_name_utcoffset);
    PyObject *local_start = series_astimezone(start, tz);
    PyObject *local_end = series_astimezone(end, tz);
    PyObject *downsample = PySequence_Fast(
        downsample_source,
        "series chart downsample rows must be an iterable of indices");
    PyObject *full = PySequence_Fast(
        full_source, "series chart full rows must be an iterable of indices");
    if (offset_method == NULL || local_start == NULL || local_end == NULL ||
        downsample == NULL || full == NULL) goto early_error;
    SeriesChartSpine spine = {
        .start = {
            PyDateTime_GET_YEAR(local_start), PyDateTime_GET_MONTH(local_start),
            PyDateTime_GET_DAY(local_start), PyDateTime_DATE_GET_HOUR(local_start),
            PyDateTime_DATE_GET_MINUTE(local_start),
            PyDateTime_DATE_GET_SECOND(local_start),
            PyDateTime_DATE_GET_MICROSECOND(local_start),
        },
        .unit = unit,
        .tz = tz,
        .offset_method = offset_method,
        .result_type = Py_TYPE(start),
        .api = api,
    };
    SeriesWallClock end_wall = {
        PyDateTime_GET_YEAR(local_end), PyDateTime_GET_MONTH(local_end),
        PyDateTime_GET_DAY(local_end), PyDateTime_DATE_GET_HOUR(local_end),
        PyDateTime_DATE_GET_MINUTE(local_end), PyDateTime_DATE_GET_SECOND(local_end),
        PyDateTime_DATE_GET_MICROSECOND(local_end),
    };
    Py_DECREF(local_start);
    local_start = NULL;
    Py_DECREF(local_end);
    local_end = NULL;
    series_wall_truncate(&spine.start, unit);
    spine.start_ordinal = series_wall_ordinal(&spine.start);
    SeriesWallClock wall = spine.start;
    SeriesWallClock last = wall;
    Py_ssize_t bucket_count = 0;
    while (series_wall_compare(&wall, &end_wall) < 0) {
        if (bucket_count == PY_SSIZE_T_MAX) {
            PyErr_NoMemory();
            goto early_error;
        }
        last = wall;
        bucket_count++;
        if (series_wall_advance(&wall, unit) < 0) goto early_error;
        if ((bucket_count & 4095) == 0 && PyErr_CheckSignals() < 0)
            goto early_error;
    }
    if (bucket_count != 0) {
        PyObject *candidate = series_wall_in_zone(
            &last, tz, offset_method, Py_TYPE(start), api);
        if (candidate == NULL) goto early_error;
        int before_end = PyObject_RichCompareBool(candidate, end, Py_LT);
        Py_DECREF(candidate);
        if (before_end < 0) goto early_error;
        if (!before_end) bucket_count--;
    }

    Py_ssize_t series_count = PyDict_GET_SIZE(sparse);
    Py_ssize_t measure_count = PyDict_GET_SIZE(fills);
    if (measure_count == 0 || series_count > PY_SSIZE_T_MAX / measure_count ||
        (size_t)measure_count > SIZE_MAX / sizeof(SeriesMeasure) ||
        (size_t)series_count > SIZE_MAX / sizeof(PyObject *)) {
        if (measure_count == 0)
            PyErr_SetString(
                PyExc_ValueError, "series chart needs at least one measure");
        else PyErr_NoMemory();
        goto early_error;
    }
    SeriesMeasure *measures = PyMem_Malloc(
        (size_t)measure_count * sizeof(*measures));
    PyObject **series_maps = PyMem_Malloc(
        (size_t)series_count * sizeof(*series_maps));
    PyObject *keys = PyTuple_New(series_count);
    if (measures == NULL || series_maps == NULL || keys == NULL) {
        PyMem_Free(measures);
        PyMem_Free(series_maps);
        Py_XDECREF(keys);
        PyErr_NoMemory();
        goto early_error;
    }
    Py_ssize_t position = 0, index = 0;
    PyObject *name, *empty;
    while (PyDict_Next(fills, &position, &name, &empty))
        measures[index++] = (SeriesMeasure){name, empty};
    position = 0;
    PyObject *key, *mapping;
    index = 0;
    while (PyDict_Next(sparse, &position, &key, &mapping)) {
        if (!PyDict_Check(mapping)) {
            PyErr_Format(
                PyExc_TypeError,
                "series %R values must be a bucket dict", key);
            PyMem_Free(measures);
            PyMem_Free(series_maps);
            Py_DECREF(keys);
            goto early_error;
        }
        series_maps[index] = mapping;
        PyTuple_SET_ITEM(keys, index++, Py_NewRef(key));
    }
    PyObject *result = series_chart_project_spine(
        bucket_count, series_count, measure_count, measures, series_maps,
        keys, downsample, full, threshold, tick_target, &spine);
    PyMem_Free(measures);
    PyMem_Free(series_maps);
    Py_DECREF(keys);
    Py_DECREF(offset_method);
    Py_DECREF(downsample);
    Py_DECREF(full);
    return result;

early_error:
    Py_XDECREF(offset_method);
    Py_XDECREF(local_start);
    Py_XDECREF(local_end);
    Py_XDECREF(downsample);
    Py_XDECREF(full);
    return NULL;
}

/* Dense/sparse reconciliation is deliberately a data kernel.  It knows no
 * declaration, model, driver row or SQL shape: callers hand it an ordered
 * bucket run, a sparse map keyed by series identity, and an ordered per-measure
 * fill map.  The returned rows are (series_key, measure_name, dense_values).
 * Python materialisation happens only once, at that public result boundary. */
PyObject *
wreath_series_reconcile(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *bucket_source, *sparse, *fills;
    if (!PyArg_ParseTuple(
            args, "OOO:series_reconcile", &bucket_source, &sparse, &fills))
        return NULL;
    if (!PyDict_Check(sparse)) {
        PyErr_Format(
            PyExc_TypeError,
            "series sparse values must be a dict keyed by (key, other), got %.200s",
            Py_TYPE(sparse)->tp_name);
        return NULL;
    }
    if (!PyDict_Check(fills)) {
        PyErr_Format(
            PyExc_TypeError,
            "series fills must be an ordered dict of measure: fill, got %.200s",
            Py_TYPE(fills)->tp_name);
        return NULL;
    }
    PyObject *buckets = PySequence_Fast(
        bucket_source, "series buckets must be an iterable dense bucket run");
    if (buckets == NULL) return NULL;

    Py_ssize_t series_count = PyDict_GET_SIZE(sparse);
    Py_ssize_t measure_count = PyDict_GET_SIZE(fills);
    if (measure_count != 0 && series_count > PY_SSIZE_T_MAX / measure_count) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    if ((size_t)measure_count > SIZE_MAX / sizeof(SeriesMeasure)) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    SeriesMeasure *measures = measure_count != 0
        ? PyMem_Malloc((size_t)measure_count * sizeof(*measures)) : NULL;
    if (measure_count != 0 && measures == NULL) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    Py_ssize_t fill_position = 0, measure_index = 0;
    PyObject *measure, *empty;
    while (PyDict_Next(fills, &fill_position, &measure, &empty))
        measures[measure_index++] = (SeriesMeasure){measure, empty};

    PyObject *result = PyTuple_New(series_count * measure_count);
    if (result == NULL) {
        PyMem_Free(measures);
        Py_DECREF(buckets);
        return NULL;
    }

    Py_ssize_t bucket_count = PySequence_Fast_GET_SIZE(buckets);
    PyObject **bucket_items = PySequence_Fast_ITEMS(buckets);
    Py_ssize_t output_index = 0;
    Py_ssize_t sparse_position = 0;
    PyObject *series_key, *by_bucket;
    while (PyDict_Next(
            sparse, &sparse_position, &series_key, &by_bucket)) {
        if (!PyDict_Check(by_bucket)) {
            PyErr_Format(
                PyExc_TypeError,
                "series %R values must be a dict keyed by bucket, got %.200s",
                series_key, Py_TYPE(by_bucket)->tp_name);
            goto error;
        }
        if ((size_t)measure_count > SIZE_MAX / sizeof(PyObject *)) {
            PyErr_NoMemory();
            goto error;
        }
        PyObject **dense_values = measure_count != 0
            ? PyMem_Calloc((size_t)measure_count, sizeof(*dense_values)) : NULL;
        PyObject *row = NULL;
        if (measure_count != 0 && dense_values == NULL) {
            PyErr_NoMemory();
            goto error;
        }
        for (Py_ssize_t index = 0; index < measure_count; index++) {
            dense_values[index] = PyTuple_New(bucket_count);
            if (dense_values[index] == NULL) goto series_error;
        }
        for (Py_ssize_t bucket_index = 0; bucket_index < bucket_count; bucket_index++) {
            int found = PyDict_GetItemRef(
                by_bucket, bucket_items[bucket_index], &row);
            if (found < 0) goto series_error;
            if (found != 0 && !PyDict_Check(row)) {
                PyErr_Format(
                    PyExc_TypeError,
                    "series bucket %R values must be a measure dict, got %.200s",
                    bucket_items[bucket_index], Py_TYPE(row)->tp_name);
                goto series_error;
            }
            for (Py_ssize_t index = 0; index < measure_count; index++) {
                PyObject *value = NULL;
                if (found != 0) {
                    int measured = PyDict_GetItemRef(
                        row, measures[index].name, &value);
                    if (measured < 0) goto series_error;
                }
                if (value == NULL || value == Py_None) {
                    Py_XDECREF(value);
                    value = Py_NewRef(measures[index].empty);
                }
                PyTuple_SET_ITEM(dense_values[index], bucket_index, value);
            }
            Py_XDECREF(row);
            row = NULL;
        }
        for (Py_ssize_t index = 0; index < measure_count; index++) {
            PyObject *dense = PyTuple_New(3);
            if (dense == NULL) goto series_error;
            PyTuple_SET_ITEM(dense, 0, Py_NewRef(series_key));
            PyTuple_SET_ITEM(dense, 1, Py_NewRef(measures[index].name));
            PyTuple_SET_ITEM(dense, 2, dense_values[index]);
            dense_values[index] = NULL;
            PyTuple_SET_ITEM(result, output_index++, dense);
        }
        PyMem_Free(dense_values);
        if ((output_index & 63) == 0 && PyErr_CheckSignals() < 0) goto error;
        continue;

series_error:
        Py_XDECREF(row);
        for (Py_ssize_t index = 0; index < measure_count; index++)
            Py_XDECREF(dense_values[index]);
        PyMem_Free(dense_values);
        goto error;
    }
    PyMem_Free(measures);
    Py_DECREF(buckets);
    return result;

error:
    PyMem_Free(measures);
    Py_DECREF(buckets);
    Py_DECREF(result);
    return NULL;
}

/* Materialize the database-neutral dense-cell result at its Python boundary.
 * The declaration has already become the two data arrays `names` and `fills`;
 * no ORM, SQL, column or measure object crosses this entry point. */
PyObject *
wreath_series_cell_rows(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *row_source, *name_source, *fill_source;
    PyObject *rows = NULL, *names = NULL, *fills = NULL, *result = NULL;
    if (!PyArg_ParseTuple(args, "OOO:series_cell_rows",
                          &row_source, &name_source, &fill_source)) return NULL;
    rows = PySequence_Fast(row_source, "cell rows must be a sequence");
    names = PySequence_Fast(name_source, "measure names must be a sequence");
    fills = PySequence_Fast(fill_source, "measure fills must be a sequence");
    if (rows == NULL || names == NULL || fills == NULL) goto error;
    Py_ssize_t measure_count = PySequence_Fast_GET_SIZE(names);
    if (PySequence_Fast_GET_SIZE(fills) != measure_count) {
        PyErr_SetString(PyExc_ValueError,
                        "measure names and fills must have the same length");
        goto error;
    }
    Py_ssize_t row_count = PySequence_Fast_GET_SIZE(rows);
    result = PyList_New(row_count);
    if (result == NULL) goto error;
    PyObject **row_items = PySequence_Fast_ITEMS(rows);
    PyObject **name_items = PySequence_Fast_ITEMS(names);
    PyObject **fill_items = PySequence_Fast_ITEMS(fills);
    for (Py_ssize_t row_index = 0; row_index < row_count; row_index++) {
        PyObject *row = PySequence_Fast(
            row_items[row_index], "each cell row must be a sequence");
        if (row == NULL) goto error;
        if (PySequence_Fast_GET_SIZE(row) < measure_count + 2) {
            PyErr_Format(PyExc_ValueError,
                         "cell row %zd has fewer than %zd columns",
                         row_index, measure_count + 2);
            Py_DECREF(row);
            goto error;
        }
        PyObject **items = PySequence_Fast_ITEMS(row);
        PyObject *row_number = PyNumber_Long(items[0]);
        PyObject *column_number = PyNumber_Long(items[1]);
        PyObject *values = _PyDict_NewPresized(measure_count);
        if (row_number == NULL || column_number == NULL || values == NULL) {
            Py_XDECREF(row_number);
            Py_XDECREF(column_number);
            Py_XDECREF(values);
            Py_DECREF(row);
            goto error;
        }
        for (Py_ssize_t measure = 0; measure < measure_count; measure++) {
            PyObject *found = items[measure + 2];
            if (PyDict_SetItem(values, name_items[measure],
                               found == Py_None ? fill_items[measure] : found) < 0) {
                Py_DECREF(row_number);
                Py_DECREF(column_number);
                Py_DECREF(values);
                Py_DECREF(row);
                goto error;
            }
        }
        PyObject *cell = PyTuple_New(3);
        if (cell == NULL) {
            Py_DECREF(row_number);
            Py_DECREF(column_number);
            Py_DECREF(values);
            Py_DECREF(row);
            goto error;
        }
        PyTuple_SET_ITEM(cell, 0, row_number);
        PyTuple_SET_ITEM(cell, 1, column_number);
        PyTuple_SET_ITEM(cell, 2, values);
        PyList_SET_ITEM(result, row_index, cell);
        Py_DECREF(row);
    }
    Py_DECREF(rows);
    Py_DECREF(names);
    Py_DECREF(fills);
    return result;

error:
    Py_XDECREF(result);
    Py_XDECREF(rows);
    Py_XDECREF(names);
    Py_XDECREF(fills);
    return NULL;
}

/* The open-addressed tables are walked twice for every result row.  NULL keys
 * already identify empty slots, and folding `other` into the stored hash keeps
 * that discriminator out of the probe record.  Three-word slots fit eight
 * entries in three 64-byte cache lines instead of four; the 28--4096 bucket
 * counter sweep also retired 3.5--4.6% fewer instructions, so this is locality
 * gained by deleting probe work rather than by adding prefetch machinery. */
typedef struct {
    Py_hash_t hash;
    PyObject *key;
    Py_ssize_t index;
} SeriesDenseIndex;

typedef struct {
    PyObject *key;
    unsigned char other;
} SeriesDenseIdentity;

typedef struct {
    PyObject **buckets;
    Py_ssize_t bucket_count;
    Py_ssize_t bucket_capacity;
    SeriesDenseIndex *bucket_index;
    size_t bucket_index_size;
    SeriesDenseIdentity *series;
    Py_ssize_t series_count;
    Py_ssize_t series_capacity;
    SeriesDenseIndex *series_index;
    size_t series_index_size;
    PyObject **cells;
    size_t cell_count;
} SeriesDensePeriod;

static void
series_dense_period_clear(SeriesDensePeriod *period)
{
    for (Py_ssize_t index = 0; index < period->bucket_count; index++)
        Py_XDECREF(period->buckets[index]);
    for (Py_ssize_t index = 0; index < period->series_count; index++)
        Py_XDECREF(period->series[index].key);
    PyMem_Free(period->buckets);
    PyMem_Free(period->bucket_index);
    PyMem_Free(period->series);
    PyMem_Free(period->series_index);
    for (size_t index = 0; index < period->cell_count; index++)
        Py_XDECREF(period->cells[index]);
    PyMem_Free(period->cells);
    *period = (SeriesDensePeriod){0};
}

static size_t
series_dense_slot(Py_hash_t hash, size_t mask)
{
    return ((size_t)hash ^ ((size_t)hash >> 16)) & mask;
}

static int
series_dense_index_resize(
    SeriesDenseIndex **table_pointer, size_t *size_pointer,
    size_t minimum)
{
    size_t size = *size_pointer == 0 ? 16 : *size_pointer;
    while (size < minimum) {
        if (size > SIZE_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        size *= 2;
    }
    if (size > SIZE_MAX / sizeof(SeriesDenseIndex)) {
        PyErr_NoMemory();
        return -1;
    }
    SeriesDenseIndex *table = PyMem_Calloc(size, sizeof(*table));
    if (table == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    SeriesDenseIndex *old = *table_pointer;
    size_t old_size = *size_pointer;
    for (size_t index = 0; index < old_size; index++) {
        if (old[index].key == NULL) continue;
        Py_hash_t hash = old[index].hash;
        size_t slot = series_dense_slot(hash, size - 1);
        while (table[slot].key != NULL) slot = (slot + 1) & (size - 1);
        table[slot] = old[index];
    }
    PyMem_Free(old);
    *table_pointer = table;
    *size_pointer = size;
    return 0;
}

static int
series_dense_equal(PyObject *left, PyObject *right)
{
    return left == right ? 1 : PyObject_RichCompareBool(left, right, Py_EQ);
}

static Py_ssize_t
series_dense_lookup(
    SeriesDenseIndex *table, size_t size, PyObject *key,
    Py_hash_t hash)
{
    size_t slot = series_dense_slot(hash, size - 1);
    for (;;) {
        SeriesDenseIndex *entry = &table[slot];
        if (entry->key == NULL) return -1;
        if (entry->hash == hash) {
            int equal = series_dense_equal(entry->key, key);
            if (equal < 0) return -2;
            if (equal) return entry->index;
        }
        slot = (slot + 1) & (size - 1);
    }
}

static int
series_dense_insert(
    SeriesDenseIndex *table, size_t size, PyObject *key,
    Py_hash_t hash, Py_ssize_t index)
{
    size_t slot = series_dense_slot(hash, size - 1);
    while (table[slot].key != NULL) slot = (slot + 1) & (size - 1);
    table[slot] = (SeriesDenseIndex){hash, key, index};
    return 0;
}

static Py_hash_t
series_dense_identity_hash(Py_hash_t hash, int other)
{
    return other ? hash ^ (Py_hash_t)UINT64_C(0x5bd1e9955bd1e995) : hash;
}

static int
series_dense_grow_objects(
    PyObject ***items_pointer, Py_ssize_t *capacity_pointer, Py_ssize_t count)
{
    if (count < *capacity_pointer) return 0;
    Py_ssize_t capacity = *capacity_pointer == 0 ? 16 : *capacity_pointer;
    if (capacity > PY_SSIZE_T_MAX / 2) {
        PyErr_NoMemory();
        return -1;
    }
    capacity *= 2;
    PyObject **items = PyMem_Realloc(
        *items_pointer, (size_t)capacity * sizeof(*items));
    if (items == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    *items_pointer = items;
    *capacity_pointer = capacity;
    return 0;
}

static int
series_dense_grow_series(SeriesDensePeriod *period)
{
    if (period->series_count < period->series_capacity) return 0;
    Py_ssize_t capacity = period->series_capacity == 0 ? 16 : period->series_capacity;
    if (capacity > PY_SSIZE_T_MAX / 2 ||
        (size_t)(capacity * 2) > SIZE_MAX / sizeof(*period->series)) {
        PyErr_NoMemory();
        return -1;
    }
    capacity *= 2;
    SeriesDenseIdentity *series = PyMem_Realloc(
        period->series, (size_t)capacity * sizeof(*series));
    if (series == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    period->series = series;
    period->series_capacity = capacity;
    return 0;
}

static Py_ssize_t
series_dense_bucket(SeriesDensePeriod *period, PyObject *bucket, int create)
{
    Py_hash_t hash = PyObject_Hash(bucket);
    if (hash == -1) return -2;
    if (period->bucket_index_size == 0 &&
        series_dense_index_resize(
            &period->bucket_index, &period->bucket_index_size, 16) < 0)
        return -2;
    Py_ssize_t found = series_dense_lookup(
        period->bucket_index, period->bucket_index_size, bucket, hash);
    if (found != -1 || !create) return found;
    if ((size_t)(period->bucket_count + 1) * 3 >=
        period->bucket_index_size * 2) {
        if (series_dense_index_resize(
                &period->bucket_index, &period->bucket_index_size,
                period->bucket_index_size * 2) < 0) return -2;
    }
    if (series_dense_grow_objects(
            &period->buckets, &period->bucket_capacity,
            period->bucket_count) < 0) return -2;
    Py_ssize_t index = period->bucket_count++;
    period->buckets[index] = Py_NewRef(bucket);
    series_dense_insert(
        period->bucket_index, period->bucket_index_size,
        period->buckets[index], hash, index);
    return index;
}

static Py_ssize_t
series_dense_series(
    SeriesDensePeriod *period, PyObject *key, int other, int create)
{
    Py_hash_t hash = PyObject_Hash(key);
    if (hash == -1) return -2;
    hash = series_dense_identity_hash(hash, other);
    if (period->series_index_size == 0 &&
        series_dense_index_resize(
            &period->series_index, &period->series_index_size, 16) < 0)
        return -2;
    Py_ssize_t found = series_dense_lookup(
        period->series_index, period->series_index_size, key, hash);
    if (found != -1 || !create) return found;
    if ((size_t)(period->series_count + 1) * 3 >=
        period->series_index_size * 2) {
        if (series_dense_index_resize(
                &period->series_index, &period->series_index_size,
                period->series_index_size * 2) < 0) return -2;
    }
    if (series_dense_grow_series(period) < 0) return -2;
    Py_ssize_t index = period->series_count++;
    period->series[index] = (SeriesDenseIdentity){
        Py_NewRef(key), (unsigned char)other,
    };
    series_dense_insert(
        period->series_index, period->series_index_size,
        period->series[index].key, hash, index);
    return index;
}

static int
series_dense_row_shape(
    PyObject *row, Py_ssize_t row_index, Py_ssize_t required, PyObject **fast_out)
{
    PyObject *fast = PySequence_Fast(
        row, "each series row must be a positional sequence");
    if (fast == NULL) return -1;
    Py_ssize_t size = PySequence_Fast_GET_SIZE(fast);
    if (size < required) {
        Py_DECREF(fast);
        PyErr_Format(
            PyExc_ValueError,
            "series row %zd has %zd columns; expected at least %zd in "
            "(bucket, [period], [key, other], measures...) form",
            row_index, size, required);
        return -1;
    }
    *fast_out = fast;
    return 0;
}

static int
series_dense_period_of(
    PyObject *period, PyObject **labels, Py_ssize_t label_count)
{
    if (label_count == 0) return 0;
    for (Py_ssize_t index = 0; index < label_count; index++) {
        int equal = series_dense_equal(period, labels[index]);
        if (equal < 0) return -1;
        if (equal) return (int)index;
    }
    PyErr_Format(
        PyExc_ValueError, "series row period %R is not one of the declared periods", period);
    return -1;
}

static int
series_dense_is_empty_group(PyObject **items, Py_ssize_t base,
                            Py_ssize_t measure_count, int other)
{
    if (items[base] != Py_None || other) return 0;
    for (Py_ssize_t index = 0; index < measure_count; index++)
        if (items[base + 2 + index] != Py_None) return 0;
    return 1;
}

static PyObject *
series_dense_materialize(
    SeriesDensePeriod *period, PyObject **measures, PyObject **fills,
    Py_ssize_t measure_count)
{
    PyObject *buckets = PyTuple_New(period->bucket_count);
    if (buckets == NULL) return NULL;
    for (Py_ssize_t index = 0; index < period->bucket_count; index++)
        PyTuple_SET_ITEM(buckets, index, Py_NewRef(period->buckets[index]));
    if (measure_count != 0 &&
        period->series_count > PY_SSIZE_T_MAX / measure_count) {
        Py_DECREF(buckets);
        return PyErr_NoMemory();
    }
    PyObject *dense = PyTuple_New(period->series_count * measure_count);
    if (dense == NULL) {
        Py_DECREF(buckets);
        return NULL;
    }
    Py_ssize_t output = 0;
    for (Py_ssize_t series = 0; series < period->series_count; series++) {
        PyObject *identity = PyTuple_New(2);
        if (identity == NULL) goto error;
        PyTuple_SET_ITEM(identity, 0, Py_NewRef(period->series[series].key));
        PyTuple_SET_ITEM(
            identity, 1, Py_NewRef(period->series[series].other ? Py_True : Py_False));
        for (Py_ssize_t measure = 0; measure < measure_count; measure++) {
            PyObject *values = PyTuple_New(period->bucket_count);
            PyObject *item = PyTuple_New(3);
            if (values == NULL || item == NULL) {
                Py_XDECREF(values);
                Py_XDECREF(item);
                Py_DECREF(identity);
                goto error;
            }
            for (Py_ssize_t bucket = 0; bucket < period->bucket_count; bucket++) {
                size_t cell = ((size_t)series * (size_t)measure_count +
                               (size_t)measure) * (size_t)period->bucket_count +
                              (size_t)bucket;
                PyObject *value = period->cells[cell];
                if (value == NULL || value == Py_None) value = fills[measure];
                PyTuple_SET_ITEM(values, bucket, Py_NewRef(value));
            }
            PyTuple_SET_ITEM(item, 0, Py_NewRef(identity));
            PyTuple_SET_ITEM(item, 1, Py_NewRef(measures[measure]));
            PyTuple_SET_ITEM(item, 2, values);
            PyTuple_SET_ITEM(dense, output++, item);
        }
        Py_DECREF(identity);
    }
    PyObject *result = PyTuple_New(2);
    if (result == NULL) goto error;
    PyTuple_SET_ITEM(result, 0, buckets);
    PyTuple_SET_ITEM(result, 1, dense);
    return result;

error:
    Py_DECREF(buckets);
    Py_DECREF(dense);
    return NULL;
}

/* Consume the positional data returned by a series backend and own the two hot
 * loops until their final Python boundary.  The kernel knows only the row
 * layout, ordered measure/fill arrays and optional period labels; declarations,
 * model types, ORM state and SQL never cross this entry point. */
PyObject *
wreath_series_dense_rows(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *row_source, *measure_source, *fill_source, *period_source;
    int grouped;
    if (!PyArg_ParseTuple(
            args, "OOOpO:series_dense_rows", &row_source, &measure_source,
            &fill_source, &grouped, &period_source)) return NULL;
    PyObject *rows = PySequence_Fast(
        row_source, "series rows must be a positional iterable");
    PyObject *measures = PySequence_Fast(
        measure_source, "series measures must be an ordered iterable");
    PyObject *fills = PySequence_Fast(
        fill_source, "series fills must be an ordered iterable");
    PyObject *periods = NULL;
    if (period_source != Py_None)
        periods = PySequence_Fast(
            period_source, "series periods must be an ordered iterable of labels");
    if (rows == NULL || measures == NULL || fills == NULL ||
        (period_source != Py_None && periods == NULL)) goto error;
    Py_ssize_t measure_count = PySequence_Fast_GET_SIZE(measures);
    if (PySequence_Fast_GET_SIZE(fills) != measure_count) {
        PyErr_Format(
            PyExc_ValueError,
            "series fills need one value per measure; got %zd measures and %zd fills",
            measure_count, PySequence_Fast_GET_SIZE(fills));
        goto error;
    }
    Py_ssize_t period_count = periods == NULL ? 0 : PySequence_Fast_GET_SIZE(periods);
    if (period_count > 8) {
        PyErr_Format(
            PyExc_ValueError, "series rows support at most 8 periods, got %zd",
            period_count);
        goto error;
    }
    Py_ssize_t state_count = period_count == 0 ? 1 : period_count;
    SeriesDensePeriod states[8] = {{0}};
    PyObject **labels = periods == NULL ? NULL : PySequence_Fast_ITEMS(periods);
    Py_ssize_t value_offset = 1 + (period_count != 0) + (grouped ? 2 : 0);
    Py_ssize_t required = value_offset + measure_count;
    Py_ssize_t row_count = PySequence_Fast_GET_SIZE(rows);
    PyObject **row_items = PySequence_Fast_ITEMS(rows);

    for (Py_ssize_t row_index = 0; row_index < row_count; row_index++) {
        PyObject *row = NULL;
        if (series_dense_row_shape(
                row_items[row_index], row_index, required, &row) < 0) goto states_error;
        PyObject **items = PySequence_Fast_ITEMS(row);
        int state_index = series_dense_period_of(
            period_count == 0 ? Py_None : items[1], labels, period_count);
        if (state_index < 0) {
            Py_DECREF(row);
            goto states_error;
        }
        SeriesDensePeriod *state = &states[state_index];
        if (series_dense_bucket(state, items[0], 1) < 0) {
            Py_DECREF(row);
            goto states_error;
        }
        Py_ssize_t base = 1 + (period_count != 0);
        PyObject *key = grouped ? items[base] : Py_None;
        int other = grouped ? PyObject_IsTrue(items[base + 1]) : 0;
        if (other < 0) {
            Py_DECREF(row);
            goto states_error;
        }
        if (!grouped || !series_dense_is_empty_group(
                items, base, measure_count, other)) {
            if (series_dense_series(state, key, other, 1) < 0) {
                Py_DECREF(row);
                goto states_error;
            }
        }
        Py_DECREF(row);
    }

    for (Py_ssize_t state_index = 0; state_index < state_count; state_index++) {
        SeriesDensePeriod *state = &states[state_index];
        size_t count = (size_t)state->series_count;
        if (measure_count != 0 && count > SIZE_MAX / (size_t)measure_count)
            goto memory_error;
        count *= (size_t)measure_count;
        if (state->bucket_count != 0 && count > SIZE_MAX / (size_t)state->bucket_count)
            goto memory_error;
        count *= (size_t)state->bucket_count;
        if (count > SIZE_MAX / sizeof(*state->cells)) goto memory_error;
        state->cell_count = count;
        state->cells = count == 0 ? NULL : PyMem_Calloc(count, sizeof(*state->cells));
        if (count != 0 && state->cells == NULL) goto memory_error;
    }

    for (Py_ssize_t row_index = 0; row_index < row_count; row_index++) {
        PyObject *row = NULL;
        if (series_dense_row_shape(
                row_items[row_index], row_index, required, &row) < 0) goto states_error;
        PyObject **items = PySequence_Fast_ITEMS(row);
        int state_index = series_dense_period_of(
            period_count == 0 ? Py_None : items[1], labels, period_count);
        if (state_index < 0) {
            Py_DECREF(row);
            goto states_error;
        }
        SeriesDensePeriod *state = &states[state_index];
        Py_ssize_t base = 1 + (period_count != 0);
        PyObject *key = grouped ? items[base] : Py_None;
        int other = grouped ? PyObject_IsTrue(items[base + 1]) : 0;
        if (other < 0) {
            Py_DECREF(row);
            goto states_error;
        }
        if (grouped && series_dense_is_empty_group(
                items, base, measure_count, other)) {
            Py_DECREF(row);
            continue;
        }
        Py_hash_t bucket_hash = PyObject_Hash(items[0]);
        Py_hash_t series_hash = PyObject_Hash(key);
        if (bucket_hash == -1 || series_hash == -1) {
            Py_DECREF(row);
            goto states_error;
        }
        Py_ssize_t bucket = series_dense_lookup(
            state->bucket_index, state->bucket_index_size,
            items[0], bucket_hash);
        Py_ssize_t series = series_dense_lookup(
            state->series_index, state->series_index_size,
            key, series_dense_identity_hash(series_hash, other));
        if (bucket < 0 || series < 0) {
            Py_DECREF(row);
            if (!PyErr_Occurred()) PyErr_SetString(
                PyExc_RuntimeError, "series row index changed during reconciliation");
            goto states_error;
        }
        for (Py_ssize_t measure = 0; measure < measure_count; measure++) {
            size_t cell = ((size_t)series * (size_t)measure_count +
                           (size_t)measure) * (size_t)state->bucket_count +
                          (size_t)bucket;
            Py_XDECREF(state->cells[cell]);
            state->cells[cell] = Py_NewRef(items[value_offset + measure]);
        }
        Py_DECREF(row);
        if ((row_index & 1023) == 1023 && PyErr_CheckSignals() < 0)
            goto states_error;
    }

    PyObject *result;
    PyObject **measure_items = PySequence_Fast_ITEMS(measures);
    PyObject **fill_items = PySequence_Fast_ITEMS(fills);
    if (period_count == 0) {
        result = series_dense_materialize(
            &states[0], measure_items, fill_items, measure_count);
    }
    else {
        result = PyTuple_New(period_count);
        if (result != NULL) {
            for (Py_ssize_t index = 0; index < period_count; index++) {
                PyObject *period = series_dense_materialize(
                    &states[index], measure_items, fill_items, measure_count);
                if (period == NULL) {
                    Py_DECREF(result);
                    result = NULL;
                    break;
                }
                PyTuple_SET_ITEM(result, index, period);
            }
        }
    }
    for (Py_ssize_t index = 0; index < state_count; index++)
        series_dense_period_clear(&states[index]);
    Py_DECREF(rows);
    Py_DECREF(measures);
    Py_DECREF(fills);
    Py_XDECREF(periods);
    return result;

memory_error:
    PyErr_NoMemory();
states_error:
    for (Py_ssize_t index = 0; index < state_count; index++)
        series_dense_period_clear(&states[index]);
error:
    Py_XDECREF(rows);
    Py_XDECREF(measures);
    Py_XDECREF(fills);
    Py_XDECREF(periods);
    return NULL;
}
