/* Wreath's native gzip boundary.
 *
 * The encoder and decoder are independent implementations.  Their public
 * symbols are namespaced in their source trees so linking both into
 * _core cannot accidentally make one half depend on the other half's parser.
 * Both still read and write ordinary RFC 1951/1952 streams.
 *
 * SPDX-License-Identifier: MPL-2.0
 */
#include "wreathcore.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct wreath_gzip_encoder_enc wreath_gzip_encoder_enc;
typedef struct wreath_gzip_decoder_dec wreath_gzip_decoder_dec;

wreath_gzip_encoder_enc *wreath_gzip_encoder_enc_new(void);
void wreath_gzip_encoder_enc_free(wreath_gzip_encoder_enc *encoder);
void wreath_gzip_encoder_enc_set_format(wreath_gzip_encoder_enc *encoder, int format);
size_t wreath_gzip_encoder_encode_bound(size_t length);
size_t wreath_gzip_encoder_encode(wreath_gzip_encoder_enc *encoder,
                                const void *input, size_t input_length,
                                void *output, size_t output_capacity,
                                int profile);
uint32_t wreath_gzip_encoder_crc32(uint32_t crc, const void *buffer, size_t length);

wreath_gzip_decoder_dec *wreath_gzip_decoder_dec_new(void);
void wreath_gzip_decoder_dec_free(wreath_gzip_decoder_dec *decoder);
void wreath_gzip_decoder_dec_set_format(wreath_gzip_decoder_dec *decoder, int format);
int wreath_gzip_decoder_decompress(wreath_gzip_decoder_dec *decoder,
                                 const void *input, size_t input_length,
                                 void *output, size_t output_capacity,
                                 size_t *output_length);

enum {
    WREATH_GZ_OK = 0,
    WREATH_GZ_ERR_HEADER = -1,
    WREATH_GZ_ERR_DATA = -2,
    WREATH_GZ_ERR_TRUNCATED = -3,
    WREATH_GZ_ERR_CRC = -4,
    WREATH_GZ_ERR_LENGTH = -5,
    WREATH_GZ_ERR_SPACE = -6,
    WREATH_GZ_ERR_TRAILING = -7,
};

#define WREATH_GZIP_ENCODER_CAPSULE "wreath.gzip.encoder"
#define WREATH_GZIP_DECODER_CAPSULE "wreath.gzip.decoder"

static int
wreath_ascii_equal(const char *value, Py_ssize_t length, const char *literal)
{
    size_t literal_length = strlen(literal);
    if ((size_t)length != literal_length) return 0;
    for (Py_ssize_t index = 0; index < length; index++) {
        unsigned char ch = (unsigned char)value[index];
        if (ch >= 'A' && ch <= 'Z') ch = (unsigned char)(ch + ('a' - 'A'));
        if (ch != (unsigned char)literal[index]) return 0;
    }
    return 1;
}

static int
wreath_ascii_ends_with(const char *value, Py_ssize_t length, const char *suffix)
{
    size_t suffix_length = strlen(suffix);
    return (size_t)length >= suffix_length &&
           wreath_ascii_equal(value + length - (Py_ssize_t)suffix_length,
                              (Py_ssize_t)suffix_length, suffix);
}

