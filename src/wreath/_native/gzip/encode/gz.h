/* Public surface of the consolidated gzip encoder.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Written from RFC 1951 and RFC 1952. zlib-ng, libdeflate and the system gzip
 * are linked by the differential suite as oracles only; no line of their source
 * appears here.
 */
#ifndef GZ_H
#define GZ_H

#include <stddef.h>
#include <stdint.h>

#include "../compat.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- CRC-32 (IEEE 802.3, reflected, as used by gzip) -------------------- */

uint32_t wreath_gzip_encoder_crc32(uint32_t crc, const void *buf, size_t len);

uint32_t wreath_gzip_encoder_crc32_bytewise(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_encoder_crc32_slice8(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_encoder_crc32_slice16(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_encoder_crc32_pclmul(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_encoder_crc32_vpclmul(uint32_t crc, const void *buf, size_t len);

enum {
  GZ_CRC_ARM_BYTEWISE = 0,
  GZ_CRC_ARM_SLICE8 = 1,
  GZ_CRC_ARM_SLICE16 = 2,
  GZ_CRC_ARM_PCLMUL = 3,
  GZ_CRC_ARM_VPCLMUL = 4,
};
int wreath_gzip_encoder_crc32_arm_available(int arm);
const char *wreath_gzip_encoder_crc32_arm_name(void);

/* ---- Encoder ------------------------------------------------------------ */

typedef struct wreath_gzip_encoder_enc wreath_gzip_encoder_enc;

/* Optional out-of-band content knowledge. This never changes the gzip format:
 * it selects an independently compiled parser policy, and UNKNOWN retains the
 * content-adaptive generic behavior. */
enum {
  GZ_FMT_UNKNOWN = 0,
  GZ_FMT_JSON,
  GZ_FMT_CHAOTIC_JSON,
  GZ_FMT_HTML,
  GZ_FMT_GRAPHQL,
  GZ_FMT_LOG,
  GZ_FMT_PLAINTEXT,
  GZ_FMT_COUNT
};
const char *wreath_gzip_encoder_format_name(int format);
int wreath_gzip_encoder_format_by_name(const char *name);

/* The profile ladder. Each is a measured point on the ratio/instruction
 * frontier, not a "level": the numbers are in RESULTS.md. `GZ_P_DEFAULT` is
 * the one to put in an HTTP framework. */
enum {
  GZ_P_FAST = 0,     /* single probe, no re-insertion   -- cheapest useful */
  GZ_P_QUICK = 1,    /* single probe, insert everything */
  GZ_P_LIGHT = 2,    /* chain 4, lazy */
  GZ_P_DEFAULT = 3,  /* chain 48, lazy, adaptive splitting -- the one to ship */
  GZ_P_HIGH = 4,     /* chain 80, deeper lazy */
  GZ_P_MAX = 5,      /* chain 200 */
  GZ_P_COUNT = 6,
};

const char *wreath_gzip_encoder_profile_name(int profile);
int wreath_gzip_encoder_profile_by_name(const char *name); /* -1 if unknown */

wreath_gzip_encoder_enc *wreath_gzip_encoder_enc_new(void);
void wreath_gzip_encoder_enc_free(wreath_gzip_encoder_enc *e);
void wreath_gzip_encoder_enc_set_format(wreath_gzip_encoder_enc *e, int format);

/* Worst-case output bound for `n` input bytes. */
size_t wreath_gzip_encoder_encode_bound(size_t n);

/* Compress `n` bytes into a complete gzip stream. Returns bytes written, or 0
 * on failure. The compressor is reused across calls; nothing is allocated per
 * call once its buffers are large enough. */
size_t wreath_gzip_encoder_encode(wreath_gzip_encoder_enc *e, const void *in, size_t n, void *out, size_t cap, int profile);

/* Token accessors, so a test can read the token stream without defl.h. */
int wreath_gzip_encoder_tok_is_match(uint32_t t);
unsigned wreath_gzip_encoder_tok_len(uint32_t t);
unsigned wreath_gzip_encoder_tok_dist(uint32_t t);
unsigned wreath_gzip_encoder_len_symbol(unsigned len);   /* 0..28 */
unsigned wreath_gzip_encoder_dist_symbol(unsigned dist); /* 0..29 */

/* ---- introspection / ablation hooks ------------------------------------- */

enum {
  GZ_ARM_AUTO = -1,
  GZ_ARM_SCALAR = 0,
};
const char *wreath_gzip_encoder_encode_arm_name(void);

int wreath_gzip_encoder_cpu_has_avx2(void);
int wreath_gzip_encoder_cpu_has_bmi2(void);
int wreath_gzip_encoder_cpu_has_pclmul(void);
int wreath_gzip_encoder_cpu_has_vpclmul(void);

#ifdef __cplusplus
}
#endif
#endif /* GZ_H */
