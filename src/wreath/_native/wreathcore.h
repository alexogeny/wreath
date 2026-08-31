/* Shared declarations for the wreath._native._core extension. */
#ifndef WREATH_CORE_H
#define WREATH_CORE_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "activate.h"
#include "header_block.h"
#include "record_api.h"
#include "bytes_writer.h"
#include "sparse_vector.h"

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

/* Read one integer configuration attribute with the CPython error protocol
 * preserved. HTTP/2 and HTTP/3 consume the same ServerConfig surface; keeping
 * this here prevents the protocol modules from growing independent decoders. */
static inline int
wreath_read_ssize_attr(PyObject *config, const char *name, Py_ssize_t *out)
{
    PyObject *value = PyObject_GetAttrString(config, name);
    Py_ssize_t parsed;
    if (value == NULL) return -1;
    parsed = PyLong_AsSsize_t(value);
    Py_DECREF(value);
    if (parsed == -1 && PyErr_Occurred()) return -1;
    *out = parsed;
    return 0;
}

/* Build a two-cell tuple from new references without the INCREF/DECREF round
 * trip imposed by PyTuple_Pack. Output adapters repeatedly materialize pairs
 * immediately after allocating both cells; stealing them is the exact ownership
 * transfer those callers need. */
static inline PyObject *
wreath_tuple2_from_owned(PyObject *first, PyObject *second)
{
    if (first == NULL || second == NULL) {
        Py_XDECREF(first);
        Py_XDECREF(second);
        return NULL;
    }
    PyObject *tuple = PyTuple_New(2);
    if (tuple == NULL) {
        Py_DECREF(first);
        Py_DECREF(second);
        return NULL;
    }
    PyTuple_SET_ITEM(tuple, 0, first);
    PyTuple_SET_ITEM(tuple, 1, second);
    return tuple;
}

/* Parse an HTTP qvalue into thousandths. Both the public web-policy selector
 * and the server policy executor consume this grammar. */
static inline int
wreath_parse_quality(const char *data, Py_ssize_t length)
{
    while (length > 0 && (*data == ' ' || *data == '\t')) {
        data++;
        length--;
    }
    while (length > 0 && (data[length - 1] == ' ' || data[length - 1] == '\t'))
        length--;
    if (length == 1 && data[0] == '0') return 0;
    if (length == 1 && data[0] == '1') return 1000;
    if (length < 3 || length > 5 || data[1] != '.' ||
        (data[0] != '0' && data[0] != '1')) return 0;
    int value = data[0] == '1' ? 1000 : 0;
    int scale = 100;
    for (Py_ssize_t i = 2; i < length; i++, scale /= 10) {
        if (data[i] < '0' || data[i] > '9' ||
            (data[0] == '1' && data[i] != '0')) return 0;
        if (data[0] == '0') value += (data[i] - '0') * scale;
    }
    return value;
}

/* client_facts.c */
int wreath_register_client_facts(PyObject *module);

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
PyObject *wreath_cedar_compile_plan(PyObject *self, PyObject *policies);
PyObject *wreath_cedar_route_denial(PyObject *self, PyObject *args);
PyObject *wreath_cedar_route_denial_prepared(PyObject *self, PyObject *args);
PyObject *wreath_cedar_is_authorized_many(PyObject *self, PyObject *args);
PyObject *wreath_cedar_is_authorized_many_native(PyObject *self, PyObject *args);
int wreath_cedar_decision_batch_read(PyObject *object, Py_ssize_t *count,
                                     const unsigned char **allowed,
                                     const unsigned char **reason);
PyObject *wreath_cedar_to_value(PyObject *self, PyObject *args);

/* env.c */
PyObject *wreath_parse_dotenv(PyObject *self, PyObject *arg);
PyObject *wreath_read_osenv(PyObject *self, PyObject *ignored);

