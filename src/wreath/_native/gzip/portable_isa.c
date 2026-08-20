/* Link-complete scalar fallbacks for platforms where the x86 ISA translation
 * units are not built.  Runtime feature probing selects none of these arms;
 * the definitions exist so the portable dispatch objects remain standalone.
 * SPDX-License-Identifier: MPL-2.0
 */
#include <stddef.h>
#include <stdint.h>

uint32_t wreath_gzip_encoder_crc32_slice16(uint32_t, const void *, size_t);
uint32_t wreath_gzip_decoder_crc32_slice16(uint32_t, const void *, size_t);

uint32_t wreath_gzip_encoder_crc32_pclmul(uint32_t crc, const void *data, size_t length)
{ return wreath_gzip_encoder_crc32_slice16(crc, data, length); }
uint32_t wreath_gzip_encoder_crc32_vpclmul(uint32_t crc, const void *data, size_t length)
{ return wreath_gzip_encoder_crc32_slice16(crc, data, length); }
uint32_t wreath_gzip_decoder_crc32_pclmul(uint32_t crc, const void *data, size_t length)
{ return wreath_gzip_decoder_crc32_slice16(crc, data, length); }
uint32_t wreath_gzip_decoder_crc32_vpclmul(uint32_t crc, const void *data, size_t length)
{ return wreath_gzip_decoder_crc32_slice16(crc, data, length); }

/* inflate.c owns these internal types and never dereferences them while a
 * scalar arm is selected, so portable stubs need only preserve the ABI. */
struct wreath_gzip_decoder_st;
int wreath_gzip_decoder_inflate_fast_bmi2(struct wreath_gzip_decoder_st *s)
{ (void)s; return -2; }
int wreath_gzip_decoder_inflate_fused_bmi2(struct wreath_gzip_decoder_st *s)
{ (void)s; return -2; }
int wreath_gzip_decoder_inflate_fast_bmi2_avx2(struct wreath_gzip_decoder_st *s)
{ (void)s; return -2; }
int wreath_gzip_decoder_inflate_fused_bmi2_avx2(struct wreath_gzip_decoder_st *s)
{ (void)s; return -2; }
int wreath_gzip_decoder_inflate_fast_bmi2_avx2_long(struct wreath_gzip_decoder_st *s)
{ (void)s; return -2; }
int wreath_gzip_decoder_inflate_fused_bmi2_avx2_long(struct wreath_gzip_decoder_st *s)
{ (void)s; return -2; }