int
wreath_gzip_format_object(PyObject *value, int *format)
{
    const char *text;
    Py_ssize_t length;
    if (PyLong_Check(value)) {
        long number = PyLong_AsLong(value);
        if (number == -1 && PyErr_Occurred()) return -1;
        if (number < 0 || number >= 7) {
            PyErr_SetString(PyExc_ValueError, "unknown gzip content format");
            return -1;
        }
        *format = (int)number;
        return 0;
    }
    if (PyBytes_Check(value)) {
        text = PyBytes_AS_STRING(value);
        length = PyBytes_GET_SIZE(value);
    }
    else if (PyUnicode_Check(value)) {
        text = PyUnicode_AsUTF8AndSize(value, &length);
        if (text == NULL) return -1;
    }
    else {
        PyErr_SetString(PyExc_TypeError, "gzip format must be str or bytes");
        return -1;
    }
    while (length > 0 && (*text == ' ' || *text == '\t')) {
        text++;
        length--;
    }
    const char *semicolon = memchr(text, ';', (size_t)length);
    if (semicolon != NULL) length = (Py_ssize_t)(semicolon - text);
    while (length > 0 && (text[length - 1] == ' ' || text[length - 1] == '\t'))
        length--;

    if (wreath_ascii_equal(text, length, "json") ||
        wreath_ascii_equal(text, length, "application/json") ||
        wreath_ascii_ends_with(text, length, "+json"))
        *format = 1;
    else if (wreath_ascii_equal(text, length, "chaotic-json"))
        *format = 2;
    else if (wreath_ascii_equal(text, length, "html") ||
             wreath_ascii_equal(text, length, "text/html"))
        *format = 3;
    else if (wreath_ascii_equal(text, length, "graphql") ||
             wreath_ascii_equal(text, length, "application/graphql") ||
             wreath_ascii_equal(text, length, "application/graphql-query"))
        *format = 4;
    else if (wreath_ascii_equal(text, length, "log") ||
             wreath_ascii_equal(text, length, "application/x-ndjson") ||
             wreath_ascii_equal(text, length, "application/jsonlines") ||
             wreath_ascii_equal(text, length, "text/x-log"))
        *format = 5;
    else if (wreath_ascii_equal(text, length, "plaintext") ||
             wreath_ascii_equal(text, length, "text/plain"))
        *format = 6;
    else
        *format = 0;
    return 0;
}

static void
wreath_gzip_encoder_capsule_free(PyObject *capsule)
{
    wreath_gzip_encoder_enc *encoder = PyCapsule_GetPointer(
        capsule, WREATH_GZIP_ENCODER_CAPSULE);
    if (encoder != NULL) wreath_gzip_encoder_enc_free(encoder);
    else PyErr_Clear();
}

PyObject *
wreath_gzip_encoder_new(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(ignored))
{
    wreath_gzip_encoder_enc *encoder = wreath_gzip_encoder_enc_new();
    if (encoder == NULL) return PyErr_NoMemory();
    PyObject *capsule = PyCapsule_New(
        encoder, WREATH_GZIP_ENCODER_CAPSULE, wreath_gzip_encoder_capsule_free);
    if (capsule == NULL) wreath_gzip_encoder_enc_free(encoder);
    return capsule;
}

static void
wreath_gzip_decoder_capsule_free(PyObject *capsule)
{
    wreath_gzip_decoder_dec *decoder = PyCapsule_GetPointer(
        capsule, WREATH_GZIP_DECODER_CAPSULE);
    if (decoder != NULL) wreath_gzip_decoder_dec_free(decoder);
    else PyErr_Clear();
}

PyObject *
wreath_gzip_decoder_new(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(ignored))
{
    wreath_gzip_decoder_dec *decoder = wreath_gzip_decoder_dec_new();
    if (decoder == NULL) return PyErr_NoMemory();
    PyObject *capsule = PyCapsule_New(
        decoder, WREATH_GZIP_DECODER_CAPSULE, wreath_gzip_decoder_capsule_free);
    if (capsule == NULL) wreath_gzip_decoder_dec_free(decoder);
    return capsule;
}

static int
wreath_gzip_profile(int level)
{
    static const unsigned char profiles[10] = {0, 0, 1, 2, 3, 3, 3, 4, 4, 5};
    return profiles[level];
}

static void
wreath_store_u16(unsigned char *destination, uint16_t value)
{
    destination[0] = (unsigned char)value;
    destination[1] = (unsigned char)(value >> 8);
}