/* codecs.c */
PyObject *wreath_cache_key_selected(PyObject *self, PyObject *args);
PyObject *wreath_float_sequence(PyObject *self, PyObject *args);
PyObject *wreath_array_coerce(PyObject *self, PyObject *args);
PyObject *wreath_map_nullable(PyObject *self, PyObject *args);
PyObject *wreath_sparsevector_parts(PyObject *self, PyObject *args);
PyObject *wreath_sparsevector_data(PyObject *self, PyObject *args);
PyObject *wreath_sparsevector_dim(PyObject *self, PyObject *arg);
PyObject *wreath_sparsevector_len(PyObject *self, PyObject *arg);
PyObject *wreath_sparsevector_indices(PyObject *self, PyObject *arg);
PyObject *wreath_sparsevector_values(PyObject *self, PyObject *arg);
PyObject *wreath_sparsevector_dict(PyObject *self, PyObject *arg);
PyObject *wreath_sparsevector_equal(PyObject *self, PyObject *args);
PyObject *wreath_sparsevector_hash(PyObject *self, PyObject *arg);
PyObject *wreath_cookie_header(PyObject *self, PyObject *args);
PyObject *wreath_parse_cookie_data_raw(const uint8_t *data, Py_ssize_t len);
PyObject *wreath_log_batch(PyObject *self, PyObject *args);
PyObject *wreath_local_walk(PyObject *self, PyObject *args);
PyObject *wreath_recurrence_plan(PyObject *self, PyObject *args);
PyObject *wreath_recurrence_next(PyObject *self, PyObject *args);
PyObject *wreath_attempt_encode(PyObject *self, PyObject *args);
PyObject *wreath_attempt_decode(PyObject *self, PyObject *args);
int wreath_register_zip_builder(PyObject *module);
PyObject *wreath_sigv4_headers(PyObject *self, PyObject *headers);
PyObject *wreath_sigv4_canonical(PyObject *self, PyObject *args);
PyObject *wreath_zip_entry_count(PyObject *self, PyObject *args);
PyObject *wreath_http_exchange_encode(PyObject *self, PyObject *args);
PyObject *wreath_http_exchange_decode(PyObject *self, PyObject *args);
int wreath_http_replay_ready(void);

/* gzip_codec.c: independent RFC 1951/1952 encode/decode kernels. */
PyObject *wreath_gzip_encoder_new(PyObject *self, PyObject *ignored);
PyObject *wreath_gzip_compress(PyObject *self, PyObject *const *args, Py_ssize_t nargs);
PyObject *wreath_gzip_compress_with(PyObject *self, PyObject *const *args, Py_ssize_t nargs);
PyObject *wreath_gzip_compress_workspace(PyObject *workspace, PyObject *data,
                                         int level, PyObject *format);
PyObject *wreath_gzip_fragment_compress_with(PyObject *self, PyObject *const *args,
                                             Py_ssize_t nargs);
PyObject *wreath_gzip_fragment_compress_workspace(
    PyObject *workspace, PyObject *data, int level, PyObject *format,
    PyObject *fragments);
int wreath_gzip_format_object(PyObject *value, int *format);
PyObject *wreath_gzip_format(PyObject *self, PyObject *value);
PyObject *wreath_gzip_decoder_new(PyObject *self, PyObject *ignored);
PyObject *wreath_gzip_decompress(PyObject *self, PyObject *const *args, Py_ssize_t nargs);
PyObject *wreath_gzip_decompress_with(PyObject *self, PyObject *const *args,
                                      Py_ssize_t nargs);
PyObject *wreath_gzip_decompress_workspace(PyObject *workspace, PyObject *data,
                                           Py_ssize_t maximum, PyObject *format);
PyObject *wreath_gzip_codec_info(PyObject *self, PyObject *ignored);

