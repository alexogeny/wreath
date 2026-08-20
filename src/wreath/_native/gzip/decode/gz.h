/* Public surface of the consolidated gzip codec.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Written from RFC 1951 and RFC 1952. The decoder is the point of this
 * directory; the encoder exists so the differential suite has two independent
 * halves to disagree, and claims nothing.
 */
#ifndef GZ_H
#define GZ_H

#include <stddef.h>
#include <stdint.h>

#include "../compat.h"

#ifdef __cplusplus
extern "C" {
#endif

enum {
  GZ_OK = 0,
  GZ_ERR_HEADER = -1,    /* not a gzip member, or an unsupported one */
  GZ_ERR_DATA = -2,      /* malformed deflate data */
  GZ_ERR_TRUNCATED = -3, /* input ended inside the stream */
  GZ_ERR_CRC = -4,       /* trailer CRC-32 mismatch */
  GZ_ERR_LENGTH = -5,    /* trailer ISIZE mismatch */
  GZ_ERR_SPACE = -6,     /* output would exceed the caller's capacity */
  GZ_ERR_TRAILING = -7   /* bytes after the last member */
};

/* ---- decode ---- */

typedef struct wreath_gzip_decoder_dec wreath_gzip_decoder_dec;

/* Optional caller-supplied content knowledge. It is never read from or written
 * to the gzip member, so ordinary RFC 1951/1952 streams remain interoperable. */
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
const char *wreath_gzip_decoder_format_name(int format);
int wreath_gzip_decoder_format_by_name(const char *name);

wreath_gzip_decoder_dec *wreath_gzip_decoder_dec_new(void);
void wreath_gzip_decoder_dec_free(wreath_gzip_decoder_dec *d);
void wreath_gzip_decoder_dec_set_format(wreath_gzip_decoder_dec *d, int format);

/* Decompress one gzip member. `out_cap` is a hard cap that the decoder
 * believes: a stream claiming to expand past it is rejected with
 * GZ_ERR_SPACE rather than decoded up to the wall. */
int wreath_gzip_decoder_decompress(wreath_gzip_decoder_dec *d, const void *in, size_t in_len, void *out,
                  size_t out_cap, size_t *out_len);

enum { GZ_DEC_SCALAR = 0, GZ_DEC_BMI2 = 1, GZ_DEC_BMI2_AVX2 = 2 };
/* ---- CRC-32 (IEEE 802.3, reflected) ---- */

uint32_t wreath_gzip_decoder_crc32(uint32_t crc, const void *buf, size_t len);

enum { GZ_CRC_BYTEWISE = 0, GZ_CRC_SLICE8 = 1, GZ_CRC_SLICE16 = 2,
       GZ_CRC_PCLMUL = 3, GZ_CRC_VPCLMUL = 4 };
uint32_t wreath_gzip_decoder_crc32_bytewise(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_decoder_crc32_slice8(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_decoder_crc32_slice16(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_decoder_crc32_pclmul(uint32_t crc, const void *buf, size_t len);
uint32_t wreath_gzip_decoder_crc32_vpclmul(uint32_t crc, const void *buf, size_t len);
int wreath_gzip_decoder_crc32_arm_available(int arm);
const char *wreath_gzip_decoder_crc32_arm_name(void);

#ifdef __cplusplus
}
#endif
#endif