static void
wreath_store_u32(unsigned char *destination, uint32_t value)
{
    destination[0] = (unsigned char)value;
    destination[1] = (unsigned char)(value >> 8);
    destination[2] = (unsigned char)(value >> 16);
    destination[3] = (unsigned char)(value >> 24);
}

static PyObject *
wreath_gzip_store(const unsigned char *input, Py_ssize_t input_length)
{
    size_t length = (size_t)input_length;
    size_t blocks = length == 0 ? 1 : (length + 65534u) / 65535u;
    size_t output_length;
    PyObject *result;
    unsigned char *output;
    size_t input_position = 0;
    size_t output_position = 10;

    if (blocks > (SIZE_MAX - length - 18u) / 5u) {
        return PyErr_NoMemory();
    }
    output_length = 18u + length + blocks * 5u;
    if (output_length > (size_t)PY_SSIZE_T_MAX) {
        return PyErr_NoMemory();
    }
    result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)output_length);
    if (result == NULL) return NULL;
    output = (unsigned char *)PyBytes_AS_STRING(result);
    output[0] = 0x1f;
    output[1] = 0x8b;
    output[2] = 8;
    output[3] = 0;
    memset(output + 4, 0, 6);
    output[9] = 255;

    do {
        size_t remaining = length - input_position;
        uint16_t chunk = (uint16_t)(remaining > 65535u ? 65535u : remaining);
        int final = input_position + chunk == length;
        output[output_position++] = (unsigned char)final;
        wreath_store_u16(output + output_position, chunk);
        wreath_store_u16(output + output_position + 2, (uint16_t)~chunk);
        output_position += 4;
        if (chunk != 0) {
            memcpy(output + output_position, input + input_position, chunk);
            input_position += chunk;
            output_position += chunk;
        }
    } while (input_position < length);

    wreath_store_u32(output + output_position,
                     wreath_gzip_encoder_crc32(0, input, length));
    wreath_store_u32(output + output_position + 4, (uint32_t)length);
    return result;
}

PyObject *
wreath_gzip_compress_workspace(PyObject *workspace, PyObject *data,
                               int level, PyObject *format_object)
{
    Py_buffer input = {0};
    int format;
    wreath_gzip_encoder_enc *encoder;
    PyObject *result = NULL;
    size_t capacity;
    size_t written;

    encoder = PyCapsule_GetPointer(workspace, WREATH_GZIP_ENCODER_CAPSULE);
    if (encoder == NULL) return NULL;
    if (PyObject_GetBuffer(data, &input, PyBUF_SIMPLE) < 0) return NULL;
    if (level < 0 || level > 9) {
        PyBuffer_Release(&input);
        PyErr_SetString(PyExc_ValueError, "gzip level must be between 0 and 9");
        return NULL;
    }
    if (wreath_gzip_format_object(format_object, &format) < 0) {
        PyBuffer_Release(&input);
        return NULL;
    }
    if (level == 0) {
        result = wreath_gzip_store(input.buf, input.len);
        PyBuffer_Release(&input);
        return result;
    }
    capacity = wreath_gzip_encoder_encode_bound((size_t)input.len);
    if (capacity > (size_t)PY_SSIZE_T_MAX) {
        PyBuffer_Release(&input);
        return PyErr_NoMemory();
    }
    result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)capacity);
    if (result == NULL) goto done;
    wreath_gzip_encoder_enc_set_format(encoder, format);
    written = wreath_gzip_encoder_encode(
        encoder, input.buf, (size_t)input.len, PyBytes_AS_STRING(result),
        capacity, wreath_gzip_profile(level));
    if (written == 0 || written > capacity) {
        PyErr_SetString(PyExc_RuntimeError, "Wreath gzip encoder failed");
        goto done;
    }
    if (_PyBytes_Resize(&result, (Py_ssize_t)written) < 0) result = NULL;

done:
    PyBuffer_Release(&input);
    if (PyErr_Occurred()) Py_CLEAR(result);
    return result;
}