/* data_kernels.c */
PyObject *wreath_first_duplicate(PyObject *self, PyObject *arg);
PyObject *wreath_minimal_prefixes(PyObject *self, PyObject *arg);
PyObject *wreath_fused_order(PyObject *self, PyObject *args);
PyObject *wreath_rank_indices(PyObject *self, PyObject *args);
PyObject *wreath_normalise_argument(PyObject *self, PyObject *args);
PyObject *wreath_transport_decode_parts(PyObject *self, PyObject *args);
PyObject *wreath_fault_encode_parts(PyObject *self, PyObject *args);
PyObject *wreath_fault_decode_parts(PyObject *self, PyObject *args);
PyObject *wreath_dns_parse_txt(PyObject *self, PyObject *args);
PyObject *wreath_siphash24(PyObject *self, PyObject *args);
PyObject *wreath_log_cell_encode(PyObject *self, PyObject *args);
PyObject *wreath_log_cell_decode(PyObject *self, PyObject *args);
PyObject *wreath_capture_slab_decode(PyObject *self, PyObject *args);
PyObject *wreath_step_encode(PyObject *self, PyObject *args);
PyObject *wreath_step_decode(PyObject *self, PyObject *args);
PyObject *wreath_metrics_flatten(PyObject *self, PyObject *args);
PyObject *wreath_locale_preference(PyObject *self, PyObject *args);
PyObject *wreath_select_language(PyObject *self, PyObject *args);
PyObject *wreath_normalize_host(PyObject *self, PyObject *args);
PyObject *wreath_parse_accept(PyObject *self, PyObject *arg);
PyObject *wreath_negotiate_media(PyObject *self, PyObject *args);
PyObject *wreath_bearer_token(PyObject *self, PyObject *arg);
PyObject *wreath_cacheable_headers(PyObject *self, PyObject *arg);
PyObject *wreath_sync_version(PyObject *self, PyObject *arg);
PyObject *wreath_sync_state(PyObject *self, PyObject *arg);
PyObject *wreath_sync_state_diff(PyObject *self, PyObject *args);
PyObject *wreath_sync_state_keys(PyObject *self, PyObject *arg);
PyObject *wreath_sync_state_size(PyObject *self, PyObject *arg);
int wreath_register_data_kernels(PyObject *module);

/* recording.c */
PyObject *wreath_ring_file_records(PyObject *self, PyObject *args);
PyObject *wreath_ring_in_flight(PyObject *self, PyObject *args);
PyObject *wreath_ring_logs_for(PyObject *self, PyObject *args);

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
int wreath_graphql_ready(void);
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
int wreath_graphql_parser_ready(void);
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
PyObject *wreath_signature_compile_pair(PyObject *self, PyObject *args);
PyObject *wreath_signature_plan_facts(PyObject *self, PyObject *capsule);
PyObject *wreath_signature_plan_base(PyObject *self, PyObject *args);
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
int wreath_observability_ready(void);
PyObject *wreath_request_id_valid(PyObject *self, PyObject *args);
PyObject *wreath_format_server_timing(PyObject *self, PyObject *args);
PyObject *wreath_prometheus_route_blocks(PyObject *self, PyObject *args);
PyObject *wreath_prometheus_global_block(PyObject *self, PyObject *args);
PyObject *wreath_prometheus_counter_block(PyObject *self, PyObject *args);
PyObject *wreath_prometheus_document(PyObject *self, PyObject *args);
PyObject *wreath_statsd_lines(PyObject *self, PyObject *args);
PyObject *wreath_statsd_packets(PyObject *self, PyObject *args);
PyObject *wreath_emf_render(PyObject *self, PyObject *args);

/* client_facts.c -- native policy boundary, no Python result materialization. */
int wreath_user_agent_blocked(PyObject *database, PyObject *value,
                              PyObject *table, int *blocked);
int wreath_user_agent_database_check(PyObject *database);
PyObject *wreath_metric_delta_state(PyObject *self, PyObject *arg);

/* series.c */
PyObject *wreath_series_reconcile(PyObject *self, PyObject *args);
PyObject *wreath_series_cell_rows(PyObject *self, PyObject *args);
PyObject *wreath_series_dense_rows(PyObject *self, PyObject *args);
PyObject *wreath_series_spine(PyObject *self, PyObject *args);
PyObject *wreath_series_spine_length(PyObject *self, PyObject *args);
PyObject *wreath_series_spine_lengths(PyObject *self, PyObject *args);
PyObject *wreath_format_duration_parts(PyObject *self, PyObject *args);
PyObject *wreath_format_iso_datetime(PyObject *self, PyObject *args);
PyObject *wreath_relative_english(PyObject *self, PyObject *args);
PyObject *wreath_relative_english_between(PyObject *self, PyObject *args);
PyObject *wreath_series_lttb(PyObject *self, PyObject *args);
PyObject *wreath_series_path(PyObject *self, PyObject *args);
PyObject *wreath_series_nice_ticks(PyObject *self, PyObject *args);
PyObject *wreath_series_chart(PyObject *self, PyObject *args);
PyObject *wreath_series_chart_text(PyObject *self, PyObject *args);
PyObject *wreath_series_chart_spine(PyObject *self, PyObject *args);
PyObject *wreath_series_data(PyObject *self, PyObject *args);
PyObject *wreath_series_data_chart(PyObject *self, PyObject *args);
PyObject *wreath_series_data_chart_text(PyObject *self, PyObject *args);
PyObject *wreath_series_data_chart_plan(PyObject *self, PyObject *args);
PyObject *wreath_series_chart_plan(PyObject *self, PyObject *plan);
PyObject *wreath_series_chart_plan_text(PyObject *self, PyObject *plan);
PyObject *wreath_series_chart_plan_text_joined(PyObject *self, PyObject *plan);
int wreath_series_ready(void);

