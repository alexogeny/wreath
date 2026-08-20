/* CRC-32 (IEEE 802.3, reflected) -- portable arms and runtime dispatch.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * The bytewise arm is the definition; the slice-by-16 arm is the portable fast
 * fallback and the oracle the carry-less arms are differentially tested
 * against. See tools/derive_crc_constants.py for the algebra the SIMD arms
 * implement.
 */
#include "crc32.h"

#include <string.h>

#include "cpu.h"
#include "../crc32_table.h"

#define wreath_gzip_encoder_crc_tab wreath_gzip_crc32_table

static inline uint32_t ld32(const uint8_t *p) {
  uint32_t v;
  memcpy(&v, p, 4);
  return v;
}

uint32_t wreath_gzip_encoder_crc32_raw_bytewise(uint32_t c, const uint8_t *p, size_t n) {
  for (size_t i = 0; i < n; i++) c = (c >> 8) ^ wreath_gzip_encoder_crc_tab[0][(c ^ p[i]) & 0xff];
  return c;
}

uint32_t wreath_gzip_encoder_crc32_raw_slice16(uint32_t c, const uint8_t *p, size_t n) {
  while (n >= 16) {
    uint32_t a = ld32(p) ^ c, b = ld32(p + 4), d = ld32(p + 8), e = ld32(p + 12);
    c = wreath_gzip_encoder_crc_tab[15][a & 0xff] ^ wreath_gzip_encoder_crc_tab[14][(a >> 8) & 0xff] ^
        wreath_gzip_encoder_crc_tab[13][(a >> 16) & 0xff] ^ wreath_gzip_encoder_crc_tab[12][a >> 24] ^
        wreath_gzip_encoder_crc_tab[11][b & 0xff] ^ wreath_gzip_encoder_crc_tab[10][(b >> 8) & 0xff] ^
        wreath_gzip_encoder_crc_tab[9][(b >> 16) & 0xff] ^ wreath_gzip_encoder_crc_tab[8][b >> 24] ^
        wreath_gzip_encoder_crc_tab[7][d & 0xff] ^ wreath_gzip_encoder_crc_tab[6][(d >> 8) & 0xff] ^
        wreath_gzip_encoder_crc_tab[5][(d >> 16) & 0xff] ^ wreath_gzip_encoder_crc_tab[4][d >> 24] ^
        wreath_gzip_encoder_crc_tab[3][e & 0xff] ^ wreath_gzip_encoder_crc_tab[2][(e >> 8) & 0xff] ^
        wreath_gzip_encoder_crc_tab[1][(e >> 16) & 0xff] ^ wreath_gzip_encoder_crc_tab[0][e >> 24];
    p += 16;
    n -= 16;
  }
  for (size_t i = 0; i < n; i++) c = (c >> 8) ^ wreath_gzip_encoder_crc_tab[0][(c ^ p[i]) & 0xff];
  return c;
}

/* Slice-by-8: the other half of the fallback-width question the arms disagreed
 * on. Same per-byte work as by-16, half the table, loop overhead amortised over
 * 8 bytes instead of 16. */
uint32_t wreath_gzip_encoder_crc32_raw_slice8(uint32_t c, const uint8_t *p, size_t n) {
  while (n >= 8) {
    uint32_t a = ld32(p) ^ c, b = ld32(p + 4);
    c = wreath_gzip_encoder_crc_tab[7][a & 0xff] ^ wreath_gzip_encoder_crc_tab[6][(a >> 8) & 0xff] ^
        wreath_gzip_encoder_crc_tab[5][(a >> 16) & 0xff] ^ wreath_gzip_encoder_crc_tab[4][a >> 24] ^
        wreath_gzip_encoder_crc_tab[3][b & 0xff] ^ wreath_gzip_encoder_crc_tab[2][(b >> 8) & 0xff] ^
        wreath_gzip_encoder_crc_tab[1][(b >> 16) & 0xff] ^ wreath_gzip_encoder_crc_tab[0][b >> 24];
    p += 8;
    n -= 8;
  }
  for (size_t i = 0; i < n; i++) c = (c >> 8) ^ wreath_gzip_encoder_crc_tab[0][(c ^ p[i]) & 0xff];
  return c;
}

uint32_t wreath_gzip_encoder_crc32_slice8(uint32_t crc, const void *buf, size_t len) {
  return ~wreath_gzip_encoder_crc32_raw_slice8(~crc, (const uint8_t *)buf, len);
}

uint32_t wreath_gzip_encoder_crc32_bytewise(uint32_t crc, const void *buf, size_t len) {
  return ~wreath_gzip_encoder_crc32_raw_bytewise(~crc, (const uint8_t *)buf, len);
}

uint32_t wreath_gzip_encoder_crc32_slice16(uint32_t crc, const void *buf, size_t len) {
  return ~wreath_gzip_encoder_crc32_raw_slice16(~crc, (const uint8_t *)buf, len);
}

/* ---- dispatch ----------------------------------------------------------- */

int wreath_gzip_encoder_crc32_pick_arm(void) {
  unsigned f = wreath_gzip_encoder_cpu_features();
  if (f & GZ_CPU_VPCLMUL) return GZ_CRC_ARM_VPCLMUL;
  if ((f & GZ_CPU_PCLMUL) && (f & GZ_CPU_SSE41)) return GZ_CRC_ARM_PCLMUL;
  return GZ_CRC_FALLBACK_ARM;
}

int wreath_gzip_encoder_crc32_arm_available(int arm) {
  unsigned f = wreath_gzip_encoder_cpu_features();
  switch (arm) {
    case GZ_CRC_ARM_BYTEWISE:
    case GZ_CRC_ARM_SLICE8:
    case GZ_CRC_ARM_SLICE16: return 1;
    case GZ_CRC_ARM_PCLMUL: return (f & GZ_CPU_PCLMUL) && (f & GZ_CPU_SSE41);
    case GZ_CRC_ARM_VPCLMUL: return (f & GZ_CPU_VPCLMUL) != 0;
    default: return 0;
  }
}

const char *wreath_gzip_encoder_crc32_arm_name(void) {
  static const char *names[] = {"bytewise", "slice8", "slice16", "pclmul", "vpclmul"};
  return names[wreath_gzip_encoder_crc32_pick_arm()];
}

uint32_t wreath_gzip_encoder_crc32_arm(int arm, uint32_t crc, const void *buf, size_t len) {
  switch (arm) {
    case GZ_CRC_ARM_VPCLMUL: return wreath_gzip_encoder_crc32_vpclmul(crc, buf, len);
    case GZ_CRC_ARM_PCLMUL: return wreath_gzip_encoder_crc32_pclmul(crc, buf, len);
    case GZ_CRC_ARM_BYTEWISE: return wreath_gzip_encoder_crc32_bytewise(crc, buf, len);
    case GZ_CRC_ARM_SLICE8: return wreath_gzip_encoder_crc32_slice8(crc, buf, len);
    default: return wreath_gzip_encoder_crc32_slice16(crc, buf, len);
  }
}

uint32_t wreath_gzip_encoder_crc32(uint32_t crc, const void *buf, size_t len) {
  return wreath_gzip_encoder_crc32_arm(
      wreath_gzip_encoder_crc32_pick_arm(), crc, buf, len);
}