PyObject *
wreath_gzip_compress_with(PyObject *Py_UNUSED(self), PyObject *const *args,
                          Py_ssize_t nargs)
{
    if (nargs != 4) {
        PyErr_Format(PyExc_TypeError,
                     "gzip_compress_with expected 4 arguments, got %zd", nargs);
        return NULL;
    }
    long level = PyLong_AsLong(args[2]);
    if (level == -1 && PyErr_Occurred()) return NULL;
    if (level < 0 || level > 9) {
        PyErr_SetString(PyExc_ValueError, "gzip level must be between 0 and 9");
        return NULL;
    }
    return wreath_gzip_compress_workspace(args[0], args[1], (int)level, args[3]);
}

PyObject *
wreath_gzip_fragment_compress_workspace(PyObject *workspace, PyObject *data,
                                        int level, PyObject *format_object,
                                        PyObject *fragments)
{
    PyObject *entry;
    PyObject *expected;
    PyObject *cached;
    Py_buffer input = {0};
    wreath_gzip_encoder_enc *encoder;
    PyObject *result = NULL;
    Py_ssize_t prefix;
    Py_ssize_t suffix;
    long prepared_level;
    size_t prefix_bound;
    size_t suffix_bound;
    size_t capacity;
    size_t position = 0;
    size_t written;
    int format;

    if (level < 0 || level > 9) {
        PyErr_SetString(PyExc_ValueError, "gzip level must be between 0 and 9");
        return NULL;
    }
    if (wreath_gzip_format_object(format_object, &format) < 0) return NULL;
    if (!PyTuple_CheckExact(fragments) || PyTuple_GET_SIZE(fragments) != 7) {
        PyErr_SetString(PyExc_RuntimeError, "invalid prepared gzip fragment table");
        return NULL;
    }
    entry = PyTuple_GET_ITEM(fragments, format);
    if (entry == Py_None) {
        return wreath_gzip_compress_workspace(
            workspace, data, level, format_object);
    }
    if (!PyTuple_CheckExact(entry) || PyTuple_GET_SIZE(entry) != 5) {
        PyErr_SetString(PyExc_RuntimeError, "invalid prepared gzip fragment");
        return NULL;
    }
    prefix = PyLong_AsSsize_t(PyTuple_GET_ITEM(entry, 0));
    suffix = PyLong_AsSsize_t(PyTuple_GET_ITEM(entry, 1));
    expected = PyTuple_GET_ITEM(entry, 2);
    cached = PyTuple_GET_ITEM(entry, 3);
    prepared_level = PyLong_AsLong(PyTuple_GET_ITEM(entry, 4));
    if (PyErr_Occurred()) return NULL;
    if (prefix < 0 || suffix < 0 || !PyBytes_CheckExact(expected) ||
        !PyBytes_CheckExact(cached)) {
        PyErr_SetString(PyExc_RuntimeError, "invalid prepared gzip fragment");
        return NULL;
    }
    if (prepared_level != level) {
        return wreath_gzip_compress_workspace(
            workspace, data, level, format_object);
    }
    encoder = PyCapsule_GetPointer(workspace, WREATH_GZIP_ENCODER_CAPSULE);
    if (encoder == NULL) return NULL;
    if (PyObject_GetBuffer(data, &input, PyBUF_SIMPLE) < 0) return NULL;
    Py_ssize_t middle = PyBytes_GET_SIZE(expected);
    if (prefix > input.len || suffix > input.len - prefix ||
        middle != input.len - prefix - suffix ||
        memcmp((const unsigned char *)input.buf + prefix,
               PyBytes_AS_STRING(expected), (size_t)middle) != 0) {
        PyBuffer_Release(&input);
        return wreath_gzip_compress_workspace(
            workspace, data, level, format_object);
    }

    prefix_bound = prefix != 0
        ? wreath_gzip_encoder_encode_bound((size_t)prefix) : 0;
    suffix_bound = suffix != 0
        ? wreath_gzip_encoder_encode_bound((size_t)suffix) : 0;
    if (prefix_bound > SIZE_MAX - suffix_bound ||
        prefix_bound + suffix_bound >
            SIZE_MAX - (size_t)PyBytes_GET_SIZE(cached)) {
        PyBuffer_Release(&input);
        return PyErr_NoMemory();
    }
    capacity = prefix_bound + suffix_bound + (size_t)PyBytes_GET_SIZE(cached);
    if (capacity > (size_t)PY_SSIZE_T_MAX) {
        PyBuffer_Release(&input);
        return PyErr_NoMemory();
    }
    result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)capacity);
    if (result == NULL) goto done;
    wreath_gzip_encoder_enc_set_format(encoder, format);
    if (prefix != 0) {
        written = wreath_gzip_encoder_encode(
            encoder, input.buf, (size_t)prefix,
            PyBytes_AS_STRING(result), capacity, wreath_gzip_profile(level));
        if (written == 0 || written > capacity) goto failed;
        position = written;
    }
    memcpy(PyBytes_AS_STRING(result) + position,
           PyBytes_AS_STRING(cached), (size_t)PyBytes_GET_SIZE(cached));
    position += (size_t)PyBytes_GET_SIZE(cached);
    if (suffix != 0) {
        written = wreath_gzip_encoder_encode(
            encoder, (const unsigned char *)input.buf + input.len - suffix,
            (size_t)suffix, PyBytes_AS_STRING(result) + position,
            capacity - position, wreath_gzip_profile(level));
        if (written == 0 || written > capacity - position) goto failed;
        position += written;
    }
    if (_PyBytes_Resize(&result, (Py_ssize_t)position) < 0) result = NULL;
    goto done;