PyObject *wreath_dcz_encoder_new(PyObject *self, PyObject *dictionary);
PyObject *wreath_dcz_compress_with(PyObject *self, PyObject *args);
PyObject *wreath_dcz_compress_fragments_with(PyObject *self, PyObject *args);

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
#define WREATH_VALIDATE_MAX_STEPS 2000000
PyObject *wreath_validate_node(PyObject *plan, PyObject *value, PyObject *loc,
                               PyObject *errors, long *steps);
PyObject *wreath_compile_validation_plan(PyObject *self, PyObject *source);
PyObject *wreath_validation_plan_source(PyObject *object);
PyObject *wreath_validate_plan_field(PyObject *plan, Py_ssize_t field_index,
                                     PyObject *value, PyObject *loc,
                                     PyObject *errors, long *steps);
PyObject *wreath_run_validation(PyObject *self, PyObject *args);
PyObject *wreath_run_validation_json(PyObject *self, PyObject *args);

/* orm_shape.c */
PyObject *wreath_orm_shape(PyObject *self, PyObject *args);
PyObject *wreath_orm_shape_configure(PyObject *self, PyObject *args);
PyObject *wreath_orm_relationship_keys(PyObject *self, PyObject *args);
PyObject *wreath_orm_attach_relationships(PyObject *self, PyObject *args);
PyObject *wreath_orm_compile_hydrate_plan(PyObject *self, PyObject *args);
PyObject *wreath_orm_hydrate_records(PyObject *self, PyObject *args);
PyObject *wreath_orm_collect_values(PyObject *self, PyObject *args);

/* codecs.c */
PyObject *wreath_percent_decode(PyObject *self, PyObject *args, PyObject *kwargs);
PyObject *wreath_parse_qs(PyObject *self, PyObject *args);
PyObject *wreath_bind_query_into(PyObject *self, PyObject *const *args,
                                 Py_ssize_t nargs);
PyObject *wreath_page_params(PyObject *self, PyObject *args);
PyObject *wreath_parse_form_urlencoded(PyObject *self, PyObject *args);
PyObject *wreath_parse_cookies(PyObject *self, PyObject *args);
PyObject *wreath_parse_cookie_headers(PyObject *self, PyObject *args);

/* sql.c */
PyObject *wreath_sql_renumber(PyObject *self, PyObject *args);

/* dkim.c */
PyObject *wreath_dkim_canonicalize_body(PyObject *self, PyObject *body);

/* recording.c */
PyObject *wreath_recording_event_cells(PyObject *self, PyObject *args);

/* scim.c */
int wreath_scim_ready(void);
PyObject *wreath_scim_parse(PyObject *self, PyObject *args);
PyObject *wreath_scim_compile(PyObject *self, PyObject *args);
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
    /* Parse a complete Cookie field after the caller has joined split lines.
     * The returned dict is the public Python boundary; scanning and joining
     * stay in the sibling extension that owns its native header block. */
    PyObject *(*parse_cookie_data)(const uint8_t *data, Py_ssize_t len);
    /* Scan every product token and report whether any stable rule id occurs in
     * the operation-owned packed table. No classification tuple crosses the
     * sibling-extension boundary. */
    int (*user_agent_blocked)(PyObject *database, PyObject *value,
                              PyObject *table, int *blocked);
    int (*user_agent_database_check)(PyObject *database);
    /* Reuse application-owned encoder workspace without crossing through a
     * Python callable from the sibling server extensions. */
    PyObject *(*gzip_compress)(PyObject *workspace, PyObject *data,
                               int level, PyObject *format);
    PyObject *(*gzip_fragment_compress)(PyObject *workspace, PyObject *data,
                                        int level, PyObject *format,
                                        PyObject *fragments);
    int (*gzip_format)(PyObject *value, int *format);
    /* Policy response edits stay inside _core but are callable by sibling
     * transports without a Python vectorcall or a duplicate implementation. */
    int (*replace_cookie)(PyObject *headers, PyObject *prefix, PyObject *value);
    int (*replace_server_timing)(PyObject *headers, PyObject *metric,
                                 PyObject *value);
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
PyObject *wreath_json_loads_validation(PyObject *arg, PyObject *plan,
                                       PyObject *loc_seq);
