/* SPDX-License-Identifier: MPL-2.0 */
#ifndef GZ_CRC32_H
#define GZ_CRC32_H

#include <stddef.h>
#include <stdint.h>

#include "gz.h"

/* State-machine form: no init/final inversion, so it composes. */
uint32_t wreath_gzip_encoder_crc32_raw_bytewise(uint32_t c, const uint8_t *p, size_t n);
uint32_t wreath_gzip_encoder_crc32_raw_slice8(uint32_t c, const uint8_t *p, size_t n);
uint32_t wreath_gzip_encoder_crc32_raw_slice16(uint32_t c, const uint8_t *p, size_t n);
int wreath_gzip_encoder_crc32_pick_arm(void);
uint32_t wreath_gzip_encoder_crc32_arm(int arm, uint32_t crc, const void *buf, size_t len);

/* Which table width the no-PCLMUL path uses. Set at build time so both can be
 * measured; the arms disagreed and the answer swaps with -O level. */
#ifndef GZ_CRC_FALLBACK_ARM
#define GZ_CRC_FALLBACK_ARM GZ_CRC_ARM_SLICE8
#endif

/* Fold constants K(e) = rev64(x^e mod P), derived and verified bit-exactly in
 * tools/derive_crc_constants.py. For a fold across D bytes the low half of the
 * register is multiplied by KLO(D) and the high half by KHI(D). */
#define GZ_CRC_K_16_LO 0x65673b4600000000ull
#define GZ_CRC_K_16_HI 0x9ba54c6f00000000ull
#define GZ_CRC_K_32_LO 0x9570d49500000000ull
#define GZ_CRC_K_32_HI 0x01b5fd1d00000000ull
#define GZ_CRC_K_48_LO 0x69ccfc0d00000000ull
#define GZ_CRC_K_48_HI 0x2a28386200000000ull
#define GZ_CRC_K_64_LO 0x653d982200000000ull
#define GZ_CRC_K_64_HI 0xcad38e8f00000000ull
#define GZ_CRC_K_80_LO 0x5a03a0cf00000000ull
#define GZ_CRC_K_80_HI 0x8e42b13e00000000ull
#define GZ_CRC_K_96_LO 0x759fc69d00000000ull
#define GZ_CRC_K_96_HI 0x101a233100000000ull
#define GZ_CRC_K_112_LO 0x019866e800000000ull
#define GZ_CRC_K_112_HI 0xc64ac0b800000000ull
#define GZ_CRC_K_128_LO 0x7d657a1000000000ull
#define GZ_CRC_K_128_HI 0x7406fa9500000000ull
#define GZ_CRC_K_192_LO 0x67f7947600000000ull
#define GZ_CRC_K_192_HI 0xc56d949600000000ull

#endif