failed:
    Py_CLEAR(result);
    PyErr_SetString(PyExc_RuntimeError, "Wreath gzip fragment encoder failed");
done:
    PyBuffer_Release(&input);
    return result;
}

PyObject *
wreath_gzip_fragment_compress_with(PyObject *Py_UNUSED(self),
                                   PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 5) {
        PyErr_Format(PyExc_TypeError,
                     "gzip_fragment_compress_with expected 5 arguments, got %zd",
                     nargs);
        return NULL;
    }
    long level = PyLong_AsLong(args[2]);
    if (level == -1 && PyErr_Occurred()) return NULL;
    return wreath_gzip_fragment_compress_workspace(
        args[0], args[1], (int)level, args[3], args[4]);
}

PyObject *
wreath_gzip_compress(PyObject *Py_UNUSED(self), PyObject *const *args,
                     Py_ssize_t nargs)
{
    if (nargs != 3) {
        PyErr_Format(PyExc_TypeError,
                     "gzip_compress expected 3 arguments, got %zd", nargs);
        return NULL;
    }
    long level = PyLong_AsLong(args[1]);
    if (level == -1 && PyErr_Occurred()) return NULL;
    if (level < 0 || level > 9) {
        PyErr_SetString(PyExc_ValueError, "gzip level must be between 0 and 9");
        return NULL;
    }
    PyObject *workspace = wreath_gzip_encoder_new(NULL, NULL);
    if (workspace == NULL) return NULL;
    PyObject *result = wreath_gzip_compress_workspace(
        workspace, args[0], (int)level, args[2]);
    Py_DECREF(workspace);
    return result;
}

