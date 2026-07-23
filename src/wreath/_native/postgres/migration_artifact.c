/* Immutable Wreath-metal migration artifacts with an internal SHA-256 checksum. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

#include "migration_artifact.h"

#define ARTIFACT_HEADER_SIZE 160
#define ARTIFACT_VERSION 1
#define TAPE_HEADER_SIZE 12
#define TAPE_RECORD_SIZE 24

typedef struct {
    uint32_t state[8];
    uint64_t bit_count;
    unsigned char block[64];
    uint32_t used;
} WreathSha256;

static const uint32_t sha256_constants[64] = {
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
    0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
    0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
    0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
    0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
    0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
    0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
    0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
    0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
    0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
    0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
    0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
    0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
    0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
    0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
    0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
};

static uint32_t
rotate_right(uint32_t value, uint32_t count)
{
    return (value >> count) | (value << (32U - count));
}

static uint32_t
read_u32_be(const unsigned char *value)
{
    return ((uint32_t)value[0] << 24) |
           ((uint32_t)value[1] << 16) |
           ((uint32_t)value[2] << 8) |
           (uint32_t)value[3];
}

static uint32_t
read_u32_le(const unsigned char *value)
{
    return ((uint32_t)value[0]) |
           ((uint32_t)value[1] << 8) |
           ((uint32_t)value[2] << 16) |
           ((uint32_t)value[3] << 24);
}

static void
write_u32_be(unsigned char *target, uint32_t value)
{
    target[0] = (unsigned char)(value >> 24);
    target[1] = (unsigned char)(value >> 16);
    target[2] = (unsigned char)(value >> 8);
    target[3] = (unsigned char)value;
}

static void
write_u32_le(unsigned char *target, uint32_t value)
{
    target[0] = (unsigned char)value;
    target[1] = (unsigned char)(value >> 8);
    target[2] = (unsigned char)(value >> 16);
    target[3] = (unsigned char)(value >> 24);
}

static void
sha256_transform(WreathSha256 *context, const unsigned char *block)
{
    uint32_t schedule[64];
    uint32_t a, b, c, d, e, f, g, h;
    uint32_t index;

    for (index = 0; index < 16; index++) {
        schedule[index] = read_u32_be(block + index * 4);
    }
    for (; index < 64; index++) {
        const uint32_t left = schedule[index - 15];
        const uint32_t right = schedule[index - 2];
        const uint32_t sigma0 = rotate_right(left, 7) ^ rotate_right(left, 18) ^ (left >> 3);
        const uint32_t sigma1 = rotate_right(right, 17) ^ rotate_right(right, 19) ^ (right >> 10);
        schedule[index] = schedule[index - 16] + sigma0 + schedule[index - 7] + sigma1;
    }
    a = context->state[0]; b = context->state[1]; c = context->state[2];
    d = context->state[3]; e = context->state[4]; f = context->state[5];
    g = context->state[6]; h = context->state[7];
    for (index = 0; index < 64; index++) {
        const uint32_t sum1 = rotate_right(e, 6) ^ rotate_right(e, 11) ^ rotate_right(e, 25);
        const uint32_t choice = (e & f) ^ ((~e) & g);
        const uint32_t temp1 = h + sum1 + choice + sha256_constants[index] + schedule[index];
        const uint32_t sum0 = rotate_right(a, 2) ^ rotate_right(a, 13) ^ rotate_right(a, 22);
        const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
        const uint32_t temp2 = sum0 + majority;
        h = g; g = f; f = e; e = d + temp1;
        d = c; c = b; b = a; a = temp1 + temp2;
    }
    context->state[0] += a; context->state[1] += b;
    context->state[2] += c; context->state[3] += d;
    context->state[4] += e; context->state[5] += f;
    context->state[6] += g; context->state[7] += h;
}

static void
sha256_init(WreathSha256 *context)
{
    static const uint32_t initial[8] = {
        0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
        0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
    };
    memcpy(context->state, initial, sizeof(initial));
    context->bit_count = 0;
    context->used = 0;
}

static void
sha256_update(WreathSha256 *context, const unsigned char *data, Py_ssize_t length)
{
    while (length > 0) {
        uint32_t available = 64U - context->used;
        uint32_t take = length < (Py_ssize_t)available ? (uint32_t)length : available;
        memcpy(context->block + context->used, data, take);
        context->used += take;
        data += take;
        length -= take;
        context->bit_count += (uint64_t)take * 8U;
        if (context->used == 64U) {
            sha256_transform(context, context->block);
            context->used = 0;
        }
    }
}

static void
sha256_final(WreathSha256 *context, unsigned char digest[32])
{
    uint32_t index;
    const uint64_t bits = context->bit_count;
    context->block[context->used++] = 0x80U;
    if (context->used > 56U) {
        memset(context->block + context->used, 0, 64U - context->used);
        sha256_transform(context, context->block);
        context->used = 0;
    }
    memset(context->block + context->used, 0, 56U - context->used);
    for (index = 0; index < 8; index++) {
        context->block[63U - index] = (unsigned char)(bits >> (index * 8U));
    }
    sha256_transform(context, context->block);
    for (index = 0; index < 8; index++) {
        write_u32_be(digest + index * 4, context->state[index]);
    }
}

static int
validate_tape(const unsigned char *tape, Py_ssize_t length)
{
    uint32_t count;
    Py_ssize_t expected;
    if (length < TAPE_HEADER_SIZE || memcmp(tape, "WMO1", 4) != 0 ||
        read_u32_le(tape + 4) != 1U) {
        PyErr_SetString(PyExc_ValueError, "operation_tape is not a supported WMO1 tape");
        return -1;
    }
    count = read_u32_le(tape + 8);
    if (count > (uint32_t)((PY_SSIZE_T_MAX - TAPE_HEADER_SIZE) / TAPE_RECORD_SIZE)) {
        PyErr_SetString(PyExc_ValueError, "operation_tape is too large");
        return -1;
    }
    expected = TAPE_HEADER_SIZE + (Py_ssize_t)count * TAPE_RECORD_SIZE;
    if (length != expected) {
        PyErr_SetString(PyExc_ValueError, "operation_tape byte length is invalid");
        return -1;
    }
    return 0;
}

void
wreath_pg_sha256(
    const unsigned char *data, Py_ssize_t length, unsigned char digest[32])
{
    WreathSha256 context;
    sha256_init(&context);
    sha256_update(&context, data, length);
    sha256_final(&context, digest);
}

static void
artifact_digest(const unsigned char *data, Py_ssize_t length, unsigned char digest[32])
{
    static const unsigned char zero_checksum[32] = {0};
    WreathSha256 context;
    sha256_init(&context);
    sha256_update(&context, data, 128);
    sha256_update(&context, zero_checksum, 32);
    sha256_update(&context, data + ARTIFACT_HEADER_SIZE, length - ARTIFACT_HEADER_SIZE);
    sha256_final(&context, digest);
}

static int
require_length(const char *name, Py_ssize_t actual, Py_ssize_t expected)
{
    if (actual == expected) {
        return 0;
    }
    PyErr_Format(PyExc_ValueError, "%s must be exactly %zd bytes", name, expected);
    return -1;
}

static PyObject *
migration_build_artifact(PyObject *self, PyObject *args)
{
    const unsigned char *migration_id, *parent, *source, *target, *tape;
    Py_ssize_t migration_id_length, parent_length, source_length, target_length, tape_length;
    PyObject *artifact;
    unsigned char *data;
    unsigned char digest[32];
    Py_ssize_t total;

    (void)self;
    if (!PyArg_ParseTuple(
            args, "y#y#y#y#y#:_migration_build_artifact",
            &migration_id, &migration_id_length, &parent, &parent_length,
            &source, &source_length, &target, &target_length, &tape, &tape_length)) {
        return NULL;
    }
    if (require_length("migration_id", migration_id_length, 16) < 0 ||
        require_length("parent_checksum", parent_length, 32) < 0 ||
        require_length("source_fingerprint", source_length, 32) < 0 ||
        require_length("target_fingerprint", target_length, 32) < 0 ||
        validate_tape(tape, tape_length) < 0) {
        return NULL;
    }
    if (tape_length > UINT32_MAX || tape_length > PY_SSIZE_T_MAX - ARTIFACT_HEADER_SIZE) {
        PyErr_SetString(PyExc_OverflowError, "migration artifact is too large");
        return NULL;
    }
    total = ARTIFACT_HEADER_SIZE + tape_length;
    artifact = PyBytes_FromStringAndSize(NULL, total);
    if (artifact == NULL) {
        return NULL;
    }
    data = (unsigned char *)PyBytes_AS_STRING(artifact);
    memset(data, 0, ARTIFACT_HEADER_SIZE);
    memcpy(data, "WMA1", 4);
    write_u32_le(data + 4, ARTIFACT_VERSION);
    write_u32_le(data + 8, (uint32_t)total);
    write_u32_le(data + 12, (uint32_t)tape_length);
    memcpy(data + 16, migration_id, 16);
    memcpy(data + 32, parent, 32);
    memcpy(data + 64, source, 32);
    memcpy(data + 96, target, 32);
    memcpy(data + ARTIFACT_HEADER_SIZE, tape, tape_length);
    artifact_digest(data, total, digest);
    memcpy(data + 128, digest, 32);
    return artifact;
}

static int
verify_artifact_data(
    const unsigned char *data, Py_ssize_t length, uint32_t *tape_length_out)
{
    uint32_t tape_length;
    unsigned char digest[32];
    unsigned char mismatch = 0;
    if (length < ARTIFACT_HEADER_SIZE || memcmp(data, "WMA1", 4) != 0 ||
        read_u32_le(data + 4) != ARTIFACT_VERSION) {
        PyErr_SetString(PyExc_ValueError, "migration artifact header is unsupported");
        return -1;
    }
    tape_length = read_u32_le(data + 12);
    if (length > UINT32_MAX || read_u32_le(data + 8) != (uint32_t)length ||
        tape_length != (uint32_t)(length - ARTIFACT_HEADER_SIZE)) {
        PyErr_SetString(PyExc_ValueError, "migration artifact byte length is invalid");
        return -1;
    }
    artifact_digest(data, length, digest);
    for (uint32_t index = 0; index < 32; index++) {
        mismatch |= digest[index] ^ data[128 + index];
    }
    if (mismatch != 0) {
        PyErr_SetString(PyExc_ValueError, "migration artifact checksum mismatch");
        return -1;
    }
    if (validate_tape(data + ARTIFACT_HEADER_SIZE, tape_length) < 0) return -1;
    *tape_length_out = tape_length;
    return 0;
}

static PyObject *
migration_verify_artifact(PyObject *self, PyObject *args)
{
    const unsigned char *data;
    Py_ssize_t length;
    uint32_t tape_length;

    (void)self;
    if (!PyArg_ParseTuple(args, "y#:_migration_verify_artifact", &data, &length)) {
        return NULL;
    }
    if (verify_artifact_data(data, length, &tape_length) < 0) return NULL;
    return Py_BuildValue(
        "(y#y#y#y#y#)",
        data + 16, (Py_ssize_t)16,
        data + 32, (Py_ssize_t)32,
        data + 64, (Py_ssize_t)32,
        data + 96, (Py_ssize_t)32,
        data + ARTIFACT_HEADER_SIZE, (Py_ssize_t)tape_length);
}

static PyObject *
migration_verify_chain(PyObject *self, PyObject *args)
{
    const unsigned char *chain, *expected_parent, *expected_source;
    Py_ssize_t chain_length, parent_length, source_length, offset = 12;
    uint32_t count;
    const unsigned char *parent;
    const unsigned char *source;
    const unsigned char *last = NULL;
    (void)self;
    if (!PyArg_ParseTuple(
            args, "y#y#y#:_migration_verify_chain",
            &chain, &chain_length, &expected_parent, &parent_length,
            &expected_source, &source_length)) return NULL;
    if (require_length("expected_parent", parent_length, 32) < 0 ||
        require_length("expected_source", source_length, 32) < 0) return NULL;
    if (chain_length < 12 || memcmp(chain, "WMC1", 4) != 0 ||
        read_u32_le(chain + 4) != 1) {
        PyErr_SetString(PyExc_ValueError, "migration chain header is unsupported");
        return NULL;
    }
    count = read_u32_le(chain + 8);
    parent = expected_parent;
    source = expected_source;
    for (uint32_t index = 0; index < count; index++) {
        uint32_t artifact_length, ignored_tape_length;
        const unsigned char *artifact;
        if (chain_length - offset < 4) {
            PyErr_SetString(PyExc_ValueError, "migration chain is truncated");
            return NULL;
        }
        artifact_length = read_u32_le(chain + offset);
        offset += 4;
        if (artifact_length > (uint32_t)(chain_length - offset)) {
            PyErr_SetString(PyExc_ValueError, "migration chain is truncated");
            return NULL;
        }
        artifact = chain + offset;
        if (verify_artifact_data(artifact, artifact_length, &ignored_tape_length) < 0)
            return NULL;
        if (memcmp(artifact + 32, parent, 32) != 0) {
            PyErr_Format(PyExc_ValueError, "migration chain parent mismatch at index %u", index);
            return NULL;
        }
        if (memcmp(artifact + 64, source, 32) != 0) {
            PyErr_Format(PyExc_ValueError, "migration chain source mismatch at index %u", index);
            return NULL;
        }
        last = artifact;
        parent = artifact + 128;
        source = artifact + 96;
        offset += artifact_length;
    }
    if (offset != chain_length) {
        PyErr_SetString(PyExc_ValueError, "migration chain has trailing bytes");
        return NULL;
    }
    if (last == NULL) {
        return Py_BuildValue(
            "(y#y#I)", expected_parent, (Py_ssize_t)32,
            expected_source, (Py_ssize_t)32, count);
    }
    return Py_BuildValue(
        "(y#y#I)", last + 128, (Py_ssize_t)32,
        last + 96, (Py_ssize_t)32, count);
}

static PyMethodDef migration_artifact_methods[] = {
    {"_migration_build_artifact", migration_build_artifact, METH_VARARGS,
     PyDoc_STR("Build a deterministic checksummed WMA1 artifact.")},
    {"_migration_verify_artifact", migration_verify_artifact, METH_VARARGS,
     PyDoc_STR("Verify and decode bounded WMA1 artifact metadata.")},
    {"_migration_verify_chain", migration_verify_chain, METH_VARARGS,
     PyDoc_STR("Verify a packed WMC1 artifact chain and continuity in metal.")},
    {NULL, NULL, 0, NULL},
};

int
wreath_pg_migration_artifact_init(PyObject *module)
{
    return PyModule_AddFunctions(module, migration_artifact_methods);
}
