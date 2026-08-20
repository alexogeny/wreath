/* CRC-32 (IEEE 802.3, reflected): portable arms and runtime dispatch.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * The bytewise arm is the definition. Slicing-by-8 and slicing-by-16 are both
 * here because arms 1 and 3 disagreed about which is the better no-PCLMUL
 * fallback and the disagreement moved with optimisation level; RESULTS.md
 * records how it came out in this build. Neither is reached on this machine,
 * which has VPCLMULQDQ.
 */
#include "crc32.h"

#include <string.h>

#include "cpu.h"
#include "../crc32_table.h"

#define wreath_gzip_decoder_crc_tab wreath_gzip_crc32_table

static inline uint32_t ld32(const uint8_t *p) {
  uint32_t v;
  memcpy(&v, p, 4);
  return v;
}

uint32_t wreath_gzip_decoder_crc32_raw_bytewise(uint32_t c, const uint8_t *p, size_t n) {
  for (size_t i = 0; i < n; i++) c = (c >> 8) ^ wreath_gzip_decoder_crc_tab[0][(c ^ p[i]) & 0xff];
  return c;
}

uint32_t wreath_gzip_decoder_crc32_raw_slice8(uint32_t c, const uint8_t *p, size_t n) {
  while (n >= 8) {
    uint32_t a = ld32(p) ^ c, b = ld32(p + 4);
    c = wreath_gzip_decoder_crc_tab[7][a & 0xff] ^ wreath_gzip_decoder_crc_tab[6][(a >> 8) & 0xff] ^
        wreath_gzip_decoder_crc_tab[5][(a >> 16) & 0xff] ^ wreath_gzip_decoder_crc_tab[4][a >> 24] ^
        wreath_gzip_decoder_crc_tab[3][b & 0xff] ^ wreath_gzip_decoder_crc_tab[2][(b >> 8) & 0xff] ^
        wreath_gzip_decoder_crc_tab[1][(b >> 16) & 0xff] ^ wreath_gzip_decoder_crc_tab[0][b >> 24];
    p += 8;
    n -= 8;
  }
  return wreath_gzip_decoder_crc32_raw_bytewise(c, p, n);
}

uint32_t wreath_gzip_decoder_crc32_raw_slice16(uint32_t c, const uint8_t *p, size_t n) {
  while (n >= 16) {
    uint32_t a = ld32(p) ^ c, b = ld32(p + 4), d = ld32(p + 8), e = ld32(p + 12);
    c = wreath_gzip_decoder_crc_tab[15][a & 0xff] ^ wreath_gzip_decoder_crc_tab[14][(a >> 8) & 0xff] ^
        wreath_gzip_decoder_crc_tab[13][(a >> 16) & 0xff] ^ wreath_gzip_decoder_crc_tab[12][a >> 24] ^
        wreath_gzip_decoder_crc_tab[11][b & 0xff] ^ wreath_gzip_decoder_crc_tab[10][(b >> 8) & 0xff] ^
        wreath_gzip_decoder_crc_tab[9][(b >> 16) & 0xff] ^ wreath_gzip_decoder_crc_tab[8][b >> 24] ^
        wreath_gzip_decoder_crc_tab[7][d & 0xff] ^ wreath_gzip_decoder_crc_tab[6][(d >> 8) & 0xff] ^
        wreath_gzip_decoder_crc_tab[5][(d >> 16) & 0xff] ^ wreath_gzip_decoder_crc_tab[4][d >> 24] ^
        wreath_gzip_decoder_crc_tab[3][e & 0xff] ^ wreath_gzip_decoder_crc_tab[2][(e >> 8) & 0xff] ^
        wreath_gzip_decoder_crc_tab[1][(e >> 16) & 0xff] ^ wreath_gzip_decoder_crc_tab[0][e >> 24];
    p += 16;
    n -= 16;
  }
  return wreath_gzip_decoder_crc32_raw_bytewise(c, p, n);
}

uint32_t wreath_gzip_decoder_crc32_bytewise(uint32_t crc, const void *buf, size_t len) {
  return ~wreath_gzip_decoder_crc32_raw_bytewise(~crc, (const uint8_t *)buf, len);
}

uint32_t wreath_gzip_decoder_crc32_slice8(uint32_t crc, const void *buf, size_t len) {
  return ~wreath_gzip_decoder_crc32_raw_slice8(~crc, (const uint8_t *)buf, len);
}

uint32_t wreath_gzip_decoder_crc32_slice16(uint32_t crc, const void *buf, size_t len) {
  return ~wreath_gzip_decoder_crc32_raw_slice16(~crc, (const uint8_t *)buf, len);
}

/* ---- dispatch ----------------------------------------------------------- */

int wreath_gzip_decoder_crc32_pick_arm_from_features(unsigned features) {
  if (features & GZ_CPU_VPCLMUL) return GZ_CRC_VPCLMUL;
  if ((features & GZ_CPU_PCLMUL) && (features & GZ_CPU_SSE41)) return GZ_CRC_PCLMUL;
  return GZ_CRC_SLICE8;
}

int wreath_gzip_decoder_crc32_pick_arm(void) {
  return wreath_gzip_decoder_crc32_pick_arm_from_features(
      wreath_gzip_decoder_cpu_features());
}

int wreath_gzip_decoder_crc32_arm_available(int arm) {
  unsigned f = wreath_gzip_decoder_cpu_features();
  switch (arm) {
    case GZ_CRC_BYTEWISE:
    case GZ_CRC_SLICE8:
    case GZ_CRC_SLICE16: return 1;
    case GZ_CRC_PCLMUL: return (f & GZ_CPU_PCLMUL) && (f & GZ_CPU_SSE41);
    case GZ_CRC_VPCLMUL: return (f & GZ_CPU_VPCLMUL) != 0;
    default: return 0;
  }
}

const char *wreath_gzip_decoder_crc32_arm_name(void) {
  static const char *names[] = {"bytewise", "slice8", "slice16", "pclmul", "vpclmul"};
  return names[wreath_gzip_decoder_crc32_pick_arm()];
}

uint32_t wreath_gzip_decoder_crc32_arm(int arm, uint32_t crc, const void *buf, size_t len) {
  switch (arm) {
    case GZ_CRC_VPCLMUL: return wreath_gzip_decoder_crc32_vpclmul(crc, buf, len);
    case GZ_CRC_PCLMUL: return wreath_gzip_decoder_crc32_pclmul(crc, buf, len);
    case GZ_CRC_BYTEWISE: return wreath_gzip_decoder_crc32_bytewise(crc, buf, len);
    case GZ_CRC_SLICE16: return wreath_gzip_decoder_crc32_slice16(crc, buf, len);
    default: return wreath_gzip_decoder_crc32_slice8(crc, buf, len);
  }
}

uint32_t wreath_gzip_decoder_crc32(uint32_t crc, const void *buf, size_t len) {
  return wreath_gzip_decoder_crc32_arm(
      wreath_gzip_decoder_crc32_pick_arm(), crc, buf, len);
}
