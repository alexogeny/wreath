/* Length-limited canonical Huffman construction for DEFLATE (RFC 1951 §3.2).
 *
 * SPDX-License-Identifier: MPL-2.0
 */
#ifndef GZ_HUFF_H
#define GZ_HUFF_H

#include <stdint.h>

#define GZ_HUFF_MAXSYM 288

/* Fills len[0..nsym) with a complete code no deeper than maxlen. Returns the
 * number of symbols with a nonzero length, or -1 if the alphabet cannot be
 * coded within maxlen at all (2^maxlen < used symbols). */
int wreath_gzip_encoder_huff_lengths(const uint32_t *freq, int nsym, int maxlen, uint8_t *len);

/* Canonical codes, already bit-reversed for a least-significant-bit-first
 * writer. */
void wreath_gzip_encoder_huff_codes(const uint8_t *len, int nsym, uint16_t *code);

#endif
