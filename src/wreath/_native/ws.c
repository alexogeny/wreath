/* WebSocket frame primitives: XOR masking and frame parsing (RFC 6455). */
#include "wreathcore.h"

/* Raw header parser shared with sibling extensions via WREATH_CORE_CAPI. */
int
wreath_ws_parse_header_raw(const uint8_t *buf, Py_ssize_t len, WreathWsFrameHeader *out)
{
    uint64_t payload_len;
    Py_ssize_t pos = 2;

    if (len < 2) {
        return 1;
    }
    if (buf[0] & 0x70) {
        return -1;  /* reserved bits set */
    }
    out->fin = (buf[0] & 0x80) != 0;
    out->opcode = buf[0] & 0x0F;
    out->masked = (buf[1] & 0x80) != 0;
    payload_len = buf[1] & 0x7F;
    if (payload_len == 126) {
        if (len < pos + 2) {
            return 1;
        }
        payload_len = ((uint64_t)buf[pos] << 8) | buf[pos + 1];
        pos += 2;
    }
    else if (payload_len == 127) {
        if (len < pos + 8) {
            return 1;
        }
        payload_len = 0;
        for (int i = 0; i < 8; i++) {
            payload_len = (payload_len << 8) | buf[pos + i];
        }
        if (payload_len > (uint64_t)PY_SSIZE_T_MAX) {
            return -1;
        }
        pos += 8;
    }
    out->mask_key = NULL;
    if (out->masked) {
        if (len < pos + 4) {
            return 1;
        }
        out->mask_key = buf + pos;
        pos += 4;
    }
    out->header_len = pos;
    out->payload_len = (Py_ssize_t)payload_len;
    return 0;
}


/* XOR src into dst with the 4-byte key, one machine word at a time. */
static void
xor_mask(uint8_t *dst, const uint8_t *src, Py_ssize_t len, const uint8_t *key)
{
    uint8_t pattern_bytes[8] = {
        key[0], key[1], key[2], key[3], key[0], key[1], key[2], key[3],
    };
    uint64_t pattern;
    memcpy(&pattern, pattern_bytes, 8);

    Py_ssize_t i = 0;
    for (; i + 8 <= len; i += 8) {
        uint64_t word;
        memcpy(&word, src + i, 8);
        word ^= pattern;
        memcpy(dst + i, &word, 8);
    }
    for (; i < len; i++) {
        dst[i] = src[i] ^ key[i & 3];
    }
}


void
wreath_ws_unmask_raw(uint8_t *dst, const uint8_t *src, Py_ssize_t len, const uint8_t *key)
{
    xor_mask(dst, src, len, key);
}

PyObject *
wreath_ws_mask(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer data, key;
    if (!PyArg_ParseTuple(args, "y*y*:ws_mask", &data, &key)) {
        return NULL;
    }
    if (key.len != 4) {
        PyBuffer_Release(&data);
        PyBuffer_Release(&key);
        PyErr_SetString(PyExc_ValueError, "mask key must be exactly 4 bytes");
        return NULL;
    }

    PyObject *result = PyBytes_FromStringAndSize(NULL, data.len);
    if (result != NULL) {
        xor_mask((uint8_t *)PyBytes_AS_STRING(result), data.buf, data.len, key.buf);
    }
    PyBuffer_Release(&data);
    PyBuffer_Release(&key);
    return result;
}

PyObject *
wreath_ws_build_frame(PyObject *Py_UNUSED(self), PyObject *args, PyObject *kwargs)
{
    static char *kwlist[] = {"opcode", "payload", "fin", "mask_key", NULL};
    int opcode;
    Py_buffer payload;
    int fin = 1;
    Py_buffer mask_key = {NULL, NULL};
    PyObject *result;
    Py_ssize_t header_len;
    uint8_t header[10];
    uint8_t *out;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "iy*|py*:ws_build_frame", kwlist,
                                     &opcode, &payload, &fin, &mask_key)) {
        return NULL;
    }
    if (opcode < 0 || opcode > 0x0F) {
        PyErr_SetString(PyExc_ValueError, "opcode must be in range 0..15");
        goto error;
    }
    if (mask_key.buf != NULL && mask_key.len != 4) {
        PyErr_SetString(PyExc_ValueError, "mask key must be exactly 4 bytes");
        goto error;
    }

    header[0] = (uint8_t)((fin ? 0x80 : 0) | opcode);
    {
        uint8_t mask_bit = mask_key.buf != NULL ? 0x80 : 0;
        if (payload.len < 126) {
            header[1] = (uint8_t)(mask_bit | payload.len);
            header_len = 2;
        }
        else if (payload.len < 65536) {
            header[1] = (uint8_t)(mask_bit | 126);
            header[2] = (uint8_t)(payload.len >> 8);
            header[3] = (uint8_t)payload.len;
            header_len = 4;
        }
        else {
            uint64_t length = (uint64_t)payload.len;
            header[1] = (uint8_t)(mask_bit | 127);
            for (int i = 0; i < 8; i++) {
                header[2 + i] = (uint8_t)(length >> (56 - 8 * i));
            }
            header_len = 10;
        }
    }

    result = PyBytes_FromStringAndSize(
        NULL, header_len + (mask_key.buf != NULL ? 4 : 0) + payload.len);
    if (result == NULL) {
        goto error;
    }
    out = (uint8_t *)PyBytes_AS_STRING(result);
    memcpy(out, header, (size_t)header_len);
    out += header_len;
    if (mask_key.buf != NULL) {
        memcpy(out, mask_key.buf, 4);
        out += 4;
        xor_mask(out, payload.buf, payload.len, mask_key.buf);
    }
    else if (payload.len > 0) {
        memcpy(out, payload.buf, (size_t)payload.len);
    }
    PyBuffer_Release(&payload);
    if (mask_key.buf != NULL) {
        PyBuffer_Release(&mask_key);
    }
    return result;

error:
    PyBuffer_Release(&payload);
    if (mask_key.buf != NULL) {
        PyBuffer_Release(&mask_key);
    }
    return NULL;
}

PyObject *
wreath_ws_parse_frame(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*:ws_parse_frame", &view)) {
        return NULL;
    }
    const uint8_t *buf = view.buf;
    Py_ssize_t len = view.len;
    WreathWsFrameHeader header;
    int rc = wreath_ws_parse_header_raw(buf, len, &header);

    if (rc == 1) {
        goto incomplete;
    }
    if (rc < 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        (len >= 1 && (buf[0] & 0x70)) ? "reserved bits set"
                                                      : "frame length out of range");
        return NULL;
    }
    if (len - header.header_len < header.payload_len) {
        goto incomplete;
    }

    PyObject *payload = PyBytes_FromStringAndSize(NULL, header.payload_len);
    if (payload == NULL) {
        PyBuffer_Release(&view);
        return NULL;
    }
    uint8_t *dst = (uint8_t *)PyBytes_AS_STRING(payload);
    if (header.masked) {
        xor_mask(dst, buf + header.header_len, header.payload_len, header.mask_key);
    }
    else {
        memcpy(dst, buf + header.header_len, (size_t)header.payload_len);
    }

    Py_ssize_t consumed = header.header_len + header.payload_len;
    PyBuffer_Release(&view);
    PyObject *result = Py_BuildValue("(OiNn)", header.fin ? Py_True : Py_False,
                                     header.opcode, payload, consumed);
    if (result == NULL) {
        /* Py_BuildValue "N" already stole the payload reference on failure
         * paths after consuming it; only decref when it was not consumed. */
        return NULL;
    }
    return result;

incomplete:
    PyBuffer_Release(&view);
    Py_RETURN_NONE;
}
