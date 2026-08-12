/* Shared declarations for the wreath._native._core extension. */
#ifndef WREATH_CORE_H
#define WREATH_CORE_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "activate.h"
#include "header_block.h"
#include "record_api.h"
#include "bytes_writer.h"

#include <stdint.h>
#include <string.h>

/* For `wreath_find`, which `wreath_memmem` below dispatches short needles to.
 * `simd.h` is free of Python.h by design, so including it here is one-way. */
#include "simd.h"

/* Fixed-width integer load/store, by the byte order named in the call.
 * Shared with every other extension rather than owned by this one -- see the
 * header for why the block moved out. */
#include "byteorder.h"

/* ASCII folding and case-insensitive compare, shared for the same reason. */
#include "ascii.h"

/* authz.c */
PyObject *wreath_build_capability_mask(PyObject *self, PyObject *args);
PyObject *wreath_build_compiled_capability_mask(PyObject *self, PyObject *args);
PyObject *wreath_compiled_capability_mask(PyObject *descriptor, PyObject *roles,
                                          PyObject *permissions);
int wreath_compiled_capability_word(PyObject *descriptor, PyObject *roles,
                                    PyObject *permissions,
                                    unsigned long long *mask_out);
PyObject *wreath_normalize_authorization_decision(PyObject *self, PyObject *args);

/* cedar.c */
PyObject *wreath_cedar_is_authorized(PyObject *self, PyObject *args);
PyObject *wreath_cedar_is_authorized_many(PyObject *self, PyObject *args);
PyObject *wreath_cedar_to_value(PyObject *self, PyObject *args);

/* env.c */
PyObject *wreath_parse_dotenv(PyObject *self, PyObject *arg);
PyObject *wreath_read_osenv(PyObject *self, PyObject *ignored);

/* codecs.c */
PyObject *wreath_cache_key_selected(PyObject *self, PyObject *args);

/* security.c */
PyObject *wreath_host_allowed(PyObject *self, PyObject *args);
PyObject *wreath_csrf_sign(PyObject *self, PyObject *args);
PyObject *wreath_random_hex(PyObject *self, PyObject *args);
PyObject *wreath_csrf_new_token(PyObject *self, PyObject *args);
PyObject *wreath_csrf_validate(PyObject *self, PyObject *args);
/* Resolves _hashlib.hmac_digest once; returns -1 on failure. */
int wreath_security_ready(void);

/* jose.c: JWT/JOSE fast paths (base64url, split, HS* verify, claim checks).
 * RSA (RS and PS family) verification lives in the wreath._auth.jwt facade. */
PyObject *wreath_jose_b64url_decode(PyObject *self, PyObject *arg);
PyObject *wreath_jose_parse(PyObject *self, PyObject *args);
PyObject *wreath_jose_verify_hs(PyObject *self, PyObject *args);
PyObject *wreath_jose_verify_rsa(PyObject *self, PyObject *args);
PyObject *wreath_jose_validate_claims(PyObject *self, PyObject *args);
/* Resolves _hashlib.hmac_digest once; returns -1 on failure. */
int wreath_jose_ready(void);

/* grpc.c: incremental five-byte gRPC message deframing. */
int wreath_register_grpc(PyObject *module);

/* graphql.c: bulk row projection and relationship layout. */
PyObject *wreath_graphql_new_results(PyObject *self, PyObject *instances);
PyObject *wreath_graphql_finish_results(PyObject *self, PyObject *builder);
PyObject *wreath_graphql_project_plain(PyObject *self, PyObject *args);
PyObject *wreath_graphql_project_json(PyObject *self, PyObject *args);
int wreath_graphql_write_projection(WreathBytesWriter *writer, PyObject *capsule,
                                    int depth);
