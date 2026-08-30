#include "wreathcore.h"

#include <stdint.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
typedef HMODULE WreathLibrary;
typedef FARPROC WreathSymbol;
#define wreath_library_open(name) LoadLibraryA(name)
#define wreath_library_symbol(handle, name) GetProcAddress(handle, name)
#define wreath_library_close(handle) FreeLibrary(handle)
#else
#include <dlfcn.h>
typedef void *WreathLibrary;
typedef void *WreathSymbol;
#define wreath_library_open(name) dlopen(name, RTLD_NOW | RTLD_LOCAL)
#define wreath_library_symbol(handle, name) dlsym(handle, name)
#define wreath_library_close(handle) dlclose(handle)
#endif

typedef void *(*zstd_create_cctx_fn)(void);
typedef size_t (*zstd_free_cctx_fn)(void *);
typedef void *(*zstd_create_cdict_fn)(const void *, size_t, int);
typedef size_t (*zstd_free_cdict_fn)(void *);
typedef size_t (*zstd_compress_bound_fn)(size_t);
typedef size_t (*zstd_compress_cdict_fn)(
    void *, void *, size_t, const void *, size_t, const void *);
typedef struct {
    const void *src;
    size_t size;
    size_t pos;
} WreathZstdIn;
typedef struct {
    void *dst;
    size_t size;
    size_t pos;
} WreathZstdOut;
typedef size_t (*zstd_compress_stream_fn)(void *, WreathZstdOut *, WreathZstdIn *, int);
typedef size_t (*zstd_ref_cdict_fn)(void *, const void *);
typedef size_t (*zstd_reset_cctx_fn)(void *, int);
typedef unsigned (*zstd_is_error_fn)(size_t);
typedef const char *(*zstd_error_name_fn)(size_t);

typedef struct {
    PyMutex mutex;
    WreathLibrary library;
    zstd_create_cctx_fn create_cctx;
    zstd_free_cctx_fn free_cctx;
    zstd_create_cdict_fn create_cdict;
    zstd_free_cdict_fn free_cdict;
    zstd_compress_bound_fn compress_bound;
    zstd_compress_cdict_fn compress_cdict;
    zstd_compress_stream_fn compress_stream;
    zstd_ref_cdict_fn ref_cdict;
    zstd_reset_cctx_fn reset_cctx;
    zstd_is_error_fn is_error;
    zstd_error_name_fn error_name;
    void *cctx;
    void *cdict;
    int level;
    PyObject *dictionary;
} WreathDczEncoder;

#define WREATH_DCZ_ENCODER_CAPSULE "wreath.dcz_encoder"

static WreathLibrary
open_zstd_library(void)
{
#ifdef _WIN32
    static const char *names[] = {"libzstd.dll", "zstd.dll", NULL};
#elif defined(__APPLE__)
    static const char *names[] = {"libzstd.1.dylib", "libzstd.dylib", NULL};
#else
    static const char *names[] = {"libzstd.so.1", "libzstd.so", NULL};
#endif
    for (int index = 0; names[index] != NULL; index++) {
        WreathLibrary library = wreath_library_open(names[index]);
        if (library != NULL) return library;
    }
    return NULL;
}

static int
load_zstd_api(WreathDczEncoder *encoder)
{
#define LOAD_ZSTD(member, name)                                               \
    do {                                                                       \
        WreathSymbol symbol = wreath_library_symbol(encoder->library, name);   \
        if (symbol == NULL) return -1;                                         \
        _Static_assert(sizeof(encoder->member) == sizeof(symbol),              \
                       "libzstd symbol pointer has an unsupported size");     \
        memcpy(&encoder->member, &symbol, sizeof(symbol));                     \
    } while (0)
    LOAD_ZSTD(create_cctx, "ZSTD_createCCtx");
    LOAD_ZSTD(free_cctx, "ZSTD_freeCCtx");
    LOAD_ZSTD(create_cdict, "ZSTD_createCDict");
    LOAD_ZSTD(free_cdict, "ZSTD_freeCDict");
    LOAD_ZSTD(compress_bound, "ZSTD_compressBound");
    LOAD_ZSTD(compress_cdict, "ZSTD_compress_usingCDict");
    LOAD_ZSTD(compress_stream, "ZSTD_compressStream2");
    LOAD_ZSTD(ref_cdict, "ZSTD_CCtx_refCDict");
    LOAD_ZSTD(reset_cctx, "ZSTD_CCtx_reset");
    LOAD_ZSTD(is_error, "ZSTD_isError");
    LOAD_ZSTD(error_name, "ZSTD_getErrorName");
#undef LOAD_ZSTD
    return 0;
}