PyObject *wreath_json_configure(PyObject *self, PyObject *args);
PyObject *wreath_jsonpath_find(PyObject *self, PyObject *args);
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
int wreath_protobuf_ready(void);
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
PyObject *wreath_template_render_compiled(PyObject *self, PyObject *args);
PyObject *wreath_template_render_compiled_tail(PyObject *self, PyObject *args);
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

/* One parsed outbound response head.  The request-owned HTTP client consumes
 * this directly so framing and connection policy remain scalars in C; the
 * standalone Python codec materializes its historical five-tuple only at its
 * public boundary.  `headers` is the final tuple of raw byte pairs and
 * `reason` is the final bytes object. */
typedef struct {
    PyObject *headers;
    PyObject *reason;
    Py_ssize_t consumed;
    Py_ssize_t content_length;
    int minor;
    int status;
    int framing;   /* 0 none, 1 chunked, 2 content-length, 3 close-delimited */
    int reusable;
} WreathHttpResponseHead;

int wreath_http_parse_response_parts(
    const uint8_t *data, Py_ssize_t size, PyObject *method,
    WreathHttpResponseHead *head
);
void wreath_http_response_head_clear(WreathHttpResponseHead *head);
PyObject *wreath_http_parse_response(PyObject *self, PyObject *args);
PyObject *wreath_http_response_framing(PyObject *self, PyObject *args);
PyObject *wreath_http_response_keeps_alive(PyObject *self, PyObject *args);
PyObject *wreath_http_parse_chunk_size(PyObject *self, PyObject *arg);
PyObject *wreath_http_serialize_request(PyObject *self, PyObject *args);
int wreath_register_http_client_protocol(PyObject *module);
PyObject *wreath_http_client_configure_fast_path(PyObject *self, PyObject *args);
PyObject *wreath_http_client_request_once(PyObject *self, PyObject *args);
PyObject *wreath_http_client_request_default(PyObject *self, PyObject *args);
PyObject *wreath_http_client_counters_new(PyObject *self, PyObject *ignored);
PyObject *wreath_http_client_counters_snapshot(PyObject *self, PyObject *capsule);

/* webpolicy.c */
PyObject *wreath_select_content_encoding(PyObject *self, PyObject *arg);
PyObject *wreath_select_prepared_content_encoding(PyObject *self, PyObject *args);
PyObject *wreath_is_compressible_content_type(PyObject *self, PyObject *arg);
PyObject *wreath_cache_control_flags(PyObject *self, PyObject *arg);
PyObject *wreath_origin_matches(PyObject *self, PyObject *args);
PyObject *wreath_append_missing_headers(PyObject *self, PyObject *args);
PyObject *wreath_append_vary(PyObject *self, PyObject *args);
PyObject *wreath_replace_content_length(PyObject *self, PyObject *args);
PyObject *wreath_replace_response_header(PyObject *self, PyObject *args);
PyObject *wreath_replace_cookie(PyObject *self, PyObject *args);
PyObject *wreath_replace_server_timing(PyObject *self, PyObject *args);
int wreath_replace_cookie_inplace(PyObject *headers, PyObject *prefix,
                                  PyObject *value);
int wreath_replace_server_timing_inplace(PyObject *headers, PyObject *metric,
                                         PyObject *value);
PyObject *wreath_find_response_header(PyObject *self, PyObject *args);
int wreath_register_webpolicy(PyObject *module);

/* policy_router.c: adds the canonical PolicyRouteTable; returns -1 on failure. */
int wreath_register_policy_router(PyObject *module);

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