PyObject *
wreath_gzip_decompress_workspace(PyObject *workspace, PyObject *data,
                                 Py_ssize_t maximum, PyObject *format_object)
{
    Py_buffer input = {0};
    int format;
    wreath_gzip_decoder_dec *decoder;
    PyObject *result = NULL;
    size_t written = 0;
    int status;

    decoder = PyCapsule_GetPointer(workspace, WREATH_GZIP_DECODER_CAPSULE);
    if (decoder == NULL) return NULL;
    if (PyObject_GetBuffer(data, &input, PyBUF_SIMPLE) < 0) return NULL;
    if (maximum < 1) {
        PyErr_Format(PyExc_ValueError,
                     "max_output_bytes must be positive, got %zd", maximum);
        goto done;
    }
    if (wreath_gzip_format_object(format_object, &format) < 0) goto done;
    /* Reject obvious non-members before reserving a caller's potentially large
     * output ceiling.  Everything after the fixed magic is parsed by Wreath. */
    if (input.len < 2 || ((const unsigned char *)input.buf)[0] != 0x1f ||
        ((const unsigned char *)input.buf)[1] != 0x8b) {
        PyErr_SetString(PyExc_ValueError, "not a readable gzip member");
        goto done;
    }
    result = PyBytes_FromStringAndSize(NULL, maximum);
    if (result == NULL) goto done;
    wreath_gzip_decoder_dec_set_format(decoder, format);
    status = wreath_gzip_decoder_decompress(
        decoder, input.buf, (size_t)input.len, PyBytes_AS_STRING(result),
        (size_t)maximum, &written);
    if (status == WREATH_GZ_OK) {
        if (_PyBytes_Resize(&result, (Py_ssize_t)written) < 0) result = NULL;
        goto done;
    }
    Py_CLEAR(result);
    switch (status) {
        case WREATH_GZ_ERR_TRUNCATED:
            PyErr_SetString(PyExc_ValueError, "gzip member is truncated");
            break;
        case WREATH_GZ_ERR_SPACE:
            PyErr_Format(PyExc_ValueError,
                         "gzip member expands past the %zd-byte limit", maximum);
            break;
        case WREATH_GZ_ERR_TRAILING:
            PyErr_SetString(PyExc_ValueError,
                            "trailing bytes follow the gzip member");
            break;
        case WREATH_GZ_ERR_HEADER:
        case WREATH_GZ_ERR_DATA:
        case WREATH_GZ_ERR_CRC:
        case WREATH_GZ_ERR_LENGTH:
        default:
            PyErr_SetString(PyExc_ValueError, "not a readable gzip member");
            break;
    }

done:
    PyBuffer_Release(&input);
    if (PyErr_Occurred()) Py_CLEAR(result);
    return result;
}

PyObject *
wreath_gzip_decompress_with(PyObject *Py_UNUSED(self), PyObject *const *args,
                            Py_ssize_t nargs)
{
    if (nargs != 4) {
        PyErr_Format(PyExc_TypeError,
                     "gzip_decompress_with expected 4 arguments, got %zd", nargs);
        return NULL;
    }
    Py_ssize_t maximum = PyLong_AsSsize_t(args[2]);
    if (maximum == -1 && PyErr_Occurred()) return NULL;
    return wreath_gzip_decompress_workspace(args[0], args[1], maximum, args[3]);
}

PyObject *
wreath_gzip_decompress(PyObject *Py_UNUSED(self), PyObject *const *args,
                       Py_ssize_t nargs)
{
    if (nargs != 3) {
        PyErr_Format(PyExc_TypeError,
                     "gzip_decompress expected 3 arguments, got %zd", nargs);
        return NULL;
    }
    Py_ssize_t maximum = PyLong_AsSsize_t(args[1]);
    if (maximum == -1 && PyErr_Occurred()) return NULL;
    PyObject *workspace = wreath_gzip_decoder_new(NULL, NULL);
    if (workspace == NULL) return NULL;
    PyObject *result = wreath_gzip_decompress_workspace(
        workspace, args[0], maximum, args[2]);
    Py_DECREF(workspace);
    return result;
}

PyObject *
wreath_gzip_codec_info(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(ignored))
{
    return Py_BuildValue("{s:s,s:s,s:s}",
                         "implementation", "wreath-native",
                         "encoder", "independent",
                         "decoder", "independent");
}