static WreathDczEncoder *dcz_encoder_get(PyObject *capsule);
static int dcz_encoder_level(WreathDczEncoder *encoder, int level);

static PyObject *
wreath_dcz_compress_fragments_workspace(
    PyObject *capsule, PyObject *digest, PyObject *prefix, PyObject *tail,
    int level)
{
    static const unsigned char magic[8] = {
        0x5e, 0x2a, 0x4d, 0x18, 0x20, 0x00, 0x00, 0x00
    };
    if (!PyBytes_CheckExact(digest) || PyBytes_GET_SIZE(digest) != 32) {
        PyErr_SetString(PyExc_ValueError, "DCZ digest must be exactly 32 bytes");
        return NULL;
    }
    if (!PyBytes_Check(prefix) || !PyBytes_CheckExact(tail)) {
        PyErr_SetString(PyExc_TypeError,
                        "DCZ prefix must be bytes and tail exact bytes");
        return NULL;
    }
    WreathDczEncoder *encoder = dcz_encoder_get(capsule);
    if (encoder == NULL) return NULL;
    PyMutex_Lock(&encoder->mutex);
    if (dcz_encoder_level(encoder, level) < 0) {
        PyMutex_Unlock(&encoder->mutex);
        return NULL;
    }
    size_t prefix_size = (size_t)PyBytes_GET_SIZE(prefix);
    size_t tail_size = (size_t)PyBytes_GET_SIZE(tail);
    if (prefix_size > SIZE_MAX - tail_size) {
        PyMutex_Unlock(&encoder->mutex);
        return PyErr_NoMemory();
    }
    size_t bound = encoder->compress_bound(prefix_size + tail_size);
    if (bound > (size_t)PY_SSIZE_T_MAX - 40) {
        PyMutex_Unlock(&encoder->mutex);
        return PyErr_NoMemory();
    }
    PyObject *result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)bound + 40);
    if (result == NULL) {
        PyMutex_Unlock(&encoder->mutex);
        return NULL;
    }
    char *output = PyBytes_AS_STRING(result);
    memcpy(output, magic, sizeof(magic));
    memcpy(output + 8, PyBytes_AS_STRING(digest), 32);
    WreathZstdOut out = {output + 40, bound, 0};
    size_t status = encoder->reset_cctx(encoder->cctx, 1);
    if (!encoder->is_error(status))
        status = encoder->ref_cdict(encoder->cctx, encoder->cdict);
    WreathZstdIn inputs[2] = {
        {PyBytes_AS_STRING(prefix), prefix_size, 0},
        {PyBytes_AS_STRING(tail), tail_size, 0},
    };
    for (int part = 0; part < 2 && !encoder->is_error(status); part++) {
        int end = part == 1 ? 2 : 0;
        do {
            status = encoder->compress_stream(
                encoder->cctx, &out, &inputs[part], end);
        } while (!encoder->is_error(status) &&
                 (inputs[part].pos != inputs[part].size || (end == 2 && status != 0)));
    }
    if (encoder->is_error(status)) {
        const char *message = encoder->error_name(status);
        PyMutex_Unlock(&encoder->mutex);
        Py_DECREF(result);
        PyErr_Format(PyExc_RuntimeError, "libzstd DCZ compression failed: %s", message);
        return NULL;
    }
    PyMutex_Unlock(&encoder->mutex);
    if (_PyBytes_Resize(&result, (Py_ssize_t)out.pos + 40) < 0) return NULL;
    return result;
}

PyObject *
wreath_dcz_compress_fragments_with(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *digest, *prefix, *tail;
    int level;
    if (!PyArg_ParseTuple(args, "OOOOi:dcz_compress_fragments_with",
                          &capsule, &digest, &prefix, &tail, &level)) return NULL;
    return wreath_dcz_compress_fragments_workspace(
        capsule, digest, prefix, tail, level);
}

static void
dcz_encoder_free(WreathDczEncoder *encoder)
{
    if (encoder == NULL) return;
    if (encoder->cdict != NULL) encoder->free_cdict(encoder->cdict);
    if (encoder->cctx != NULL) encoder->free_cctx(encoder->cctx);
    Py_XDECREF(encoder->dictionary);
    if (encoder->library != NULL) wreath_library_close(encoder->library);
    PyMem_Free(encoder);
}

static void
dcz_encoder_destroy(PyObject *capsule)
{
    WreathDczEncoder *encoder = PyCapsule_GetPointer(
        capsule, WREATH_DCZ_ENCODER_CAPSULE);
    if (encoder == NULL) {
        PyErr_WriteUnraisable(capsule);
        return;
    }
    dcz_encoder_free(encoder);
}