PyObject *wreath_graphql_project_constant(PyObject *self, PyObject *args);
PyObject *wreath_graphql_project_attribute(PyObject *self, PyObject *args);
PyObject *wreath_graphql_project_values(PyObject *self, PyObject *args);
PyObject *wreath_graphql_flatten_values(PyObject *self, PyObject *args);
PyObject *wreath_graphql_flatten_relationship(PyObject *self, PyObject *args);
PyObject *wreath_graphql_restore_layout(PyObject *self, PyObject *args);
PyObject *wreath_graphql_restore_values(PyObject *self, PyObject *args);
PyObject *wreath_graphql_parse(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_schema(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_state(PyObject *self, PyObject *schema);
PyObject *wreath_graphql_policy_prepare(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_resources(PyObject *self, PyObject *plan);
PyObject *wreath_graphql_policy_items(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_apply(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_result(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_cached(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_store(PyObject *self, PyObject *args);
PyObject *wreath_graphql_policy_resource(PyObject *self, PyObject *args);
PyObject *wreath_flight_project_cells(PyObject *self, PyObject *args);
PyObject *wreath_flight_settle(PyObject *self, PyObject *args);
PyObject *wreath_flight_evict_pending(PyObject *self, PyObject *args);
PyObject *wreath_flight_metadata_bytes(PyObject *self, PyObject *args);
PyObject *wreath_flight_metadata_decode(PyObject *self, PyObject *args);
int wreath_register_flight_project(PyObject *module);
PyObject *wreath_signature_parse_dictionary(PyObject *self, PyObject *args);
PyObject *wreath_signature_parse_string(PyObject *self, PyObject *args);
PyObject *wreath_signature_base(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_add(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_negate(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_equal(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_scalar(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_double_scalar(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_recover_x(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_decode(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_encode(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_verify(PyObject *self, PyObject *args);
PyObject *wreath_curve_ed_public_key(PyObject *self, PyObject *arg);
PyObject *wreath_curve_ed_sign(PyObject *self, PyObject *args);
PyObject *wreath_curve_p256_on_curve(PyObject *self, PyObject *args);
PyObject *wreath_curve_p256_double_scalar(PyObject *self, PyObject *args);
PyObject *wreath_curve_p256_scalar(PyObject *self, PyObject *args);
PyObject *wreath_curve_p256_verify(PyObject *self, PyObject *args);
PyObject *wreath_curve_p256_sign(PyObject *self, PyObject *args);

/* observability.c */
PyObject *wreath_request_id_valid(PyObject *self, PyObject *args);
PyObject *wreath_format_server_timing(PyObject *self, PyObject *args);
PyObject *wreath_prometheus_route_blocks(PyObject *self, PyObject *args);
PyObject *wreath_prometheus_counter_block(PyObject *self, PyObject *args);
PyObject *wreath_statsd_lines(PyObject *self, PyObject *args);
PyObject *wreath_emf_render(PyObject *self, PyObject *args);
PyObject *wreath_metric_delta_state(PyObject *self, PyObject *arg);

/* series.c */
PyObject *wreath_series_reconcile(PyObject *self, PyObject *args);
PyObject *wreath_series_dense_rows(PyObject *self, PyObject *args);
PyObject *wreath_series_spine(PyObject *self, PyObject *args);
PyObject *wreath_series_spine_length(PyObject *self, PyObject *args);
PyObject *wreath_series_lttb(PyObject *self, PyObject *args);
PyObject *wreath_series_path(PyObject *self, PyObject *args);
PyObject *wreath_series_nice_ticks(PyObject *self, PyObject *args);
PyObject *wreath_series_chart(PyObject *self, PyObject *args);
PyObject *wreath_series_chart_spine(PyObject *self, PyObject *args);

/* proxy.c: adds the TrustedNetworks type; returns -1 on failure. */
int wreath_register_proxy(PyObject *module);

/* ratelimit.c: adds the TokenBucket type; returns -1 on failure. */
int wreath_register_ratelimit(PyObject *module);
/* kv.c: binds a vectorcall frame onto `slots` (pre-filled with defaults) using
 * `names` as the positional order. Shared with queue.c so both hot primitives
 * can be METH_FASTCALL rather than METH_VARARGS; the comment above the
 * definition records what that convention is worth and why it is not
 * housekeeping. Returns 0, or -1 with an exception set. */
int wreath_bind_args(PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames,
                     const char *const *names, PyObject **slots, Py_ssize_t count,
                     Py_ssize_t required, const char *what);

/* kv.c: adds the KV type; returns -1 on failure. */
int wreath_register_kv(PyObject *module);
/* queue.c: adds Queue, QueueEmpty and QueueFull; returns -1 on failure. */
int wreath_register_queue(PyObject *module);
int wreath_register_scheduler(PyObject *module);

/* headers.c */
PyObject *wreath_find_header(PyObject *self, PyObject *args);
PyObject *wreath_build_header_map(PyObject *self, PyObject *args);

/* validate.c */
PyObject *wreath_run_validation(PyObject *self, PyObject *args);
PyObject *wreath_run_validation_json(PyObject *self, PyObject *args);

/* orm_shape.c */
PyObject *wreath_orm_shape(PyObject *self, PyObject *args);
PyObject *wreath_orm_shape_configure(PyObject *self, PyObject *args);
PyObject *wreath_orm_relationship_keys(PyObject *self, PyObject *args);
PyObject *wreath_orm_attach_relationships(PyObject *self, PyObject *args);
PyObject *wreath_orm_hydrate_records(PyObject *self, PyObject *args);
PyObject *wreath_orm_assemble_joins(PyObject *self, PyObject *args);
PyObject *wreath_orm_collect_values(PyObject *self, PyObject *args);

/* codecs.c */
PyObject *wreath_percent_decode(PyObject *self, PyObject *args, PyObject *kwargs);
PyObject *wreath_parse_qs(PyObject *self, PyObject *args);
PyObject *wreath_parse_cookies(PyObject *self, PyObject *args);

/* sql.c */
PyObject *wreath_sql_renumber(PyObject *self, PyObject *args);

/* dkim.c */
PyObject *wreath_dkim_canonicalize_body(PyObject *self, PyObject *body);

/* recording.c */
PyObject *wreath_recording_event_cells(PyObject *self, PyObject *args);

/* scim.c */
PyObject *wreath_scim_parse(PyObject *self, PyObject *args);
PyObject *wreath_scim_values_at(PyObject *self, PyObject *args);
PyObject *wreath_scim_matches(PyObject *self, PyObject *args);
PyObject *wreath_scim_filter(PyObject *self, PyObject *args);

/* ws.c */
PyObject *wreath_ws_mask(PyObject *self, PyObject *args);
PyObject *wreath_b64encode(PyObject *self, PyObject *args, PyObject *kwargs);
PyObject *wreath_simd_arms(PyObject *self, PyObject *ignored);
PyObject *wreath_simd_probe(PyObject *self, PyObject *args);
PyObject *wreath_ws_parse_frame(PyObject *self, PyObject *args);
PyObject *wreath_ws_build_frame(PyObject *self, PyObject *args, PyObject *kwargs);

/* WebSocket frame header, filled by the C-level parser shared through the
 * WREATH_CORE_CAPI capsule so sibling extensions (the native server) can parse
 * frames without a Python call per frame. */
typedef struct {
    int fin;
    int opcode;
    int masked;
    Py_ssize_t header_len;    /* bytes before the payload, mask key included */
    Py_ssize_t payload_len;
    const uint8_t *mask_key;  /* points into the input; NULL when unmasked */
} WreathWsFrameHeader;

typedef struct {
    /* Returns 0 with *out filled when a complete header is available,
     * 1 when more bytes are needed, -1 on a protocol error (reserved bits,
     * oversized length).  Never touches the Python error indicator. */
    int (*ws_parse_header)(const uint8_t *buf, Py_ssize_t len, WreathWsFrameHeader *out);
    /* XOR (un)mask src into dst with the 4-byte key. dst may equal src. */
    void (*ws_unmask)(uint8_t *dst, const uint8_t *src, Py_ssize_t len,
                      const uint8_t *key);
} WreathCoreCAPI;

#define WREATH_CORE_CAPI_NAME "wreath._native._core._C_API"

int wreath_ws_parse_header_raw(const uint8_t *buf, Py_ssize_t len, WreathWsFrameHeader *out);
void wreath_ws_unmask_raw(uint8_t *dst, const uint8_t *src, Py_ssize_t len,
                       const uint8_t *key);

/* multipart.c */
PyObject *wreath_multipart_parse(PyObject *self, PyObject *args, PyObject *kwds);
PyObject *wreath_multipart_part_info(PyObject *self, PyObject *arg);
int wreath_register_multipart(PyObject *module);

/* json.c */
PyObject *wreath_json_dumps(PyObject *self, PyObject *arg);
PyObject *wreath_json_loads(PyObject *self, PyObject *arg);
PyObject *wreath_json_configure(PyObject *self, PyObject *args);
int wreath_json_write_string(WreathBytesWriter *writer, PyObject *value);
int wreath_json_write_value(WreathBytesWriter *writer, PyObject *value, int depth);

/* msgpack.c */
PyObject *wreath_msgpack_dumps(PyObject *self, PyObject *arg);

/* aesgcm.c: scalar AES-128-GCM, dispatched to AES-NI/PCLMULQDQ where
 * available. Explicit scalar entries keep both native kernels testable. */
PyObject *wreath_aesgcm_arms(PyObject *self, PyObject *ignored);
PyObject *wreath_aes128gcm_encrypt(PyObject *self, PyObject *args);
PyObject *wreath_aes128gcm_decrypt(PyObject *self, PyObject *args);
PyObject *wreath_aes128gcm_encrypt_scalar(PyObject *self, PyObject *args);
PyObject *wreath_aes128gcm_decrypt_scalar(PyObject *self, PyObject *args);

/* geospatial.c */
PyObject *wreath_geo_haversine(PyObject *self, PyObject *const *args, Py_ssize_t nargs);
PyObject *wreath_geo_trajectory_grid_summary(PyObject *self, PyObject *args);
PyObject *wreath_geo_trajectory_compile(PyObject *self, PyObject *source);
PyObject *wreath_geo_trajectory_fixes(PyObject *self, PyObject *capsule);
PyObject *wreath_geo_trajectory_info(PyObject *self, PyObject *capsule);
PyObject *wreath_geo_trajectory_between(PyObject *self, PyObject *args);

/* protobuf.c */
PyObject *wreath_protobuf_compile(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_encode(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_decode(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_encode_message(PyObject *self, PyObject *message);
PyObject *wreath_protobuf_decode_message(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_otlp_from_json(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_encode_otlp_json(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_encode_otlp_metrics(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_encode_otlp_traces(PyObject *self, PyObject *args);
PyObject *wreath_protobuf_encode_otlp_logs(PyObject *self, PyObject *args);

/* sse.c */
PyObject *wreath_sse_frame(PyObject *self, PyObject *args);

/* xml.c: the strict XML profile behind wreath.xml. `wreath_xml_configure`
 * hands over the XMLRefusal type so C raises the class `wreath._xml_model`
 * declares; without it every refusal is a RuntimeError instead. */
PyObject *wreath_xml_configure(PyObject *self, PyObject *arg);
PyObject *wreath_xml_parse(PyObject *self, PyObject *args);
PyObject *wreath_xml_c14n(PyObject *self, PyObject *args);

/* templates.c */
PyObject *wreath_template_compile(PyObject *self, PyObject *arg);
PyObject *wreath_template_render(PyObject *self, PyObject *args);
PyObject *wreath_template_render_compiled(PyObject *self, PyObject *args);
PyObject *wreath_template_configure(PyObject *self, PyObject *args);
PyObject *wreath_template_record_configure(PyObject *self, PyObject *capsule);
PyObject *wreath_html_response_configure(PyObject *self, PyObject *args);

/* http.c */
typedef struct {
    Py_ssize_t host_count;
    Py_ssize_t length;
    int kind;              /* 0 none, 1 fixed, 2 chunked */
    int err_status;        /* 0 or the HTTP status that rejects this head */
    int send_continue;
    int keep_alive;
    int upgrade_request;
} WreathHttpRequestMeta;

int wreath_http_parse_request_parts(
    const uint8_t *data, Py_ssize_t len, Py_ssize_t head_end_off,
    PyObject **method, PyObject **target, int *minor_version,
    PyObject **headers, Py_ssize_t *consumed, Py_ssize_t max_headers,
    WreathHttpRequestMeta *request_meta
);
PyObject *wreath_http_parse_request(PyObject *self, PyObject *args);
PyObject *wreath_http_parse_response(PyObject *self, PyObject *args);
PyObject *wreath_http_response_framing(PyObject *self, PyObject *args);
PyObject *wreath_http_response_keeps_alive(PyObject *self, PyObject *args);
PyObject *wreath_http_serialize_request(PyObject *self, PyObject *args);
int wreath_register_http_client_protocol(PyObject *module);

/* webpolicy.c */
PyObject *wreath_select_content_encoding(PyObject *self, PyObject *arg);
PyObject *wreath_is_compressible_content_type(PyObject *self, PyObject *arg);
PyObject *wreath_cache_control_flags(PyObject *self, PyObject *arg);
PyObject *wreath_origin_matches(PyObject *self, PyObject *args);
PyObject *wreath_append_missing_headers(PyObject *self, PyObject *args);
PyObject *wreath_append_vary(PyObject *self, PyObject *args);
PyObject *wreath_replace_content_length(PyObject *self, PyObject *args);
PyObject *wreath_replace_response_header(PyObject *self, PyObject *args);
PyObject *wreath_replace_cookie(PyObject *self, PyObject *args);
PyObject *wreath_replace_server_timing(PyObject *self, PyObject *args);
PyObject *wreath_find_response_header(PyObject *self, PyObject *args);
int wreath_register_webpolicy(PyObject *module);

/* router.c: adds the RouteTable type to the module; returns -1 on failure. */
int wreath_register_router(PyObject *module);

/* dtrouter.c: adds the DecisionRouteTable type; returns -1 on failure. */
int wreath_register_dtrouter(PyObject *module);
int wreath_register_dtbitset(PyObject *module);

/* Substring search.
 *
 * Delegates to libc memmem (two-way algorithm, linear worst case) where it
 * exists — this matters for attacker-controlled inputs like multipart
 * boundaries. CPython's pyconfig.h defines _GNU_SOURCE, so glibc declares
 * memmem.
 *
 * Define WREATH_FORCE_PORTABLE_MEMMEM to select the portable path on a platform
 * that would otherwise use libc. That is a test/diagnostic switch only: it
 * deliberately does not participate in the release platform detection below,
 * because a result produced by glibc's memmem says nothing about the fallback.
 */
#if !defined(WREATH_FORCE_PORTABLE_MEMMEM) && \
    (defined(__GLIBC__) || defined(__APPLE__) || defined(__FreeBSD__) || \
     defined(__OpenBSD__) || defined(__NetBSD__) || \
     (defined(__linux__) && !defined(__ANDROID__)))
#define WREATH_MEMMEM_USES_LIBC 1
#else
#define WREATH_MEMMEM_USES_LIBC 0
#endif

#if !WREATH_MEMMEM_USES_LIBC
/* Maximal suffix of `needle` under a byte ordering (`reverse` flips it).
 *
 * Returns the index of the maximal suffix (-1 when it is the whole string) and
 * stores its period. Half of the Crochemore-Perrin critical factorization. */
static inline Py_ssize_t
wreath_max_suffix(const uint8_t *needle, Py_ssize_t len, Py_ssize_t *period, int reverse)
{
    Py_ssize_t ms = -1, j = 0, k = 1, p = 1;
    while (j + k < len) {
        uint8_t a = needle[j + k];
        uint8_t b = needle[ms + k];   /* ms == -1 reads needle[k - 1] */
        int a_before_b = reverse ? (a > b) : (a < b);
        int b_before_a = reverse ? (a < b) : (a > b);
        if (a_before_b) {
            j += k;
            k = 1;
            p = j - ms;
        }
        else if (b_before_a) {
            ms = j;
            j = ms + 1;
            k = p = 1;
        }
        else if (k != p) {          /* a == b, still inside the period */
            k++;
        }
        else {
            j += p;
            k = 1;
        }
    }
    *period = p;
    return ms;
}

/* Critical factorization: the position splitting the needle into halves whose
 * local period is the needle's period. Returns the first index of the right
 * half and stores that period. */
static inline Py_ssize_t
wreath_critical_factorization(const uint8_t *needle, Py_ssize_t len, Py_ssize_t *period)
{
    Py_ssize_t forward_period, reverse_period;
    Py_ssize_t forward = wreath_max_suffix(needle, len, &forward_period, 0);
    Py_ssize_t reverse = wreath_max_suffix(needle, len, &reverse_period, 1);
    if (reverse < forward) {        /* keep the longer left half */
        *period = forward_period;
        return forward + 1;
    }
    *period = reverse_period;
    return reverse + 1;
}

/* Two-way substring search (Crochemore-Perrin): linear worst case with no
 * allocation and no shift table, which suits the short needles this sees (a
 * multipart delimiter is at most 74 bytes). The naive memchr-accelerated scan
 * this replaces was O(hay * needle) on repetitive input with overlapping
 * prefixes -- exactly what an attacker-supplied boundary can produce. */
static inline const uint8_t *
wreath_two_way(const uint8_t *hay, Py_ssize_t hay_len, const uint8_t *needle,
            Py_ssize_t needle_len)
{
    Py_ssize_t period;
    Py_ssize_t suffix = wreath_critical_factorization(needle, needle_len, &period);
    Py_ssize_t j = 0;

    if (period + suffix <= needle_len &&
        memcmp(needle, needle + period, (size_t)suffix) == 0) {
        /* The needle is periodic: a mismatch can only advance by the period, so
         * remember the prefix already known to match and never rescan it. */
        Py_ssize_t memory = 0;
        while (j <= hay_len - needle_len) {
            Py_ssize_t i = suffix > memory ? suffix : memory;
            while (i < needle_len && needle[i] == hay[i + j]) {
                i++;
            }
            if (i < needle_len) {
                j += i - suffix + 1;
                memory = 0;
                continue;
            }
            i = suffix - 1;
            while (i >= memory && needle[i] == hay[i + j]) {
                i--;
            }
            if (i < memory) {
                return hay + j;
            }
            j += period;
            memory = needle_len - period;
        }
        return NULL;
    }

    /* Aperiodic: a mismatch may advance past the whole left or right half. */
    Py_ssize_t shift = (suffix > needle_len - suffix ? suffix : needle_len - suffix) + 1;
    while (j <= hay_len - needle_len) {
        Py_ssize_t i = suffix;
        while (i < needle_len && needle[i] == hay[i + j]) {
            i++;
        }
        if (i < needle_len) {
            j += i - suffix + 1;
            continue;
        }
        i = suffix - 1;
        while (i >= 0 && needle[i] == hay[i + j]) {
            i--;
        }
        if (i < 0) {
            return hay + j;
        }
        j += shift;
    }
    return NULL;
}
#endif /* !WREATH_MEMMEM_USES_LIBC */

/* Needles at or below this length go to the vector scan; longer ones stay on
 * the two-way search.
 *
 * The two are good at opposite things and the crossover is measured, not
 * guessed. A two-way search skips by up to the needle's length on a mismatch,
 * so it gets *faster* as the needle grows; the vector scan compares the
 * needle's first and last bytes against 32-byte strides and so runs at a flat
 * rate whatever the needle is. Over a 16 KiB haystack with the match at the
 * end (2026-08-01, 11 interleaved rounds against an A/A control):
 *
 *     needle     glibc      avx2    winner
 *          2   15.470us    1.310us  avx2, 11.8x
 *          4    5.746us    1.326us  avx2, 4.3x
 *          8    2.659us    1.314us  avx2, 2.0x
 *         16    1.424us    1.315us  tie
 *         24    1.011us    1.242us  glibc
 *         64    0.611us    1.246us  glibc, 2.0x
 *
 * So this deliberately does **not** take the multipart delimiter, which is
 * CRLF + "--" + a boundary of up to 70 bytes and is exactly where the
 * two-way search is strongest -- the case that motivated writing the kernel in
 * the first place, and the one it loses. What it does take is every short
 * needle in the tree: `\r\n\r\n` ending a request head, and the `\r\n` the
 * multipart header scanner runs per header line.
 */
#define WREATH_SIMD_NEEDLE_MAX 16

static inline const uint8_t *
wreath_memmem(const uint8_t *hay, Py_ssize_t hay_len, const uint8_t *needle, Py_ssize_t needle_len)
{
    if (needle_len <= 0 || hay_len < needle_len) {
        return NULL;
    }
    if (needle_len == 1) {
        /* Never the vector scan: glibc's `memchr` is already vectorised and
         * beating it is not on offer. */
        return (const uint8_t *)memchr(hay, needle[0], (size_t)hay_len);
    }
    if (needle_len <= WREATH_SIMD_NEEDLE_MAX) {
        return wreath_find(hay, (ptrdiff_t)hay_len, needle, (ptrdiff_t)needle_len);
    }
#if WREATH_MEMMEM_USES_LIBC
    return memmem(hay, (size_t)hay_len, needle, (size_t)needle_len);
#else
    return wreath_two_way(hay, hay_len, needle, needle_len);
#endif
}

#endif /* WREATH_CORE_H */
