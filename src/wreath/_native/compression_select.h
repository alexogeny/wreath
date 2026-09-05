#ifndef WREATH_COMPRESSION_SELECT_H
#define WREATH_COMPRESSION_SELECT_H

/* Shared Accept-Encoding selection for portable ASGI and the metal server.
 * Keep this header-only: those callers live in separate extension modules and
 * cannot safely link a hidden symbol out of one another. */

static inline void
wreath_compression_trim_ows(const char **data, Py_ssize_t *length)
{
    while (*length > 0 && ((*data)[0] == ' ' || (*data)[0] == '\t')) {
        (*data)++;
        (*length)--;
    }
    while (*length > 0 && ((*data)[*length - 1] == ' ' ||
                           (*data)[*length - 1] == '\t')) (*length)--;
}

/* 0 none, 1 gzip, 2 zstd, 3 dcz. `fallback` retains the ordinary coding so a
 * DCZ dictionary miss continues without reparsing Accept-Encoding. */
static inline int
wreath_select_compression_data(const char *data, Py_ssize_t size,
                               int allow_dcz, int *fallback)
{
    if (data == NULL) return 0;
    Py_ssize_t start = 0;
    int gzip_named = 0, gzip_q = 0, zstd_q = 0, dcz_q = 0, wildcard_q = 0;
    for (Py_ssize_t i = 0; i <= size; i++) {
        if (i < size && data[i] != ',') continue;
        const char *item = data + start;
        Py_ssize_t item_len = i - start;
        start = i + 1;
        wreath_compression_trim_ows(&item, &item_len);
        if (item_len == 0) continue;
        Py_ssize_t semi = 0;
        while (semi < item_len && item[semi] != ';') semi++;
        const char *coding = item;
        Py_ssize_t coding_len = semi;
        wreath_compression_trim_ows(&coding, &coding_len);
        int quality = 1000;
        Py_ssize_t parameter = semi;
        while (parameter < item_len) {
            parameter++;
            Py_ssize_t end = parameter;
            while (end < item_len && item[end] != ';') end++;
            const char *part = item + parameter;
            Py_ssize_t part_len = end - parameter;
            wreath_compression_trim_ows(&part, &part_len);
            Py_ssize_t equals = 0;
            while (equals < part_len && part[equals] != '=') equals++;
            const char *name = part;
            Py_ssize_t name_len = equals;
            wreath_compression_trim_ows(&name, &name_len);
            if (wreath_ascii_equal_ci_str(name, name_len, "q")) {
                quality = equals < part_len
                    ? wreath_parse_quality(part + equals + 1, part_len - equals - 1)
                    : 0;
            }
            parameter = end;
        }
        if (wreath_ascii_equal_ci_str(coding, coding_len, "gzip")) {
            gzip_named = 1;
            gzip_q = quality;
        }
        else if (wreath_ascii_equal_ci_str(coding, coding_len, "zstd")) zstd_q = quality;
        else if (wreath_ascii_equal_ci_str(coding, coding_len, "dcz")) dcz_q = quality;
        else if (coding_len == 1 && coding[0] == '*') wildcard_q = quality;
    }
    if (!gzip_named) gzip_q = wildcard_q;
    int ordinary = zstd_q > 0 && zstd_q >= gzip_q ? 2 : (gzip_q > 0 ? 1 : 0);
    int ordinary_q = ordinary == 2 ? zstd_q : (ordinary == 1 ? gzip_q : 0);
    int selected = allow_dcz && dcz_q > 0 && dcz_q >= ordinary_q ? 3 : ordinary;
    if (fallback != NULL) {
        *fallback = selected == 3 && gzip_q > 0 && gzip_q >= zstd_q
            ? 1 : ordinary;
    }
    return selected;
}

static inline int
wreath_select_compression_value(PyObject *arg, int allow_dcz, int *fallback)
{
    if (arg == NULL) return 0;
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return -1;
    int selected = wreath_select_compression_data(
        view.buf, view.len, allow_dcz, fallback);
    PyBuffer_Release(&view);
    return selected;
}

#endif