static WreathDczEncoder *
dcz_encoder_get(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, WREATH_DCZ_ENCODER_CAPSULE);
}

PyObject *
wreath_dcz_encoder_new(PyObject *Py_UNUSED(self), PyObject *dictionary)
{
    if (!PyBytes_CheckExact(dictionary)) {
        PyErr_SetString(PyExc_TypeError, "DCZ dictionary must be exact bytes");
        return NULL;
    }
    WreathDczEncoder *encoder = PyMem_Calloc(1, sizeof(*encoder));
    if (encoder == NULL) return PyErr_NoMemory();
    encoder->level = INT32_MIN;
    encoder->library = open_zstd_library();
    if (encoder->library == NULL || load_zstd_api(encoder) < 0) {
        dcz_encoder_free(encoder);
        Py_RETURN_NONE;
    }
    encoder->cctx = encoder->create_cctx();
    encoder->dictionary = Py_NewRef(dictionary);
    if (encoder->cctx == NULL) {
        dcz_encoder_free(encoder);
        PyErr_SetString(PyExc_RuntimeError, "libzstd could not allocate a DCZ context");
        return NULL;
    }
    PyObject *capsule = PyCapsule_New(
        encoder, WREATH_DCZ_ENCODER_CAPSULE, dcz_encoder_destroy);
    if (capsule == NULL) dcz_encoder_free(encoder);
    return capsule;
}

static int
dcz_encoder_level(WreathDczEncoder *encoder, int level)
{
    if (encoder->cdict != NULL && encoder->level == level) return 0;
    if (encoder->cdict != NULL) {
        encoder->free_cdict(encoder->cdict);
        encoder->cdict = NULL;
    }
    encoder->cdict = encoder->create_cdict(
        PyBytes_AS_STRING(encoder->dictionary),
        (size_t)PyBytes_GET_SIZE(encoder->dictionary), level);
    if (encoder->cdict == NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                        "libzstd could not prepare the DCZ dictionary");
        return -1;
    }
    encoder->level = level;
    return 0;
}

static PyObject *
wreath_dcz_compress_workspace(
    PyObject *capsule, PyObject *digest, PyObject *body, int level)
{
    static const unsigned char magic[8] = {
        0x5e, 0x2a, 0x4d, 0x18, 0x20, 0x00, 0x00, 0x00
    };
    if (!PyBytes_CheckExact(digest) || PyBytes_GET_SIZE(digest) != 32) {
        PyErr_SetString(PyExc_ValueError, "DCZ digest must be exactly 32 bytes");
        return NULL;
    }
    if (!PyBytes_CheckExact(body)) {
        PyErr_SetString(PyExc_TypeError, "DCZ body must be exact bytes");
        return NULL;
    }
    WreathDczEncoder *encoder = dcz_encoder_get(capsule);
    if (encoder == NULL) return NULL;
    PyMutex_Lock(&encoder->mutex);
    if (dcz_encoder_level(encoder, level) < 0) {
        PyMutex_Unlock(&encoder->mutex);
        return NULL;
    }
    size_t body_size = (size_t)PyBytes_GET_SIZE(body);
    size_t bound = encoder->compress_bound(body_size);
    if (bound > (size_t)PY_SSIZE_T_MAX - 40) {
        PyMutex_Unlock(&encoder->mutex);
        return PyErr_NoMemory();
    }
    PyObject *result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)bound + 40);
    if (result == NULL) {
        PyMutex_Unlock(&encoder->mutex);
        return NULL;
    }
    char *output = PyBytes_AS_STRING(result);
    memcpy(output, magic, sizeof(magic));
    memcpy(output + 8, PyBytes_AS_STRING(digest), 32);
    size_t written = encoder->compress_cdict(
        encoder->cctx, output + 40, bound,
        PyBytes_AS_STRING(body), body_size, encoder->cdict);
    if (encoder->is_error(written)) {
        const char *message = encoder->error_name(written);
        PyMutex_Unlock(&encoder->mutex);
        Py_DECREF(result);
        PyErr_Format(PyExc_RuntimeError, "libzstd DCZ compression failed: %s", message);
        return NULL;
    }
    PyMutex_Unlock(&encoder->mutex);
    if (_PyBytes_Resize(&result, (Py_ssize_t)written + 40) < 0) return NULL;
    return result;
}

PyObject *
wreath_dcz_compress_with(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *digest, *body;
    int level;
    if (!PyArg_ParseTuple(
            args, "OOOi:dcz_compress_with", &capsule, &digest, &body,
            &level)) return NULL;
    return wreath_dcz_compress_workspace(capsule, digest, body, level);
}
