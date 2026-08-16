#ifndef WREATH_SERVER_POLICY_H
#define WREATH_SERVER_POLICY_H

#include <Python.h>
#include <stdint.h>
#include "flight.h"
#include "header_block.h"
#include "wreathcore.h"

/* Frozen descriptor indexes; mirrored by policy.HttpPolicy._freeze_native. */
enum {
    WREATH_POLICY_TAG = 0,
    WREATH_POLICY_PROXY = 1,
    WREATH_POLICY_TRUSTED_HOST = 2,
    WREATH_POLICY_AI_SCRAPING = 3,
    WREATH_POLICY_RATE = 4,
    WREATH_POLICY_REQUEST_ID = 5,
    WREATH_POLICY_TIMING = 6,
    WREATH_POLICY_CORS = 7,
    WREATH_POLICY_CSRF = 8,
    WREATH_POLICY_SECURITY = 9,
    WREATH_POLICY_WEBSOCKET_ORIGIN = 10,
    WREATH_POLICY_CACHE = 11,
    WREATH_POLICY_COMPRESSION = 12,
    WREATH_POLICY_SIZE = 13,
};

enum {
    WREATH_POLICY_DONE_PROXY = 1u << 0,
    WREATH_POLICY_DONE_TRUSTED_HOST = 1u << 1,
    WREATH_POLICY_DONE_RATE = 1u << 2,
    WREATH_POLICY_DONE_REQUEST_ID = 1u << 3,
    WREATH_POLICY_DONE_TIMING = 1u << 4,
    WREATH_POLICY_DONE_CORS = 1u << 5,
    WREATH_POLICY_DONE_CSRF = 1u << 6,
    WREATH_POLICY_DONE_SECURITY = 1u << 7,
};

typedef struct {
    PyObject *descriptor;  /* owned immutable tuple, NULL means no native policy */
    const WreathCoreCAPI *core; /* borrowed immutable capsule table */
    unsigned char response_transform;
} WreathPolicyProgram;

typedef struct {
    PyObject *client;      /* owned effective client tuple/None */
    PyObject *scheme;      /* owned effective scheme str */
    PyObject *origin;      /* owned inbound Origin bytes, or NULL */
    PyObject *request_id;  /* owned bytes, or NULL */
    PyObject *csrf_token;  /* owned unicode token, or NULL */
    PyObject *csrf_config; /* borrowed through program descriptor */
    uint64_t started_ns;
    uint64_t elapsed_ns;
    uint32_t completed;
    unsigned char csrf_issue;
    unsigned char csrf_minter;
    unsigned char native;
    unsigned char compression_coding; /* 0 none, 1 gzip, 2 zstd */
    unsigned char method_is_head;
} WreathPolicyState;

typedef struct {
    int status;
    uint16_t flight_flags;
    PyObject *headers; /* owned list */
    PyObject *body;    /* owned bytes */
} WreathPolicyReply;

int wreath_policy_program_load(WreathPolicyProgram *, PyObject *app);
void wreath_policy_program_clear(WreathPolicyProgram *);
void wreath_policy_state_init(WreathPolicyState *);
void wreath_policy_state_clear(WreathPolicyState *);
void wreath_policy_reply_clear(WreathPolicyReply *);
void wreath_policy_record_completion(const WreathFlightCAPI *, wreath_nfr_worker *,
                                     uint64_t connection_id, uint8_t protocol,
                                     uint64_t start_ns, uint64_t end_ns,
                                     PyObject *headers,
                                     const WreathPolicyReply *, uint64_t bytes_in,
                                     uint64_t bytes_out);

/* 0 continues into the framework, 1 supplies a complete reply, -1 is error. */
int wreath_policy_ingress(WreathPolicyProgram *, WreathPolicyState *,
                          PyObject *method, PyObject *path, PyObject *scheme,
                          PyObject *client, PyObject *headers,
                          WreathPolicyReply *reply);

/* Mutates the response header list in C immediately before serialization. */
int wreath_policy_egress(WreathPolicyProgram *, WreathPolicyState *,
                         PyObject *headers);
/* Static cache/compression policy over a complete in-memory response. */
int wreath_policy_response(WreathPolicyProgram *, WreathPolicyState *,
                           PyObject *status, PyObject *headers, PyObject **body,
                           int authenticated);
int wreath_policy_websocket_origin(WreathPolicyProgram *, PyObject *headers,
                                   WreathPolicyReply *reply);

/* Mint lazily when a handler asks for csrf_token(request). */
PyObject *wreath_policy_csrf_token(WreathPolicyState *);

#endif
